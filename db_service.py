"""
SQLite browser data-access layer.

A thin, testable wrapper around a single ``sqlite3.Connection`` that powers the
web UI. The connection is kept open with an *explicit* transaction so the app
can offer DB Browser for SQLite style "pending changes": edits are staged and
either written (COMMIT) or reverted (ROLLBACK).

Everything UI-facing returns plain JSON-able dicts/lists so ``server.py`` stays
a thin HTTP shim. No Flask imports here on purpose - this module is unit-tested
directly against temp database files.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Default page size for Browse Data - never load a whole table into the browser.
DEFAULT_PAGE_SIZE = 1000

# Identifiers we allow without quoting hassle; everything user-supplied still
# gets quoted via _quote(), but we validate names to reject obvious injection
# in code paths (DDL) where parameter binding is not possible.
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class DBError(Exception):
    """Raised for user-facing database errors (bad SQL, missing table, ...)."""


def _quote(identifier: str) -> str:
    """Quote an SQL identifier (table/column) by doubling embedded quotes."""
    if identifier is None:
        raise DBError("Identifier cannot be null")
    return '"' + str(identifier).replace('"', '""') + '"'


def _require_safe_name(name: str, kind: str = "name") -> str:
    """Validate a brand-new identifier the user is creating (table/column/index).

    Used only when we cannot bind the value (DDL). Browsing existing objects
    goes through _quote() and does not require this stricter check.
    """
    if not name or not _SAFE_NAME.match(name):
        raise DBError(
            f"Invalid {kind}: {name!r}. Use letters, digits and underscores; "
            "must not start with a digit."
        )
    return name


class Database:
    """Open, introspect and mutate a single SQLite database file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        # check_same_thread=False: Flask's dev server may serve requests from
        # different worker threads; this is a single-user local tool so we
        # accept the connection being shared (access is effectively serial).
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # We manage transactions explicitly for the pending-changes model.
        self.conn.isolation_level = None  # autocommit OFF via manual BEGIN
        self._in_txn = False

    # ------------------------------------------------------------------ #
    # Transaction / pending-changes control
    # ------------------------------------------------------------------ #
    def _begin(self) -> None:
        """Ensure a write transaction is open so edits can be staged."""
        if not self._in_txn:
            self.conn.execute("BEGIN")
            self._in_txn = True

    def is_dirty(self) -> bool:
        """True when there are staged, uncommitted changes."""
        return self._in_txn

    def write_changes(self) -> None:
        """Commit all staged changes."""
        if self._in_txn:
            self.conn.execute("COMMIT")
            self._in_txn = False

    def revert(self) -> None:
        """Roll back all staged changes."""
        if self._in_txn:
            self.conn.execute("ROLLBACK")
            self._in_txn = False

    def close(self) -> None:
        try:
            self.revert()
        finally:
            self.conn.close()

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def list_objects(self) -> dict[str, list[dict[str, Any]]]:
        """Return tables, views and indexes with row counts where sensible."""
        cur = self.conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view','index') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
        tables, views, indexes = [], [], []
        rows = cur.fetchall()
        for r in rows:
            name, typ = r["name"], r["type"]
            if typ == "table":
                tables.append({"name": name, "rows": self._row_count(name)})
            elif typ == "view":
                views.append({"name": name})
            else:
                indexes.append({"name": name})
        return {"tables": tables, "views": views, "indexes": indexes}

    def _row_count(self, table: str) -> int | None:
        try:
            cur = self.conn.execute(f"SELECT COUNT(*) AS c FROM {_quote(table)}")
            return cur.fetchone()["c"]
        except sqlite3.Error:
            return None

    def table_info(self, table: str) -> dict[str, Any]:
        """Columns (with PK flags), foreign keys and indexes for a table/view."""
        if not self._object_exists(table):
            raise DBError(f"No such table or view: {table}")
        cols = []
        for r in self.conn.execute(f"PRAGMA table_info({_quote(table)})"):
            cols.append(
                {
                    "cid": r["cid"],
                    "name": r["name"],
                    "type": r["type"],
                    "notnull": bool(r["notnull"]),
                    "default": r["dflt_value"],
                    "pk": r["pk"],
                }
            )
        fks = []
        for r in self.conn.execute(f"PRAGMA foreign_key_list({_quote(table)})"):
            fks.append(
                {
                    "from": r["from"],
                    "to": r["to"],
                    "table": r["table"],
                    "on_update": r["on_update"],
                    "on_delete": r["on_delete"],
                }
            )
        indexes = []
        for r in self.conn.execute(f"PRAGMA index_list({_quote(table)})"):
            idx_cols = [
                ic["name"]
                for ic in self.conn.execute(f"PRAGMA index_info({_quote(r['name'])})")
            ]
            indexes.append(
                {"name": r["name"], "unique": bool(r["unique"]), "columns": idx_cols}
            )
        return {
            "name": table,
            "columns": cols,
            "foreign_keys": fks,
            "indexes": indexes,
            "rows": self._row_count(table),
            "has_rowid": self._has_rowid(table),
        }

    def _object_exists(self, name: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table','view')",
            (name,),
        )
        return cur.fetchone() is not None

    def _is_table(self, name: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type = 'table'", (name,)
        )
        return cur.fetchone() is not None

    def _has_rowid(self, table: str) -> bool:
        """WITHOUT ROWID tables can't be addressed by rowid; detect that."""
        if not self._is_table(table):
            return False
        try:
            self.conn.execute(f"SELECT rowid FROM {_quote(table)} LIMIT 1")
            return True
        except sqlite3.Error:
            return False

    def _column_names(self, table: str) -> list[str]:
        return [r["name"] for r in self.conn.execute(f"PRAGMA table_info({_quote(table)})")]

    # ------------------------------------------------------------------ #
    # Browse data (paginated / sorted / filtered)
    # ------------------------------------------------------------------ #
    def get_rows(
        self,
        table: str,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        order_by: str | None = None,
        descending: bool = False,
        filters: dict[str, str] | None = None,
        exact_filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a page of rows plus total count and the rowid (if available).

        ``filters`` maps column -> substring (LIKE); ``exact_filters`` maps
        column -> a value to match exactly (used by the filter dropdowns).
        Returns ``{columns, rows, total, has_rowid}`` where each row is a list
        aligned to ``columns`` and (when present) ``_rowid`` carries the sqlite
        rowid used to key edits.
        """
        if not self._object_exists(table):
            raise DBError(f"No such table or view: {table}")
        valid_cols = set(self._column_names(table))
        has_rowid = self._has_rowid(table)

        where_sql, params = self._build_where(filters, valid_cols, exact_filters)

        # Total (filtered) count.
        total = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM {_quote(table)}{where_sql}", params
        ).fetchone()["c"]

        order_sql = ""
        if order_by:
            if order_by not in valid_cols:
                raise DBError(f"Cannot sort by unknown column: {order_by}")
            order_sql = f" ORDER BY {_quote(order_by)} {'DESC' if descending else 'ASC'}"

        select_cols = "*"
        if has_rowid:
            select_cols = "rowid AS _rowid, *"

        sql = (
            f"SELECT {select_cols} FROM {_quote(table)}{where_sql}{order_sql} "
            f"LIMIT ? OFFSET ?"
        )
        cur = self.conn.execute(sql, (*params, int(limit), int(offset)))
        fetched = cur.fetchall()

        columns = [d[0] for d in cur.description]
        if has_rowid:
            columns = columns[1:]  # hide the synthetic _rowid from the column list

        rows = []
        for r in fetched:
            d = dict(r)
            rowid = d.pop("_rowid", None) if has_rowid else None
            rows.append({"rowid": rowid, "values": [d[c] for c in columns]})

        return {
            "columns": columns,
            "rows": rows,
            "total": total,
            "has_rowid": has_rowid,
        }

    def _build_where(
        self,
        filters: dict[str, str] | None,
        valid_cols: set[str],
        exact_filters: dict[str, Any] | None = None,
    ) -> tuple[str, list[Any]]:
        clauses, params = [], []
        for col, val in (filters or {}).items():
            if col not in valid_cols or val is None or val == "":
                continue
            clauses.append(f"CAST({_quote(col)} AS TEXT) LIKE ? ESCAPE '\\'")
            esc = val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{esc}%")
        for col, val in (exact_filters or {}).items():
            if col not in valid_cols or val is None or val == "":
                continue
            # No CAST: column affinity coerces the (string) param to the column
            # type, so picking "500" matches a REAL 500.0 and "2" matches INT 2
            # only (not 20).
            clauses.append(f"{_quote(col)} = ?")
            params.append(val)
        if not clauses:
            return "", []
        return " WHERE " + " AND ".join(clauses), params

    # ------------------------------------------------------------------ #
    # Arbitrary SQL
    # ------------------------------------------------------------------ #
    def run_sql(self, sql: str) -> dict[str, Any]:
        """Execute one statement. SELECT-like returns rows; writes return rowcount.

        Writes/DDL are staged inside the pending transaction so the user can
        Write or Revert them like any other edit.
        """
        sql = sql.strip()
        if not sql:
            raise DBError("Empty statement")
        was_dirty = self._in_txn
        before = self.conn.total_changes
        self._begin()
        try:
            cur = self.conn.execute(sql)
        except sqlite3.Error as e:
            # We opened the txn only for this statement and nothing else was
            # staged - release it so a failed query leaves no lingering lock.
            if not was_dirty and self._in_txn:
                self.conn.execute("ROLLBACK")
                self._in_txn = False
            raise DBError(str(e)) from e
        made_changes = self.conn.total_changes != before
        if cur.description is not None:
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
            # A pure read with no pending edits must not hold a lock or mark the
            # database dirty - roll back the transaction we opened for it.
            if not made_changes and not was_dirty:
                self.conn.execute("ROLLBACK")
                self._in_txn = False
            return {"type": "select", "columns": columns, "rows": rows, "rowcount": len(rows)}
        return {"type": "write", "columns": [], "rows": [], "rowcount": cur.rowcount}

    def distinct_values(self, table, column, cap: int = 200):
        """Distinct non-null values of a column, for building filter dropdowns.

        Returns ``(values, truncated)``. ``truncated`` is True when more than
        ``cap`` distinct values exist (caller should fall back to a text filter).
        """
        if not self._object_exists(table):
            raise DBError(f"No such table or view: {table}")
        if column not in set(self._column_names(table)):
            raise DBError(f"Unknown column: {column}")
        rows = self.conn.execute(
            f"SELECT DISTINCT {_quote(column)} FROM {_quote(table)} "
            f"WHERE {_quote(column)} IS NOT NULL ORDER BY 1 LIMIT ?",
            (cap + 1,),
        ).fetchall()
        values = [r[0] for r in rows]
        return values[:cap], len(values) > cap

    def run_script(self, sql: str) -> dict[str, Any]:
        """Execute multiple statements (no result rows). Staged in the txn."""
        self._begin()
        try:
            self.conn.executescript(sql)
        except sqlite3.Error as e:
            raise DBError(str(e)) from e
        return {"type": "script", "rowcount": -1}

    # ------------------------------------------------------------------ #
    # Row edits (staged)
    # ------------------------------------------------------------------ #
    def apply_changes(
        self,
        table: str,
        inserts: list[dict[str, Any]] | None = None,
        updates: list[dict[str, Any]] | None = None,
        deletes: list[Any] | None = None,
    ) -> dict[str, int]:
        """Stage inserts/updates/deletes inside the pending transaction.

        - ``inserts``: list of {column: value} dicts.
        - ``updates``: list of {"rowid": <id>, "values": {column: value}} dicts.
        - ``deletes``: list of rowids.
        Requires the table to have a rowid. Returns counts.
        """
        if not self._is_table(table):
            raise DBError(f"Not an editable table: {table}")
        if not self._has_rowid(table):
            raise DBError(
                f"Table {table} has no rowid; inline editing is unsupported."
            )
        valid_cols = set(self._column_names(table))
        self._begin()
        counts = {"inserted": 0, "updated": 0, "deleted": 0}
        try:
            for row in inserts or []:
                cols = [c for c in row if c in valid_cols]
                if not cols:
                    self.conn.execute(f"INSERT INTO {_quote(table)} DEFAULT VALUES")
                else:
                    placeholders = ", ".join("?" for _ in cols)
                    col_sql = ", ".join(_quote(c) for c in cols)
                    self.conn.execute(
                        f"INSERT INTO {_quote(table)} ({col_sql}) VALUES ({placeholders})",
                        [row[c] for c in cols],
                    )
                counts["inserted"] += 1

            for upd in updates or []:
                rowid = upd.get("rowid")
                values = {c: v for c, v in (upd.get("values") or {}).items() if c in valid_cols}
                if rowid is None or not values:
                    continue
                set_sql = ", ".join(f"{_quote(c)} = ?" for c in values)
                self.conn.execute(
                    f"UPDATE {_quote(table)} SET {set_sql} WHERE rowid = ?",
                    [*values.values(), rowid],
                )
                counts["updated"] += 1

            for rowid in deletes or []:
                self.conn.execute(
                    f"DELETE FROM {_quote(table)} WHERE rowid = ?", (rowid,)
                )
                counts["deleted"] += 1
        except sqlite3.Error as e:
            raise DBError(str(e)) from e
        return counts

    def bulk_update_column(
        self,
        table: str,
        column: str,
        value: Any,
        filters: dict[str, str] | None = None,
        exact_filters: dict[str, Any] | None = None,
    ) -> int:
        """Set ``column`` to ``value`` for all rows matching the filters."""
        if not self._is_table(table):
            raise DBError(f"Not an editable table: {table}")
        valid_cols = set(self._column_names(table))
        if column not in valid_cols:
            raise DBError(f"Unknown column: {column}")
        where_sql, params = self._build_where(filters, valid_cols, exact_filters)
        self._begin()
        try:
            cur = self.conn.execute(
                f"UPDATE {_quote(table)} SET {_quote(column)} = ?{where_sql}",
                [value, *params],
            )
        except sqlite3.Error as e:
            raise DBError(str(e)) from e
        return cur.rowcount

    # ------------------------------------------------------------------ #
    # Schema: tables
    # ------------------------------------------------------------------ #
    def create_table(self, name: str, columns: list[dict[str, Any]]) -> None:
        """Create a table. ``columns`` items: {name, type, pk, notnull, default}."""
        _require_safe_name(name, "table name")
        if not columns:
            raise DBError("A table needs at least one column")
        defs = []
        pk_cols = [c for c in columns if c.get("pk")]
        single_pk = len(pk_cols) == 1
        for c in columns:
            _require_safe_name(c["name"], "column name")
            parts = [_quote(c["name"]), c.get("type") or "TEXT"]
            if c.get("pk") and single_pk:
                parts.append("PRIMARY KEY")
                if c.get("autoincrement"):
                    parts.append("AUTOINCREMENT")
            if c.get("notnull"):
                parts.append("NOT NULL")
            if c.get("default") not in (None, ""):
                parts.append(f"DEFAULT {c['default']}")
            defs.append(" ".join(parts))
        if len(pk_cols) > 1:
            pk_list = ", ".join(_quote(c["name"]) for c in pk_cols)
            defs.append(f"PRIMARY KEY ({pk_list})")
        self._begin()
        try:
            self.conn.execute(f"CREATE TABLE {_quote(name)} ({', '.join(defs)})")
        except sqlite3.Error as e:
            raise DBError(str(e)) from e

    def drop_table(self, name: str) -> None:
        if not self._object_exists(name):
            raise DBError(f"No such table or view: {name}")
        self._begin()
        try:
            kind = "VIEW" if not self._is_table(name) else "TABLE"
            self.conn.execute(f"DROP {kind} {_quote(name)}")
        except sqlite3.Error as e:
            raise DBError(str(e)) from e

    def rename_table(self, name: str, new_name: str) -> None:
        _require_safe_name(new_name, "table name")
        self._begin()
        try:
            self.conn.execute(
                f"ALTER TABLE {_quote(name)} RENAME TO {_quote(new_name)}"
            )
        except sqlite3.Error as e:
            raise DBError(str(e)) from e

    # ------------------------------------------------------------------ #
    # Schema: columns
    # ------------------------------------------------------------------ #
    def add_column(self, table: str, column: dict[str, Any]) -> None:
        _require_safe_name(column["name"], "column name")
        parts = [_quote(column["name"]), column.get("type") or "TEXT"]
        if column.get("notnull"):
            # NOT NULL without a default fails on a non-empty table.
            if column.get("default") in (None, ""):
                raise DBError("A NOT NULL column must have a default value")
            parts.append("NOT NULL")
        if column.get("default") not in (None, ""):
            parts.append(f"DEFAULT {column['default']}")
        self._begin()
        try:
            self.conn.execute(
                f"ALTER TABLE {_quote(table)} ADD COLUMN {' '.join(parts)}"
            )
        except sqlite3.Error as e:
            raise DBError(str(e)) from e

    def rename_column(self, table: str, old: str, new: str) -> None:
        _require_safe_name(new, "column name")
        self._begin()
        try:
            self.conn.execute(
                f"ALTER TABLE {_quote(table)} RENAME COLUMN {_quote(old)} TO {_quote(new)}"
            )
        except sqlite3.OperationalError:
            self._rebuild_without_column(table, drop=None, rename=(old, new))
        except sqlite3.Error as e:
            raise DBError(str(e)) from e

    def drop_column(self, table: str, column: str) -> None:
        self._begin()
        try:
            self.conn.execute(
                f"ALTER TABLE {_quote(table)} DROP COLUMN {_quote(column)}"
            )
        except sqlite3.OperationalError:
            # Older SQLite (<3.35) - fall back to a table rebuild.
            self._rebuild_without_column(table, drop=column, rename=None)
        except sqlite3.Error as e:
            raise DBError(str(e)) from e

    def _rebuild_without_column(
        self,
        table: str,
        drop: str | None,
        rename: tuple[str, str] | None,
    ) -> None:
        """Recreate a table to drop/rename a column on SQLite without ALTER support."""
        info = [r for r in self.conn.execute(f"PRAGMA table_info({_quote(table)})")]
        keep = [r for r in info if r["name"] != drop]
        if not keep:
            raise DBError("Cannot drop the only column of a table")

        def out_name(src: str) -> str:
            if rename and src == rename[0]:
                return rename[1]
            return src

        col_defs = []
        for r in keep:
            parts = [_quote(out_name(r["name"])), r["type"] or "TEXT"]
            if r["notnull"]:
                parts.append("NOT NULL")
            if r["dflt_value"] is not None:
                parts.append(f"DEFAULT {r['dflt_value']}")
            if r["pk"]:
                parts.append("PRIMARY KEY")
            col_defs.append(" ".join(parts))

        src_cols = ", ".join(_quote(r["name"]) for r in keep)
        dst_cols = ", ".join(_quote(out_name(r["name"])) for r in keep)
        tmp = f"_rebuild_{table}"
        # legacy_alter_table stops the DROP/RENAME from rewriting (and choking
        # on) views or triggers that reference the table being rebuilt.
        self.conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            self.conn.execute(f"CREATE TABLE {_quote(tmp)} ({', '.join(col_defs)})")
            self.conn.execute(
                f"INSERT INTO {_quote(tmp)} ({dst_cols}) SELECT {src_cols} FROM {_quote(table)}"
            )
            self.conn.execute(f"DROP TABLE {_quote(table)}")
            self.conn.execute(f"ALTER TABLE {_quote(tmp)} RENAME TO {_quote(table)}")
        except sqlite3.Error as e:
            raise DBError(str(e)) from e
        finally:
            self.conn.execute("PRAGMA legacy_alter_table = OFF")

    # ------------------------------------------------------------------ #
    # Schema: indexes
    # ------------------------------------------------------------------ #
    def create_index(
        self, name: str, table: str, columns: list[str], unique: bool = False
    ) -> None:
        _require_safe_name(name, "index name")
        valid = set(self._column_names(table))
        for c in columns:
            if c not in valid:
                raise DBError(f"Unknown column: {c}")
        col_sql = ", ".join(_quote(c) for c in columns)
        uniq = "UNIQUE " if unique else ""
        self._begin()
        try:
            self.conn.execute(
                f"CREATE {uniq}INDEX {_quote(name)} ON {_quote(table)} ({col_sql})"
            )
        except sqlite3.Error as e:
            raise DBError(str(e)) from e

    def drop_index(self, name: str) -> None:
        self._begin()
        try:
            self.conn.execute(f"DROP INDEX {_quote(name)}")
        except sqlite3.Error as e:
            raise DBError(str(e)) from e

    # ------------------------------------------------------------------ #
    # Import / export
    # ------------------------------------------------------------------ #
    def import_dataframe(self, df, table: str, mode: str = "create") -> int:
        """Load a pandas DataFrame into ``table``.

        mode: 'create' (fail if exists), 'replace' (drop+recreate), 'append'.
        Commits any pending changes first, then writes (pandas manages its own
        transaction). Returns number of rows written.
        """
        if mode not in ("create", "replace", "append"):
            raise DBError(f"Invalid import mode: {mode}")
        if mode == "create":
            _require_safe_name(table, "table name")
        # pandas.to_sql needs control of the connection's transaction state.
        self.write_changes()
        if_exists = {"create": "fail", "replace": "replace", "append": "append"}[mode]
        try:
            df.to_sql(table, self.conn, if_exists=if_exists, index=False)
        except ValueError as e:
            raise DBError(str(e)) from e
        except sqlite3.Error as e:
            raise DBError(str(e)) from e
        self.conn.commit()
        return len(df)

    def export_rows(self, table_or_sql: str, is_query: bool = False):
        """Return (columns, rows) for a table or a SELECT query, for export."""
        if is_query:
            cur = self.conn.execute(table_or_sql)
        else:
            if not self._object_exists(table_or_sql):
                raise DBError(f"No such table or view: {table_or_sql}")
            cur = self.conn.execute(f"SELECT * FROM {_quote(table_or_sql)}")
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        return columns, rows

    # ------------------------------------------------------------------ #
    # Backup / restore
    # ------------------------------------------------------------------ #
    def backup(self, backup_dir: str | Path, timestamp: str | None = None) -> Path:
        """Copy the live DB file into ``backup_dir`` with a timestamped name."""
        self.write_changes()  # flush staged edits so the backup is consistent
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"{self.path.stem}-{ts}{self.path.suffix or '.db'}"
        # Use sqlite's online backup API so an open connection is safe.
        with sqlite3.connect(str(dest)) as bck:
            self.conn.backup(bck)
        return dest

    def restore(self, backup_path: str | Path) -> None:
        """Replace the live DB with a backup copy (current file is overwritten)."""
        backup_path = Path(backup_path)
        if not backup_path.exists():
            raise DBError(f"Backup not found: {backup_path}")
        self.revert()
        self.conn.close()
        shutil.copyfile(backup_path, self.path)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.isolation_level = None
        self._in_txn = False
