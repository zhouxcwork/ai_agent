from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class DatabaseConfig(BaseModel):
    path: str = "data/gateway.db"


class AdminConfig(BaseModel):
    token_env: str = "GATEWAY_ADMIN_TOKEN"


class ProviderConfig(BaseModel):
    # 供应商连接信息：OpenAI 兼容地址 + 密钥环境变量名（真实密钥只在进程环境里）。
    base_url: str
    api_key_env: str
    enabled: bool = True


class RouteTarget(BaseModel):
    # 档位候选池中的一项：供应商模型 + 权重 + 能力 + 单价；健康度以此为粒度。
    provider: str
    model: str
    weight: int = Field(default=1, ge=1)
    supports_structured_output: bool = False
    structured_output_mode: str = "json_schema"  # json_schema | json_object
    price_per_million: dict[str, float] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"


class ModeConfig(BaseModel):
    # 档位：调用方在 model 字段表达的意图，候选池按权重轮询选路。
    strategy: str = "weighted_round_robin"
    targets: list[RouteTarget] = Field(min_length=1)


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = Field(default=5, ge=1)
    cooldown_seconds: float = Field(default=30, ge=0)


class GatewayConfig(BaseModel):
    database: DatabaseConfig = DatabaseConfig()
    admin: AdminConfig = AdminConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    providers: dict[str, ProviderConfig]
    modes: dict[str, ModeConfig]

    @model_validator(mode="after")
    def validate_routes(self) -> GatewayConfig:
        unknown = {
            target.provider
            for mode in self.modes.values()
            for target in mode.targets
            if target.provider not in self.providers
        }
        if unknown:
            raise ValueError(f"modes 引用了未定义的 providers: {sorted(unknown)}")
        return self


class ResolvedTarget(BaseModel):
    # 路由决策产物：路由目标 + 供应商连接信息合一，供供应商适配层与成本计算使用。
    provider_model: str
    key: str  # 供应商/模型，对外即 actual_model
    base_url: str
    api_key_env: str
    supports_structured_output: bool
    structured_output_mode: str
    price_per_million: dict[str, float]


def load_config(path: str | Path | None = None) -> GatewayConfig:
    # 从 YAML 读取配置；路径优先级：显式参数 > GATEWAY_CONFIG 环境变量 > 工作目录 config.yaml。
    config_path = Path(path or os.getenv("GATEWAY_CONFIG", "config.yaml"))
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return GatewayConfig.model_validate(raw)
