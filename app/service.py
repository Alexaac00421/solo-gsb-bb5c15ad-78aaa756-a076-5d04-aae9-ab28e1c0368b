"""核心对账逻辑。

并发正确性设计
--------------
每张凭证用一个事务级咨询锁 ``pg_advisory_xact_lock(ns, hashtext(code))`` 保护，
所有涉及多凭证的事务都按凭证号升序依次取锁，因此：

* 同一张凭证的上报/导入天然串行化，重算结果与
  ``ORDER BY verified_at, terminal_no, seq`` 的单一规则严格一致；
* 不同事务按同一全局顺序取锁，不会死锁；
* 尚未导入的凭证（not_in_list）也按凭证号取锁，凭证随后导入时会与迟到上报互斥。

同一 (terminal_no, seq) 的唯一约束兜底拦截并发首报：撞约束时事务回滚后重开
事务重试，竞争事务此时已提交，即可识别为“同内容重传（幂等）”或“异内容（整批
拒绝）”。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

from .config import get_settings

log = logging.getLogger("expo.service")

JUDGMENT_VALID = "valid"
JUDGMENT_INVALID = "invalid"
REASON_NOT_IN_LIST = "not_in_list"
REASON_OUTSIDE_WINDOW = "outside_window"
REASON_EXHAUSTED = "exhausted"

# 并发首报撞 (terminal_no, seq) 唯一约束时的最大尝试次数。
_MAX_ATTEMPTS = 2


class BatchConflict(Exception):
    """(终端号, 序号) 已存在但内容不同：整批拒绝。HTTP 409。"""

    def __init__(self, conflicts: list[dict[str, Any]]):
        super().__init__("相同终端号+序号的记录内容不一致，整批拒绝")
        self.conflicts = conflicts


class ValidationError(Exception):
    """请求级别的数据校验错误。HTTP 400。"""


# ---------------------------------------------------------------- SQL

# 取一批凭证（codes）下的全部记录，按统一规则重算判定：
#   not_in_list    凭证不存在
#   outside_window 核销时间不在 [valid_from, valid_to]
#   exhausted      时段内，但按 (verified_at, terminal_no, seq) 排序后超出名额
# 窗口外记录被分到 eligible=0 分区，rn 必然 > 非负的 max_entries，不占用名额。
_RECOMPUTE_SQL = """
WITH affected AS (
    SELECT
        gr.id,
        c.max_entries AS max_entries,
        CASE
            WHEN c.code IS NULL THEN %(not_in_list)s
            WHEN gr.verified_at < c.valid_from
              OR gr.verified_at > c.valid_to THEN %(outside)s
            ELSE NULL
        END AS fail_reason,
        CASE
            WHEN c.code IS NOT NULL
             AND gr.verified_at >= c.valid_from
             AND gr.verified_at <= c.valid_to THEN 1
            ELSE 0
        END AS eligible,
        ROW_NUMBER() OVER (
            PARTITION BY gr.credential_code,
                CASE WHEN c.code IS NOT NULL
                      AND gr.verified_at >= c.valid_from
                      AND gr.verified_at <= c.valid_to
                     THEN 1 ELSE 0 END
            ORDER BY gr.verified_at, gr.terminal_no, gr.seq
        ) AS rn
    FROM gate_records gr
    LEFT JOIN credentials c ON c.code = gr.credential_code
    WHERE gr.credential_code = ANY(%(codes)s)
),
final AS (
    SELECT
        a.id,
        CASE
            WHEN a.fail_reason IS NOT NULL THEN %(invalid)s
            WHEN a.eligible = 1 AND a.rn <= a.max_entries THEN %(valid)s
            ELSE %(invalid)s
        END AS judgment,
        CASE
            WHEN a.fail_reason IS NOT NULL THEN a.fail_reason
            WHEN a.eligible = 1 AND a.rn > a.max_entries THEN %(exhausted)s
            ELSE NULL
        END AS reason
    FROM affected a
)
UPDATE gate_records gr
SET judgment = f.judgment,
    reason   = f.reason,
    updated_at = now()
FROM final f
WHERE gr.id = f.id
  AND (gr.judgment IS DISTINCT FROM f.judgment
       OR gr.reason IS DISTINCT FROM f.reason)
"""

_FETCH_RECORDS_SQL = """
SELECT gr.terminal_no, gr.seq, gr.credential_code, gr.verified_at,
       gr.judgment, gr.reason, c.max_entries
