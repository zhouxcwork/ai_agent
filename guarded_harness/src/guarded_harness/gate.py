"""门禁（G1 参数校验 / G2 连续重复检测）。纯逻辑，无 agent 依赖。

拒绝一律返回 GateRejection（含 rule 与面向模型的明确错误文本），不抛异常——
工具层将其转为 observation 让模型自纠。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# 参数 schema：工具名 → {参数名: 类型}，全部必填（受控工具入参本就极简）
ParamSchema = dict[str, dict[str, type]]


@dataclass
class GateRejection:
    rule: str  # gate.param_schema / gate.repeat / gate.unknown_tool
    message: str  # 返回给模型的明确错误（含字段名等定位信息）
    detail: dict | None = None


def fingerprint(tool_name: str, arguments: dict) -> str:
    """(工具, 参数) 规范化指纹，用于重复检测与调用身份比对。"""
    return json.dumps([tool_name, arguments], sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class RepeatGuard:
    """连续重复检测（G2）：同一 (工具, 参数) 连续第 3 次起拦截。

    状态可序列化（Checkpoint.repeat_guard），跨进程恢复后计数不清零。
    """

    def __init__(self, last_fingerprint: str | None = None, consecutive_count: int = 0):
        self.last_fingerprint = last_fingerprint
        self.consecutive_count = consecutive_count

    def observe(self, tool_name: str, arguments: dict) -> GateRejection | None:
        """记录一次调用请求；返回 None 表示放行（前两次），第 3 次起返回拒绝。"""
        fp = fingerprint(tool_name, arguments)
        if fp == self.last_fingerprint:
            self.consecutive_count += 1
        else:
            self.last_fingerprint = fp
            self.consecutive_count = 1
        if self.consecutive_count >= 3:
            return GateRejection(
                rule="gate.repeat",
                message=(
                    f"Rejected: tool '{tool_name}' called with identical arguments 3 times in a row. "
                    "Change your arguments or approach before retrying."
                ),
                detail={"fingerprint": fp, "consecutive_count": self.consecutive_count},
            )
        return None

    def state(self) -> dict:
        return {"last_fingerprint": self.last_fingerprint, "consecutive_count": self.consecutive_count}


class Gate:
    """受控调用入口检查：G1 参数 schema → G2 连续重复。"""

    def __init__(self, schemas: ParamSchema, repeat_guard: RepeatGuard | None = None):
        self.schemas = schemas
        self.repeat_guard = repeat_guard or RepeatGuard()

    def check(self, tool_name: str, arguments: dict | None) -> GateRejection | None:
        if tool_name not in self.schemas:
            return GateRejection(
                rule="gate.unknown_tool",
                message=f"Unknown tool '{tool_name}', allowed: {sorted(self.schemas)}",
            )
        args = arguments if isinstance(arguments, dict) else {}
        schema = self.schemas[tool_name]
        missing = [name for name in schema if name not in args]
        if missing:
            return GateRejection(
                rule="gate.param_schema",
                message=f"Rejected: missing required argument(s) {missing} for tool '{tool_name}'.",
                detail={"missing": missing},
            )
        bad = {}
        for name, expected in schema.items():
            value = args.get(name)
            # bool 是 int 子类，按声明的精确类型拒绝（bool 不冒充 str/int）
            if not isinstance(value, expected) or isinstance(value, bool) and expected is not bool:
                bad[name] = type(value).__name__
        if bad:
            return GateRejection(
                rule="gate.param_schema",
                message=(
                    f"Rejected: wrong argument type for tool '{tool_name}': "
                    + ", ".join(f"{n} expects {schema[n].__name__}, got {t}" for n, t in bad.items())
                ),
                detail={"bad_types": bad},
            )
        return self.repeat_guard.observe(tool_name, args)
