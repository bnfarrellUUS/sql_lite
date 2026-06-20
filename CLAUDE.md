# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

# SQLite Browser — Project Notes

A local, web-based SQLite database manager. View tables, run SQL, bulk-edit
data, manage schema, import/export, and back up `.db` files from the browser.
**Single-user, localhost only, no authentication** by design.

## How to run

- Double-click **`run_browser.bat`** (installs deps on first run, then opens the
  app in your browser).
- Or: `pip install -r requirements.txt` then `python server.py`.
- Serves at **http://localhost:5050** (chosen so it doesn't clash with the AR
  Dashboard on 5000). Override with the `PORT` env var, e.g.
  `$env:PORT=5060; python server.py`.

## Stack

Python 3 + Flask backend, single-page vanilla HTML/CSS/JS frontend (no build
step). SQLite via the stdlib `sqlite3`. `pandas` + `openpyxl` for CSV/Excel
import-export. This mirrors the sibling `ar_dashboard` project's pattern.

## File layout

```
server.py            Flask REST API (thin HTTP layer over db_service)
db_service.py        All SQLite logic — the heart of the app, fully unit-tested
static/
  index.html         Tabbed UI shell
  app.js             Fetch calls, grid/tree rendering, pending-change tracking
  style.css
tests/test_db_service.py   pytest suite (run: python -m pytest tests/ -q)
requirements.txt
run_browser.bat
uploads/   backups/   app_meta.sqlite   <- created at runtime (not in git)
```

## Architecture notes (read before editing)

- **`db_service.py` has no Flask imports** on purpose, so it can be unit-tested
  directly against temp database files. Keep HTTP concerns in `server.py`.
- **Pending-changes model:** the open database keeps one `sqlite3` connection in
  autocommit mode (`isolation_level=None`). Edits run inside an explicit
  transaction (`BEGIN`); the UI's **Write Changes** = `COMMIT`, **Revert** =
  `ROLLBACK`. `is_dirty()` reflects whether that transaction is open.
- **Reads must not dirty the DB:** `run_sql()` opens a transaction only when a
  statement actually changes data (or DDL); a pure `SELECT` is rolled back so it
  holds no lock and doesn't flip the dirty flag. Don't reintroduce an
  unconditional `BEGIN` for reads.
- **Auto-backup before destructive ops:** committing changes, dropping a table,
  and dropping a column each copy the live `.db` into `backups/` first (sqlite
  online-backup API). Confirmation dialogs guard destructive UI actions.
- **App metadata** (query history, saved queries) lives in a separate
  `app_meta.sqlite`, never mixed into the user's database.
- **Schema edits** use native `ALTER TABLE` (SQLite 3.35+). `_rebuild_without_
  column()` is a table-rebuild fallback for older SQLite and uses
  `PRAGMA legacy_alter_table=ON` so dependent views/triggers don't break.

## Performance guardrails (don't remove these)

- **Browse Data** is always paginated (default 1000 rows/page) — never load a
  whole table into the browser.
- **Execute SQL** caps the rendered grid at `SQL_RENDER_CAP` (2000 rows in
  `app.js`); the status line reports the true count and points to Export for the
  full set. Rendering an unbounded result set previously froze the browser.
- **Filter dropdowns** (Browse Data) list a column's distinct values for exact-
  match filtering. They are cached per table, skipped for tables larger than
  `FILTER_MAX` (50,000 rows) in favor of text filters, and capped at 300 distinct
  values per column. Free-text filters use substring `LIKE`; dropdowns use exact
  match via column affinity.
- Single shared DB connection ⇒ DB access is serialized; a genuinely slow query
  blocks other requests until it finishes. Acceptable for single-user use.

## Verifying changes

- Unit: `python -m pytest tests/ -q` (covers introspection, paged/sorted/
  filtered browse, stage→write→revert, schema ops incl. drop-column fallback,
  CSV/Excel round-trip, backup/restore, distinct values).
- End-to-end: run the server and exercise the flow in the browser (create DB →
  add table → import CSV → bulk edit → run SQL → drop table → backup/restore).
  Always stop the server and delete runtime artifacts (`*.db`, `backups/`,
  `uploads/`, `app_meta.sqlite`) when done testing.

## Version control — commit every change

This project is tracked at **https://github.com/bnfarrellUUS/sql_lite** (`origin`,
branch `main`).

**After making any change to the app, commit it and push.** Don't leave edits
uncommitted. The routine after a change is verified:

1. `git add -A`
2. `git commit -m "<concise description of the change>"`
3. `git push origin main`

Never commit runtime artifacts — they're excluded by `.gitignore`: the user's
`.db`/`.sqlite` files, `backups/`, `uploads/`, `app_meta.sqlite`, `__pycache__/`,
`.pytest_cache/`, `*.log`. Clean those up before committing if any were created
during testing.
