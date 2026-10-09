"""Shared test fixtures. Every test uses a temporary database and demo mode
unless it explicitly configures a (mocked) language model."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.retrieval.index import policy_index  # noqa: E402
from app.services.llm import LLMClient  # noqa: E402

SAMPLES = json.loads((ROOT / "data" / "sample_cases" / "sample_cases.json").read_text(encoding="utf-8"))["cases"]


def make_settings(tmp_path: Path, **overrides) -> Settings:
    s = Settings()
    s.llm_provider = "mock"
    s.llm_model = ""
    s.database_path = tmp_path / "test.db"
    s.policy_dir = ROOT / "data" / "policies"
    s.access_code = ""
    s.admin_token = ""
    s.llm_min_interval_seconds = 0
    s.llm_max_retries = 1
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.fixture(scope="session", autouse=True)
def _index():
    policy_index.build(ROOT / "data" / "policies")
    return policy_index


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def sample(sample_id: str) -> dict:
    return next(s for s in SAMPLES if s["id"] == sample_id)


def submit(client: TestClient, sample_id: str | None = None, **fields) -> dict:
    """Create a case synchronously (the workflow finishes before returning)."""
    data = {}
    if sample_id:
        s = sample(sample_id)
        data.update({k: v for k, v in s["input"].items()})
        data["sample_case_id"] = s["id"]
        if s.get("document"):
            data["sample_document"] = s["document"]
    data.update(fields)
    data.setdefault("sync", "true")
    r = client.post("/api/cases", data=data)
    assert r.status_code == 201, r.text
    return r.json()


def get_case(client: TestClient, case_id: str) -> dict:
    r = client.get(f"/api/cases/{case_id}")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Fake language model (Gemini wire format) for testing live-AI behaviour
# without network access or an API key.
# ---------------------------------------------------------------------------


def gemini_reply(payload: dict | str, status: int = 200, headers: dict | None = None) -> httpx.Response:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    body = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return httpx.Response(status, json=body if status == 200 else {"error": {"message": "x"}}, headers=headers or {})


def agent_of(request: httpx.Request) -> str:
    body = json.loads(request.content)
    system = body["systemInstruction"]["parts"][0]["text"]
    for name in ("Employee Relations Orchestrator", "HR Policy Expert", "Employee Relations Case Analyst",
                 "Labour Compliance Reviewer", "Case Documentation Specialist", "Quality & Risk Gate"):
        if f"You are the {name} agent" in system:
            return name
    raise AssertionError("unknown agent prompt")


def user_prompt(request: httpx.Request) -> str:
    return json.loads(request.content)["contents"][0]["parts"][0]["text"]


@pytest.fixture
def live_client(tmp_path, monkeypatch):
    """Factory: live_client(handler) -> TestClient wired to a fake Gemini endpoint."""
    clients = []

    def factory(handler):
        monkeypatch.setenv("GEMINI_API_KEY", "test-secret-key-123")
        s = make_settings(tmp_path, llm_provider="gemini")
        app = create_app(s)
        app.state.llm = LLMClient(s, transport=httpx.MockTransport(handler))
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield factory
    for c in clients:
        c.__exit__(None, None, None)
