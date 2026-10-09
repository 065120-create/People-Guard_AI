"""Agent 1 - Employee Relations Orchestrator.

Triage runs synchronously at intake (before any other agent) so that urgent
risks are shown to the user immediately rather than after the workflow.
"""
from __future__ import annotations

from app.agents.base import Agent, WorkflowContext, compact
from app.models.schemas import GRIEVANCE_CATEGORIES, OrchestratorLLM
from app.retrieval.index import PolicyIndex
from app.services import rules

WORKFLOW_PLAN = [
    ("orchestrator", "Classify, detect sensitive/urgent risk, set route and escalations"),
    ("policy_expert", "Retrieve and cite relevant handbook provisions"),
    ("er_case_analyst", "Separate allegations, record facts, evidence and gaps; draft neutral questions"),
    ("compliance_reviewer", "Identify legal issues needing verification and statutory routing"),
    ("documentation_specialist", "Assemble the draft HR case report and evidence checklist"),
    ("quality_gate", "Check grounding, neutrality, invented law, data exposure and escalation"),
]

ROUTES = {
    "posh_ic": "POSH — route to Internal Committee / designated statutory process",
    "urgent_safety": "Urgent — immediate human intervention for safety risk",
    "standard_er": "Standard employee-relations review",
}


def escalation_row(index: PolicyIndex, label_startswith: str) -> dict | None:
    for s in index.sections:
        if s.kind == "escalation_table" and s.row_label.lower().startswith(label_startswith.lower()):
            return {"chunk_id": s.chunk_id, "ref": s.ref}
    return None


def section_ref(index: PolicyIndex, number: str) -> dict | None:
    for s in index.sections:
        if s.section_number == number:
            return {"chunk_id": s.chunk_id, "ref": s.ref}
    return None


def triage(case_input: dict, documents: list[dict], index: PolicyIndex) -> dict:
    """Deterministic intake triage. Used at submission and by the Orchestrator."""
    text = " ".join([case_input.get("title", ""), case_input.get("description", ""), case_input.get("requested_outcome", "")])
    doc_text = " ".join(d.get("text", "") for d in documents)
    raw_flags = rules.detect_flags(text + " " + doc_text)
    flags = {k: v for k, v in raw_flags.items() if k != "manager_mentioned"}

    user_category = case_input.get("grievance_category") or "other"
    inferred, scores = rules.infer_category(text)
    category_note = ""
    final_category = user_category
    if user_category == "other" and inferred != "other":
        final_category = inferred
        category_note = f"Category was not selected; inferred '{GRIEVANCE_CATEGORIES[inferred]}' from the description."
    elif inferred not in ("other", user_category) and scores.get(inferred, 0) >= 2 * max(1, scores.get(user_category, 0)):
        category_note = f"The description also strongly suggests '{GRIEVANCE_CATEGORIES[inferred]}'. The selected category is kept; HR should confirm."

    if "posh" in flags and final_category != "posh":
        category_note = (category_note + " " if category_note else "") + "Sexual-harassment indicators found: POSH routing applies regardless of the selected category."

    manager_subject = rules.manager_is_subject(text, final_category) and case_input.get("reported_by") not in ("manager",)
    if manager_subject:
        flags["manager_involved"] = ["complaint concerns a manager/supervisor"]

    injection = rules.detect_injection(text + " " + doc_text)
    if injection:
        flags["prompt_injection"] = injection

    urgency = rules.urgency_level(set(flags), text + " " + doc_text)
    if "posh" in flags or final_category == "posh":
        route = "posh_ic"
    elif urgency == "URGENT":
        route = "urgent_safety"
    else:
        route = "standard_er"

    urgent_actions: list[str] = []
    if urgency == "URGENT":
        urgent_actions = [
            "Do not wait for this analysis: if anyone is in immediate danger, contact local emergency services or the designated site emergency response now (§7.4).",
            "Notify authorised management and the designated safety/HR contact immediately (§7.2, §7.4).",
            "Arrange appropriate protective/safety measures for the people involved (§8.5 where POSH-related).",
        ]
        if "self_harm_welfare" in flags:
            urgent_actions.insert(1, "The text suggests a possible risk to the employee's own safety: a trained person should make contact now and offer the designated wellbeing channel (§7.5) and emergency support.")

    escalations = []

    def add(row: str, reason: str, priority: str, basis_number: str | None = None):
        basis = escalation_row(index, row)
        extra = section_ref(index, basis_number) if basis_number else None
        escalations.append({
            "to": row,
            "reason": reason,
            "priority": priority,
            "basis": [b for b in (basis, extra) if b],
        })

    if route == "posh_ic":
        add("IC / designated statutory body", "Possible sexual harassment: must be handled through the Internal Committee / designated statutory route, not ordinary ER workflow.", "HIGH", "8.2")
        add("Legal / Compliance", "Statutory process applies; confirm IC constitution, time limits and confidentiality requirements.", "HIGH", "8.3")
    if urgency == "URGENT":
        add("HR / Employee Relations", "Serious risk indicated: immediate human intervention and safety measures.", "URGENT", "7.4")
    if manager_subject:
        add("HR / Employee Relations", "Complaint concerns the employee's manager: the manager must not control the assessment; assign an impartial reviewer.", "HIGH", "9.6")
    if final_category in ("wages_overtime", "social_security") or (final_category == "separation"):
        add("Payroll / Benefits", "Possible underpayment, deduction or benefit dispute must be verified against payroll and attendance records.", "STANDARD", "4.5" if final_category == "wages_overtime" else None)
    if "retaliation" in flags:
        add("HR / Employee Relations", "Possible retaliation for raising a concern: protect the employee from further adverse action pending review.", "HIGH", "7.3")
    if "legal_action" in flags:
        add("Legal / Compliance", "External legal action or authority is mentioned.", "HIGH")
    if "repeated_issue" in flags and final_category == "wages_overtime":
        add("Legal / Compliance", "Discrepancy described as repeated: §4.5 calls for escalation of repeated discrepancies.", "STANDARD", "4.5")
    if "discrimination" in flags and route != "posh_ic":
        add("Legal / Compliance", "Possible discrimination involving a protected or legally relevant characteristic.", "HIGH", "2.2")

    restricted = route == "posh_ic" or bool({"self_harm_welfare"} & set(flags))
    escalation_flag = route != "standard_er" or urgency in ("URGENT", "HIGH") or any(e["to"] in ("Legal / Compliance", "IC / designated statutory body") for e in escalations)

    return {
        "user_category": user_category,
        "inferred_category": inferred,
        "category_scores": scores,
        "final_category": final_category,
        "category_label": GRIEVANCE_CATEGORIES.get(final_category, final_category),
        "category_note": category_note.strip(),
        "flags": [{"flag": k, "label": rules.FLAG_LABELS.get(k, k), "evidence": v} for k, v in flags.items()],
        "flag_keys": sorted(flags),
        "urgency": urgency,
        "route": route,
        "route_label": ROUTES[route],
        "restricted_access": restricted,
        "manager_is_subject": manager_subject,
        "urgent_actions": urgent_actions,
        "escalations": escalations,
        "escalation_flag": escalation_flag,
        "injection_detected": injection,
        "pii_found": rules.find_pii(text + " " + doc_text),
    }


