"""HTTP API 入口。

接口一览
========
POST /api/v1/credentials/import   导入/更新凭证（名单、时段、次数），并立即重算
POST /api/v1/reports/batch        终端批量上报核销记录（幂等、乱序、迟到重算）
GET  /api/v1/records              查询记录当前判定及凭证剩余次数
GET  /api/v1/credentials/{code}   查询单个凭证及剩余次数
GET  /healthz                     存活探针
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import service
from .config import get_settings
from .db import close_db, get_pool, init_db
from .schemas import (
    BatchRejectedResponse,
    BatchReportRequest,
    BatchReportResponse,
    CredentialImportRequest,
    CredentialView,
    RecordQueryResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("expo.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时等待数据库并完成幂等初始化。
    init_db()
    log.info("对账服务启动完成")
    try:
        yield
    finally:
        close_db()


app = FastAPI(
    title="展会断网闸机 · 入场凭证对账服务",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(service.BatchConflict)
async def batch_conflict_handler(request: Request, exc: service.BatchConflict):
    return JSONResponse(
        status_code=409,
        content={
            "rejected": True,
            "error": str(exc),
            "conflicts": exc.conflicts,
        },
    )


@app.get("/healthz")
def healthz():
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/api/v1/credentials/import")
def import_credentials(req: CredentialImportRequest):
    settings = get_settings()
    if len(req.credentials) > settings.max_batch_size:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多导入 {settings.max_batch_size} 条凭证",
        )
    items = []
    try:
        for c in req.credentials:
            c.validate_window()
            items.append(c.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = service.import_credentials(get_pool(), items)
    except service.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@app.post(
    "/api/v1/reports/batch",
    response_model=BatchReportResponse,
    responses={
        409: {
            "model": BatchRejectedResponse,
            "description": "同终端号+序号内容不同，整批拒绝且不写入",
        }
    },
)
def report_batch(req: BatchReportRequest):
    settings = get_settings()
    if len(req.records) > settings.max_batch_size:
        raise HTTPException(
            status_code=400,
            detail=f"单批最多 {settings.max_batch_size} 条记录",
        )
    items = [r.model_dump() for r in req.records]
    try:
        return service.submit_batch(get_pool(), items)
    except service.BatchConflict:
        # 交给 exception_handler 统一返回 409
        raise
    except service.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/records", response_model=RecordQueryResponse)
def list_records(
    credential_code: str | None = Query(default=None, description="按凭证号过滤"),
    terminal_no: str | None = Query(default=None, description="按终端号过滤"),
    limit: int = Query(default=200, ge=1, le=2000),
):
    records = service.query_records(
        get_pool(), credential_code, terminal_no, limit
    )
    return {"records": records, "limit": limit}


@app.get("/api/v1/credentials/{code}", response_model=CredentialView)
def get_credential(code: str):
    result = service.get_credential(get_pool(), code.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="凭证不在名单")
    return result
