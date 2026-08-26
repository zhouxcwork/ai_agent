from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    path: str = "data/gateway.db"


class AdminConfig(BaseModel):
    token_env: str = "GATEWAY_ADMIN_TOKEN"


class ModelConfig(BaseModel):
    # 平台模型名到供应商模型、地址、密钥环境变量与能力/价格的映射。
    provider_model: str
    base_url: str
    api_key_env: str
    supports_structured_output: bool = False
    structured_output_mode: str = "json_schema"  # json_schema | json_object
    price_per_million: dict[str, float] = Field(default_factory=dict)


class GatewayConfig(BaseModel):
    database: DatabaseConfig = DatabaseConfig()
    admin: AdminConfig = AdminConfig()
    fallback_model: str = "general-backup"
    models: dict[str, ModelConfig]


def load_config(path: str | Path | None = None) -> GatewayConfig:
    # 从 YAML 读取配置；路径优先级：显式参数 > GATEWAY_CONFIG 环境变量 > 工作目录 config.yaml。
    config_path = Path(path or os.getenv("GATEWAY_CONFIG", "config.yaml"))
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return GatewayConfig.model_validate(raw)
