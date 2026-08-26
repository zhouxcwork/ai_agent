from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from mini_llm_gateway import __version__
from mini_llm_gateway.api import llm_routes, prompt_routes, trace_routes
from mini_llm_gateway.config import GatewayConfig, load_config
from mini_llm_gateway.errors import GatewayError, gateway_error_handler
from mini_llm_gateway.provider.openai_compatible import OpenAICompatibleProvider
from mini_llm_gateway.provider.base import Provider
from mini_llm_gateway.repository.prompt_repository import PromptRepository
from mini_llm_gateway.repository.trace_repository import TraceRepository
from mini_llm_gateway.schemas.prompt import PromptTemplateCreate
from mini_llm_gateway.service.gateway_service import GatewayService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("llm_gateway")

SEED_TEMPLATES: list[tuple[str, str, str]] = [
    # (name, version, 包内种子文件路径)：启动时若不存在则写入，保证开箱即用。
    ("agent_code_reviewer", "v1", "seeds/agent_code_reviewer.jinja2"),
]


def _seed_file(relative_path: str) -> str:
    path = Path(__file__).resolve().parent / relative_path
    return path.read_text(encoding="utf-8")


async def seed_prompt_templates(prompts: PromptRepository) -> None:
    for name, version, relative_path in SEED_TEMPLATES:
        if not await prompts.exists(name, version):
            await prompts.create(PromptTemplateCreate(name=name, version=version, system_template=_seed_file(relative_path)))
            logger.info("seeded prompt template %s/%s", name, version)


def create_app(config: GatewayConfig | None = None, provider: Provider | None = None) -> FastAPI:
    gateway_config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        prompts = PromptRepository(gateway_config.database.path)
        traces = TraceRepository(gateway_config.database.path)
        await prompts.initialize()
        await traces.initialize()
        await seed_prompt_templates(prompts)

        app.state.config = gateway_config
        app.state.prompts = prompts
        app.state.traces = traces
        app.state.gateway = GatewayService(
            gateway_config, provider or OpenAICompatibleProvider(), prompts, traces
        )
        yield

    app = FastAPI(title="Mini LLM Gateway", version=__version__, lifespan=lifespan)
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.include_router(llm_routes.router)
    app.include_router(trace_routes.router)
    app.include_router(prompt_routes.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
