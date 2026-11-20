"""End-to-end tests for the exhibition visit-path API.

Covers the required self-test matrix:
  1. happy path with dwell calculation
  2. out-of-order / late open that re-sorts the path and changes dwell data
  3. missing close -> dwell unknown, visit still counted
  4. idempotent retries (same event_id, including different payload)
  5. consent withdrawal deletes the path and blocks re-uploads forever
  6. withdrawal interleaved with concurrent uploads/queries (no half paths)
  7. batch atomicity: one bad event rolls back the whole batch
  8. input validation (naive timestamps rejected, unknown visitor empty path)
  9. orphan close without an open is not shown as a visit
"""
from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app import config, crud
from app.db import connect, write_transaction
from app.main import app
from app.models import EventIn


def _event(event_id, visitor, page_view, exhibit, event_type, event_time):
    return {
        "event_id": event_id,
        "visitor_id": visitor,
        "page_view_id": page_view,
        "exhibit_id": exhibit,
        "event_type": event_type,
        "event_time": event_time,
    }


def _post(client, **kwargs):
    return client.post("/events", json=_event(**kwargs))


# ---------------------------------------------------------------------------
# 1. Happy path: paired open/close produces ordered visits with dwell times.
# ---------------------------------------------------------------------------
def test_happy_path_pairs_and_orders_visits(client):
    v = "visitor-A"
    # Exhibit X: 08:00 -> 08:05 (300s); Exhibit Y: 09:00 -> 09:02 (120s).
    _post(client, event_id="e1", visitor=v, page_view="pv1",
          exhibit="X", event_type="open", event_time="2026-11-18T08:00:00Z")
    _post(client, event_id="e2", visitor=v, page_view="pv1",
          exhibit="X", event_type="close", event_time="2026-11-18T08:05:00Z")
    _post(client, event_id="e3", visitor=v, page_view="pv2",
          exhibit="Y", event_type="open", event_time="2026-11-18T09:00:00Z")
    _post(client, event_id="e4", visitor=v, page_view="pv2",
          exhibit="Y", event_type="close", event_time="2026-11-18T09:02:00Z")

    resp = client.get(f"/visitors/{v}/path")
    assert resp.status_code == 200
    path = resp.json()
    assert path["consent_withdrawn"] is False
    assert path["total_visits"] == 2
    first, second = path["visits"]
    assert (first["sequence"], first["exhibit_id"], first["dwell_seconds"],
            first["dwell_known"]) == (1, "X", 300.0, True)
    assert first["closed_at"] == "2026-11-18T08:05:00+00:00"
    assert (second["sequence"], second["exhibit_id"], second["dwell_seconds"],
            second["dwell_known"]) == (2, "Y", 120.0, True)


# ---------------------------------------------------------------------------
# 2. Out-of-order reporting: late events still pair, and a late earlier open
#    re-sorts the path on the next query.
# ---------------------------------------------------------------------------
def test_out_of_order_events_pair_and_resort_path(client):
    v = "visitor-B"
    # Close arrives before its open (network reordering).
    _post(client, event_id="c1", visitor=v, page_view="pv1",
          exhibit="X", event_type="close", event_time="2026-11-18T08:10:00Z")
    _post(client, event_id="o1", visitor=v, page_view="pv1",
          exhibit="X", event_type="open", event_time="2026-11-18T08:00:00Z")
    # A later visit is already fully present.
    _post(client, event_id="o2", visitor=v, page_view="pv2",
          exhibit="Y", event_type="open", event_time="2026-11-18T09:00:00Z")
    _post(client, event_id="c2", visitor=v, page_view="pv2",
          exhibit="Y", event_type="close", event_time="2026-11-18T09:30:00Z")

    path = client.get(f"/visitors/{v}/path").json()
    assert [visit["exhibit_id"] for visit in path["visits"]] == ["X", "Y"]
    assert path["visits"][0]["dwell_seconds"] == 600.0

    # Late backfill: an even earlier open (exhibit Z at 07:00). Its close was
    # already reported two days late. The path order must change.
    _post(client, event_id="c0", visitor=v, page_view="pv0",
          exhibit="Z", event_type="close", event_time="2026-11-18T20:00:00Z")
    _post(client, event_id="o0", visitor=v, page_view="pv0",
          exhibit="Z", event_type="open", event_time="2026-11-18T07:00:00Z")

    path = client.get(f"/visitors/{v}/path").json()
    assert path["total_visits"] == 3
    assert [visit["exhibit_id"] for visit in path["visits"]] == ["Z", "X", "Y"]
    assert [visit["sequence"] for visit in path["visits"]] == [1, 2, 3]
    z = path["visits"][0]
    assert z["dwell_seconds"] == 13 * 3600.0 and z["dwell_known"] is True


