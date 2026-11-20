"""End-to-end tests for the legacy card retrieval API (旧卡检索).

Covers the required self-test matrix:
  1. record + transcribe + exact-name index lookup
  2. unmapped positions reported per card; incomplete cards excluded from index
  3. missing-code backfill: a swap adding the missing entry completes the card
  4. same-name cards separate after a table swap corrects a duplicate mapping
  5. incremental rebuild: only cards touching changed codes are rebuilt
  6. atomicity: invalid swap (422) and mid-swap failure both keep old table+index
  7. per-batch independence of mapping tables
  8. card re-recording replaces codes and rebuild scope
  9. hex validation / normalization, 404s, exact-match semantics
 10. concurrent swaps and reads never observe a half-rebuilt index
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import config, legacy
from app.db import connect, write_transaction
from app.main import app

# Demo-shaped fixture data (mirrors app.demo_data).
TABLE_V1 = {
    "0B01": "明", "0B02": "清",
    "0A01": "青", "0A02": "花", "0A03": "瓷",
    "0A04": "瓶", "0A05": "碗", "0A08": "花",
}
TABLE_V2 = {
    "0B01": "明", "0B02": "清",
    "0A01": "青", "0A02": "花", "0A03": "瓷",
    "0A04": "瓶", "0A05": "盌", "0A06": "罐",
    "0A07": "砚", "0A08": "卉",
}


def _put_table(client, batch, entries):
    return client.put(f"/legacy/batches/{batch}/mapping-table", json={"entries": entries})


def _post_card(client, batch, card_no, codes):
    return client.post(
        f"/legacy/batches/{batch}/cards", json={"card_no": card_no, "codes": codes}
    )


def _get_card(client, batch, card_no):
    return client.get(f"/legacy/batches/{batch}/cards/{card_no}")


def _lookup(client, name, batch_id=None):
    params = {"name": name}
    if batch_id is not None:
        params["batch_id"] = batch_id
    return client.get("/legacy/cards/by-name", params=params)


def _seed_demo(client, batch="M1987"):
    """Table v1 + the seven demo cards; returns nothing, asserts success."""
    assert _put_table(client, batch, TABLE_V1).status_code == 200
    cards = {
        "0001": ["0B01", "0A01", "0A02", "0A03", "0A04"],
        "0002": ["0B01", "0A01", "0A02", "0A03", "0A05"],
        "0003": ["0B02", "0A01", "0A02", "0A03", "0A04"],
        "0004": ["0B02", "0A01", "0A02", "0A03", "0A06"],
        "0005": ["0B01", "0A07", "0A04"],
        "0006": ["0B01", "0A01", "0A02"],
        "0007": ["0B01", "0A01", "0A08"],
    }
    for card_no, codes in cards.items():
        resp = _post_card(client, batch, card_no, codes)
        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# 1. Recording, transcription, name lookup.
# ---------------------------------------------------------------------------
def test_record_transcribe_and_lookup_by_name(client):
    _seed_demo(client)

    detail = _get_card(client, "M1987", "0001").json()
    assert detail["codes"] == ["0B01", "0A01", "0A02", "0A03", "0A04"]
    assert detail["complete"] is True
    assert detail["full_name"] == "明青花瓷瓶"
    assert detail["unmapped_positions"] == []
    assert detail["table_version"] == 1
    assert [s["text"] for s in detail["segments"]] == ["明", "青", "花", "瓷", "瓶"]

    result = _lookup(client, "明青花瓷瓶").json()
    assert result["total"] == 1
    assert result["cards"][0]["card_no"] == "0001"
    assert result["cards"][0]["batch_id"] == "M1987"


def test_card_recorded_before_any_table_is_fully_unmapped(client):
    resp = _post_card(client, "NEW", "0001", ["0A01", "0A02"])
    body = resp.json()
    assert body["status"] == "stored"
    assert body["table_version"] is None
    assert body["complete"] is False
    assert body["unmapped_positions"] == [0, 1]

    # Not findable by name while incomplete; detail still queryable.
    assert _lookup(client, "青花").json()["total"] == 0
    detail = _get_card(client, "NEW", "0001").json()
    assert detail["unmapped_positions"] == [0, 1]
    assert detail["full_name"] is None

    # Creating the batch's first table backfills it (all codes are "changed").
    swap = _put_table(client, "NEW", {"0A01": "青", "0A02": "花"}).json()
    assert swap["previous_version"] is None and swap["version"] == 1
    assert swap["affected_cards"] == ["0001"]
    assert swap["became_complete"] == ["0001"]
    assert _lookup(client, "青花").json()["cards"][0]["card_no"] == "0001"


# ---------------------------------------------------------------------------
# 2. Unmapped positions and index exclusion.
# ---------------------------------------------------------------------------
def test_unmapped_positions_reported_and_excluded_from_index(client):
    _seed_demo(client)

    detail = _get_card(client, "M1987", "0004").json()
    assert detail["complete"] is False
    assert detail["unmapped_positions"] == [4]
    assert detail["full_name"] is None
    assert detail["segments"][4] == {"position": 4, "code": "0A06", "text": None}

    # Incomplete cards never appear in the name index, even for their
    # partially-known prefix.
    assert _lookup(client, "清青花瓷").json()["total"] == 0


# ---------------------------------------------------------------------------
# 3. 缺码补齐: a swap that adds the missing mapping completes the card.
# ---------------------------------------------------------------------------
def test_missing_code_backfilled_by_table_swap(client):
    _seed_demo(client)
    assert _lookup(client, "清青花瓷罐").json()["total"] == 0

    swap = _put_table(client, "M1987", TABLE_V2).json()
    assert swap["status"] == "swapped"
    assert swap["version"] == 2 and swap["previous_version"] == 1
    # 0004 (0A06) and 0005 (0A07) were previously unfindable; the swap must
    # still reach them via the code index, not the name index.
    assert swap["became_complete"] == ["0004", "0005"]
    assert swap["became_incomplete"] == []

    detail = _get_card(client, "M1987", "0004").json()
    assert detail["complete"] is True
    assert detail["full_name"] == "清青花瓷罐"
    assert detail["unmapped_positions"] == []
    assert detail["table_version"] == 2

    assert _lookup(client, "清青花瓷罐").json()["cards"][0]["card_no"] == "0004"
    assert _lookup(client, "明砚瓶").json()["cards"][0]["card_no"] == "0005"


# ---------------------------------------------------------------------------
# 4. 同名卡换表后分开: 0A02 and 0A08 both meant 花 in v1; v2 corrects 0A08.
# ---------------------------------------------------------------------------
def test_same_name_cards_separate_after_swap(client):
    _seed_demo(client)

    before = _lookup(client, "明青花").json()
    assert before["total"] == 2
    assert [c["card_no"] for c in before["cards"]] == ["0006", "0007"]

    _put_table(client, "M1987", TABLE_V2)

    after = _lookup(client, "明青花").json()
    assert [c["card_no"] for c in after["cards"]] == ["0006"]
    corrected = _lookup(client, "明青卉").json()
    assert [c["card_no"] for c in corrected["cards"]] == ["0007"]
    # The old combined name no longer matches either card exactly.
    assert _get_card(client, "M1987", "0007").json()["full_name"] == "明青卉"


# ---------------------------------------------------------------------------
# 5. Incremental rebuild: only cards whose codes touch changed entries.
# ---------------------------------------------------------------------------
def test_swap_rebuilds_only_affected_cards(client):
    _seed_demo(client)

    swap = _put_table(client, "M1987", TABLE_V2).json()
    assert swap["changed_codes"] == ["0A05", "0A06", "0A07", "0A08"]
    # 0001/0003/0006 contain none of the changed codes and are not rebuilt.
    assert swap["affected_cards"] == ["0002", "0004", "0005", "0007"]

    # A swap changing nothing is a no-op: no new version, no rebuilds.
    noop = _put_table(client, "M1987", TABLE_V2).json()
    assert noop["status"] == "unchanged"
    assert noop["version"] == 2 and noop["affected_cards"] == []
    assert client.get("/legacy/batches/M1987/mapping-table").json()["version"] == 2

    # Untouched cards keep their transcription under the new table version.
    assert _get_card(client, "M1987", "0001").json()["full_name"] == "明青花瓷瓶"


def test_swap_can_break_cards_and_index_follows(client):
    _seed_demo(client)
    # Remove 0A04's mapping: cards using it become incomplete.
    broken = dict(TABLE_V1)
    del broken["0A04"]
    swap = _put_table(client, "M1987", broken).json()
    assert swap["changed_codes"] == ["0A04"]
    assert swap["affected_cards"] == ["0001", "0003", "0005"]
    assert sorted(swap["became_incomplete"]) == ["0001", "0003"]

    assert _lookup(client, "明青花瓷瓶").json()["total"] == 0
    detail = _get_card(client, "M1987", "0001").json()
    assert detail["complete"] is False
    assert detail["unmapped_positions"] == [4]


# ---------------------------------------------------------------------------
# 6. Atomicity: failed swaps keep the old table and old index results.
# ---------------------------------------------------------------------------
def test_invalid_swap_rejected_and_old_table_intact(client):
    _seed_demo(client)

    bad = _put_table(client, "M1987", {"0A01": "青", "NOT-HEX": "x"})
    assert bad.status_code == 422
    empty = client.put("/legacy/batches/M1987/mapping-table", json={"entries": {}})
    assert empty.status_code == 422

    table = client.get("/legacy/batches/M1987/mapping-table").json()
    assert table["version"] == 1 and table["entries"] == TABLE_V1
    assert _lookup(client, "明青花").json()["total"] == 2


def test_mid_swap_failure_rolls_back_table_and_index(client, monkeypatch):
    _seed_demo(client)

    # Force a failure after the new table row has been written but before the
    # index rebuild finishes: everything must roll back together.
    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash during rebuild")

    original = legacy._write_index
    monkeypatch.setattr(legacy, "_write_index", boom)
    conn = connect(config.DB_PATH)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            with write_transaction(conn):
                legacy.swap_mapping_table(conn, "M1987", TABLE_V2)
    finally:
        conn.close()
        # Restore only our patch; monkeypatch.undo() would also revert the
        # client fixture's DB_PATH patch.
        legacy._write_index = original

    # Old table still active, old index results unchanged.
    table = client.get("/legacy/batches/M1987/mapping-table").json()
    assert table["version"] == 1 and table["entries"] == TABLE_V1
    assert _lookup(client, "明青花").json()["total"] == 2
    assert _get_card(client, "M1987", "0004").json()["complete"] is False

    # A later valid swap succeeds and reuses the rolled-back version number,
    # proving the failed insert left no trace.
    retry = _put_table(client, "M1987", TABLE_V2).json()
    assert retry["status"] == "swapped" and retry["version"] == 2
    assert _lookup(client, "清青花瓷罐").json()["total"] == 1


# ---------------------------------------------------------------------------
# 7. Per-batch independence.
# ---------------------------------------------------------------------------
def test_batches_have_independent_tables(client):
    _seed_demo(client, batch="A")
    # Same code, different text in batch B.
    _put_table(client, "B", {"0A01": "靑"})
    _post_card(client, "B", "0001", ["0A01"])

    _put_table(client, "A", TABLE_V2)

    # Batch B's card and table are untouched by A's swap.
    assert _get_card(client, "B", "0001").json()["full_name"] == "靑"
    assert client.get("/legacy/batches/B/mapping-table").json()["version"] == 1
    # Name lookup can be scoped per batch.
    scoped = _lookup(client, "明青花", batch_id="B").json()
    assert scoped["total"] == 0


# ---------------------------------------------------------------------------
# 8. Re-recording replaces the code sequence.
# ---------------------------------------------------------------------------
def test_card_upsert_replaces_codes_and_rebuild_scope(client):
    _seed_demo(client)

    again = _post_card(client, "M1987", "0002", ["0B01", "0A01"]).json()
    assert again["status"] == "updated"
    assert again["full_name"] == "明青"
    assert _lookup(client, "明青花瓷碗").json()["total"] == 0
    assert _lookup(client, "明青").json()["total"] == 1

    # The replaced codes no longer tie the card to them: a swap touching only
    # 0A05 (its old last code) must not rebuild it.
    v3 = dict(TABLE_V1)
    v3["0A05"] = "盌"
    swap = _put_table(client, "M1987", v3).json()
    assert swap["affected_cards"] == []


# ---------------------------------------------------------------------------
# 9. Validation, normalization, 404s, exact-match semantics.
# ---------------------------------------------------------------------------
def test_code_validation_and_normalization(client):
    assert _post_card(client, "V", "1", ["123"]).status_code == 422
    assert _post_card(client, "V", "1", ["12345"]).status_code == 422
    assert _post_card(client, "V", "1", ["GGGG"]).status_code == 422
    assert _post_card(client, "V", "1", []).status_code == 422
    assert _post_card(client, "V", "1", ["0A01"],).status_code == 201

    # Lowercase is accepted and normalized to uppercase everywhere.
    _put_table(client, "V", {"0a01": "青"})
    resp = _post_card(client, "V", "2", ["0a01"])
    assert resp.json()["full_name"] == "青"
    detail = _get_card(client, "V", "2").json()
    assert detail["codes"] == ["0A01"]
    assert detail["segments"][0]["text"] == "青"

    # Case-variant duplicate keys in one table are rejected as ambiguous.
    dup = _put_table(client, "V", {"0A01": "青", "0a01": "蓝"})
    assert dup.status_code == 422
    # Empty mapping text is rejected.
    blank = _put_table(client, "V", {"0A01": ""})
    assert blank.status_code == 422


def test_unknown_card_and_table_return_404(client):
    assert _get_card(client, "NOPE", "0001").status_code == 404
    assert client.get("/legacy/batches/NOPE/mapping-table").status_code == 404


def test_name_lookup_is_exact_and_only_complete(client):
    _seed_demo(client)
    # Prefix / substring queries do not match.
    assert _lookup(client, "明青").json()["total"] == 0
    assert _lookup(client, "明青花瓷").json()["total"] == 0
    # Empty name rejected.
    assert client.get("/legacy/cards/by-name", params={"name": ""}).status_code == 422
    # Result ordering is deterministic: (batch_id, card_no).
    _seed_demo(client, batch="A000")
    names = _lookup(client, "明青花").json()
    assert [(c["batch_id"], c["card_no"]) for c in names["cards"]] == [
        ("A000", "0006"), ("A000", "0007"), ("M1987", "0006"), ("M1987", "0007"),
    ]


# ---------------------------------------------------------------------------
# 10. Concurrency: swaps and reads never observe a half-rebuilt index.
# ---------------------------------------------------------------------------
def test_concurrent_swaps_and_reads_are_consistent(client):
    batch = "CONC"
    v1 = {"0B01": "明", "0A01": "青", "0A02": "花", "0A08": "花"}
    v2 = {"0B01": "明", "0A01": "青", "0A02": "华", "0A08": "卉"}
    _put_table(client, batch, v1)
    _post_card(client, batch, "0006", ["0B01", "0A01", "0A02"])
    _post_card(client, batch, "0007", ["0B01", "0A01", "0A08"])

    errors: list[str] = []
    barrier = threading.Barrier(3)

    def swapper() -> None:
        barrier.wait()
        try:
            with TestClient(app) as local:
                for i in range(8):
                    local.put(
                        f"/legacy/batches/{batch}/mapping-table",
                        json={"entries": v2 if i % 2 == 0 else v1},
                    )
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(f"swapper: {exc!r}")

    def reader() -> None:
        barrier.wait()
        try:
            with TestClient(app) as local:
                for _ in range(120):
                    # Both same-name cards are rebuilt in the swap's single
                    # transaction: a snapshot shows both or neither, never one.
                    body = local.get(
                        "/legacy/cards/by-name", params={"name": "明青花"}
                    ).json()
                    got = sorted(c["card_no"] for c in body["cards"])
                    if got not in ([], ["0006", "0007"]):
                        errors.append(f"half-rebuilt index: {got}")
                    detail = local.get(f"/legacy/batches/{batch}/cards/0006").json()
                    if detail["full_name"] not in ("明青花", "明青华"):
                        errors.append(f"table/index mismatch: {detail['full_name']}")
                    if detail["complete"] != (detail["unmapped_positions"] == []):
                        errors.append("inconsistent complete/unmapped flags")
        except Exception as exc:  # pragma: no cover
            errors.append(f"reader: {exc!r}")

    threads = [
        threading.Thread(target=swapper),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
