"""请求体模型。时间字段若为朴素时间（无时区）一律按 UTC 处理。"""
from datetime import datetime, timezone
from typing import List

from pydantic import BaseModel, Field, field_validator, model_validator


def _ensure_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


class CredentialIn(BaseModel):
    credential_no: str = Field(min_length=1, max_length=128)
    valid_from: datetime
    valid_to: datetime
    max_entries: int = Field(ge=0, description="可入场次数")

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v)

    @model_validator(mode="after")
    def _check_window(self):
        if self.valid_from > self.valid_to:
            raise ValueError("valid_from 必须不晚于 valid_to")
        return self


class CredentialImportIn(BaseModel):
    credentials: List[CredentialIn] = Field(min_length=1, max_length=10000)


class ReportItem(BaseModel):
    terminal_id: str = Field(min_length=1, max_length=64, description="终端号")
    seq: int = Field(ge=1, description="终端侧递增序号")
    credential_no: str = Field(min_length=1, max_length=128)
    verified_at: datetime = Field(description="核销时间")

    @field_validator("verified_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v)


class BatchReportIn(BaseModel):
    records: List[ReportItem] = Field(min_length=1, max_length=1000)