# ---------------------------------------------------------------------------
# 3. Missing close: visit counted, but dwell is unknown.
# ---------------------------------------------------------------------------
def test_missing_close_marks_dwell_unknown(client):
    v = "visitor-C"
    _post(client, event_id="o1", visitor=v, page_view="pv1",
          exhibit="X", event_type="open", event_time="2026-11-18T08:00:00Z")
    # Page never closed (tab abandoned). A complete visit exists alongside it.
    _post(client, event_id="o2", visitor=v, page_view="pv2",
          exhibit="Y", event_type="open", event_time="2026-11-18T09:00:00Z")
    _post(client, event_id="c2", visitor=v, page_view="pv2",
          exhibit="Y", event_type="close", event_time="2026-11-18T09:10:00Z")

    path = client.get(f"/visitors/{v}/path").json()
    assert path["total_visits"] == 2  # the unclosed page is still a visit
    x, y = path["visits"]
    assert x["exhibit_id"] == "X"
    assert x["closed_at"] is None and x["dwell_known"] is False
    assert x["dwell_seconds"] is None
    assert y["dwell_seconds"] == 600.0 and y["dwell_known"] is True

    # When the close finally arrives later, dwell becomes known.
    _post(client, event_id="c1", visitor=v, page_view="pv1",
          exhibit="X", event_type="close", event_time="2026-11-18T10:00:00Z")
    path = client.get(f"/visitors/{v}/path").json()
    x = path["visits"][0]
    assert x["closed_at"] == "2026-11-18T10:00:00+00:00"
    assert x["dwell_known"] is True and x["dwell_seconds"] == 7200.0


def test_orphan_close_without_open_is_not_a_visit(client):
    v = "visitor-orphan"
    _post(client, event_id="c1", visitor=v, page_view="pv9",
          exhibit="Q", event_type="close", event_time="2026-11-18T10:00:00Z")
    path = client.get(f"/visitors/{v}/path").json()
    assert path["total_visits"] == 0 and path["visits"] == []


# ---------------------------------------------------------------------------
# 4. Idempotent retries: same event_id never double-counted.
# ---------------------------------------------------------------------------
def test_duplicate_event_ids_are_idempotent(client):
    v = "visitor-D"
    payload = _event("dup1", v, "pv1", "X", "open", "2026-11-18T08:00:00Z")
    first = client.post("/events", json=payload)
    assert first.status_code == 201 and first.json()["status"] == "stored"

    # Exact replay. The HTTP status stays 201 (route-level); the response
    # body's status field distinguishes first store from idempotent replay.
    second = client.post("/events", json=payload)
    assert second.status_code == 201 and second.json()["status"] == "duplicate"

    # Replay with a *different* body under the same event_id: the event_id is
    # the identity, so it is still a duplicate and the original row wins.
    tampered = dict(payload)
    tampered["event_time"] = "2026-11-18T11:11:11Z"
    tampered["exhibit_id"] = "HACK"
    third = client.post("/events", json=tampered)
    assert third.json()["status"] == "duplicate"

    path = client.get(f"/visitors/{v}/path").json()
    assert path["total_visits"] == 1
    visit = path["visits"][0]
    assert visit["exhibit_id"] == "X"
    assert visit["opened_at"] == "2026-11-18T08:00:00+00:00"


def test_batch_endpoint_dedupes_and_counters(client):
    v = "visitor-batch"
    events = [
        _event("b1", v, "pv1", "X", "open", "2026-11-18T08:00:00Z"),
        _event("b2", v, "pv1", "X", "close", "2026-11-18T08:01:00Z"),
        _event("b1", v, "pv1", "X", "open", "2026-11-18T08:00:00Z"),  # dup
    ]
    resp = client.post("/events/batch", json={"events": events})
    assert resp.status_code == 201
    body = resp.json()
    assert (body["accepted"], body["duplicates"],
            body["rejected_withdrawn"]) == (2, 1, 0)


