"""Unit tests for db_service.Database against temporary SQLite files."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_service import Database, DBError  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    # Seed with sqlite3 directly so Database starts clean.
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER);
        INSERT INTO people (name, age) VALUES ('Alice', 30), ('Bob', 25), ('Carol', 40);
        CREATE TABLE notes (body TEXT);
        INSERT INTO notes (body) VALUES ('hello'), ('world');
        CREATE VIEW adults AS SELECT name FROM people WHERE age >= 18;
        CREATE INDEX idx_age ON people(age);
        """
    )
    con.commit()
    con.close()
    database = Database(path)
    yield database
    database.close()


# --------------------------------------------------------------------- #
# Introspection
# --------------------------------------------------------------------- #
def test_list_objects(db):
    objs = db.list_objects()
    names = {t["name"]: t["rows"] for t in objs["tables"]}
    assert names == {"people": 3, "notes": 2}
    assert [v["name"] for v in objs["views"]] == ["adults"]
    assert "idx_age" in [i["name"] for i in objs["indexes"]]


def test_table_info(db):
    info = db.table_info("people")
    cols = {c["name"]: c for c in info["columns"]}
    assert cols["id"]["pk"] == 1
    assert cols["name"]["notnull"] is True
    assert info["rows"] == 3
    assert info["has_rowid"] is True
    assert any(ix["name"] == "idx_age" for ix in info["indexes"])


def test_table_info_missing(db):
    with pytest.raises(DBError):
        db.table_info("nope")


# --------------------------------------------------------------------- #
# Browse
# --------------------------------------------------------------------- #
def test_get_rows_basic(db):
    page = db.get_rows("people")
    assert page["columns"] == ["id", "name", "age"]
    assert page["total"] == 3
    assert page["has_rowid"] is True
    assert {r["values"][1] for r in page["rows"]} == {"Alice", "Bob", "Carol"}


def test_get_rows_pagination(db):
    page = db.get_rows("people", limit=2, offset=0, order_by="id")
    assert [r["values"][0] for r in page["rows"]] == [1, 2]
    assert page["total"] == 3
    page2 = db.get_rows("people", limit=2, offset=2, order_by="id")
    assert [r["values"][0] for r in page2["rows"]] == [3]


def test_get_rows_sort_desc(db):
    page = db.get_rows("people", order_by="age", descending=True)
    assert [r["values"][2] for r in page["rows"]] == [40, 30, 25]


def test_get_rows_filter(db):
    page = db.get_rows("people", filters={"name": "a"})  # Alice, Carol
    assert page["total"] == 2
    assert {r["values"][1] for r in page["rows"]} == {"Alice", "Carol"}


def test_get_rows_bad_sort(db):
    with pytest.raises(DBError):
        db.get_rows("people", order_by="bogus")


# --------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------- #
def test_run_sql_select(db):
    res = db.run_sql("SELECT name FROM people ORDER BY name")
    assert res["type"] == "select"
    assert res["columns"] == ["name"]
    assert [r[0] for r in res["rows"]] == ["Alice", "Bob", "Carol"]


def test_run_sql_write_is_staged(db):
    res = db.run_sql("UPDATE people SET age = 99 WHERE name = 'Bob'")
    assert res["type"] == "write"
    assert res["rowcount"] == 1
    assert db.is_dirty() is True
    db.revert()
    assert db.run_sql("SELECT age FROM people WHERE name='Bob'")["rows"][0][0] == 25


def test_run_sql_error(db):
    with pytest.raises(DBError):
        db.run_sql("SELECT * FROM does_not_exist")


def test_select_does_not_dirty(db):
    db.run_sql("SELECT * FROM people")
    assert db.is_dirty() is False  # a pure read must not leave a transaction open


def test_select_preserves_pending_edits(db):
    db.apply_changes("people", deletes=[1])
    assert db.is_dirty() is True
    db.run_sql("SELECT * FROM people")  # reading must not drop the staged delete
    assert db.is_dirty() is True
    db.revert()


def test_distinct_values(db):
    vals, truncated = db.distinct_values("notes", "body")
    assert set(vals) == {"hello", "world"}
    assert truncated is False
    vals2, trunc2 = db.distinct_values("people", "age", cap=2)
    assert len(vals2) == 2 and trunc2 is True  # 3 distinct ages, cap 2


# --------------------------------------------------------------------- #
# Pending-changes editing
# --------------------------------------------------------------------- #
def test_apply_changes_roundtrip(db):
    counts = db.apply_changes(
        "people",
        inserts=[{"name": "Dave", "age": 50}],
        updates=[{"rowid": 1, "values": {"age": 31}}],
        deletes=[2],
    )
    assert counts == {"inserted": 1, "updated": 1, "deleted": 1}
    db.write_changes()
    page = db.get_rows("people", order_by="id")
    by_name = {r["values"][1]: r["values"][2] for r in page["rows"]}
    assert by_name == {"Alice": 31, "Carol": 40, "Dave": 50}


