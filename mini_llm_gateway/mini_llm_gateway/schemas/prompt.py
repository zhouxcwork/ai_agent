from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PromptTemplateCreate(BaseModel):
    # 创建模板或新版本：版本不可变，重复 (name, version) 返回 409。
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)
    system_template: str = Field(min_length=1, max_length=100_000)


class PromptTemplateRecord(PromptTemplateCreate):
    # 模板在数据库中的完整记录，多一个创建时间。
    created_at: datetime


class PromptRenderRequest(BaseModel):
    # 渲染预览请求：传入变量，返回渲染后的系统提示词。
    model_config = ConfigDict(extra="forbid")

    variables: dict[str, Any] = Field(default_factory=dict)
