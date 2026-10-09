"""Health, configuration, missing API key behaviour, pages and access control."""
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import get_case, make_settings, submit


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["policy_index_ready"] is True
    assert body["policy_sections"] > 50
    assert body["llm"]["state"] == "demo"


def test_all_pages_render(client):
    for path in ["/", "/cases/new", "/cases", "/policies", "/settings", "/cases/PG-NOPE"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Fictional data" in r.text


def test_missing_api_key_falls_back_to_labelled_demo_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    s = make_settings(tmp_path, llm_provider="gemini")
    with TestClient(create_app(s)) as c:
        status = c.get("/health").json()["llm"]
        assert status["state"] == "missing_key"
        assert status["live"] is False
        assert "GEMINI_API_KEY" in status["message"]
        assert "AI not configured" in c.get("/").text
        res = submit(c, "S1")
        data = get_case(c, res["case_id"])
        assert data["case"]["mode"] == "demo"
        # No agent may claim to be AI-generated when no model is configured.
        for a in data["agents"]:
            assert a["mode"] in ("simulated", "rules")
            assert a["output"]["narrative_source"] in ("simulated", "rules")


def test_unknown_provider_reported(tmp_path):
    s = make_settings(tmp_path, llm_provider="banana")
    assert s.llm_status()["state"] == "error"
    assert s.llm_enabled is False


def test_api_key_never_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    s = make_settings(tmp_path, llm_provider="gemini")
    with TestClient(create_app(s)) as c:
        for path in ["/health", "/api/config", "/settings", "/"]:
            assert "super-secret-value" not in c.get(path).text


def test_access_code_gate(tmp_path):
    s = make_settings(tmp_path, access_code="letmein", secret_key="k")
    with TestClient(create_app(s)) as c:
        assert c.get("/health").status_code == 200  # health stays open for the host
        assert c.get("/api/cases").status_code == 401
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        bad = c.post("/login", data={"code": "wrong", "next": "/"}, follow_redirects=False)
        assert "error=1" in bad.headers["location"]
        ok = c.post("/login", data={"code": "letmein", "next": "/"}, follow_redirects=False)
        assert ok.status_code == 303
        assert c.get("/api/cases").status_code == 200
