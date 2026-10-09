"""The Quality & Risk Gate must catch problems, not approve everything."""
import copy

import pytest

from app.agents.base import WorkflowContext
from app.agents.quality_gate import QualityGate
from app.config import Settings
from app.services.llm import LLMClient
from conftest import get_case, submit


def _ctx_for(client, case_id, _index):
    data = get_case(client, case_id)
    full = client.app.state.db.get_case(client.cookies.get("pg_ws"), case_id)
    outputs = {a["agent"]: a["output"] for a in data["agents"]}
    ctx = WorkflowContext(case=dict(full, _agent_status={a["agent"]: a["status"] for a in data["agents"]}), index=_index,
                          llm=LLMClient(Settings()), documents=full.get("documents") or [], settings=client.app.state.settings)
    ctx.outputs = outputs
    return ctx, copy.deepcopy(outputs["documentation_specialist"]["report"])


def _check(results, name):
    return next(c for c in results["checks"] if c["check"] == name)


def test_clean_report_is_not_blocked(client, _index):
    cid = submit(client, "S2")["case_id"]
    gate = get_case(client, cid)["case"]["gate"]
    assert gate["verdict"] in ("passed", "warnings")
    assert gate["requires_human_review"] is True
    assert len(gate["checks"]) >= 10


def test_fabricated_and_unretrieved_citations_removed(client, _index):
    ctx, report = _ctx_for(client, submit(client, "S1")["case_id"], _index)
    report["policy_provisions"].append({"chunk_id": "99-9-fake", "ref": "§99.9 Fake", "quote": "x", "retrieved_by": "x"})
    report["policy_provisions"].append({"chunk_id": "11-2-termination-and-retrenchment", "ref": "§11.2 Termination and retrenchment", "quote": "No termination", "retrieved_by": "x"})
    res = QualityGate().check(report, ctx)
    refs = [p["ref"] for p in res["report"]["policy_provisions"]]
    assert "§99.9 Fake" not in refs and "§11.2 Termination and retrenchment" not in refs
    assert _check(res, "Citations match retrieved material")["status"] == "corrected"


def test_altered_quote_replaced(client, _index):
    ctx, report = _ctx_for(client, submit(client, "S1")["case_id"], _index)
    report["policy_provisions"][0]["quote"] = "Overtime is always paid at double the rate."
    res = QualityGate().check(report, ctx)
    assert "double the rate" not in res["report"]["policy_provisions"][0]["quote"]
    assert _check(res, "Policy quotes are verbatim")["status"] == "corrected"


@pytest.mark.parametrize("sentence", [
    "The manager is guilty of harassment.",
    "The employee should be terminated.",
    "It is confirmed that the manager shouted at staff.",
    "The employee was underpaid.",
    "The employee abandoned his job.",
])
def test_judgmental_language_removed(client, _index, sentence):
    ctx, report = _ctx_for(client, submit(client, "S3")["case_id"], _index)
    report["executive_summary"] += " " + sentence
    res = QualityGate().check(report, ctx)
    assert sentence not in res["report"]["executive_summary"]
    assert "Removed by Quality & Risk Gate" in res["report"]["executive_summary"]
    assert res["verdict"] == "corrected"


@pytest.mark.parametrize("sentence", [
    "Under Section 59 the rate is double.",
    "The Factories Act, 1948 requires payment within 7 days.",
    "Overtime is payable at 200% of wages.",
    "The employee is owed ₹4,500.",
])
def test_invented_legal_specifics_removed(client, _index, sentence):
    ctx, report = _ctx_for(client, submit(client, "S1")["case_id"], _index)
    report["next_steps"].append({"step": sentence, "owner": "Payroll / Benefits", "ref": "", "chunk_id": None})
    res = QualityGate().check(report, ctx)
    assert all(sentence not in s["step"] for s in res["report"]["next_steps"])
    assert _check(res, "No invented legal or policy specifics")["status"] == "corrected"


def test_legal_names_present_in_handbook_are_allowed(client, _index):
    ctx, report = _ctx_for(client, submit(client, "S4")["case_id"], _index)
    s = "Verify the matter under the Code on Wages, 2019 framework named in the handbook."
    report["executive_summary"] += " " + s
    res = QualityGate().check(report, ctx)
    assert s in res["report"]["executive_summary"]


def test_nonexistent_section_reference_removed(client, _index):
    ctx, report = _ctx_for(client, submit(client, "S1")["case_id"], _index)
    report["follow_up_questions"].append("Apply §42.1 penalty schedule.")
    res = QualityGate().check(report, ctx)
    assert "Apply §42.1 penalty schedule." not in res["report"]["follow_up_questions"]


def test_complaint_text_cannot_be_confirmed_fact(client, _index):
    ctx, report = _ctx_for(client, submit(client, "S3")["case_id"], _index)
    report["confirmed_facts"]["case_record_facts"].append("Last week he called me useless in front of the whole team.")
    res = QualityGate().check(report, ctx)
    assert not any("useless" in f for f in res["report"]["confirmed_facts"]["case_record_facts"])


def test_personal_data_redacted(client, _index):
    ctx, report = _ctx_for(client, submit(client, "S1")["case_id"], _index)
    report["reported_concern"]["summary"] += " Reach her on 9876543210 or x.y@example.com."
    res = QualityGate().check(report, ctx)
    assert "9876543210" not in res["report"]["reported_concern"]["summary"]
    assert _check(res, "Sensitive data minimised")["status"] == "corrected"


def test_posh_route_cannot_be_removed(client, _index):
    ctx, report = _ctx_for(client, submit(client, "S4")["case_id"], _index)
    report["route"] = "standard_er"
    report["escalation"]["items"] = []
    report["reported_concern"]["statements"] = ["detail that must not be here"]
    res = QualityGate().check(report, ctx)
    assert res["report"]["route"] == "posh_ic"
    assert any(e["to"] == "IC / designated statutory body" for e in res["report"]["escalation"]["items"])
    assert res["report"]["reported_concern"]["statements"] == []


def test_failed_agent_blocks_report(client, _index):
    ctx, report = _ctx_for(client, submit(client, "S1")["case_id"], _index)
    ctx.case["_agent_status"]["compliance_reviewer"] = "failed"
    res = QualityGate().check(report, ctx)
    assert res["verdict"] == "blocked"


def test_blocked_report_cannot_be_approved(client):
    cid = submit(client, "S1")["case_id"]
    db = client.app.state.db
    case = db.get_case(client.cookies.get("pg_ws"), cid)
    db.update_case(cid, gate=dict(case["gate"], verdict="blocked"))
    r = client.post(f"/api/cases/{cid}/review", json={"decision": "approve", "reviewer": "HR"})
    assert r.status_code == 409


def test_no_evidence_case_flagged(client):
    rep = get_case(client, submit(client, "S7")["case_id"])
    gate = rep["case"]["gate"]
    assert next(c for c in gate["checks"] if c["check"] == "Policy evidence found")["status"] == "warn"
    assert next(c for c in gate["checks"] if c["check"] == "Untrusted instructions ignored")["status"] == "warn"
    assert rep["case"]["report"]["human_review"]["status"] == "pending"  # injected "approve" ignored


@pytest.mark.parametrize("sid", ["S1", "S2", "S3", "S4", "S5", "S6", "S7"])
def test_red_team_stress_test_catches_all_faults(client, sid):
    cid = submit(client, sid)["case_id"]
    r = client.post(f"/api/cases/{cid}/gate-stress-test").json()
    missed = [f["fault"] for f in r["faults"] if not f["caught"]]
    assert not missed, missed
    assert r["total"] >= 7
