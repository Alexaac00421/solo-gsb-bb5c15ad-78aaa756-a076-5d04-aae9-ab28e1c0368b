"""HTTP 接口的请求/响应模型。

时间一律 ISO 8601；不带时区的时间戳按 UTC 解释（终端本地时间请直接带偏移量）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Judgment = Literal["valid", "invalid"]
Reason = Literal["not_in_list", "outside_window", "exhausted"]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_code(value: str) -> str:
    code = value.strip()
    if not code:
        raise ValueError("凭证号不能为空")
    if len(code) > 128:
        raise ValueError("凭证号长度不能超过 128")
    return code


def _normalize_terminal(value: str) -> str:
    t = value.strip()
    if not t:
        raise ValueError("终端号不能为空")
    if len(t) > 128:
        raise ValueError("终端号长度不能超过 128")
    return t


# ---------- 凭证导入 ----------

class CredentialIn(BaseModel):
    code: str = Field(..., description="凭证号")
    valid_from: datetime
    valid_to: datetime
    max_entries: int = Field(..., ge=0, description="可入场次数")

    @field_validator("code")
    @classmethod
    def _v_code(cls, v: str) -> str:
        return _normalize_code(v)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _v_dt(cls, v: datetime) -> datetime:
        return _as_utc(v)

    def validate_window(self) -> "CredentialIn":
        if self.valid_from > self.valid_to:
            raise ValueError("valid_from 不能晚于 valid_to")
        return self


class CredentialImportRequest(BaseModel):
    credentials: list[CredentialIn] = Field(..., min_length=1)


class CredentialView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    valid_from: datetime
    valid_to: datetime
    max_entries: int
    used_entries: int = Field(..., description="当前判定为 valid 的记录数")
    remaining_entries: int


# ---------- 上报 ----------

class RecordIn(BaseModel):
    terminal_no: str
    seq: int = Field(..., ge=0, description="终端侧递增序号（含 0）")
    credential_code: str
    verified_at: datetime = Field(..., description="终端核销时间，ISO 8601")

    @field_validator("terminal_no")
    @classmethod
    def _v_terminal(cls, v: str) -> str:
        return _normalize_terminal(v)

    @field_validator("credential_code")
    @classmethod
    def _v_code(cls, v: str) -> str:
        return _normalize_code(v)

    @field_validator("verified_at")
    @classmethod
    def _v_verified(cls, v: datetime) -> datetime:
        return _as_utc(v)


class BatchReportRequest(BaseModel):
    records: list[RecordIn] = Field(..., min_length=1)


class RecordView(BaseModel):
    terminal_no: str
    seq: int
    credential_code: str
    verified_at: datetime
    judgment: Judgment
    reason: Optional[Reason] = None
    remaining_entries: Optional[int] = Field(
        default=None, description="凭证当前剩余次数；凭证不在名单时为 null"
    )


class ChangedRecord(BaseModel):
    terminal_no: str
    seq: int
    credential_code: str
    verified_at: datetime
    old_judgment: Judgment
    old_reason: Optional[Reason] = None
    new_judgment: Judgment
    new_reason: Optional[Reason] = None
    remaining_entries: Optional[int] = None


class BatchReportResponse(BaseModel):
    inserted: list[RecordView] = Field(..., description="本批首次写入的记录及当前判定")
    duplicate: list[RecordView] = Field(
        ..., description="同终端号+序号且同内容的幂等重传"
    )
    changed: list[ChangedRecord] = Field(
        ..., description="因迟到数据导致判定被改判的记录（含本批新记录从临时判定变为最终判定的过程）"
    )
    rejected: bool = False


class BatchRejectedResponse(BaseModel):
    rejected: bool = True
    error: str
    conflicts: list[dict] = Field(
        default_factory=list,
        description="与已存在记录内容不一致的 (终端号, 序号)，整批未写入",
    )


# ---------- 查询 ----------

class RecordQueryResponse(BaseModel):
    records: list[RecordView]
    limit: int
