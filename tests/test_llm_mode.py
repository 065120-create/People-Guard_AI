"""Live-AI behaviour, tested against a fake Gemini endpoint (no network, no key).

These prove the plumbing (prompting, JSON validation, retries, fallbacks and
guardrails). They do NOT measure the quality of any real model.
"""
import httpx
import pytest

from app.services.llm import LLMClient, LLMMalformedResponse, extract_json
from conftest import agent_of, gemini_reply, get_case, make_settings, submit, user_prompt


def good_handler(request: httpx.Request) -> httpx.Response:
    agent = agent_of(request)
    prompt = user_prompt(request)
    if agent == "Employee Relations Orchestrator":
        return gemini_reply({"suggested_category": "wages_overtime", "category_rationale": "Pay issue.", "additional_sensitivity_flags": [], "neutral_issue_statement": "The employee reports that recent overtime may not have been paid."})
    if agent == "HR Policy Expert":
        return gemini_reply({"findings": [{"chunk_id": "3-3-overtime", "relevance": "high", "how_it_applies": "Sets the overtime recording and verification standard."},
                                          {"chunk_id": "99-1-invented", "relevance": "high", "how_it_applies": "Invented."}],
                             "gaps": [], "policy_vs_law_notes": []})
    if agent == "Employee Relations Case Analyst":
        return gemini_reply({"neutral_summary": "The reporter states that additional hours were worked and not paid.", "allegations": ["The reporter states that they stayed late on several dates."],
                             "documentary_evidence": [], "unresolved_questions": ["Were the hours recorded?"], "missing_information": ["Shift roster"],
                             "follow_up_questions": ["Which dates are concerned?"], "conflicts_of_interest": [], "recommended_process": []})
    if agent == "Labour Compliance Reviewer":
        return gemini_reply({"issues": [{"topic": "Overtime pay", "framework_chunk_id": "", "why_relevant": "x", "facts_needed": []}], "escalate_to_legal": False, "escalation_reason": ""})
    if agent == "Case Documentation Specialist":
        return gemini_reply({"executive_summary": "Overtime concern reported; records must be checked. Under Section 59 overtime is paid at twice the rate.", "recommended_next_steps": ["Collect the payslip."]})
    if agent == "Quality & Risk Gate":
        assert "Section 59" not in prompt  # gate's own corrections happen before the AI reviewer sees the draft
        return gemini_reply({"concerns": ["Confirm the employee category before payroll review."]})
    raise AssertionError(agent)


def test_live_mode_runs_and_labels_ai_output(live_client):
    c = live_client(good_handler)
    assert c.get("/health").json()["llm"]["state"] == "live"
    data = get_case(c, submit(c, "S1")["case_id"])
    assert data["case"]["mode"] == "live"
    for a in data["agents"]:
        assert a["status"] == "completed", (a["agent"], a["error"])
        assert a["mode"] == "llm" and a["output"]["narrative_source"] == "llm"
        assert a["model"] == "gemini-2.5-flash"


def test_live_mode_guardrails_apply(live_client):
    c = live_client(good_handler)
    data = get_case(c, submit(c, "S1")["case_id"])
    agents = {a["agent"]: a["output"] for a in data["agents"]}
    # Policy Expert rejects a citation that was not retrieved
    assert "99-1-invented" in agents["policy_expert"]["rejected_citations"]
    rep = data["case"]["report"]
    assert all(p["chunk_id"] != "99-1-invented" for p in rep["policy_provisions"])
    # Gate removes the invented statute the model wrote into the summary
    assert "Section 59" not in rep["executive_summary"]
    assert data["case"]["gate"]["verdict"] == "corrected"
    # AI reviewer concerns are added, never used to clear checks
    assert data["case"]["gate"]["ai_concerns"] == ["Confirm the employee category before payroll review."]


