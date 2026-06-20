"""
SQLite Browser - Flask backend.

A thin HTTP layer over ``db_service.Database``. Single-user, localhost only.
The currently-open database lives in module state (one per process); this is a
desktop-style tool, not a multi-tenant service.

Run locally:
    pip install -r requirements.txt
    python server.py
    # open http://localhost:5000
"""

from __future__ import annotations

import io
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
from flask import (
    Flask,
    jsonify,
    request,
    send_file,
    send_from_directory,
)
from werkzeug.exceptions import HTTPException

from db_service import DEFAULT_PAGE_SIZE, Database, DBError

def _resource_dir() -> Path:
    """Directory holding bundled read-only assets (``static/``). Under a frozen
    PyInstaller build this is the temp extraction dir (``sys._MEIPASS``);
    otherwise it's the source tree."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _data_dir() -> Path:
    """Directory for writable runtime files (uploads, backups, metadata). In a
    frozen build the source dir is a temp folder that's deleted on exit, so use
    ``%LOCALAPPDATA%\\SQLiteBrowser``; in dev it's the source tree (unchanged)."""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SQLiteBrowser"
    else:
        base = Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = _resource_dir() / "static"
DATA_DIR = _data_dir()
UPLOAD_DIR = DATA_DIR / "uploads"
BACKUP_DIR = DATA_DIR / "backups"
META_PATH = DATA_DIR / "app_meta.sqlite"

# Roots the "browse server files" picker is allowed to list. In a frozen build
# the source dir is meaningless (temp extraction), so anchor on the exe's
# location and the data dir; in dev, the project and its parent workspace.
if getattr(sys, "frozen", False):
    BROWSE_ROOTS = [Path(sys.executable).resolve().parent, DATA_DIR, Path.home()]
else:
    BROWSE_ROOTS = [BASE_DIR, BASE_DIR.parent, Path.home()]
DB_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".db3"}

for d in (UPLOAD_DIR, BACKUP_DIR):
    d.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=None)

# Module-level handle to the open database (single-user tool).
_current: Database | None = None


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #
def current() -> Database:
    if _current is None:
        raise DBError("No database is open. Open or upload a .db file first.")
    return _current


def ok(**data):
    return jsonify({"ok": True, **data})


@app.errorhandler(DBError)
def _handle_db_error(e: DBError):
    return jsonify({"ok": False, "error": str(e)}), 400


@app.errorhandler(Exception)
def _handle_unexpected(e: Exception):
    # Let normal HTTP responses (404 favicon, etc.) pass through untouched.
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


