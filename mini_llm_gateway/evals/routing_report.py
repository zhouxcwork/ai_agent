"""路由质量离线报告：从 traces 表统计各档位的成功率、fallback 率与路由目标表现。

fallback 率口径：attempts > 1 的请求占比（含同目标重试与候选切换，结构化修复重试同样推进 attempts）。

用法：
    uv run python evals/routing_report.py --db data/gateway.db [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3


def load_traces(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT requested_model, actual_model, latency_ms, cost_usd, attempts, status, error_code "
            "FROM llm_call_traces"
        ).fetchall()
    return [dict(row) for row in rows]


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def build_report(traces: list[dict]) -> dict:
    modes: dict[str, dict] = {}
    for trace in traces:
        mode = modes.setdefault(
            trace["requested_model"],
            {"requests": 0, "success": 0, "latencies": [], "attempts": [], "targets": {}},
        )
        mode["requests"] += 1
        if trace["status"] == "success":
            mode["success"] += 1
            target = mode["targets"].setdefault(
                trace["actual_model"], {"requests": 0, "latencies": [], "cost_usd": 0.0}
            )
            target["requests"] += 1
            target["latencies"].append(trace["latency_ms"])
            target["cost_usd"] += trace["cost_usd"]
        mode["latencies"].append(trace["latency_ms"])
        mode["attempts"].append(trace["attempts"])

    report: dict = {"total_traces": len(traces), "modes": {}, "failures": {}}
    for name, mode in sorted(modes.items()):
        requests = mode["requests"]
        report["modes"][name] = {
            "requests": requests,
            "success_rate": round(mode["success"] / requests, 3),
            "avg_latency_ms": _avg(mode["latencies"]),
            "fallback_rate": round(sum(1 for a in mode["attempts"] if a > 1) / requests, 3),
            "avg_attempts": _avg(mode["attempts"]),
            "targets": {
                key: {
                    "requests": t["requests"],
                    "avg_latency_ms": _avg(t["latencies"]),
                    "cost_usd_total": round(t["cost_usd"], 4),
                }
                for key, t in mode["targets"].items()
            },
        }
    for trace in traces:
        if trace["status"] == "failed" and trace["error_code"]:
            report["failures"][trace["error_code"]] = report["failures"].get(trace["error_code"], 0) + 1
    return report


def render_text(report: dict) -> str:
    lines = [f"路由质量报告（{report['total_traces']} 条 trace）"]
    for name, m in report["modes"].items():
        lines.append(
            f"  档位 {name}: {m['requests']} 次, 成功率 {m['success_rate']:.1%}, "
            f"fallback 率 {m['fallback_rate']:.1%}, 平均尝试 {m['avg_attempts']}, "
            f"平均延迟 {m['avg_latency_ms']}ms"
        )
        for key, t in m["targets"].items():
            lines.append(
                f"    {key}: 承接 {t['requests']} 次, 平均延迟 {t['avg_latency_ms']}ms, "
                f"成本 ${t['cost_usd_total']}"
            )
    if report["failures"]:
        lines.append(f"  失败分布: {report['failures']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="基于 traces 的路由质量离线报告")
    parser.add_argument("--db", default="data/gateway.db", help="traces 数据库路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非文本摘要")
    args = parser.parse_args()
    report = build_report(load_traces(args.db))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report))


if __name__ == "__main__":
    main()
