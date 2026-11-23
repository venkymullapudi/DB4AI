"""Dataset records in the connector framework: snapshots diffed into data.* tables, drift and deletions via the harness."""

import pytest
from fastapi.testclient import TestClient

from db4ai import connectors, datasets, harness
from db4ai.api import create_app
from db4ai.connectors import Batch, DatasetRecord

HEADER = ["Region", "Revenue", "Closed"]
ROWS = [["EMEA", "100", "2026-01-05"], ["APAC", "80", "2026-02-01"], ["EMEA", "100", "2026-01-05"]]


def _sync(w, recs=(), deleted=(), source="sheets", cursor=None, expected=None):
    return connectors.apply_batch(w.conn, w.bob, source, w.eng, [], cursor, expected,
                                  dataset_records=list(recs), deleted_datasets=list(deleted))


def _rows(w, table):
    return w.conn.execute(f"select * from {table} order by _row_id").fetchall()


def _data_log(w, table):
    return w.conn.execute("select op from change_log where entity=%s order by id", (table,)).fetchall()


def test_snapshot_creates_table_and_diffs_rows(world):
    w = world
    r = _sync(w, [DatasetRecord("s1#0", "Pipeline Q1", HEADER, ROWS)], cursor="c1")
    d = r["datasets"][0]
    assert (d["status"], d["created"], d["inserted"], d["deleted"]) == ("synced", True, 3, 0)
    assert d["table"] == "data.eng_pipeline_q1"
    ds = w.conn.execute("select * from datasets where id=%s", (d["dataset_id"],)).fetchone()
    assert ds["external_key"] == "sheets:s1#0" and ds["name"] == "pipeline_q1"
    assert [(c["name"], c["type"]) for c in ds["columns"]] == [
        ("region", "text"), ("revenue", "bigint"), ("closed", "timestamptz")]
    assert [x["region"] for x in _rows(w, d["table"])] == ["EMEA", "APAC", "EMEA"]
    first_ids = [x["_row_id"] for x in _rows(w, d["table"])]

    # Same snapshot again: nothing written (idempotent, change_log unchanged).
    n_log = len(_data_log(w, d["table"]))
    again = _sync(w, [DatasetRecord("s1#0", "renamed later", HEADER, ROWS)], cursor="c2", expected="c1")
    assert (again["datasets"][0]["inserted"], again["datasets"][0]["deleted"]) == (0, 0)
    assert len(_data_log(w, d["table"])) == n_log
    assert [x["_row_id"] for x in _rows(w, d["table"])] == first_ids

    # One duplicate removed, one value changed, one row added: only those rows change (multiset diff).
    new = [["EMEA", "100", "2026-01-05"], ["APAC", "90", "2026-02-01"], ["AMER", "", ""]]
    r3 = _sync(w, [DatasetRecord("s1#0", "x", HEADER, new)], cursor="c3", expected="c2")
    assert (r3["datasets"][0]["inserted"], r3["datasets"][0]["deleted"]) == (2, 2)
    rows = _rows(w, d["table"])
    assert rows[0]["_row_id"] == first_ids[0]
    assert sorted((x["region"], x["revenue"]) for x in rows) == [("AMER", None), ("APAC", 90), ("EMEA", 100)]
    ops = [x["op"] for x in _data_log(w, d["table"])]
    assert ops.count("insert") == 5 and ops.count("delete") == 2
    src = w.conn.execute("select distinct source from change_log where entity=%s", (d["table"],)).fetchall()
    assert [x["source"] for x in src] == ["connector:sheets"]
    cur = connectors.get_cursor(w.conn, w.bob, "sheets", w.eng)
    assert (cur["cursor"], cur["records"]) == ("c3", 3)


def test_name_collision_gets_a_suffix(world):
    w = world
    from db4ai.db import Provenance, transaction
    prov = Provenance(w.bob.id, "cli")
    with transaction(w.conn, prov):
        datasets.import_csv(w.conn, w.bob, prov, w.eng, "pipeline", "a\n1\n")
    r = _sync(w, [DatasetRecord("k", "Pipeline", ["a"], [["x"]])])
    assert r["datasets"][0]["table"].startswith("data.eng_pipeline_") and r["datasets"][0]["status"] == "synced"
    assert [x["a"] for x in _rows(w, "data.eng_pipeline")] == [1]  # the CSV import is untouched


