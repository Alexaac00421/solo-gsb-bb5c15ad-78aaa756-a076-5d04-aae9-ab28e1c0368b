"""凭证核销判定核心逻辑（纯函数，不依赖数据库，便于单元测试）。

排序与分配规则：
    同一凭证的所有核销记录按 (核销时间, 终端号, 序号) 升序排列，依次判定：
    - 凭证不在名单中            -> INVALID / NOT_IN_LIST
    - 核销时间不在有效时段内     -> INVALID / OUT_OF_WINDOW（不占用入场次数）
    - 有效入场次数已用完         -> INVALID / OVER_QUOTA
    - 其余                      -> VALID，按顺序分配第 entry_no 次入场

该排序规则是全局唯一的判定口径：无论记录到达顺序如何（乱序、迟到、并发），
只要集合相同，最终判定结果必然一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

VALID = "VALID"
INVALID = "INVALID"

NOT_IN_LIST = "NOT_IN_LIST"      # 不在名单
OUT_OF_WINDOW = "OUT_OF_WINDOW"  # 时段外
OVER_QUOTA = "OVER_QUOTA"        # 超次


@dataclass(frozen=True)
class CredentialRule:
    """凭证规则；为 None 时表示凭证不在名单中。"""

    valid_from: datetime
    valid_to: datetime
    max_entries: int


@dataclass(frozen=True)
class RecordKey:
    """参与判定所需的记录字段（排序键 + 主键）。"""

    terminal_id: str
    seq: int
    verified_at: datetime


@dataclass(frozen=True)
class Judgment:
    terminal_id: str
    seq: int
    status: str               # VALID / INVALID
    reason: Optional[str]     # INVALID 时的原因；VALID 时为 None
    entry_no: Optional[int]   # VALID 时分配到的入场次序（1 起）；否则为 None


def judge(records, credential: Optional[CredentialRule]) -> list[Judgment]:
    """对同一凭证的全部记录按统一排序规则分配有效次数。

    records:    RecordKey 可迭代对象（同一 credential_no 的全部记录）
    credential: CredentialRule 或 None（不在名单）
    返回:       按 (核销时间, 终端号, 序号) 排序的 Judgment 列表
    """
    ordered = sorted(records, key=lambda r: (r.verified_at, r.terminal_id, r.seq))
    judgments: list[Judgment] = []
    used = 0
    for r in ordered:
        if credential is None:
            judgments.append(Judgment(r.terminal_id, r.seq, INVALID, NOT_IN_LIST, None))
        elif not (credential.valid_from <= r.verified_at <= credential.valid_to):
            judgments.append(Judgment(r.terminal_id, r.seq, INVALID, OUT_OF_WINDOW, None))
        elif used < credential.max_entries:
            used += 1
            judgments.append(Judgment(r.terminal_id, r.seq, VALID, None, used))
        else:
            judgments.append(Judgment(r.terminal_id, r.seq, INVALID, OVER_QUOTA, None))
    return judgments