def test_batch_is_atomic_when_one_event_invalid(client):
    v = "visitor-bad-batch"
    good = _event("g1", v, "pv1", "X", "open", "2026-11-18T08:00:00Z")
    bad = _event("g2", v, "pv1", "X", "close", "not-a-timestamp")
    resp = client.post("/events/batch", json={"events": [good, bad]})
    assert resp.status_code == 400
    # The good event must have been rolled back too: no half batch.
    path = client.get(f"/visitors/{v}/path").json()
    assert path["total_visits"] == 0


# ---------------------------------------------------------------------------
# 5. Withdrawal: path disappears, later uploads and replays cannot restore it.
# ---------------------------------------------------------------------------
def test_withdrawal_erases_path_and_blocks_future_events(client):
    v = "visitor-E"
    _post(client, event_id="w1", visitor=v, page_view="pv1",
          exhibit="X", event_type="open", event_time="2026-11-18T08:00:00Z")
    _post(client, event_id="w2", visitor=v, page_view="pv1",
          exhibit="X", event_type="close", event_time="2026-11-18T08:05:00Z")
    assert client.get(f"/visitors/{v}/path").json()["total_visits"] == 1

    resp = client.post(f"/visitors/{v}/withdraw", json={"visitor_id": v})
    assert resp.status_code == 200
    body = resp.json()
    assert body["consent_withdrawn"] is True
    assert body["already_withdrawn"] is False
    assert body["events_deleted"] == 2

    # Existing path is gone.
    path = client.get(f"/visitors/{v}/path").json()
    assert path["consent_withdrawn"] is True
    assert path["visits"] == [] and path["total_visits"] == 0

    # Repeated withdrawal is idempotent.
    again = client.post(f"/visitors/{v}/withdraw", json={"visitor_id": v}).json()
    assert again["already_withdrawn"] is True and again["events_deleted"] == 0

    # New events are rejected and never stored.
    new_event = _event("w3", v, "pv2", "Y", "open", "2026-11-18T09:00:00Z")
    r = client.post("/events", json=new_event)
    assert r.status_code == 201 and r.json()["status"] == "rejected_withdrawn"

    # Replaying an old (pre-withdrawal) event cannot resurrect the path either.
    replay = _event("w1", v, "pv1", "X", "open", "2026-11-18T08:00:00Z")
    r = client.post("/events", json=replay)
    assert r.json()["status"] == "rejected_withdrawn"

    path = client.get(f"/visitors/{v}/path").json()
    assert path["total_visits"] == 0

    # Batch uploads likewise reject this visitor while accepting others.
    mixed = client.post("/events/batch", json={"events": [
        new_event,
        _event("ok1", "visitor-other", "pv1", "Z", "open",
               "2026-11-18T09:00:00Z"),
    ]}).json()
    assert mixed["rejected_withdrawn"] == 1 and mixed["accepted"] == 1


