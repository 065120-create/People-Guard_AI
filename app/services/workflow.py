"""Runs the six-stage agent workflow for one case and records every step."""
from __future__ import annotations

import logging

from app.agents.base import WorkflowContext
from app.agents.compliance_reviewer import ComplianceReviewer
from app.agents.documentation_specialist import DocumentationSpecialist
from app.agents.er_case_analyst import ERCaseAnalyst
from app.agents.orchestrator import Orchestrator
from app.agents.policy_expert import PolicyExpert
from app.agents.quality_gate import QualityGate
from app.config import Settings
from app.retrieval.index import PolicyIndex
from app.services.llm import LLMClient
from app.storage.db import Database

log = logging.getLogger("peopleguard.workflow")

AGENTS = [Orchestrator(), PolicyExpert(), ERCaseAnalyst(), ComplianceReviewer(), DocumentationSpecialist(), QualityGate()]

# Agents whose output later agents cannot work without.
REQUIRED_UPSTREAM = {
    "policy_expert": ["orchestrator"],
    "er_case_analyst": ["orchestrator"],
    "compliance_reviewer": ["orchestrator"],
    "documentation_specialist": ["orchestrator", "policy_expert", "er_case_analyst", "compliance_reviewer"],
    "quality_gate": ["documentation_specialist"],
}


def run_workflow(db: Database, workspace: str, case_id: str, settings: Settings, index: PolicyIndex, llm: LLMClient | None = None) -> dict:
    case = db.get_case(workspace, case_id)
    if not case:
        raise KeyError(case_id)
    llm = llm or LLMClient(settings)
    mode = "live" if llm.enabled else "demo"
    db.reset_agents(case_id)
    db.update_case(case_id, status="running", error=None, mode=mode)
    db.log(workspace, case_id, f"Workflow started for {case_id} ({'live AI' if llm.enabled else 'demo mode'})")

    ctx = WorkflowContext(case=case, index=index, llm=llm, documents=case.get("documents") or [], settings=settings)
    statuses: dict[str, str] = {}
    ctx.case["_agent_status"] = statuses

    for agent in AGENTS:
        missing = [u for u in REQUIRED_UPSTREAM.get(agent.key, []) if statuses.get(u) not in ("completed", "fallback")]
        if missing:
            statuses[agent.key] = "skipped"
            db.set_agent(case_id, agent.key, status="skipped", error=f"Not run because an upstream agent did not complete: {', '.join(missing)}.")
            continue
        db.set_agent(case_id, agent.key, status="running")
        try:
            result = agent.run(ctx)
        except Exception as exc:  # noqa: BLE001 - never leave an agent shown as running
            log.exception("Agent %s crashed", agent.key)
            statuses[agent.key] = "failed"
            db.set_agent(case_id, agent.key, status="failed", error=f"Unexpected error: {type(exc).__name__}")
            continue
        statuses[agent.key] = result.status
        if result.status in ("completed", "fallback"):
            ctx.outputs[agent.key] = result.output
        db.set_agent(
            case_id, agent.key, status=result.status, mode=result.mode, started_at=result.started_at,
            finished_at=result.finished_at, duration_ms=result.duration_ms, model=result.model,
            attempts=result.attempts, output=result.output, notes=result.notes, error=result.error,
        )

    tri = ctx.outputs.get("orchestrator", case.get("triage") or {})
    gate = ctx.outputs.get("quality_gate")
    if gate:
        report = gate["report"]
        gate_summary = {k: v for k, v in gate.items() if k != "report"}
        flagged = bool(report["escalation"].get("flagged") or tri.get("escalation_flag"))
        status = "escalated_urgent" if tri.get("urgency") == "URGENT" else "awaiting_review"
        db.update_case(case_id, report=report, gate=gate_summary, status=status, escalation_flag=flagged,
                       route=report["route"], restricted=bool(report.get("restricted_access")))
        db.log(workspace, case_id, f"Workflow finished for {case_id}: {gate_summary['verdict_label']}")
    else:
        failed = [k for k, v in statuses.items() if v in ("failed", "skipped")]
        db.update_case(case_id, status="error", error=f"Workflow incomplete: {', '.join(failed)} did not complete.")
        db.log(workspace, case_id, f"Workflow for {case_id} did not complete")
    return {"case_id": case_id, "statuses": statuses, "mode": mode}