def test_schema_drift_waits_for_the_harness(world):
    w = world
    d = _sync(w, [DatasetRecord("s", "deals", HEADER, ROWS[:2])])["datasets"][0]
    table = d["table"]
    # Column added, one removed, and revenue now holds text.
    hdr2 = ["Region", "Revenue", "Owner"]
    rows2 = [["EMEA", "n/a", "Ana"], ["APAC", "80", "Bo"]]
    r = _sync(w, [DatasetRecord("s", "deals", hdr2, rows2)])["datasets"][0]
    assert r["status"] == "schema_drift" and r["decision_status"] == "pending"
    assert r["drift"] == {"add": [{"name": "owner", "type": "text"}], "retype": ["revenue"], "retire": ["closed"]}
    assert [x["revenue"] for x in _rows(w, table)] == [100, 80]  # nothing applied (fail closed)

    # A second drifted snapshot replaces the pending proposal instead of stacking a new one next to it.
    r_b = _sync(w, [DatasetRecord("s", "deals", hdr2 + ["Stage"], [x + ["won"] for x in rows2])])["datasets"][0]
    old = w.conn.execute("select status from harness_decisions where id=%s", (r["decision_id"],)).fetchone()
    assert old["status"] == "expired" and r_b["decision_id"] != r["decision_id"]
    # Re-sending the same drift is idempotent.
    r_c = _sync(w, [DatasetRecord("s", "deals", hdr2 + ["Stage"], [x + ["won"] for x in rows2])])["datasets"][0]
    assert r_c["decision_id"] == r_b["decision_id"]

    pending = harness.list_decisions(w.conn, w.alice)
    assert [p["action_type"] for p in pending] == ["alter_dataset"] and pending[0]["tier"] == "ask_first"
    assert pending[0]["diff"]["retire_columns"] == ["closed"]
    harness.decide(w.conn, w.alice, r_b["decision_id"], approve=True)

    ds = w.conn.execute("select columns from datasets where id=%s", (d["dataset_id"],)).fetchone()
    cols = {c["name"]: c for c in ds["columns"]}
    assert cols["revenue"]["type"] == "text" and cols["closed"]["retired"] and cols["stage"]["type"] == "text"
    done = _sync(w, [DatasetRecord("s", "deals", hdr2 + ["Stage"], [x + ["won"] for x in rows2])])["datasets"][0]
    assert done["status"] == "synced"
    rows = _rows(w, table)
    assert sorted((x["region"], x["revenue"], x["owner"], x["closed"] is None) for x in rows) == [
        ("APAC", "80", "Bo", True), ("EMEA", "n/a", "Ana", True)]

    # A retired column that comes back is un-retired through the harness too.
    back = _sync(w, [DatasetRecord("s", "deals", ["Region", "Closed"], [["EMEA", "2026-03-01"]])])["datasets"][0]
    assert back["drift"]["add"] == [{"name": "closed", "type": "timestamptz"}]
    assert sorted(back["drift"]["retire"]) == ["owner", "revenue", "stage"]


def test_auto_policy_applies_drift_in_the_same_batch(world):
    w = world
    _sync(w, [DatasetRecord("s", "deals", ["a"], [["1"]])])
    harness.set_policy(w.conn, w.alice, "alter_dataset", "auto", team_id=w.eng)
    r = _sync(w, [DatasetRecord("s", "deals", ["a", "b"], [["1", "x"]])])["datasets"][0]
    assert (r["status"], r["decision_status"], r["inserted"], r["deleted"]) == ("synced", "applied_auto", 1, 1)


def test_rejected_drift_is_not_reproposed(world):
    w = world
    _sync(w, [DatasetRecord("s", "deals", ["a"], [["1"]])])
    r = _sync(w, [DatasetRecord("s", "deals", ["a", "b"], [["1", "x"]])])["datasets"][0]
    harness.decide(w.conn, w.alice, r["decision_id"], approve=False)
    again = _sync(w, [DatasetRecord("s", "deals", ["a", "b"], [["1", "x"]])])["datasets"][0]
    assert (again["status"], again["decision_status"]) == ("schema_drift", "rejected")
    assert len(harness.list_decisions(w.conn, w.alice)) == 0


