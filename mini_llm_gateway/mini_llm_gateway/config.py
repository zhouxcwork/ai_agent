from __future__ import annotations

import os
from pathlib import Path

from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class DatabaseConfig(BaseModel):
    path: str = "data/gateway.db"


class AdminConfig(BaseModel):
    token_env: str = "GATEWAY_ADMIN_TOKEN"


class ProviderConfig(BaseModel):
    # 协议端点连接：供应商经某协议接入的地址 + 密钥环境变量名（真实密钥只在进程环境里）。
    # 同一供应商可平铺多条连接（如 deepseek-anthropic / deepseek-responses 共用一个 Key），ADR 0013。
    base_url: str
    api_key_env: str
    protocol: Literal["openai", "anthropic", "responses"] = "openai"
    enabled: bool = True


class RouteTarget(BaseModel):
    # 档位候选池中的一项：供应商模型 + 权重 + 能力 + 单价；健康度以此为粒度。
    provider: str
    model: str
    weight: int = Field(default=1, ge=1)
    supports_structured_output: bool = False
    structured_output_mode: str = "json_schema"  # json_schema | json_object | tool_use（Anthropic，ADR 0013）
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
    # 单路由目标（供应商/模型）资源边界：并发上限 + 可选 QPS 令牌桶（ADR 0011/0013 作业扩展）；
    # 未配置的目标不限制；burst 缺省按 qps 取整（1 秒突发额度）。
    max_concurrency: int = Field(default=20, ge=1)
    qps: float | None = Field(default=None, gt=0)
    burst: int | None = Field(default=None, ge=1)


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
    # Fallback 切换前每个路由目标的重试次数（不含初次尝试，ADR 0014）；流式与非流式一致
    retries_per_target: int = Field(default=2, ge=0)
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
    # 路由决策产物：路由目标 + 协议端点连接信息合一，供供应商适配层与成本计算使用。
    provider: str
    provider_model: str
    key: str  # 供应商/模型，即 trace 里的 actual_model（不对外透出，ADR 0015）
    protocol: Literal["openai", "anthropic", "responses"] = "openai"
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
