# SQLite Browser

A local, web-based SQLite database manager — view tables, run SQL, bulk-edit
data, manage schema, import/export, and back up `.db` files, all from your
browser. Inspired by [DB Browser for SQLite](https://sqlitebrowser.org/).

**Single-user, localhost only, no authentication** — it's a desktop-style tool
for managing database files on your own machine.

## Features

- **Open any database** — browse `.db`/`.sqlite` files already on your machine,
  upload one through the browser, or create a new empty database.
- **Database Structure** — sidebar tree of tables, views, and indexes with row
  counts; column / primary-key / foreign-key / index details.
- **Browse Data** — paginated, sortable grid with per-column **filter
  dropdowns** (the distinct values in each column); inline cell editing; add,
  delete, and **bulk-edit** rows.
- **Execute SQL** — run any query, see results in a grid, with **query history**
  and **saved queries**. Large result sets are capped in the display (export for
  the full set).
- **Schema editing** — create / rename / drop tables, add / rename / drop
  columns, and create / drop indexes.
- **Import / Export** — import CSV and Excel into new or existing tables; export
  tables or query results to CSV/XLSX; download the live `.db` file.
- **Backup / Restore** — one-click backups, plus an automatic backup before any
  destructive operation.
- **Pending changes** — edits are staged in a transaction; click **Write
  Changes** to commit or **Revert** to roll back (mirrors DB Browser for
  SQLite). A backup is taken automatically before every commit.

## Requirements

- Python 3.9+
- Dependencies in `requirements.txt` (Flask, pandas, openpyxl)

## Running

**Windows:** double-click `run_browser.bat` — it installs dependencies on the
first run and opens the app in your browser.

**Any platform:**

```bash
pip install -r requirements.txt
python server.py
```

Then open <http://localhost:5050>. Port 5050 is the default (chosen to avoid
clashing with other local apps on 5000); override it with the `PORT` environment
variable, e.g. `PORT=5060 python server.py`.

## Project layout

```
server.py            Flask REST API (thin HTTP layer over db_service)
db_service.py        All SQLite logic — fully unit-tested
static/
  index.html         Tabbed UI shell
  app.js             Frontend: fetch calls, grid/tree rendering, edits
  style.css
tests/test_db_service.py   pytest suite
requirements.txt
run_browser.bat      Windows launcher
```

Runtime files — your `.db`/`.sqlite` files, `backups/`, `uploads/`,
`app_meta.sqlite` (query history & saved queries) — are created as needed and
are excluded from version control via `.gitignore`.

## Testing

```bash
python -m pytest tests/ -q
```

## Tech stack

Python + Flask backend, single-page vanilla HTML/CSS/JS frontend (no build
step). SQLite via the standard-library `sqlite3`; pandas + openpyxl for
CSV/Excel import-export.

## Notes & limitations

- A single shared database connection is used, so database access is serialized;
  a genuinely slow query will block other actions until it finishes. Fine for
  single-user local use.
- `DROP COLUMN` / `RENAME COLUMN` use native `ALTER TABLE` (SQLite 3.35+), with a
  table-rebuild fallback for older SQLite.
- No authentication — do not expose this server beyond `localhost`.