# ---------------------------------------------------------------------------
# 6. Concurrency: withdrawal interleaved with many uploads and queries.
#    Every observed query result must be internally consistent; after
#    withdrawal the path must stay empty no matter how uploads interleave.
# ---------------------------------------------------------------------------
def test_concurrent_withdrawal_never_shows_half_state(client):
    v = "visitor-concurrent"
    # TestClient uses an in-process ASGI transport (no TCP port), so each
    # thread gets its own client backed by the same SQLite file. That is the
    # same shape as multiple uvicorn workers sharing the volume.
    errors: list[str] = []
    barrier = threading.Barrier(4)

    def uploader(start: int) -> None:
        barrier.wait()
        try:
            with TestClient(app) as local:
                for i in range(start, start + 25):
                    pv = f"pv-{i}"
                    local.post("/events", json=_event(
                        f"e-{i}-o", v, pv, f"EX{i}", "open",
                        f"2026-11-18T08:{i % 60:02d}:00Z"))
                    local.post("/events", json=_event(
                        f"e-{i}-c", v, pv, f"EX{i}", "close",
                        f"2026-11-18T08:{i % 60:02d}:30Z"))
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(f"uploader: {exc!r}")

    def reader() -> None:
        barrier.wait()
        try:
            with TestClient(app) as local:
                for _ in range(100):
                    body = local.get(f"/visitors/{v}/path").json()
                    # Invariant 1: never withdrawn -> every paired visit is
                    # either fully open+close or explicitly dwell-unknown.
                    # Invariant 2: withdrawn -> always empty.
                    if body["consent_withdrawn"]:
                        if body["visits"] or body["total_visits"] != 0:
                            errors.append("path visible after withdrawal")
                    else:
                        for visit in body["visits"]:
                            if visit["closed_at"] is None:
                                assert visit["dwell_known"] is False
                            else:
                                assert visit["dwell_known"] is True
                        sequences = [x["sequence"] for x in body["visits"]]
                        if sorted(sequences) != list(range(1, len(sequences) + 1)):
                            errors.append("non-contiguous sequence numbers")
        except Exception as exc:  # pragma: no cover
            errors.append(f"reader: {exc!r}")

    def withdrawer() -> None:
        barrier.wait()
        try:
            with TestClient(app) as local:
                local.post(f"/visitors/{v}/withdraw", json={"visitor_id": v})
        except Exception as exc:  # pragma: no cover
            errors.append(f"withdrawer: {exc!r}")

    threads = [
        threading.Thread(target=uploader, args=(0,)),
        threading.Thread(target=uploader, args=(100,)),
        threading.Thread(target=reader),
        threading.Thread(target=withdrawer),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors

    # Post-withdrawal steady state: all further attempts rejected, path empty.
    r = client.post("/events", json=_event(
        "late1", v, "pv-late", "LATE", "open", "2026-11-18T12:00:00Z"))
    assert r.json()["status"] == "rejected_withdrawn"
    final = client.get(f"/visitors/{v}/path").json()
    assert final == {
        "visitor_id": v,
        "consent_withdrawn": True,
        "visits": [],
        "total_visits": 0,
    }


# ---------------------------------------------------------------------------
# 7. Validation and storage-level guarantees.
# ---------------------------------------------------------------------------
def test_naive_timestamp_is_rejected(client):
    resp = client.post("/events", json=_event(
        "n1", "visitor-naive", "pv1", "X", "open", "2026-11-18T08:00:00"))
    assert resp.status_code == 400
    assert "UTC" in resp.json()["detail"]


def test_offset_timestamps_normalize_to_utc(client):
    v = "visitor-tz"
    # 09:00+01:00 == 08:00Z; fractional seconds are accepted.
    client.post("/events", json=_event(
        "tz1", v, "pv1", "X", "open", "2026-11-18T09:00:00+01:00"))
    client.post("/events", json=_event(
        "tz2", v, "pv1", "X", "close", "2026-11-18T08:00:30.500Z"))
    visit = client.get(f"/visitors/{v}/path").json()["visits"][0]
    assert visit["opened_at"] == "2026-11-18T08:00:00+00:00"
    assert visit["dwell_seconds"] == 30.5


def test_unknown_visitor_returns_empty_path(client):
    body = client.get("/visitors/nobody/path").json()
    assert body["consent_withdrawn"] is False
    assert body["total_visits"] == 0 and body["visits"] == []


def test_negative_dwell_is_flagged_unknown_but_close_kept(client):
    v = "visitor-anomaly"
    client.post("/events", json=_event(
        "a1", v, "pv1", "X", "open", "2026-11-18T10:00:00Z"))
    client.post("/events", json=_event(
        "a2", v, "pv1", "X", "close", "2026-11-18T09:59:00Z"))
    visit = client.get(f"/visitors/{v}/path").json()["visits"][0]
    assert visit["closed_at"] == "2026-11-18T09:59:00+00:00"
    assert visit["dwell_known"] is False and visit["dwell_seconds"] is None


def test_wal_mode_and_db_level_constraints(client):
    # Direct storage check: WAL journaling is enabled and event_id is the
    # primary key (the basis of idempotent replay).
    conn = connect(config.DB_PATH)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        pk = conn.execute(
            "SELECT name FROM pragma_table_info('events') WHERE pk > 0"
        ).fetchall()
        assert [row[0] for row in pk] == ["event_id"]
    finally:
        conn.close()


def test_withdrawal_marker_survives_across_connections(client):
    # Simulate a replay arriving on a different connection/worker: the
    # withdrawal row is durable committed state, not in-memory.
    v = "visitor-persist"
    client.post("/events", json=_event(
        "p1", v, "pv1", "X", "open", "2026-11-18T08:00:00Z"))
    client.post(f"/visitors/{v}/withdraw", json={"visitor_id": v})

    conn = connect(config.DB_PATH)
    try:
        with write_transaction(conn):
            result = crud.ingest_event(conn, EventIn(**_event(
                "p1", v, "pv1", "X", "open", "2026-11-18T08:00:00Z")))
        assert result.status == "rejected_withdrawn"
        remaining = conn.execute(
            "SELECT COUNT(*) FROM events WHERE visitor_id = ?", (v,)
        ).fetchone()[0]
        assert remaining == 0
    finally:
        conn.close()
