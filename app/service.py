"""业务编排：凭证导入、批次上报（幂等/冲突/重算）、查询。

并发一致性设计：
    1. 每张凭证的重算在事务内先取 pg_advisory_xact_lock(hashtext(credential_no))，
       同一凭证的判定重算被串行化；
    2. 一个批次涉及多张凭证时，按凭证号字典序依次加锁，避免交叉死锁；
    3. 判定只依赖 (核销时间, 终端号, 序号) 的全量排序，与到达顺序无关，
       因此并发上报的最终结果与按同一排序规则串行处理完全一致；
    4. 并发提交相同 (终端号, 序号) 导致的唯一键竞争/死锁为瞬态错误，
       回滚后整批重试即可收敛到幂等或冲突的确定结果。
"""
from __future__ import annotations

from typing import Any

from psycopg import errors

from . import config
from .judgment import CredentialRule, RecordKey, judge
from .models import BatchReportIn, CredentialImportIn

UPSERT_CREDENTIAL_SQL = """
INSERT INTO credentials (credential_no, valid_from, valid_to, max_entries)
VALUES (%(credential_no)s, %(valid_from)s, %(valid_to)s, %(max_entries)s)
ON CONFLICT (credential_no) DO UPDATE
SET valid_from  = EXCLUDED.valid_from,
    valid_to    = EXCLUDED.valid_to,
    max_entries = EXCLUDED.max_entries,
    updated_at  = now()
"""

INSERT_RECORD_SQL = """
INSERT INTO records (terminal_id, seq, credential_no, verified_at, status, reason)
VALUES (%s, %s, %s, %s, 'INVALID', 'NOT_IN_LIST')
"""

# 瞬态可重试错误：并发下同主键插入竞争 / 死锁 / 序列化失败
RETRYABLE_ERRORS = (
    errors.UniqueViolation,
    errors.DeadlockDetected,
    errors.SerializationFailure,
)


class BatchConflict(Exception):
    """同一 (终端号, 序号) 已存在不同内容的记录 -> 整批拒绝，不写入任何数据。"""

    def __init__(self, terminal_id: str, seq: int, existing: dict, received: dict):
        self.detail = {
            "error": "REPORT_CONFLICT",
            "message": "相同终端号与序号的记录内容不一致，整批拒绝，未写入任何数据",
            "terminal_id": terminal_id,
            "seq": seq,
            "existing": existing,
            "received": received,
        }
        super().__init__(self.detail["message"])


def recompute_credential(cur, credential_no: str) -> list[dict[str, Any]]:
    """在调用方事务内对单张凭证全量重算判定，返回被改判的记录。

    通过事务级咨询锁把同一凭证的重算串行化；锁在事务提交时自动释放。
    """
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)", (credential_no,))
    cur.execute(
        "SELECT valid_from, valid_to, max_entries FROM credentials WHERE credential_no = %s",
        (credential_no,),
    )
    row = cur.fetchone()
    rule = (
        CredentialRule(valid_from=row[0], valid_to=row[1], max_entries=row[2])
        if row
        else None
    )
    cur.execute(
        """
        SELECT terminal_id, seq, verified_at, status, reason, entry_no
        FROM records
        WHERE credential_no = %s
        """,
        (credential_no,),
    )
    rows = cur.fetchall()
    current = {(r[0], r[1]): (r[3], r[4], r[5]) for r in rows}
    keys = [RecordKey(terminal_id=r[0], seq=r[1], verified_at=r[2]) for r in rows]

    changed: list[dict[str, Any]] = []
    for j in judge(keys, rule):
        old = current[(j.terminal_id, j.seq)]
        new = (j.status, j.reason, j.entry_no)
        if old != new:
            cur.execute(
                """
                UPDATE records
                SET status = %s, reason = %s, entry_no = %s, updated_at = now()
                WHERE terminal_id = %s AND seq = %s
                """,
                (j.status, j.reason, j.entry_no, j.terminal_id, j.seq),
            )
            changed.append(
                {
                    "credential_no": credential_no,
                    "terminal_id": j.terminal_id,
                    "seq": j.seq,
                    "old": {"status": old[0], "reason": old[1], "entry_no": old[2]},
                    "new": {"status": j.status, "reason": j.reason, "entry_no": j.entry_no},
                }
            )
    return changed


def import_credentials(conn, payload: CredentialImportIn) -> dict[str, Any]:
    """批量导入/更新凭证；导入后对相关凭证重算（名单变化可能改判历史记录）。"""
    with conn.transaction():
        cur = conn.cursor()
        for item in payload.credentials:
            cur.execute(UPSERT_CREDENTIAL_SQL, item.model_dump())
        changed: list[dict[str, Any]] = []
        for cred_no in sorted({c.credential_no for c in payload.credentials}):
            changed.extend(recompute_credential(cur, cred_no))
    return {"imported": len(payload.credentials), "changed": changed}


