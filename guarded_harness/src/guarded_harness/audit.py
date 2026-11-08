"""审计流（A1/A2）：JSONL 追加写，run_id/step/toolCallId 贯穿，按 run 聚合查询。

两阶段审计（PLAN §6.1/6.2）：执行 INTENT 与 EXECUTED 分开落盘；
恢复对齐（§6.2.1）依赖尾部完整性——日志损坏时拒绝恢复而非猜测。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .types import Actor, AuditRecord, Decision


class AuditCorruptionError(Exception):
    """日志无法完整解析（半写/损坏）。恢复路径必须停止，不得猜测执行状态。"""


class AuditLog:
    def __init__(self, path: Path):
        self.failed = False
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: AuditRecord) -> int:
        """追加一条记录（行 flush + fsync），返回其行号（1 起，即 journal seq）。"""
        line = json.dumps(
            {
                "run_id": record.run_id,
                "step": record.step,
                "tool_call_id": record.tool_call_id,
                "rule": record.rule,
                "decision": record.decision.value,
                "actor": record.actor.value,
                "detail": record.detail,
                "timestamp": record.timestamp,
            },
            ensure_ascii=False,
        )
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return self.count()
        except OSError:
            self.failed = True
            raise

    def count(self) -> int:
        """当前总行数（作为 journal_cursor 的自然定义：已消费行数上限）。"""
        if not self.path.exists():
            return 0
        with open(self.path, "rb") as fh:
            return sum(1 for _ in fh)

    def read_all(self, strict: bool = False) -> list[tuple[int, AuditRecord]]:
        """读取全部记录，返回 (行号, 记录)。strict=True 时尾部不完整即抛 AuditCorruptionError。"""
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            text = fh.read()
        records: list[tuple[int, AuditRecord]] = []
        if text and not text.endswith("\n"):
            if strict:
                raise AuditCorruptionError("audit journal has a torn tail (no trailing newline)")
            text = text[: text.rfind("\n") + 1] if "\n" in text else ""
        for i, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                records.append(
                    (
                        i,
                        AuditRecord(
                            run_id=d["run_id"],
                            step=d["step"],
                            tool_call_id=d["tool_call_id"],
                            rule=d["rule"],
                            decision=Decision(d["decision"]),
                            actor=Actor(d["actor"]),
                            detail=d.get("detail", ""),
                            timestamp=d.get("timestamp", 0.0),
                        ),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                # 非 JSON / 合法 JSON 但缺字段或枚举值非法：一律视为损坏行
                if strict:
                    raise AuditCorruptionError(f"audit journal line {i} is corrupt") from None
                continue
        return records

    def records_for_run(self, run_id: str) -> list[AuditRecord]:
        return [rec for _, rec in self.read_all() if rec.run_id == run_id]

    def tail_for_run(self, run_id: str, after_line: int) -> list[tuple[int, AuditRecord]]:
        """同一 run 在 after_line（journal_cursor）之后的记录——恢复对齐用。"""
        return [(line, rec) for line, rec in self.read_all(strict=True) if rec.run_id == run_id and line > after_line]
