"""Legacy catalog-card retrieval (旧卡检索): code-sequence recording,
per-batch mapping-table swaps, and name-index lookup.

Design notes
------------
- Original codes are stored verbatim (``legacy_cards`` plus one normalized
  row per code position in ``legacy_card_codes``). The transcription is
  *derived* state kept in ``legacy_card_index``.
- A table swap and the index rebuild run in ONE write transaction, so a
  reader (one snapshot per request) only ever sees old-table+old-index or
  new-table+new-index. A failed swap rolls everything back: the old table
  stays active and old lookup results are unchanged.
- Rebuilds are incremental: ``legacy_card_codes`` lets a swap select exactly
  the cards whose original codes touch changed entries — including cards
  that were previously incomplete and therefore absent from the name index.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from .crud import utc_now_iso
from .db import get_connection, write_transaction
from .legacy_models import (
    CardDetail,
    CardIn,
    CardRecordResult,
    CodeSegment,
    LegacyCardRef,
    MappingTableIn,
    MappingTableView,
    NameLookupResult,
    TableSwapResult,
)
from .models import ErrorResponse


# ---------------------------------------------------------------------------
# Domain logic
# ---------------------------------------------------------------------------
def _transcribe(
    codes: list[str], entries: dict[str, str]
) -> tuple[list[str | None], list[int], bool, str | None]:
    """Map each code through the table. Returns (texts, unmapped, complete, name)."""
    texts = [entries.get(code) for code in codes]
    unmapped = [i for i, text in enumerate(texts) if text is None]
    complete = not unmapped
    full_name = "".join(text for text in texts if text is not None) if complete else None
    return texts, unmapped, complete, full_name


def _active_table(conn: sqlite3.Connection, batch_id: str) -> tuple[int, dict[str, str]] | None:
    row = conn.execute(
        "SELECT version, entries FROM legacy_mapping_tables "
        "WHERE batch_id = ? AND active = 1",
        (batch_id,),
    ).fetchone()
    if row is None:
        return None
    return row["version"], json.loads(row["entries"])


def _write_index(
    conn: sqlite3.Connection,
    batch_id: str,
    card_no: str,
    texts: list[str | None],
    unmapped: list[int],
    complete: bool,
    full_name: str | None,
    table_version: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO legacy_card_index
            (batch_id, card_no, texts, full_name, complete, unmapped, table_version)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(batch_id, card_no) DO UPDATE SET
            texts = excluded.texts,
            full_name = excluded.full_name,
            complete = excluded.complete,
            unmapped = excluded.unmapped,
            table_version = excluded.table_version
        """,
        (
            batch_id,
            card_no,
            json.dumps(texts, ensure_ascii=False),
            full_name,
            int(complete),
            json.dumps(unmapped),
            table_version,
        ),
    )


