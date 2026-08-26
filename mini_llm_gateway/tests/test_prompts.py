from __future__ import annotations

ADMIN_HEADERS = {"X-Admin-Token": "secret-token"}


def test_admin_page_served(client):
    response = client.get("/admin")
    assert response.status_code == 200
    assert "模板管理" in response.text


def test_seed_template_loaded(client):
    records = client.get("/v1/prompts").json()
    assert any(r["name"] == "agent_code_reviewer" and r["version"] == "v1" for r in records)


def test_create_requires_admin_token(client):
    payload = {"name": "t", "version": "v1", "system_template": "hello"}
    assert client.post("/v1/prompts", json=payload).status_code == 401
    wrong = client.post("/v1/prompts", json=payload, headers={"X-Admin-Token": "bad"})
    assert wrong.status_code == 401


def test_create_and_version_conflict(client):
    payload = {"name": "greeting", "version": "v1", "system_template": "你是{{product}}助手"}
    created = client.post("/v1/prompts", json=payload, headers=ADMIN_HEADERS)
    assert created.status_code == 201

    conflict = client.post("/v1/prompts", json=payload, headers=ADMIN_HEADERS)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "prompt.version_conflict"

    v2 = {**payload, "version": "v2"}
    assert client.post("/v1/prompts", json=v2, headers=ADMIN_HEADERS).status_code == 201
    versions = client.get("/v1/prompts", params={"name": "greeting"}).json()
    assert {r["version"] for r in versions} == {"v1", "v2"}


def test_render_success_and_missing_variable(client):
    payload = {"name": "greeting", "version": "v1", "system_template": "你是{{product}}助手"}
    client.post("/v1/prompts", json=payload, headers=ADMIN_HEADERS)

    ok = client.post("/v1/prompts/greeting/v1/render", json={"variables": {"product": "知识库"}})
    assert ok.status_code == 200
    assert ok.json()["content"] == "你是知识库助手"

    missing = client.post("/v1/prompts/greeting/v1/render", json={"variables": {}})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "prompt.missing_variable"


def test_render_sandbox_blocks_ssti(client):
    payload = {"name": "evil", "version": "v1", "system_template": "{{ ''.__class__.__mro__ }}"}
    assert client.post("/v1/prompts", json=payload, headers=ADMIN_HEADERS).status_code == 201
    blocked = client.post("/v1/prompts/evil/v1/render", json={"variables": {}})
    assert blocked.status_code == 400
    assert blocked.json()["error"]["code"] == "prompt.render_failed"


def test_get_unknown_prompt(client):
    response = client.get("/v1/prompts/nope/v9")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "prompt.unknown_template"
