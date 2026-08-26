from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse

from mini_llm_gateway.api.deps import require_admin
from mini_llm_gateway.schemas.llm import Message
from mini_llm_gateway.schemas.prompt import PromptRenderRequest, PromptTemplateCreate, PromptTemplateRecord

router = APIRouter()

_ADMIN_PAGE = Path(__file__).resolve().parent.parent / "static" / "admin.html"


@router.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    # 模板管理页面：静态单页，浏览器直连本文件内的管理 API。
    return FileResponse(_ADMIN_PAGE, media_type="text/html")


@router.get("/v1/prompts", response_model=list[PromptTemplateRecord])
async def list_prompts(http: Request, name: str | None = None) -> list[PromptTemplateRecord]:
    # 浏览不鉴权；传 name 则只返回该模板的全部版本。
    return await http.app.state.prompts.list(name)


@router.get("/v1/prompts/{name}/{version}", response_model=PromptTemplateRecord)
async def get_prompt(name: str, version: str, http: Request) -> PromptTemplateRecord:
    return await http.app.state.prompts.require(name, version)


@router.post(
    "/v1/prompts",
    response_model=PromptTemplateRecord,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_prompt(payload: PromptTemplateCreate, http: Request) -> PromptTemplateRecord:
    # 创建模板或新版本（版本不可变）；重复 (name, version) 返回 409。
    return await http.app.state.prompts.create(payload)


@router.post("/v1/prompts/{name}/{version}/render")
async def render_prompt(
    name: str, version: str, payload: PromptRenderRequest, http: Request
) -> Message:
    # 渲染预览：供管理页面在保存前验证模板与变量。
    record = await http.app.state.prompts.require(name, version)
    return http.app.state.prompts.render(record, payload.variables)