class Orchestrator(Agent):
    key = "orchestrator"
    name = "Employee Relations Orchestrator"
    responsibility = (
        "Review the intake triage of an employee grievance. Confirm or question the category, identify any additional "
        "sensitivity concerns the keyword rules missed, and write a one-sentence neutral issue statement. You cannot "
        "remove safety flags or downgrade urgency."
    )
    llm_model = OrchestratorLLM
    llm_schema_text = (
        '{"suggested_category": one of ' + ", ".join(GRIEVANCE_CATEGORIES) + ', "category_rationale": str, '
        '"additional_sensitivity_flags": [str], "neutral_issue_statement": str}'
    )

    def rules(self, ctx: WorkflowContext) -> dict:
        tri = ctx.case.get("triage") or triage(ctx.inp, ctx.documents, ctx.index)
        out = dict(tri)
        out["case_id"] = ctx.case["id"]
        out["received_at"] = ctx.case["created_at"]
        out["workflow_plan"] = [{"agent": a, "purpose": p} for a, p in WORKFLOW_PLAN]
        return out

    def simulate(self, ctx: WorkflowContext, base: dict) -> dict:
        out = dict(base)
        who = {"manager": "a manager", "hr": "HR", "colleague": "a colleague", "anonymous": "an anonymous reporter", "employee": "the employee"}.get(ctx.inp.get("reported_by", ""), "the reporter")
        out["issue_statement"] = f"A concern categorised as '{base['category_label']}' has been reported by {who}; it is unverified and will be reviewed against the Northstar handbook."
        out["llm_additional_flags"] = []
        return out

    def build_prompt(self, ctx: WorkflowContext, base: dict) -> str:
        summary = {k: base[k] for k in ("final_category", "category_note", "flag_keys", "urgency", "route", "manager_is_subject")}
        return (
            f"<untrusted_case_text>\n{ctx.safe_case_text()}\n</untrusted_case_text>\n"
            f"{ctx.safe_documents_text(1500)}\n\nRule-based triage already performed:\n{compact(summary)}\n\n"
            "Return your review as JSON."
        )

    def merge(self, ctx: WorkflowContext, base: dict, llm_out: OrchestratorLLM) -> dict:
        out = dict(base)
        out["issue_statement"] = llm_out.neutral_issue_statement.strip() or self.simulate(ctx, base)["issue_statement"]
        out["llm_additional_flags"] = [f for f in llm_out.additional_sensitivity_flags if f][:6]
        if llm_out.suggested_category in GRIEVANCE_CATEGORIES and llm_out.suggested_category != base["final_category"]:
            out["category_note"] = (base["category_note"] + " " if base["category_note"] else "") + (
                f"AI reviewer suggests '{GRIEVANCE_CATEGORIES[llm_out.suggested_category]}': {llm_out.category_rationale.strip()} (HR to confirm; routing unchanged)."
            )
        if out["llm_additional_flags"]:
            # Additional AI-detected concerns can only raise attention, never lower it.
            out["escalation_flag"] = True
        return out