def record_card(
    conn: sqlite3.Connection, batch_id: str, card_no: str, codes: list[str]
) -> CardRecordResult:
    """Insert or replace one card. Must be called inside a write transaction.

    Re-recording the same (batch, card) replaces the code sequence and
    re-transcribes against the batch's current table.
    """
    existing = conn.execute(
        "SELECT 1 FROM legacy_cards WHERE batch_id = ? AND card_no = ?",
        (batch_id, card_no),
    ).fetchone()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO legacy_cards (batch_id, card_no, codes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(batch_id, card_no) DO UPDATE SET
            codes = excluded.codes,
            updated_at = excluded.updated_at
        """,
        (batch_id, card_no, json.dumps(list(codes)), now, now),
    )
    conn.execute(
        "DELETE FROM legacy_card_codes WHERE batch_id = ? AND card_no = ?",
        (batch_id, card_no),
    )
    conn.executemany(
        "INSERT INTO legacy_card_codes (batch_id, card_no, position, code) "
        "VALUES (?, ?, ?, ?)",
        [(batch_id, card_no, position, code) for position, code in enumerate(codes)],
    )

    active = _active_table(conn, batch_id)
    version, entries = active if active is not None else (None, {})
    texts, unmapped, complete, full_name = _transcribe(codes, entries)
    _write_index(conn, batch_id, card_no, texts, unmapped, complete, full_name, version)
    return CardRecordResult(
        batch_id=batch_id,
        card_no=card_no,
        status="updated" if existing is not None else "stored",
        table_version=version,
        complete=complete,
        full_name=full_name,
        unmapped_positions=unmapped,
    )


def swap_mapping_table(
    conn: sqlite3.Connection, batch_id: str, entries: dict[str, str]
) -> TableSwapResult:
    """Replace the batch's whole mapping table. Must be called inside a write
    transaction: the new active table and every rebuilt index row commit or
    roll back together, so a failed swap leaves the old table fully in effect.

    Only cards whose original codes touch changed entries are read and
    rebuilt; ``legacy_card_codes`` covers all recorded cards, so cards that
    were previously unfindable (incomplete, absent from the name index) are
    rebuilt too.
    """
    active = _active_table(conn, batch_id)
    old_version, old_entries = active if active is not None else (None, {})
    if old_entries == entries:
        return TableSwapResult(
            batch_id=batch_id,
            status="unchanged",
            version=old_version,
            previous_version=old_version,
            entries_count=len(entries),
            changed_codes=[],
            affected_cards=[],
            became_complete=[],
            became_incomplete=[],
        )

    changed = sorted(
        code
        for code in set(old_entries) | set(entries)
        if old_entries.get(code) != entries.get(code)
    )
    placeholders = ", ".join("?" for _ in changed)
    affected = [
        row["card_no"]
        for row in conn.execute(
            f"SELECT DISTINCT card_no FROM legacy_card_codes "
            f"WHERE batch_id = ? AND code IN ({placeholders}) ORDER BY card_no",
            (batch_id, *changed),
        )
    ]

    new_version = (old_version or 0) + 1
    conn.execute(
        "UPDATE legacy_mapping_tables SET active = 0 WHERE batch_id = ? AND active = 1",
        (batch_id,),
    )
    conn.execute(
        "INSERT INTO legacy_mapping_tables (batch_id, version, entries, created_at, active) "
        "VALUES (?, ?, ?, ?, 1)",
        (batch_id, new_version, json.dumps(entries, ensure_ascii=False), utc_now_iso()),
    )

    became_complete: list[str] = []
    became_incomplete: list[str] = []
    for card_no in affected:
        row = conn.execute(
            "SELECT codes FROM legacy_cards WHERE batch_id = ? AND card_no = ?",
            (batch_id, card_no),
        ).fetchone()
        codes = json.loads(row["codes"])
        was = conn.execute(
            "SELECT complete FROM legacy_card_index WHERE batch_id = ? AND card_no = ?",
            (batch_id, card_no),
        ).fetchone()
        was_complete = bool(was["complete"]) if was is not None else False

        texts, unmapped, complete, full_name = _transcribe(codes, entries)
        _write_index(
            conn, batch_id, card_no, texts, unmapped, complete, full_name, new_version
        )
        if complete and not was_complete:
            became_complete.append(card_no)
        elif was_complete and not complete:
            became_incomplete.append(card_no)

    return TableSwapResult(
        batch_id=batch_id,
        status="swapped",
        version=new_version,
        previous_version=old_version,
        entries_count=len(entries),
        changed_codes=changed,
        affected_cards=affected,
        became_complete=became_complete,
        became_incomplete=became_incomplete,
    )


@contextmanager
def _snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Read-only snapshot: one consistent view of table + index per request."""
    conn.execute("BEGIN DEFERRED")
    try:
        yield
    finally:
        conn.rollback()


def get_card_detail(
    conn: sqlite3.Connection, batch_id: str, card_no: str
) -> CardDetail | None:
    with _snapshot(conn):
        row = conn.execute(
            """
            SELECT c.codes, i.texts, i.unmapped, i.complete, i.full_name, i.table_version
            FROM legacy_cards c
            LEFT JOIN legacy_card_index i
              ON i.batch_id = c.batch_id AND i.card_no = c.card_no
            WHERE c.batch_id = ? AND c.card_no = ?
            """,
            (batch_id, card_no),
        ).fetchone()
    if row is None:
        return None
    codes = json.loads(row["codes"])
    if row["texts"] is not None:
        texts = json.loads(row["texts"])
        unmapped = json.loads(row["unmapped"])
    else:  # pragma: no cover - index row is always written with the card
        texts = [None] * len(codes)
        unmapped = list(range(len(codes)))
    return CardDetail(
        batch_id=batch_id,
        card_no=card_no,
        codes=codes,
        segments=[
            CodeSegment(position=i, code=code, text=texts[i])
            for i, code in enumerate(codes)
        ],
        unmapped_positions=unmapped,
        complete=bool(row["complete"]),
        full_name=row["full_name"],
        table_version=row["table_version"],
    )


def get_active_table_view(conn: sqlite3.Connection, batch_id: str) -> MappingTableView | None:
    with _snapshot(conn):
        row = conn.execute(
            "SELECT version, entries, created_at FROM legacy_mapping_tables "
            "WHERE batch_id = ? AND active = 1",
            (batch_id,),
        ).fetchone()
    if row is None:
        return None
    entries = json.loads(row["entries"])
    return MappingTableView(
        batch_id=batch_id,
        version=row["version"],
        entries=entries,
        entries_count=len(entries),
        created_at=row["created_at"],
    )


def lookup_by_name(
    conn: sqlite3.Connection, name: str, batch_id: str | None = None
) -> NameLookupResult:
    """Exact-match lookup over the derived index; only complete cards qualify."""
    sql = (
        "SELECT batch_id, card_no, full_name FROM legacy_card_index "
        "WHERE complete = 1 AND full_name = ?"
    )
    params: list[str] = [name]
    if batch_id is not None:
        sql += " AND batch_id = ?"
        params.append(batch_id)
    sql += " ORDER BY batch_id, card_no"
    with _snapshot(conn):
        rows = conn.execute(sql, params).fetchall()
    return NameLookupResult(
        name=name,
        total=len(rows),
        cards=[
            LegacyCardRef(
                batch_id=row["batch_id"], card_no=row["card_no"], full_name=row["full_name"]
            )
            for row in rows
        ],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/legacy", tags=["旧卡检索"])


@router.post(
    "/batches/{batch_id}/cards",
    response_model=CardRecordResult,
    status_code=status.HTTP_201_CREATED,
    summary="录入登记卡（品名的四位十六进制字码序列）",
)
def post_card(
    batch_id: Annotated[str, Path(min_length=1, description="批次号")],
    payload: CardIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> CardRecordResult:
    """录入即按批次现行对照表转写；缺映射的位置记入 ``unmapped_positions``。

    同批次同卡号重复录入视为更新：原码序列被替换并重新转写，返回
    ``status=updated``。
    """
    with write_transaction(conn):
        return record_card(conn, batch_id, payload.card_no, payload.codes)


@router.get(
    "/batches/{batch_id}/cards/{card_no}",
    response_model=CardDetail,
    responses={404: {"model": ErrorResponse}},
    summary="按批次和卡号查询原码与未映射位置",
)
def get_card(
    batch_id: Annotated[str, Path(min_length=1)],
    card_no: Annotated[str, Path(min_length=1)],
    conn: sqlite3.Connection = Depends(get_connection),
) -> CardDetail:
    """返回原始字码序列、逐位转写结果与未映射位置（0 起）。"""
    detail = get_card_detail(conn, batch_id, card_no)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="登记卡不存在"
        )
    return detail


@router.put(
    "/batches/{batch_id}/mapping-table",
    response_model=TableSwapResult,
    summary="整表更换本批对照表（与索引重建同一事务，失败整体回滚）",
)
def put_mapping_table(
    batch_id: Annotated[str, Path(min_length=1)],
    payload: MappingTableIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> TableSwapResult:
    """只重建原码涉及变动对照项的卡（含此前因缺映射未进索引的卡）。

    新表与索引重建在同一事务提交：换表失败时旧表继续生效，查询结果不变。
    新表与现行表完全一致时不产生新版本（``status=unchanged``）。
    """
    with write_transaction(conn):
        return swap_mapping_table(conn, batch_id, payload.entries)


@router.get(
    "/batches/{batch_id}/mapping-table",
    response_model=MappingTableView,
    responses={404: {"model": ErrorResponse}},
    summary="查询批次现行对照表",
)
def get_mapping_table(
    batch_id: Annotated[str, Path(min_length=1)],
    conn: sqlite3.Connection = Depends(get_connection),
) -> MappingTableView:
    view = get_active_table_view(conn, batch_id)
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="该批次尚无对照表"
        )
    return view


@router.get(
    "/cards/by-name",
    response_model=NameLookupResult,
    summary="按完整品名查同名卡（仅已完整转写的卡）",
)
def get_cards_by_name(
    name: Annotated[str, Query(min_length=1, description="完整品名，精确匹配")],
    batch_id: Annotated[str | None, Query(description="可选：限定批次")] = None,
    conn: sqlite3.Connection = Depends(get_connection),
) -> NameLookupResult:
    """从派生索引精确匹配完整品名；未完整转写的卡不出现在结果中。"""
    return lookup_by_name(conn, name, batch_id)
