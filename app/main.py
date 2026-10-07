"""HTTP API 入口（FastAPI）。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from . import db, service
from .models import BatchReportIn, CredentialImportIn


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # 启动时完成数据库初始化（等待就绪 + 幂等建表）
    db.pool.open()
    yield
    db.pool.close()


app = FastAPI(
    title="展会闸机入场凭证对账服务",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
def root():
    return {"service": "gate-reconciliation", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    with db.pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/credentials/import")
def import_credentials(payload: CredentialImportIn):
    """批量导入（或更新）凭证：凭证号、有效时段、可入场次数。"""
    with db.pool.connection() as conn:
        return service.import_credentials(conn, payload)


@app.post("/reports")
def submit_reports(batch: BatchReportIn):
    """终端批量上报核销记录。

    - 相同 (终端号, 序号) 同内容重传：幂等，返回 DUPLICATE；
    - 相同 (终端号, 序号) 内容不同：409，整批拒绝且不写入；
    - 批次可乱序到达；迟到记录触发该凭证重算，响应 changed 字段列出被改判记录。
    """
    try:
        return service.submit_batch_with_retry(db.pool, batch)
    except service.BatchConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail)


@app.get("/credentials/{credential_no}")
def get_credential(credential_no: str):
    """查询单张凭证：每条记录的当前判定、已用/剩余次数。"""
    with db.pool.connection() as conn:
        view = service.get_credential_view(conn, credential_no)
    if view is None:
        raise HTTPException(status_code=404, detail="凭证不存在且无任何上报记录")
    return view