def submit_batch(conn, batch: BatchReportIn) -> dict[str, Any]:
    """处理一个上报批次（在单个事务内）。

    - 相同 (终端号, 序号) 且内容一致 -> DUPLICATE，幂等跳过；
    - 相同 (终端号, 序号) 但内容不同 -> 抛 BatchConflict，事务回滚，整批不写入；
    - 新记录插入后，对涉及的每张凭证按统一排序规则重算，返回被改判记录。
    """
    results: list[tuple[Any, str]] = []
    affected: set[str] = set()

    with conn.transaction():
        cur = conn.cursor()
        seen_in_batch: dict[tuple[str, int], tuple[str, Any]] = {}

        for item in batch.records:
            key = (item.terminal_id, item.seq)
            content = (item.credential_no, item.verified_at)

            # 批内自冲突检查
            if key in seen_in_batch:
                if seen_in_batch[key] != content:
                    raise BatchConflict(
                        item.terminal_id,
                        item.seq,
                        existing={
                            "credential_no": seen_in_batch[key][0],
                            "verified_at": seen_in_batch[key][1],
                        },
                        received={
                            "credential_no": item.credential_no,
                            "verified_at": item.verified_at,
                        },
                    )
                results.append((item, "DUPLICATE"))
                continue
            seen_in_batch[key] = content

            cur.execute(
                "SELECT credential_no, verified_at FROM records WHERE terminal_id = %s AND seq = %s",
                key,
            )
            row = cur.fetchone()
            if row is not None:
                if (row[0], row[1]) == content:
                    # 同内容重传：幂等
                    results.append((item, "DUPLICATE"))
                else:
                    raise BatchConflict(
                        item.terminal_id,
                        item.seq,
                        existing={"credential_no": row[0], "verified_at": row[1]},
                        received={
                            "credential_no": item.credential_no,
                            "verified_at": item.verified_at,
                        },
                    )
            else:
                cur.execute(
                    INSERT_RECORD_SQL,
                    (item.terminal_id, item.seq, item.credential_no, item.verified_at),
                )
                results.append((item, "RECEIVED"))
                affected.add(item.credential_no)

        # 迟到/乱序记录插入后，对涉及的凭证重算；按凭证号排序加锁避免死锁
        changed: list[dict[str, Any]] = []
        for cred_no in sorted(affected):
            changed.extend(recompute_credential(cur, cred_no))

        # 回填批次内每条记录的当前判定
        final_results: list[dict[str, Any]] = []
        for item, outcome in results:
            cur.execute(
                "SELECT status, reason, entry_no FROM records WHERE terminal_id = %s AND seq = %s",
                (item.terminal_id, item.seq),
            )
            status, reason, entry_no = cur.fetchone()
            final_results.append(
                {
                    "terminal_id": item.terminal_id,
                    "seq": item.seq,
                    "credential_no": item.credential_no,
                    "verified_at": item.verified_at,
                    "outcome": outcome,
                    "status": status,
                    "reason": reason,
                    "entry_no": entry_no,
                }
            )

    return {"accepted": True, "results": final_results, "changed": changed}


def submit_batch_with_retry(pool, batch: BatchReportIn) -> dict[str, Any]:
    """带瞬态错误重试的批次提交。BatchConflict 为确定性冲突，不重试。"""
    for attempt in range(1, config.SUBMIT_RETRY_ATTEMPTS + 1):
        try:
            with pool.connection() as conn:
                return submit_batch(conn, batch)
        except RETRYABLE_ERRORS:
            if attempt == config.SUBMIT_RETRY_ATTEMPTS:
                raise
    raise RuntimeError("unreachable")


def get_credential_view(conn, credential_no: str) -> dict[str, Any] | None:
    """查询凭证当前状态：每条记录的当前判定 + 剩余次数。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT valid_from, valid_to, max_entries FROM credentials WHERE credential_no = %s",
        (credential_no,),
    )
    cred = cur.fetchone()
    cur.execute(
        """
        SELECT terminal_id, seq, verified_at, status, reason, entry_no
        FROM records
        WHERE credential_no = %s
        ORDER BY verified_at, terminal_id, seq
        """,
        (credential_no,),
    )
    rows = cur.fetchall()
    if cred is None and not rows:
        return None

    used = sum(1 for r in rows if r[3] == "VALID")
    return {
        "credential_no": credential_no,
        "in_list": cred is not None,
        "valid_from": cred[0] if cred else None,
        "valid_to": cred[1] if cred else None,
        "max_entries": cred[2] if cred else None,
        "used_entries": used,
        "remaining_entries": (cred[2] - used) if cred else None,
        "records": [
            {
                "terminal_id": r[0],
                "seq": r[1],
                "verified_at": r[2],
                "status": r[3],
                "reason": r[4],
                "entry_no": r[5],
            }
            for r in rows
        ],
    }
