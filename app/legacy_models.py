"""Pydantic request/response models for the legacy card retrieval API (旧卡检索)."""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

# Old-system character code: exactly four hexadecimal digits. Lowercase input
# is accepted and normalized to uppercase so "0a01" and "0A01" are one code.
_CODE_RE = re.compile(r"^[0-9A-Fa-f]{4}$")


def normalize_code(value: str) -> str:
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise ValueError(f"字码必须是四位十六进制（如 0A1F），收到: {value!r}")
    return value.upper()


HexCode = Annotated[str, AfterValidator(normalize_code)]


class CardIn(BaseModel):
    """One legacy registration card: the item name as an exported code sequence."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"card_no": "0004", "codes": ["0B02", "0A01", "0A02", "0A03", "0A06"]}
            ]
        },
    )

    card_no: str = Field(..., min_length=1, description="登记卡号（批次内唯一，重复录入视为更新）")
    codes: list[HexCode] = Field(
        ..., min_length=1, description="品名字码序列，每个字码为四位十六进制"
    )


class CardRecordResult(BaseModel):
    batch_id: str
    card_no: str
    # stored=首次录入；updated=同批次同卡号重录，原码序列已被替换。
    status: Literal["stored", "updated"]
    # 本次转写依据的对照表版本；批次尚无对照表时为 null（所有位置未映射）。
    table_version: int | None
    complete: bool
    full_name: str | None
    unmapped_positions: list[int]


class MappingTableIn(BaseModel):
    """A whole mapping table; swapping replaces the batch's active table."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "entries": {
                        "0B01": "明", "0B02": "清",
                        "0A01": "青", "0A02": "花", "0A03": "瓷",
                        "0A04": "瓶", "0A05": "盌", "0A06": "罐",
                        "0A07": "砚", "0A08": "卉",
                    }
                }
            ]
        },
    )

    entries: dict[str, str] = Field(
        ..., min_length=1, description="整张对照表：四位十六进制字码 -> 一段 Unicode 文本"
    )

    @field_validator("entries", mode="before")
    @classmethod
    def _normalize_keys(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized: dict[str, object] = {}
        for key, text in value.items():
            code = normalize_code(key)
            if code in normalized:
                raise ValueError(f"字码大小写规范化后重复: {key!r}")
            normalized[code] = text
        return normalized

    @field_validator("entries")
    @classmethod
    def _check_texts(cls, value: dict[str, str]) -> dict[str, str]:
        for code, text in value.items():
            if not isinstance(text, str) or not text:
                raise ValueError(f"字码 {code} 对应的文本必须是非空字符串")
        return value


class TableSwapResult(BaseModel):
    batch_id: str
    # unchanged: 新表与现行表完全一致，未产生新版本、未重建任何卡。
    status: Literal["swapped", "unchanged"]
    version: int | None
    previous_version: int | None
    entries_count: int
    # 新增 / 修改 / 删除的对照项字码。
    changed_codes: list[str]
    # 原码涉及变动字码、本次被重新读取并重建的卡（含此前因缺映射未进索引的卡）。
    affected_cards: list[str]
    # 其中由不完整变完整的卡（如缺码补齐）。
    became_complete: list[str]
    # 其中由完整变不完整的卡。
    became_incomplete: list[str]


class MappingTableView(BaseModel):
    batch_id: str
    version: int
    entries: dict[str, str]
    entries_count: int
    created_at: str


class CodeSegment(BaseModel):
    position: int
    code: str
    # null 表示该字码在现行对照表中没有映射。
    text: str | None


class CardDetail(BaseModel):
    batch_id: str
    card_no: str
    codes: list[str]
    segments: list[CodeSegment]
    unmapped_positions: list[int]
    complete: bool
    full_name: str | None
    # 转写依据的对照表版本；批次尚无对照表时为 null。
    table_version: int | None


class LegacyCardRef(BaseModel):
    batch_id: str
    card_no: str
    full_name: str


class NameLookupResult(BaseModel):
    name: str
    total: int
    # 仅含已完整转写的卡；按 (批次, 卡号) 排序，结果稳定。
    cards: list[LegacyCardRef]