def test_llm_cannot_remove_posh_routing(live_client):
    def handler(request):
        agent = agent_of(request)
        if agent == "Employee Relations Orchestrator":
            return gemini_reply({"suggested_category": "manager_conduct", "category_rationale": "Just rudeness.", "additional_sensitivity_flags": [], "neutral_issue_statement": "Workplace conduct concern."})
        if agent == "Labour Compliance Reviewer":
            return gemini_reply({"issues": [], "escalate_to_legal": False, "escalation_reason": ""})
        return good_handler(request)

    c = live_client(handler)
    res = submit(c, "S4")
    case = get_case(c, res["case_id"])["case"]
    assert case["route"] == "posh_ic"
    assert case["report"]["compliance"]["escalate_to_legal"] is True
    assert any(e["to"] == "IC / designated statutory body" for e in case["report"]["escalation"]["items"])


def test_malformed_json_falls_back_and_is_labelled(live_client):
    def handler(request):
        if agent_of(request) == "Employee Relations Case Analyst":
            return gemini_reply("Sure! Here is my analysis: the employee is right.")
        return good_handler(request)

    c = live_client(handler)
    data = get_case(c, submit(c, "S1")["case_id"])
    analyst = next(a for a in data["agents"] if a["agent"] == "er_case_analyst")
    assert analyst["status"] == "fallback"
    assert analyst["mode"] == "rules"
    assert analyst["output"]["narrative_source"] == "rules_fallback"
    assert analyst["attempts"] == 0 or analyst["error"]
    gate = data["case"]["gate"]
    assert next(ch for ch in gate["checks"] if ch["check"] == "Agent completion")["status"] == "warn"


def test_schema_mismatch_is_treated_as_malformed(live_client):
    def handler(request):
        if agent_of(request) == "Case Documentation Specialist":
            return gemini_reply({"summary": "missing the required field"})
        return good_handler(request)

    c = live_client(handler)
    data = get_case(c, submit(c, "S2")["case_id"])
    doc = next(a for a in data["agents"] if a["agent"] == "documentation_specialist")
    assert doc["status"] == "fallback"
    assert "schema" in doc["error"]


def test_rate_limit_handled(live_client):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"message": "quota"}}, headers={"retry-after": "0"})

    c = live_client(handler)
    data = get_case(c, submit(c, "S2")["case_id"])
    assert all(a["status"] == "fallback" for a in data["agents"])
    assert "rate limit" in data["agents"][0]["error"]
    assert calls["n"] == 12  # 6 agents x (1 try + 1 retry)
    assert data["case"]["report"] is not None  # rule-based report still produced, clearly labelled


def test_bad_key_error_message_has_no_secret(live_client):
    def handler(request):
        return httpx.Response(403, text="API key test-secret-key-123 invalid")

    c = live_client(handler)
    data = get_case(c, submit(c, "S1")["case_id"])
    err = data["agents"][0]["error"]
    assert "rejected the API key" in err
    assert "test-secret-key-123" not in str(data)


def test_untrusted_text_is_wrapped_in_prompts(live_client):
    seen = []

    def handler(request):
        seen.append(user_prompt(request))
        return good_handler(request)

    c = live_client(handler)
    submit(c, "S7")
    joined = "\n".join(seen)
    assert "<untrusted_case_text>" in joined and "<untrusted_document" in joined
    assert "ravi.k@example.com" not in joined and "9876543210" not in joined  # PII redacted before reaching the model


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": 2} thanks') == {"a": 2}
    with pytest.raises(LLMMalformedResponse):
        extract_json("no json here")
    with pytest.raises(LLMMalformedResponse):
        extract_json("[1, 2]")


def test_openai_compatible_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    s = make_settings(tmp_path, llm_provider="groq")

    def handler(request):
        assert request.url.host == "api.groq.com"
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    res = LLMClient(s, transport=httpx.MockTransport(handler)).complete_json("sys", "user")
    assert res.data == {"ok": True}
