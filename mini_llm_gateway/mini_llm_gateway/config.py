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


class StructuredOutputConfig(BaseModel):
    # 非流式结构化失败的修复重试边界（ADR 0009）：无损提取修复内置，此处只管上游重试次数。
    max_retries: int = Field(default=2, ge=0, le=5)


class AuthConfig(BaseModel):
    # 调用方认证白名单：apikey → tenantId；为空 = 不启用认证（本地开发）。
    # 后续对接外部鉴权系统时替换这一映射来源即可。
    api_keys: dict[str, str] = Field(default_factory=dict)


class TenantLimitConfig(BaseModel):
    # 单租户资源边界：QPS 令牌桶（容量即突发额度）+ 并发上限，超限快速拒绝。
    qps: float = Field(default=10, gt=0)
    burst: int = Field(default=20, ge=1)
    max_concurrency: int = Field(default=5, ge=1)


class TargetLimitConfig(BaseModel):
    # 单路由目标（供应商/模型）并发上限，对齐上游按模型的并发配额；未配置的目标不限制。
    max_concurrency: int = Field(default=20, ge=1)


class LimitsConfig(BaseModel):
    default: TenantLimitConfig = Field(default_factory=TenantLimitConfig)
    tenants: dict[str, TenantLimitConfig] = Field(default_factory=dict)
    targets: dict[str, TargetLimitConfig] = Field(default_factory=dict)


class GatewayConfig(BaseModel):
    database: DatabaseConfig = DatabaseConfig()
    admin: AdminConfig = AdminConfig()
    auth: AuthConfig = AuthConfig()
    limits: LimitsConfig = LimitsConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    structured_output: StructuredOutputConfig = StructuredOutputConfig()
    # Fallback 切换前每个路由目标的尝试次数（流式与非流式一致）
    attempts_per_target: int = Field(default=2, ge=1)
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
    provider: str
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