def test_deleted_dataset_proposes_archive_and_can_come_back(world):
    w = world
    d = _sync(w, [DatasetRecord("s", "deals", ["a"], [["1"]])])["datasets"][0]
    assert _sync(w, deleted=["s", "unknown"])["datasets_deleted"] == 1
    assert _sync(w, deleted=["s"])["deletions"][0]["decision_id"]  # idempotent: same decision
    pend = harness.list_decisions(w.conn, w.alice)
    assert [p["action_type"] for p in pend] == ["archive_dataset"] and pend[0]["tier"] == "ask_first"

    # Back in the source before anyone decided: the proposal is withdrawn.
    _sync(w, [DatasetRecord("s", "deals", ["a"], [["1"]])])
    assert harness.list_decisions(w.conn, w.alice) == []

    # Deleted again (a fresh proposal) and approved: the table stays, syncing stops, revert resumes it.
    dec = _sync(w, deleted=["s"])["deletions"][0]
    assert dec["status"] == "pending"
    harness.decide(w.conn, w.alice, dec["decision_id"], approve=True)
    r = _sync(w, [DatasetRecord("s", "deals", ["a"], [["2"]])])["datasets"][0]
    assert (r["status"], r["inserted"]) == ("archived", 0)
    assert [x["a"] for x in _rows(w, d["table"])] == [1]
    assert _sync(w, deleted=["s"])["datasets_deleted"] == 0
    harness.revert(w.conn, w.root, dec["decision_id"])
    r = _sync(w, [DatasetRecord("s", "deals", ["a"], [["2"]])])["datasets"][0]
    assert r["status"] == "synced" and [x["a"] for x in _rows(w, d["table"])] == [2]


def test_batch_validation(world):
    w = world
    with pytest.raises(ValueError, match="more cells"):
        _sync(w, [DatasetRecord("s", "x", ["a"], [["1", "2"]])])
    with pytest.raises(ValueError, match="at least one column"):
        _sync(w, [DatasetRecord("s", "x", [], [])])
    with pytest.raises(ValueError, match="both saved and deleted"):
        _sync(w, [DatasetRecord("s", "x", ["a"], [])], deleted=["s"])
    with pytest.raises(ValueError, match="only once"):
        _sync(w, [DatasetRecord("s", "x", ["a"], []), DatasetRecord("s", "x", ["a"], [])])
    assert datasets.snapshot_header(["Name", "name", "", "_row_id"]) == ["name", "name_2", "column", "row_id"]
    assert w.conn.execute("select count(*) n from datasets").fetchone()["n"] == 0


def test_run_counts_datasets(world):
    w = world

    class Fake:
        def fetch(self, cursor):
            yield Batch([], "1", datasets=[DatasetRecord("a", "A", ["x"], [["1"]])])
            yield Batch([], "2", deleted_datasets=["a"])

    r = connectors.run(w.conn, w.bob, "fake", w.eng, Fake())
    assert (r["batches"], r["datasets"], r["datasets_deleted"], r["cursor"]) == (2, 1, 1, "2")


def test_http_sync_with_datasets(world, db_url):
    w = world
    client = TestClient(create_app(db_url))
    with client:
        h = {"Authorization": f"Bearer {w.tokens['bob']}"}
        body = {"team": "eng", "datasets": [{"key": "k", "name": "Budget", "columns": ["Dept", "Amount"],
                                             "rows": [["ops", "10"], ["it", None]]}], "cursor": "p1"}
        r = client.post("/sync/sheets", json=body, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["datasets"][0]["status"] == "synced"
        q = client.get("/datasets/budget", params={"team": "eng"}, headers=h)
        assert q.status_code == 200 and [x["dept"] for x in q.json()] == ["ops", "it"]
        bad = client.post("/sync/sheets", json={**body, "expected_cursor": "p1",
                                                "datasets": [{"key": "k", "name": "B", "columns": []}]}, headers=h)
        assert bad.status_code in (400, 422)
