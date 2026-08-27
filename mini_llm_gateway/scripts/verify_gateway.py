#!/usr/bin/env python3
"""交付验收脚本：覆盖网关全部功能点，PASS/FAIL 逐项输出，退出码 0 = 全部通过。

用法：
    python scripts/verify_gateway.py                     # 完整验收（含真实上游调用）
    python scripts/verify_gateway.py --offline           # 只验网关自身功能（无需上游 Key）
    python scripts/verify_gateway.py --base-url http://127.0.0.1:8000 \
        --api-key replace-with-a-long-random-secret --admin-token change-me

前置：网关已启动（uvicorn / PyCharm main.py / docker compose 均可）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        RESULTS.append((name, True, detail or "OK"))
        print(f"  PASS  {name}" + (f"  — {detail}" if detail else ""))
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
        print(f"  FAIL  {name}  — {e}")
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  FAIL  {name}  — {type(e).__name__}: {e}")


class Gateway:
    def __init__(self, base_url: str, api_key: str, admin_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.admin = admin_token

    def request(self, method: str, path: str, body: dict | None = None, *, auth: bool = True, admin: bool = False) -> tuple[int, dict | str]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers |= self.auth
        if admin:
            headers["X-Admin-Token"] = self.admin
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw

    def sse(self, path: str, body: dict) -> tuple[int, list[dict | str]]:
        """按块读取 SSE，解析 data: 行（含 [DONE] 字符串项）。"""
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode(), headers=self.auth, method="POST"
        )
        try:
            events: list[dict | str] = []
            buf = ""
            with urllib.request.urlopen(req, timeout=120) as resp:
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    buf += chunk.decode()
                    while "\n\n" in buf:
                        part, buf = buf.split("\n\n", 1)
                        line = part.strip()
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        events.append(payload if payload == "[DONE]" else json.loads(payload))
                return resp.status, events
        except urllib.error.HTTPError as e:
            e.read()
            return e.code, []


def verify(gw: Gateway, offline: bool) -> None:
    def healthz():
        status, body = gw.request("GET", "/healthz", auth=False)
        assert status == 200 and body.get("status") == "ok", f"status={status}"
        return "status=ok"

    def models():
        status, body = gw.request("GET", "/v1/models")
        assert status == 200, f"status={status}"
        modes = [m["id"] for m in body["data"]]
        assert set(modes) >= {"fast", "smart"}, f"档位缺失: {modes}"
        routes = body.get("gateway_routes", {})
        targets = [r["target"] for rs in routes.values() for r in rs]
        assert any("anthropic" in t for t in targets), f"应有 Anthropic 协议目标: {targets}"
        return f"档位 {modes}，双协议目标 {len(targets)} 个"

    def auth_reject():
        # 无 Bearer 的裸请求 → 401
        req = urllib.request.Request(
            gw.base_url + "/v1/chat/completions",
            data=json.dumps({"model": "fast", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, body = resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            status, body = e.code, json.loads(e.read().decode())
        assert status == 401, f"status={status}"
        assert body["error"]["code"] == "auth.invalid_key", body
        return "401 auth.invalid_key"

    def unknown_mode():
        status, body = gw.request("POST", "/v1/chat/completions", {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
        assert status == 400 and body["error"]["code"] == "route.unknown_mode", f"status={status}"
        return "400 route.unknown_mode"

    def empty_messages():
        status, body = gw.request("POST", "/v1/chat/completions", {"model": "fast", "messages": []})
        assert status == 422 and body["error"]["code"] == "request.validation_error", f"status={status}"
        return "422 request.validation_error"

    def prompts_list():
        status, body = gw.request("GET", "/v1/prompts")
        assert status == 200, f"status={status}"
        names = {p["name"] for p in body}
        assert "agent_code_reviewer" in names, f"seed 模板缺失: {names}"
        return f"{len(body)} 个版本，含 seed agent_code_reviewer"

    def prompt_render():
        status, body = gw.request(
            "POST", "/v1/prompts/agent_code_reviewer/v1/render",
            {"variables": {"language": "python", "project_type": "demo", "focus_areas": "安全",
                           "strictness_level": "P2", "source_code": "print(1)",
                           "focus_security": "true", "focus_reliability": "", "focus_cost": "", "focus_observability": ""}},
        )
        assert status == 200 and body["role"] == "system", f"status={status}"
        return "渲染 200，role=system"

    def prompt_conflict():
        status, body = gw.request(
            "POST", "/v1/prompts",
            {"name": "agent_code_reviewer", "version": "v1", "system_template": "dup"},
            admin=True,
        )
        assert status == 409 and body["error"]["code"] == "prompt.version_conflict", f"status={status}"
        return "409 prompt.version_conflict（版本不可变）"

    def unknown_template():
        status, body = gw.request("POST", "/v1/prompts/no-such/v9/render", {"variables": {}})
        assert status == 400 and body["error"]["code"] == "prompt.unknown_template", f"status={status}"
        return "400 prompt.unknown_template"

    print("== 网关自身功能（无需上游）==")
    check("健康检查 GET /healthz", healthz)
    check("档位与健康 GET /v1/models（含双协议目标）", models)
    check("认证拒绝：无 Bearer → 401 auth.invalid_key", auth_reject)
    check("错误分段：未知档位 → 400 route.unknown_mode", unknown_mode)
    check("错误分段：空 messages → 422 request.validation_error", empty_messages)
    check("模板列表 GET /v1/prompts（含 seed）", prompts_list)
    check("模板渲染 render（Jinja2 变量替换）", prompt_render)
    check("模板版本不可变：重复创建 → 409", prompt_conflict)
    check("错误分段：未知模板 → 400 prompt.unknown_template", unknown_template)

    if offline:
        print("\n--offline 模式：跳过真实上游调用（chat/responses/结构化/模板调用/trace）")
        return

    # ---------- 以下需要服务端配置上游 Key（DEEPSEEK_API_KEY） ----------
    def chat(mode: str):
        status, body = gw.request("POST", "/v1/chat/completions", {"model": mode, "messages": [{"role": "user", "content": "用一个词回答：1+1=?"}]})
        assert status == 200, f"status={status} body={body}"
        assert body["model"] == mode, f"model 应回显档位: {body['model']}"
        content = body["choices"][0]["message"]["content"]
        assert content, "content 为空"
        assert body["usage"]["total_tokens"] > 0, "usage 应有真实 token"
        assert "actual_model" not in body, "actual_model 不应透出（ADR 0015：仅入 Trace）"
        return f"model={body['model']} tokens={body['usage']['total_tokens']}（actual_model 见 Trace）"

    def chat_stream():
        status, events = gw.sse("/v1/chat/completions", {"model": "fast", "messages": [{"role": "user", "content": "数到3"}], "stream": True})
        assert status == 200, f"status={status}"
        deltas = [e["choices"][0]["delta"].get("content") for e in events if isinstance(e, dict) and e.get("choices")]
        text = "".join(d for d in deltas if d)
        assert text, "流式无内容增量"
        assert events and events[-1] == "[DONE]", "缺少 [DONE] 终止符"
        usage = [e for e in events if isinstance(e, dict) and e.get("usage")]
        assert usage and usage[0]["usage"]["total_tokens"] > 0, "末块无真实 usage"
        return f"{len(deltas)} 增量，末块 usage tokens={usage[0]['usage']['total_tokens']}"

    def structured():
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
        status, body = gw.request("POST", "/v1/chat/completions", {
            "model": "fast",
            "messages": [{"role": "user", "content": "回答：1+1 等于几"}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}},
        })
        assert status == 200, f"status={status} body={body}"
        # 调用方视角验收两层保证：响应 content 本身就是合法 JSON 且通过 Schema
        parsed = json.loads(body["choices"][0]["message"]["content"])
        assert "answer" in parsed, f"Schema 校验失败: {parsed}"
        return f"content 即合法 JSON（fast 档走 Anthropic tool_use）: {json.dumps(parsed, ensure_ascii=False)[:40]}"

    def prompt_call():
        status, body = gw.request("POST", "/v1/chat/completions", {
            "model": "smart",
            "messages": [{"role": "user", "content": "审查：def add(a, b): return a + b"}],
            "timeout_seconds": 120,  # 大模板 + thinking 模型可能超过默认 30s
            "gateway_prompt": {"name": "agent_code_reviewer", "version": "v1",
                               "variables": {"language": "python", "project_type": "demo", "focus_areas": "安全",
                                             "strictness_level": "P3", "source_code": "def add(a, b): return a + b",
                                             "focus_security": "true", "focus_reliability": "", "focus_cost": "", "focus_observability": ""}},
        })
        assert status == 200, f"status={status} body={str(body)[:200]}"
        assert body["usage"]["total_tokens"] > 0, "usage 应有真实 token"
        return f"tokens={body['usage']['total_tokens']}（模板渲染后经 smart 档调用）"

    def responses_flow():
        status, body = gw.request("POST", "/v1/responses", {"model": "smart", "input": "我叫小明，记住这个名字"})
        assert status == 200 and body["status"] == "completed", f"status={status}"
        resp_id = body["id"]
        text = "".join(c["text"] for o in body.get("output", []) for c in o.get("content", []) if c.get("type") == "output_text")
        assert text, "output 无文本"
        status2, body2 = gw.request("POST", "/v1/responses", {"model": "smart", "input": "我叫什么？", "previous_response_id": resp_id})
        assert status2 == 200, f"续接失败 status={status2} body={body2}"
        text2 = "".join(c["text"] for o in body2.get("output", []) for c in o.get("content", []) if c.get("type") == "output_text")
        assert "小明" in text2, f"续接未记住上下文: {text2[:60]}"
        return f"续接成功（第一轮 id={resp_id[:12]}… 回复含上下文）"

    def responses_stream():
        status, events = gw.sse("/v1/responses", {"model": "smart", "input": "hi", "stream": True})
        assert status == 200, f"status={status}"
        types = [e["type"] for e in events if isinstance(e, dict)]
        assert "response.created" in types and "response.output_text.delta" in types, f"事件不全: {types}"
        assert types[-1] == "response.completed", f"末事件非 completed: {types[-1]}"
        return f"事件链 {' → '.join(types)}"

    def traces():
        status, body = gw.request("GET", "/v1/traces?limit=10")
        assert status == 200 and body, "无 trace 记录"
        t = body[0]
        for field in ("requested_model", "actual_model", "input_tokens", "output_tokens",
                      "cached_tokens", "reasoning_tokens", "ttft_ms", "cost_usd", "latency_ms", "status", "endpoint"):
            assert field in t, f"trace 缺字段 {field}"
        assert t["input_tokens"] > 0, "token 分类统计缺失"
        assert t["actual_model"], "actual_model 应记入 Trace（ADR 0015：不透出、仅入 Trace）"
        assert any(x.get("ttft_ms") is not None for x in body), "流式调用应记录 ttft_ms（非流式为空）"
        return f"最近记录 {t['endpoint']} {t['requested_model']}→{t['actual_model']} in/out={t['input_tokens']}/{t['output_tokens']} cost=${t['cost_usd']:.5f} status={t['status']}"

    def retry_exhaustion():
        # 0.1s 超时确定性触发上游超时（连接都不够建立）→ 同目标指数退避重试（0.2/0.4s，ADR 0014）→ 耗尽 503
        status, body = gw.request("POST", "/v1/chat/completions", {
            "model": "fast", "messages": [{"role": "user", "content": "hi"}], "timeout_seconds": 0.1,
        })
        assert status == 503, f"status={status} body={body}"
        assert body["error"]["code"] == "route.model_unavailable", body
        time.sleep(0.2)
        _, latest = gw.request("GET", "/v1/traces?limit=1")
        t = latest[0]
        assert t["attempts"] == 3, f"应共 3 次尝试（初次+2 次重试）: {t['attempts']}"
        assert t["status"] == "failed" and t["error_code"] == "route.model_unavailable"
        # 3 次 0.1s 超时 ≈ 0.3s；含指数退避（0.2+0.4）总耗时 >0.5s 才能证明退避真实发生
        assert t["latency_ms"] > 500, f"应含退避等待 0.6s（纯尝试仅 ~0.3s）: {t['latency_ms']}ms"
        return f"attempts=3（初次+2次重试）latency={t['latency_ms']}ms（含 0.2+0.4s 指数退避）"

    def rate_limit():
        # 并发爆发触发租户资源边界（burst=40/并发=10）：请求打满后 429 快速拒绝（零上游成本）
        import concurrent.futures

        def fire(_):
            req = urllib.request.Request(
                gw.base_url + "/v1/chat/completions",
                data=json.dumps({"model": "no-such-mode", "messages": [{"role": "user", "content": "x"}]}).encode(),
                headers=gw.auth, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.status, ""
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read().decode())["error"]["code"]
                except Exception:  # noqa: BLE001
                    return e.code, ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=45) as ex:
            results = list(ex.map(fire, range(45)))
        limited = [code for s, code in results if s == 429 and code.startswith("resource.")]
        assert limited, f"45 并发应触发限流 429，实际: {results[:5]}"
        return f"45 并发中 {len(limited)} 个 429（首见 {limited[0]}）"

    print("\n== 真实上游调用（需服务端 DEEPSEEK_API_KEY）==")
    check("Chat 非流式 fast（Anthropic 协议路径）", lambda: chat("fast"))
    check("Chat 非流式 smart（Responses 协议路径）", lambda: chat("smart"))
    check("Chat 流式（SSE + usage 末块 + [DONE]）", chat_stream)
    check("结构化输出（json_schema → parsed，两层保证）", structured)
    check("模板调用（gateway_prompt agent_code_reviewer/v1）", prompt_call)
    check("Responses 非流式 + 会话续接（记住上下文）", responses_flow)
    check("Responses 流式（类型化事件链）", responses_stream)
    check("Trace 审计（token 分类/TTFT/cost/状态字段齐全）", traces)
    check("指数退避重试（1s 超时 → 3 次尝试耗尽 503）", retry_exhaustion)
    check("限流 429（并发爆发触发租户资源边界）", rate_limit)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mini LLM Gateway 交付验收脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default="replace-with-a-long-random-secret")
    parser.add_argument("--admin-token", default="change-me")
    parser.add_argument("--offline", action="store_true", help="跳过真实上游调用")
    args = parser.parse_args()

    print(f"验收目标: {args.base_url}（--offline={args.offline}）\n")
    gw = Gateway(args.base_url, args.api_key, args.admin_token)
    started = time.time()
    verify(gw, args.offline)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n== 结果: {passed}/{len(RESULTS)} 通过，耗时 {time.time() - started:.1f}s ==")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name}: {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
