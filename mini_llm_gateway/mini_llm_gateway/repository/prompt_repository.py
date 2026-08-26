from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from jinja2 import StrictUndefined, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from mini_llm_gateway.errors import GatewayError
from mini_llm_gateway.repository.database import connect
from mini_llm_gateway.schemas.llm import Message
from mini_llm_gateway.schemas.prompt import PromptTemplateCreate, PromptTemplateRecord

_COLUMNS = "name, version, system_template, created_at"


class PromptRepository:
    # 模板资产的数据库存储：(name, version) 唯一且不可变，"修改"即创建新版本。
    # 渲染使用 Jinja2 沙箱环境，防止页面编辑的模板内容触发 SSTI（服务端模板注入）。
    # StrictUndefined：缺变量直接报错（与旧版 string.Template 行为对等），
    # 可选开关变量由调用方显式传空串表示关闭。
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self.env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)

    async def initialize(self) -> None:
        async with connect(self.database_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS prompt_templates (
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    system_template TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (name, version)
                )
                """
            )
            await db.commit()

    async def exists(self, name: str, version: str) -> bool:
        return await self.get(name, version) is not None

    async def get(self, name: str, version: str) -> PromptTemplateRecord | None:
        async with connect(self.database_path) as db:
            cursor = await db.execute(
                f"SELECT {_COLUMNS} FROM prompt_templates WHERE name = ? AND version = ?",
                (name, version),
            )
            row = await cursor.fetchone()
        return PromptTemplateRecord.model_validate(dict(row)) if row else None

    async def require(self, name: str, version: str) -> PromptTemplateRecord:
        record = await self.get(name, version)
        if record is None:
            raise GatewayError("unknown_prompt_template", f"Prompt 模板不存在: {name}/{version}", 400)
        return record

    async def list(self, name: str | None = None) -> list[PromptTemplateRecord]:
        query = f"SELECT {_COLUMNS} FROM prompt_templates"
        params: tuple[Any, ...] = ()
        if name is not None:
            query += " WHERE name = ?"
            params = (name,)
        query += " ORDER BY name, created_at DESC"
        async with connect(self.database_path) as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [PromptTemplateRecord.model_validate(dict(row)) for row in rows]

    async def create(self, template: PromptTemplateCreate) -> PromptTemplateRecord:
        # 版本不可变：重复 (name, version) 拒绝，调用方必须使用新版本号。
        if await self.exists(template.name, template.version):
            raise GatewayError(
                "prompt_version_conflict",
                f"模板版本已存在: {template.name}/{template.version}，请使用新版本号",
                409,
            )
        created_at = datetime.now(timezone.utc).isoformat()
        async with connect(self.database_path) as db:
            await db.execute(
                "INSERT INTO prompt_templates (name, version, system_template, created_at) VALUES (?,?,?,?)",
                (template.name, template.version, template.system_template, created_at),
            )
            await db.commit()
        return PromptTemplateRecord(created_at=created_at, **template.model_dump())

    def render(self, record: PromptTemplateRecord, variables: dict[str, Any]) -> Message:
        # 渲染受控模板正文，调用方只能传变量，不能提交模板本身。
        try:
            content = self.env.from_string(record.system_template).render(**variables)
        except UndefinedError as exc:
            raise GatewayError("missing_prompt_variable", f"缺少 Prompt 变量: {exc}", 400) from exc
        except Exception as exc:
            raise GatewayError("prompt_render_failed", f"Prompt 渲染失败: {exc}", 400) from exc
        content = content.strip()
        if not content:
            raise GatewayError("prompt_render_failed", "Prompt 渲染结果为空", 400)
        return Message(role="system", content=content)
