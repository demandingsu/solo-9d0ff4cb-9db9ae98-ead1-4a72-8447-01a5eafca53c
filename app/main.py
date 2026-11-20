"""FastAPI application: event ingestion, visit-path queries, withdrawal."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from . import crud
from .db import get_connection, init_db, write_transaction
from .models import (
    ErrorResponse,
    EventBatchIn,
    EventIn,
    IngestBatchResult,
    IngestResult,
    VisitPath,
    WithdrawRequest,
    WithdrawResult,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Ensure schema exists before serving the first request.
    init_db()
    yield


app = FastAPI(
    title="线上展馆观众参观路径 API",
    description=(
        "从浏览器 open/close 事件还原观众实际参观路径。"
        "支持事件重传去重、乱序补报、缺关闭标记停留未知，以及数据同意撤回。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.BadEventTime)
def _bad_event_time_handler(_request, exc: crud.BadEventTime) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.post(
    "/events",
    response_model=IngestResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="上报单个浏览事件（打开/关闭）",
)
def post_event(
    event: EventIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestResult:
    """同一 ``event_id`` 重传返回 duplicate，不重复计数。

    整个请求在一个 ``BEGIN IMMEDIATE`` 事务内：校验、撤回检查与插入要么
    全部生效，要么全部回滚，不会出现半份写入。
    """
    with write_transaction(conn):
        return crud.ingest_event(conn, event)


@app.post(
    "/events/batch",
    response_model=IngestBatchResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="批量上报浏览事件（同一事务，原子提交）",
)
def post_events(
    batch: EventBatchIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestBatchResult:
    """批量事件作为一个事务提交：任一事件非法则整批回滚。

    已撤回观众的事件记为 ``rejected_withdrawn``，其余事件仍可正常入库，
    二者在同一已提交状态上串行判定。
    """
    with write_transaction(conn):
        results = [crud.ingest_event(conn, event) for event in batch.events]
    accepted = sum(r.status == "stored" for r in results)
    duplicates = sum(r.status == "duplicate" for r in results)
    rejected = sum(r.status == "rejected_withdrawn" for r in results)
    return IngestBatchResult(
        accepted=accepted,
        duplicates=duplicates,
        rejected_withdrawn=rejected,
        results=results,
    )


@app.get(
    "/visitors/{visitor_id}/path",
    response_model=VisitPath,
    summary="查询观众参观路径（按打开时间排序）",
)
def get_path(
    visitor_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> VisitPath:
    """按 ``page_view_id`` 配对开关；缺关闭的展品 ``dwell_known=false``。

    路径在查询时从已提交事件实时推导，因此较早事件的补报会自动改变
    路径顺序与停留时长。撤回后返回空路径（``consent_withdrawn=true``）。
    """
    return crud.get_visitor_path(conn, visitor_id)


@app.post(
    "/visitors/{visitor_id}/withdraw",
    response_model=WithdrawResult,
    summary="撤回数据使用同意（删除已存事件，之后拒绝该观众的任何事件）",
)
def post_withdraw(
    visitor_id: str,
    payload: WithdrawRequest | None = None,
    conn: sqlite3.Connection = Depends(get_connection),
) -> WithdrawResult:
    """撤回标记与事件删除在同一事务内完成，重复撤回幂等。

    若请求带 body，其中的 visitor_id 必须与路径参数一致。
    """
    if payload is not None and payload.visitor_id != visitor_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径中的 visitor_id 与请求体不一致",
        )
    with write_transaction(conn):
        return crud.withdraw_consent(conn, visitor_id)


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}