FROM gate_records gr
LEFT JOIN credentials c ON c.code = gr.credential_code
WHERE gr.credential_code = ANY(%s)
ORDER BY gr.credential_code, gr.verified_at, gr.terminal_no, gr.seq
"""

_EXISTING_BY_KEYS_SQL = """
SELECT terminal_no, seq, credential_code, verified_at, judgment, reason
FROM gate_records
WHERE (terminal_no, seq) IN (
    SELECT term, s FROM unnest(%s::text[], %s::bigint[]) AS u(term, s)
)
"""

_QUERY_RECORDS_SQL = """
WITH base AS (
    SELECT gr.terminal_no, gr.seq, gr.credential_code, gr.verified_at,
           gr.judgment, gr.reason, c.max_entries,
           count(*) FILTER (WHERE gr.judgment = 'valid') OVER (
               PARTITION BY gr.credential_code
           ) AS valid_cnt
    FROM gate_records gr
    LEFT JOIN credentials c ON c.code = gr.credential_code
    WHERE (%(code)s::text IS NULL OR gr.credential_code = %(code)s)
      AND (%(terminal)s::text IS NULL OR gr.terminal_no = %(terminal)s)
)
SELECT terminal_no, seq, credential_code, verified_at, judgment, reason,
       max_entries, valid_cnt
FROM base
ORDER BY verified_at, terminal_no, seq
LIMIT %(limit)s
"""

_QUERY_CREDENTIAL_SQL = """
SELECT c.code, c.valid_from, c.valid_to, c.max_entries,
       count(gr.*) FILTER (WHERE gr.judgment = 'valid') AS used