# --------------------------------------------------------------------- #
# App-metadata store (query history + saved queries), kept out of user DB
# --------------------------------------------------------------------- #
def _meta():
    import sqlite3

    con = sqlite3.connect(META_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS query_history (
            id INTEGER PRIMARY KEY, sql TEXT, db TEXT, ran_at TEXT);
        CREATE TABLE IF NOT EXISTS saved_queries (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE, sql TEXT, saved_at TEXT);
        """
    )
    return con


def _record_history(sql: str):
    con = _meta()
    db_name = str(_current.path) if _current else ""
    con.execute(
        "INSERT INTO query_history (sql, db, ran_at) VALUES (?,?,?)",
        (sql, db_name, datetime.now().isoformat(timespec="seconds")),
    )
    # Keep the history bounded.
    con.execute(
        "DELETE FROM query_history WHERE id NOT IN "
        "(SELECT id FROM query_history ORDER BY id DESC LIMIT 200)"
    )
    con.commit()
    con.close()


# --------------------------------------------------------------------- #
# Static
# --------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


# --------------------------------------------------------------------- #
# Open / upload / list databases
# --------------------------------------------------------------------- #
@app.get("/api/databases")
def list_databases():
    """List .db files under the allowed browse roots (shallow, deduped)."""
    found = {}
    for root in BROWSE_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file() and path.suffix.lower() in DB_EXTENSIONS:
                    # Skip our own backups/meta to keep the list relevant.
                    if BACKUP_DIR in path.parents or path == META_PATH:
                        continue
                    found[str(path)] = {
                        "path": str(path),
                        "name": path.name,
                        "size": path.stat().st_size,
                        "modified": datetime.fromtimestamp(
                            path.stat().st_mtime
                        ).strftime("%Y-%m-%d %H:%M"),
                    }
            except (OSError, PermissionError):
                continue
            if len(found) >= 500:
                break
    items = sorted(found.values(), key=lambda d: d["modified"], reverse=True)
    return ok(databases=items)


@app.post("/api/open")
def open_db():
    global _current
    data = request.get_json(force=True)
    path = Path(data.get("path", ""))
    if not path.exists() or path.suffix.lower() not in DB_EXTENSIONS:
        raise DBError(f"Not a database file: {path}")
    if _current is not None:
        _current.close()
    _current = Database(path)
    return ok(name=path.name, path=str(path))


@app.post("/api/create_db")
def create_db():
    """Create a brand-new empty database file in the project folder."""
    global _current
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        raise DBError("Provide a file name")
    if not name.lower().endswith(tuple(DB_EXTENSIONS)):
        name += ".db"
    path = BASE_DIR / name
    if path.exists():
        raise DBError(f"File already exists: {name}")
    if _current is not None:
        _current.close()
    _current = Database(path)
    # Touch the file so it exists on disk immediately.
    _current.run_sql("PRAGMA user_version")
    _current.write_changes()
    return ok(name=path.name, path=str(path))


@app.post("/api/upload")
def upload_db():
    global _current
    file = request.files.get("file")
    if not file or not file.filename:
        raise DBError("No file uploaded")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in DB_EXTENSIONS:
        raise DBError(f"Unsupported file type: {suffix}")
    dest = UPLOAD_DIR / Path(file.filename).name
    file.save(dest)
    if _current is not None:
        _current.close()
    _current = Database(dest)
    return ok(name=dest.name, path=str(dest))


@app.get("/api/status")
def status():
    if _current is None:
        return ok(open=False)
    return ok(open=True, name=_current.path.name, path=str(_current.path),
              dirty=_current.is_dirty())


@app.get("/api/download")
def download_current():
    """Download the live database file (handy after editing an upload)."""
    db = current()
    db.write_changes()
    return send_file(db.path, as_attachment=True, download_name=db.path.name)


# --------------------------------------------------------------------- #
# Structure / browse
# --------------------------------------------------------------------- #
@app.get("/api/structure")
def structure():
    return ok(**current().list_objects(), dirty=current().is_dirty())


@app.get("/api/table/<name>/info")
def table_info(name):
    return ok(info=current().table_info(name))


@app.get("/api/table/<name>/distinct")
def table_distinct(name):
    column = request.args.get("column", "")
    cap = int(request.args.get("cap", 200))
    values, truncated = current().distinct_values(name, column, cap=cap)
    return ok(values=values, truncated=truncated)


@app.get("/api/table/<name>/filteroptions")
def table_filteroptions(name):
    """Distinct values per column, for the Browse filter dropdowns."""
    db = current()
    cap = int(request.args.get("cap", 300))
    options = {}
    for c in db.table_info(name)["columns"]:
        try:
            vals, trunc = db.distinct_values(name, c["name"], cap=cap)
        except DBError:
            vals, trunc = [], True
        options[c["name"]] = {"values": vals, "truncated": trunc}
    return ok(options=options)


@app.get("/api/table/<name>/rows")
def table_rows(name):
    args = request.args
    filters, exact = {}, {}
    for key, val in args.items():
        if key.startswith("f_"):
            filters[key[2:]] = val
        elif key.startswith("e_"):
            exact[key[2:]] = val
    page = current().get_rows(
        name,
        limit=int(args.get("limit", DEFAULT_PAGE_SIZE)),
        offset=int(args.get("offset", 0)),
        order_by=args.get("order_by") or None,
        descending=args.get("desc") == "1",
        filters=filters or None,
        exact_filters=exact or None,
    )
    return ok(**page)


# --------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------- #
@app.post("/api/sql")
def run_sql():
    data = request.get_json(force=True)
    sql = (data.get("sql") or "").strip()
    result = current().run_sql(sql)
    _record_history(sql)
    return ok(result=result, dirty=current().is_dirty())


# --------------------------------------------------------------------- #
# Edits (staged) + write/revert
# --------------------------------------------------------------------- #
@app.post("/api/table/<name>/changes")
def changes(name):
    data = request.get_json(force=True)
    counts = current().apply_changes(
        name,
        inserts=data.get("inserts"),
        updates=data.get("updates"),
        deletes=data.get("deletes"),
    )
    return ok(counts=counts, dirty=current().is_dirty())


@app.post("/api/table/<name>/bulk_update")
def bulk_update(name):
    data = request.get_json(force=True)
    n = current().bulk_update_column(
        name, data["column"], data.get("value"),
        filters=data.get("filters"), exact_filters=data.get("exact_filters"),
    )
    return ok(updated=n, dirty=current().is_dirty())


@app.post("/api/write")
def write_changes():
    db = current()
    if db.is_dirty():
        db.backup(BACKUP_DIR)  # auto-backup before committing
    db.write_changes()
    return ok(dirty=False)


@app.post("/api/revert")
def revert():
    current().revert()
    return ok(dirty=False)


# --------------------------------------------------------------------- #
# Schema editing
# --------------------------------------------------------------------- #
@app.post("/api/table")
def create_table():
    data = request.get_json(force=True)
    current().create_table(data["name"], data["columns"])
    return ok(dirty=current().is_dirty())


@app.delete("/api/table/<name>")
def drop_table(name):
    db = current()
    db.backup(BACKUP_DIR)  # destructive - back up first
    db.drop_table(name)
    return ok(dirty=db.is_dirty())


@app.patch("/api/table/<name>")
def alter_table(name):
    data = request.get_json(force=True)
    db = current()
    op = data.get("op")
    if op == "rename_table":
        db.rename_table(name, data["new_name"])
    elif op == "add_column":
        db.add_column(name, data["column"])
    elif op == "rename_column":
        db.rename_column(name, data["old"], data["new"])
    elif op == "drop_column":
        db.backup(BACKUP_DIR)
        db.drop_column(name, data["column"])
    elif op == "create_index":
        db.create_index(data["index_name"], name, data["columns"],
                        unique=data.get("unique", False))
    elif op == "drop_index":
        db.drop_index(data["index_name"])
    else:
        raise DBError(f"Unknown alter op: {op}")
    return ok(dirty=db.is_dirty())


# --------------------------------------------------------------------- #
# Import / export
# --------------------------------------------------------------------- #
@app.post("/api/import")
def import_data():
    db = current()
    file = request.files.get("file")
    if not file or not file.filename:
        raise DBError("No file uploaded")
    table = request.form.get("table", "").strip()
    mode = request.form.get("mode", "create")
    if not table:
        raise DBError("Provide a destination table name")
    suffix = Path(file.filename).suffix.lower()
    raw = file.read()
    try:
        if suffix in (".xlsx", ".xls"):
            df = pd.read_excel(io.BytesIO(raw))
        elif suffix == ".csv":
            df = pd.read_csv(io.BytesIO(raw))
        else:
            raise DBError(f"Unsupported import type: {suffix}")
    except DBError:
        raise
    except Exception as e:  # pandas parse errors
        raise DBError(f"Could not read file: {e}") from e
    n = db.import_dataframe(df, table, mode=mode)
    return ok(rows=n, columns=list(df.columns))


@app.get("/api/export")
def export_data():
    db = current()
    fmt = request.args.get("format", "csv")
    table = request.args.get("table")
    query = request.args.get("query")
    if query:
        columns, rows = db.export_rows(query, is_query=True)
        base = "query_result"
    elif table:
        columns, rows = db.export_rows(table)
        base = table
    else:
        raise DBError("Provide a table or query to export")
    df = pd.DataFrame(rows, columns=columns)
    buf = io.BytesIO()
    if fmt == "xlsx":
        df.to_excel(buf, index=False)
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        fname = f"{base}.xlsx"
    else:
        buf.write(df.to_csv(index=False).encode("utf-8"))
        mime = "text/csv"
        fname = f"{base}.csv"
    buf.seek(0)
    return send_file(buf, mimetype=mime, as_attachment=True, download_name=fname)


# --------------------------------------------------------------------- #
# Backup / restore
# --------------------------------------------------------------------- #
@app.post("/api/backup")
def backup():
    dest = current().backup(BACKUP_DIR)
    return ok(backup=dest.name)


@app.get("/api/backups")
def list_backups():
    db = current()
    stem = db.path.stem
    items = []
    for p in sorted(BACKUP_DIR.glob(f"{stem}-*"), reverse=True):
        items.append(
            {
                "name": p.name,
                "size": p.stat().st_size,
                "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
        )
    return ok(backups=items)


@app.post("/api/restore")
def restore():
    data = request.get_json(force=True)
    name = data.get("name", "")
    path = BACKUP_DIR / Path(name).name  # prevent path traversal
    current().restore(path)
    return ok()


# --------------------------------------------------------------------- #
# Query history + saved queries
# --------------------------------------------------------------------- #
@app.get("/api/history")
def history():
    con = _meta()
    rows = con.execute(
        "SELECT sql, ran_at FROM query_history ORDER BY id DESC LIMIT 50"
    ).fetchall()
    con.close()
    return ok(history=[dict(r) for r in rows])


@app.get("/api/saved")
def saved_queries():
    con = _meta()
    rows = con.execute(
        "SELECT id, name, sql FROM saved_queries ORDER BY name"
    ).fetchall()
    con.close()
    return ok(saved=[dict(r) for r in rows])


@app.post("/api/saved")
def save_query():
    data = request.get_json(force=True)
    name, sql = data.get("name", "").strip(), data.get("sql", "").strip()
    if not name or not sql:
        raise DBError("Provide a name and SQL")
    con = _meta()
    con.execute(
        "INSERT INTO saved_queries (name, sql, saved_at) VALUES (?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET sql=excluded.sql, saved_at=excluded.saved_at",
        (name, sql, datetime.now().isoformat(timespec="seconds")),
    )
    con.commit()
    con.close()
    return ok()


@app.delete("/api/saved/<int:qid>")
def delete_saved(qid):
    con = _meta()
    con.execute("DELETE FROM saved_queries WHERE id = ?", (qid,))
    con.commit()
    con.close()
    return ok()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    print(f"SQLite Browser running at http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