def test_write_and_revert(db):
    db.apply_changes("people", deletes=[1])
    assert db.is_dirty()
    db.revert()
    assert db.get_rows("people")["total"] == 3
    assert not db.is_dirty()


def test_apply_changes_no_rowid_table(tmp_path):
    path = tmp_path / "wr.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (k TEXT PRIMARY KEY) WITHOUT ROWID")
    con.commit()
    con.close()
    d = Database(path)
    with pytest.raises(DBError):
        d.apply_changes("t", deletes=["x"])
    d.close()


def test_bulk_update_column(db):
    n = db.bulk_update_column("people", "age", 0, filters={"name": "a"})
    assert n == 2
    db.write_changes()
    page = db.get_rows("people")
    ages = {r["values"][1]: r["values"][2] for r in page["rows"]}
    assert ages["Alice"] == 0 and ages["Carol"] == 0 and ages["Bob"] == 25


# --------------------------------------------------------------------- #
# Schema: tables
# --------------------------------------------------------------------- #
def test_create_and_drop_table(db):
    db.create_table(
        "widgets",
        [
            {"name": "id", "type": "INTEGER", "pk": True, "autoincrement": True},
            {"name": "label", "type": "TEXT", "notnull": True},
        ],
    )
    db.write_changes()
    assert "widgets" in [t["name"] for t in db.list_objects()["tables"]]
    db.drop_table("widgets")
    db.write_changes()
    assert "widgets" not in [t["name"] for t in db.list_objects()["tables"]]


def test_drop_view(db):
    db.drop_table("adults")
    db.write_changes()
    assert "adults" not in [v["name"] for v in db.list_objects()["views"]]


def test_rename_table(db):
    db.rename_table("notes", "memos")
    db.write_changes()
    names = [t["name"] for t in db.list_objects()["tables"]]
    assert "memos" in names and "notes" not in names


def test_create_table_bad_name(db):
    with pytest.raises(DBError):
        db.create_table("bad name!", [{"name": "x", "type": "TEXT"}])


# --------------------------------------------------------------------- #
# Schema: columns + indexes
# --------------------------------------------------------------------- #
def test_add_rename_drop_column(db):
    db.add_column("people", {"name": "email", "type": "TEXT"})
    db.write_changes()
    assert "email" in db._column_names("people")

    db.rename_column("people", "email", "contact")
    db.write_changes()
    assert "contact" in db._column_names("people")
    assert "email" not in db._column_names("people")

    db.drop_column("people", "contact")
    db.write_changes()
    assert "contact" not in db._column_names("people")
    # Data survived the column ops.
    assert db.get_rows("people")["total"] == 3


def test_add_notnull_column_requires_default(db):
    with pytest.raises(DBError):
        db.add_column("people", {"name": "x", "type": "TEXT", "notnull": True})


def test_rebuild_drop_column_preserves_data(db):
    # Exercise the fallback path directly.
    db._rebuild_without_column("people", drop="age", rename=None)
    db.write_changes()
    assert "age" not in db._column_names("people")
    assert db.get_rows("people")["total"] == 3


def test_create_and_drop_index(db):
    db.create_index("idx_name", "people", ["name"], unique=True)
    db.write_changes()
    assert "idx_name" in [i["name"] for i in db.list_objects()["indexes"]]
    db.drop_index("idx_name")
    db.write_changes()
    assert "idx_name" not in [i["name"] for i in db.list_objects()["indexes"]]


# --------------------------------------------------------------------- #
# Import / export
# --------------------------------------------------------------------- #
def test_import_dataframe_create(db):
    df = pd.DataFrame({"city": ["NYC", "LA"], "pop": [8, 4]})
    n = db.import_dataframe(df, "cities", mode="create")
    assert n == 2
    assert db.get_rows("cities")["total"] == 2


def test_import_dataframe_append(db):
    df = pd.DataFrame({"body": ["again"]})
    db.import_dataframe(df, "notes", mode="append")
    assert db.get_rows("notes")["total"] == 3


def test_export_table_and_query(db):
    cols, rows = db.export_rows("people")
    assert cols == ["id", "name", "age"]
    assert len(rows) == 3
    cols2, rows2 = db.export_rows("SELECT name FROM people WHERE age > 30", is_query=True)
    assert cols2 == ["name"]
    assert [r[0] for r in rows2] == ["Carol"]


# --------------------------------------------------------------------- #
# Backup / restore
# --------------------------------------------------------------------- #
def test_backup_and_restore(db, tmp_path):
    backup_dir = tmp_path / "backups"
    dest = db.backup(backup_dir, timestamp="20260619-000000")
    assert dest.exists()

    # Mutate, commit, then restore from the backup.
    db.apply_changes("people", deletes=[1, 2, 3])
    db.write_changes()
    assert db.get_rows("people")["total"] == 0

    db.restore(dest)
    assert db.get_rows("people")["total"] == 3