FROM credentials c
LEFT JOIN gate_records gr ON gr.credential_code = c.code
WHERE c.code = %s
GROUP BY c.code
"""


# ---------------------------------------------------------------- 辅助函数

def _acquire_locks(conn, codes: Iterable[str]) -> None:
    """按凭证号升序取事务级咨询锁，保证跨事务的统一取锁顺序。"""
    ns = get_settings().lock_namespace
    for code in sorted(set(codes)):
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (ns, code)
        )


def _recompute(conn, codes: list[str]) -> None:
    conn.execute(
        _RECOMPUTE_SQL,
        {
            "codes": list(codes),
            "not_in_list": REASON_NOT_IN_LIST,
            "outside": REASON_OUTSIDE_WINDOW,
            "exhausted": REASON_EXHAUSTED,
            "valid": JUDGMENT_VALID,
            "invalid": JUDGMENT_INVALID,
        },
    )


def _records_by_codes(conn, codes: list[str]) -> list[dict[str, Any]]:
    cur = conn.execute(_FETCH_RECORDS_SQL, (list(codes),))
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _build_views(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    """组装 key->记录视图（含剩余次数）。"""
    valid_count: dict[str, int] = {}
    for r in rows:
        if r["judgment"] == JUDGMENT_VALID:
            valid_count[r["credential_code"]] = (
                valid_count.get(r["credential_code"], 0) + 1
            )

    views: dict[tuple[str, int], dict[str, Any]] = {}
    for r in rows:
        code = r["credential_code"]
        remaining = (
            None
            if r["max_entries"] is None
            else r["max_entries"] - valid_count.get(code, 0)
        )
        views[(r["terminal_no"], r["seq"])] = {
            "terminal_no": r["terminal_no"],
            "seq": r["seq"],
            "credential_code": code,
            "verified_at": r["verified_at"],
            "judgment": r["judgment"],
            "reason": r["reason"],
            "remaining_entries": remaining,
        }
    return views


def _dedup_items(
    items: list[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    """批内去重：同键同内容保留一条；同键异内容抛 BatchConflict。"""
    first: dict[tuple[str, int], dict[str, Any]] = {}
    for item in items:
        key = (item["terminal_no"], item["seq"])
        prev = first.get(key)
        if prev is not None and (
            prev["credential_code"] != item["credential_code"]
            or prev["verified_at"] != item["verified_at"]
        ):
            raise BatchConflict(
                [
                    {
                        "terminal_no": key[0],
                        "seq": key[1],
                        "reason": "同一批次内相同终端号+序号但凭证号/核销时间不同",
                    }
                ]
            )
        first.setdefault(key, item)
    return first


def _existing_by_keys(conn, seen):
    keys = list(seen.keys())
    cur = conn.execute(
        _EXISTING_BY_KEYS_SQL, ([k[0] for k in keys], [k[1] for k in keys])
    )
    cols = [d.name for d in cur.description]
    return {(row[0], row[1]): dict(zip(cols, row)) for row in cur.fetchall()}


def _conflict_if_content_differs(item, ex) -> dict[str, Any] | None:
    if (
        ex["credential_code"] == item["credential_code"]
        and ex["verified_at"] == item["verified_at"]
    ):
        return None
    return {
        "terminal_no": item["terminal_no"],
        "seq": item["seq"],
        "existing": {
            "credential_code": ex["credential_code"],
            "verified_at": ex["verified_at"].isoformat(),
        },
        "received": {
            "credential_code": item["credential_code"],
            "verified_at": item["verified_at"].isoformat(),
        },
    }


def _provisional_judgment(
    cred_map: dict[str, tuple[datetime, datetime, int]],
    item: dict[str, Any],
) -> tuple[str, str | None]:
    known = cred_map.get(item["credential_code"])
    if known is None:
        return JUDGMENT_INVALID, REASON_NOT_IN_LIST
    vf, vt, _ = known
    if not (vf <= item["verified_at"] <= vt):
        return JUDGMENT_INVALID, REASON_OUTSIDE_WINDOW
    return JUDGMENT_VALID, None


def _insert_new(conn, codes, new_items) -> None:
    """写入新记录；撞并发唯一约束时抛 UniqueViolation 交由外层重试。"""
    cred_map: dict[str, tuple[datetime, datetime, int]] = {
        code: (vf, vt, me)
        for code, vf, vt, me in conn.execute(
            "SELECT code, valid_from, valid_to, max_entries "
            "FROM credentials WHERE code = ANY(%s)",
            (codes,),
        ).fetchall()
    }
    t_cols: list[str] = []
    s_cols: list[int] = []
    code_cols: list[str] = []
    ts_cols: list[datetime] = []
    j_cols: list[str] = []
    r_cols: list[str | None] = []
    for item in new_items:
        judgment, reason = _provisional_judgment(cred_map, item)
        t_cols.append(item["terminal_no"])
        s_cols.append(item["seq"])
        code_cols.append(item["credential_code"])
        ts_cols.append(item["verified_at"])
        j_cols.append(judgment)
        r_cols.append(reason)
    conn.execute(
        "INSERT INTO gate_records "
        "(terminal_no, seq, credential_code, verified_at, judgment, reason) "
        "SELECT v.t, v.s, v.code, v.ts, v.j, v.r "
        "FROM unnest(%s::text[], %s::bigint[], %s::text[], "
        "          %s::timestamptz[], %s::text[], %s::text[]) "
        "AS v(t, s, code, ts, j, r)",
        (t_cols, s_cols, code_cols, ts_cols, j_cols, r_cols),
    )


# ---------------------------------------------------------------- 凭证导入

def import_credentials(
    pool: ConnectionPool, items: list[dict[str, Any]]
) -> dict[str, Any]:
    codes = [i["code"] for i in items]
    if len(set(codes)) != len(codes):
        raise ValidationError("同一请求中凭证号不得重复")

    with pool.connection() as conn:
        _acquire_locks(conn, codes)

        before = {
            (r["terminal_no"], r["seq"]): (r["judgment"], r["reason"])
            for r in _records_by_codes(conn, codes)
        }

        conn.execute(
            """
            INSERT INTO credentials (code, valid_from, valid_to, max_entries)
            SELECT v.code, v.valid_from, v.valid_to, v.max_entries
            FROM unnest(%s::text[], %s::timestamptz[], %s::timestamptz[],
                       %s::integer[])
                 AS v(code, valid_from, valid_to, max_entries)
            ON CONFLICT (code) DO UPDATE SET
                valid_from  = EXCLUDED.valid_from,
                valid_to    = EXCLUDED.valid_to,
                max_entries = EXCLUDED.max_entries,
                updated_at  = now()
            """,
            (
                codes,
                [i["valid_from"] for i in items],
                [i["valid_to"] for i in items],
                [i["max_entries"] for i in items],
            ),
        )

        _recompute(conn, codes)

        views = _build_views(_records_by_codes(conn, codes))
        changed = []
        for key, (old_j, old_r) in before.items():
            view = views.get(key)
            if view is None:
                continue
            if (view["judgment"], view["reason"]) != (old_j, old_r):
                changed.append(
                    {
                        "terminal_no": view["terminal_no"],
                        "seq": view["seq"],
                        "credential_code": view["credential_code"],
                        "verified_at": view["verified_at"],
                        "old_judgment": old_j,
                        "old_reason": old_r,
                        "new_judgment": view["judgment"],
                        "new_reason": view["reason"],
                        "remaining_entries": view["remaining_entries"],
                    }
                )

        cred_rows = conn.execute(
            """
            SELECT c.code, c.valid_from, c.valid_to, c.max_entries,
                   count(gr.*) FILTER (WHERE gr.judgment = 'valid') AS used
            FROM credentials c
            LEFT JOIN gate_records gr ON gr.credential_code = c.code
            WHERE c.code = ANY(%s)
            GROUP BY c.code, c.valid_from, c.valid_to, c.max_entries
            ORDER BY c.code
            """,
            (codes,),
        ).fetchall()
        credentials = [
            {
                "code": code,
                "valid_from": vf,
                "valid_to": vt,
                "max_entries": me,
                "used_entries": int(used),
                "remaining_entries": me - int(used),
            }
            for code, vf, vt, me, used in cred_rows
        ]

    return {"credentials": credentials, "changed": changed}


# ---------------------------------------------------------------- 批量上报

def submit_batch(
    pool: ConnectionPool, items: list[dict[str, Any]]
) -> dict[str, Any]:
    seen = _dedup_items(items)

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        with pool.connection() as conn:
            unique_items = list(seen.values())
            codes = sorted({i["credential_code"] for i in unique_items})

            _acquire_locks(conn, codes)

            # 重算前快照：涉及凭证下全部既有记录的判定，用于发现被迟到数据挤掉者。
            before = {
                (r["terminal_no"], r["seq"]): (r["judgment"], r["reason"])
                for r in _records_by_codes(conn, codes)
            }

            existing = _existing_by_keys(conn, seen)

            conflicts = []
            for item in unique_items:
                ex = existing.get((item["terminal_no"], item["seq"]))
                if ex is not None:
                    c = _conflict_if_content_differs(item, ex)
                    if c is not None:
                        conflicts.append(c)
            if conflicts:
                raise BatchConflict(conflicts)

            new_items = [
                item
                for item in unique_items
                if (item["terminal_no"], item["seq"]) not in existing
            ]
            dup_keys = [
                (item["terminal_no"], item["seq"])
                for item in unique_items
                if (item["terminal_no"], item["seq"]) in existing
            ]
            new_keys = {
                (item["terminal_no"], item["seq"]) for item in new_items
            }

            try:
                if new_items:
                    _insert_new(conn, codes, new_items)
                _recompute(conn, codes)
            except UniqueViolation:
                # 与并发首报撞键：回滚整个事务（释放锁），重开事务重试。
                conn.rollback()
                if attempt >= _MAX_ATTEMPTS:
                    raise BatchConflict(
                        [{"reason": "并发写入相同终端号+序号，请稍后重传该批"}]
                    )
                log.info("并发撞键，事务重试 attempt=%d", attempt + 1)
                continue

            views = _build_views(_records_by_codes(conn, codes))

            inserted = [views[k] for k in new_keys]
            duplicate = [views[k] for k in dup_keys]
            changed = []
            for key, (old_j, old_r) in before.items():
                if key in new_keys:
                    continue  # 本批新记录没有历史判定，不属于“改判”
                view = views.get(key)
                if view is None:
                    continue
                if (view["judgment"], view["reason"]) != (old_j, old_r):
                    changed.append(
                        {
                            "terminal_no": view["terminal_no"],
                            "seq": view["seq"],
                            "credential_code": view["credential_code"],
                            "verified_at": view["verified_at"],
                            "old_judgment": old_j,
                            "old_reason": old_r,
                            "new_judgment": view["judgment"],
                            "new_reason": view["reason"],
                            "remaining_entries": view["remaining_entries"],
                        }
                    )

            return {
                "inserted": inserted,
                "duplicate": duplicate,
                "changed": changed,
                "rejected": False,
            }

    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------- 查询

def query_records(
    pool: ConnectionPool,
    credential_code: str | None,
    terminal_no: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(
            _QUERY_RECORDS_SQL,
            {
                "code": credential_code,
                "terminal": terminal_no,
                "limit": limit,
            },
        )
        rows = cur.fetchall()
    result = []
    for term, seq, code, ts, judgment, reason, max_entries, valid_cnt in rows:
        result.append(
            {
                "terminal_no": term,
                "seq": seq,
                "credential_code": code,
                "verified_at": ts,
                "judgment": judgment,
                "reason": reason,
                "remaining_entries": (
                    None if max_entries is None
                    else max_entries - int(valid_cnt)
                ),
            }
        )
    return result


def get_credential(
    pool: ConnectionPool, code: str
) -> dict[str, Any] | None:
    with pool.connection() as conn:
        row = conn.execute(_QUERY_CREDENTIAL_SQL, (code,)).fetchone()
    if row is None:
        return None
    c, vf, vt, me, used = row
    return {
        "code": c,
        "valid_from": vf,
        "valid_to": vt,
        "max_entries": me,
        "used_entries": int(used),
        "remaining_entries": me - int(used),
    }
