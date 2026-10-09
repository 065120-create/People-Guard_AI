"""Agent 5 - Case Documentation Specialist: assembles the draft HR case report."""
from __future__ import annotations

from app.agents.base import Agent, WorkflowContext, compact, utcnow
from app.agents.orchestrator import escalation_row
from app.models.schemas import DocumentationLLM
from app.services import rules

CATEGORY_STEPS = {
    "wages_overtime": [
        ("Log the concern and the wage period(s) involved.", "Payroll / Benefits", "§4.5"),
        ("Collect payslip, attendance, roster, overtime approval/record and payroll calculation.", "Payroll / Benefits", "§4.5"),
        ("Compare the records with the applicable wage rules and employment terms (legal rate and eligibility to be verified).", "Payroll / Benefits", "§3.3"),
        ("If work was performed but not recorded, refer it to HR/payroll for review even if approval is disputed.", "HR / Employee Relations", "§3.5"),
        ("Correct any verified error promptly and communicate the outcome in writing.", "Payroll / Benefits", "§4.5"),
    ],
    "leave_absence": [
        ("Review the roster, attendance, leave ledger, applicable rules and written communications.", "HR / Employee Relations", "§5.5"),
        ("Check whether any statutory or protected leave was involved before treating the matter as a policy dispute.", "HR / Employee Relations", "§5.2"),
        ("Correct the ledger if the records show an error, and record the reason, date and approver.", "HR / Employee Relations", "§3.2"),
    ],
    "absence": [
        ("Carry out a welfare check and make documented contact attempts.", "HR / Employee Relations", "§5.4"),
        ("Check leave and attendance records, and any illness/emergency notification.", "HR / Employee Relations", "§5.3"),
        ("Allow a reasonable opportunity for explanation before any adverse step; do not treat the absence as abandonment.", "HR / Employee Relations", "§5.4"),
    ],
    "manager_conduct": [
        ("Assign an impartial HR reviewer who is not in the employee's reporting line.", "HR / Employee Relations", "§9.6"),
        ("Gather specific examples, dates and any witnesses through neutral questions.", "HR / Employee Relations", "§10.2"),
        ("Give the manager an appropriate opportunity to respond, consistent with confidentiality.", "HR / Employee Relations", "§9.4"),
        ("Monitor for and prevent retaliation against the employee.", "HR / Employee Relations", "§2.2"),
    ],
    "posh": [
        ("Refer the concern to the Internal Committee / designated statutory channel.", "IC / designated statutory body", "§8.2"),
        ("Limit access to this record to designated HR/IC contacts.", "HR / Employee Relations", "§8.4"),
        ("Arrange any safety or protective measures needed and prevent retaliation.", "HR / Employee Relations", "§8.5"),
    ],
    "safety_threat": [
        ("Confirm immediate safety has been secured and emergency response contacted where needed.", "HR / Employee Relations", "§7.4"),
        ("Notify the designated safety/HR contact and record the incident.", "HR / Employee Relations", "§7.2"),
        ("Protect the reporter from retaliation for raising the concern.", "HR / Employee Relations", "§7.3"),
    ],
}


def _ref_lookup(ctx: WorkflowContext, basis: str) -> dict:
    """Turn '§4.5' into {'ref':..., 'chunk_id':...} only if that section exists."""
    number = basis.lstrip("§").strip()
    for s in ctx.index.sections:
        if s.section_number == number and s.kind in ("policy",):
            return {"ref": s.ref, "chunk_id": s.chunk_id}
    return {"ref": basis, "chunk_id": None}


class DocumentationSpecialist(Agent):
    key = "documentation_specialist"
    name = "Case Documentation Specialist"
    responsibility = (
        "Write the executive summary and the recommended procedural next steps for a DRAFT HR case report, using only the "
        "other agents' findings supplied. Do not decide outcomes, penalties or whether allegations are true."
    )
    llm_model = DocumentationLLM
    llm_schema_text = '{"executive_summary": str (max 120 words), "recommended_next_steps": [str]}'

    def rules(self, ctx: WorkflowContext) -> dict:
        tri = ctx.outputs.get("orchestrator", {})
        pol = ctx.outputs.get("policy_expert", {})
        ana = ctx.outputs.get("er_case_analyst", {})
        com = ctx.outputs.get("compliance_reviewer", {})
        category = tri.get("final_category", "other")
        route = tri.get("route", "standard_er")
        absence = rules.is_absence_case(ctx.all_text().lower(), category)

        # Next steps
        key = "posh" if route == "posh_ic" else ("absence" if absence else category)
        steps = []
        if route == "urgent_safety" and key != "safety_threat":
            for s, owner, basis in CATEGORY_STEPS["safety_threat"]:
                steps.append({"step": s, "owner": owner, **_ref_lookup(ctx, basis)})
        for s, owner, basis in CATEGORY_STEPS.get(key, []):
            steps.append({"step": s, "owner": owner, **_ref_lookup(ctx, basis)})
        if route != "posh_ic":
            used_basis = {x["ref"] for x in steps}
            for p in ana.get("recommended_process", []):
                basis = _ref_lookup(ctx, p["basis"])["ref"]
                # In urgent cases the safety playbook already covers the same sections;
                # skip analyst steps with the same basis to avoid repeating them.
                duplicate_basis = route == "urgent_safety" and basis in used_basis
                if not duplicate_basis and not any(p["step"][:40] == x["step"][:40] for x in steps):
                    used_basis.add(basis)
                    steps.append({"step": p["step"], "owner": "HR / Employee Relations", **_ref_lookup(ctx, p["basis"])})
        steps.append({"step": "An authorised HR manager reviews and approves this draft before any decision or employee-facing communication.", "owner": "HR / Employee Relations", **_ref_lookup(ctx, "§13.5")})

        # Escalations (orchestrator + compliance), de-duplicated by target
        escalations = [dict(e) for e in tri.get("escalations", [])]
        if com.get("escalate_to_legal") and not any(e["to"] == "Legal / Compliance" for e in escalations):
            row = escalation_row(ctx.index, "Legal / Compliance")
            escalations.append({"to": "Legal / Compliance", "reason": f"Legal verification needed: {com.get('escalation_reason') or 'see compliance issues'}.", "priority": "HIGH", "basis": [row] if row else []})
        if not escalations:
            row = escalation_row(ctx.index, "HR / Employee Relations")
            escalations.append({"to": "HR / Employee Relations", "reason": "Standard handling; escalate further if a statutory process, conflict of interest, legal uncertainty or serious risk emerges.", "priority": "STANDARD", "basis": [row] if row else []})

        # Merge escalations to the same function into one entry.
        merged: dict[str, dict] = {}
        rank = {"STANDARD": 0, "HIGH": 1, "URGENT": 2}
        for e in escalations:
            m = merged.get(e["to"])
            if not m:
                merged[e["to"]] = {"to": e["to"], "reasons": [e["reason"]], "priority": e["priority"], "basis": list(e.get("basis", []))}
                continue
            m["reasons"].append(e["reason"])
            if rank.get(e["priority"], 0) > rank.get(m["priority"], 0):
                m["priority"] = e["priority"]
            for b in e.get("basis", []):
                if b and b not in m["basis"]:
                    m["basis"].append(b)
        escalations = []
        for m in merged.values():
            escalations.append({"to": m["to"], "reason": " ".join(m["reasons"]), "reasons": m["reasons"], "priority": m["priority"], "basis": m["basis"]})
        escalations.sort(key=lambda e: -rank.get(e["priority"], 0))

        functions = []
        for e in escalations:
            if e["to"] not in functions:
                functions.append(e["to"])
        for s in steps:
            if s["owner"] not in functions:
                functions.append(s["owner"])

        # Limitations
        limitations = [
            "Northstar Retail Services Pvt. Ltd., its handbook and this case are fictional; this is an academic prototype.",
            "This is a draft for HR decision support. It makes no finding on whether any allegation is true and recommends no penalty.",
            "No records have been examined by the system; the reported account is unverified.",
            "No live legal research was performed. All legal points require verification against current law and state/UT rules.",
        ]
        if pol.get("no_evidence"):
            limitations.append("No supporting handbook passage was retrieved. Human review must decide which policy, if any, applies.")
        req_missing = [m for m in ana.get("missing_information", []) if m.get("severity") == "required"]
        if req_missing:
            limitations.append(f"{len(req_missing)} required item(s) of information are missing; conclusions are limited until they are obtained.")
        if ctx.documents:
            limitations.append("Uploaded documents were treated as unauthenticated data, not as instructions.")
        if tri.get("injection_detected"):
            limitations.append("The submission contained instruction-like text (e.g. asking the system to approve or close the case). It was ignored.")

        return {
            "report": {
                "case_id": ctx.case["id"],
                "title": ctx.inp.get("title"),
                "category": category,
                "category_label": tri.get("category_label"),
                "category_note": tri.get("category_note"),
                "urgency": tri.get("urgency"),
                "route": route,
                "route_label": tri.get("route_label"),
                "restricted_access": tri.get("restricted_access"),
                "generated_at": utcnow(),
                "executive_summary": "",
                "reported_concern": {
                    "summary": ana.get("neutral_summary", ""),
                    "statements": ana.get("reported_account", []),
                    "requested_outcome": rules.redact_pii(ctx.inp.get("requested_outcome", "")) if route != "posh_ic" else "",
                    "label": "Employee allegation / reported account — not verified",
                },
                "confirmed_facts": {
                    "case_record_facts": ana.get("case_record_facts", []),
                    "documentary_evidence": ana.get("documentary_evidence", []),
                    "note": "Only facts about the case record are treated as confirmed. No event described in the complaint has been confirmed.",
                },
                "policy_provisions": [
                    {k: f.get(k) for k in ("chunk_id", "ref", "document", "quote", "source_type", "relevance", "how_it_applies", "retrieved_by")}
                    for f in pol.get("findings", [])
                ],
                "policy_no_evidence": pol.get("no_evidence", False),
                "policy_gaps": pol.get("gaps", []),
                "policy_vs_law_notes": pol.get("policy_vs_law_notes", []),
                "legislation_mentioned": pol.get("legislation_mentioned", []),
                "missing_information": ana.get("missing_information", []),
                "compliance": {
                    "issues": com.get("issues", []),
                    "applicability_facts": com.get("applicability_facts", []),
                    "fact_sufficiency": com.get("fact_sufficiency"),
                    "statutory_route": com.get("statutory_route"),
                    "escalate_to_legal": com.get("escalate_to_legal"),
                    "disclaimer": com.get("disclaimer"),
                    "verification_sources": com.get("verification_sources", []),
                    "unmapped_ai_issues": com.get("unmapped_ai_issues", []),
                },
                "follow_up_questions": ana.get("follow_up_questions", []),
                "conflicts_of_interest": ana.get("conflicts_of_interest", []),
                "unresolved_questions": ana.get("unresolved_questions", []),
                "next_steps": steps,
                "escalation": {"items": escalations, "urgent_actions": tri.get("urgent_actions", []), "flagged": bool(tri.get("escalation_flag") or com.get("escalate_to_legal"))},
                "evidence_checklist": ana.get("evidence_checklist", []),
                "responsible_functions": functions,
                "limitations": limitations,
                "human_review": {"status": "pending", "required": True, "reviewer": None, "notes": None, "decided_at": None},
            }
        }

    def simulate(self, ctx: WorkflowContext, base: dict) -> dict:
        out = {"report": dict(base["report"])}
        r = out["report"]
        if r["route"] == "posh_ic":
            summary = (
                f"Case {r['case_id']} indicates a possible sexual-harassment concern. It has been restricted and must be routed to the "
                "Internal Committee / designated statutory process. HR's role is safe routing, confidentiality and protection from retaliation; "
                "this system has not assessed and will not assess the allegation."
            )
        else:
            urgent = " Urgent safety indicators were detected and require immediate human action." if r["urgency"] == "URGENT" else ""
            summary = (
                f"Case {r['case_id']} concerns {(r['category_label'] or 'an employment matter').lower()}.{urgent} "
                f"{len(r['policy_provisions'])} handbook provision(s) were identified, {len([m for m in r['missing_information'] if m.get('severity') == 'required'])} "
                f"required information gap(s) remain, and {len(r['compliance']['issues'])} legal question(s) need verification. "
                "The reported account has not been verified; no conclusion has been reached on the merits."
            )
        r["executive_summary"] = summary
        return out

    def build_prompt(self, ctx: WorkflowContext, base: dict) -> str:
        r = base["report"]
        brief = {
            "category": r["category_label"], "urgency": r["urgency"], "route": r["route_label"],
            "reported_summary": r["reported_concern"]["summary"],
            "policy_refs": [p["ref"] for p in r["policy_provisions"]],
            "missing": [m["item"] for m in r["missing_information"]][:8],
            "legal_issues": [i["topic"] for i in r["compliance"]["issues"]],
            "escalations": [e["to"] + ": " + e["reason"] for e in r["escalation"]["items"]],
            "draft_steps": [s["step"] for s in r["next_steps"]],
        }
        return f"Agent findings for case {r['case_id']}:\n{compact(brief, 4000)}\n\nWrite the executive summary and next steps. Return JSON."

    def merge(self, ctx: WorkflowContext, base: dict, llm_out: DocumentationLLM) -> dict:
        out = self.simulate(ctx, base)
        r = out["report"]
        if r["route"] != "posh_ic" and llm_out.executive_summary.strip():
            r["executive_summary"] = llm_out.executive_summary.strip()
        if llm_out.recommended_next_steps:
            r["ai_suggested_steps"] = [s.strip() for s in llm_out.recommended_next_steps if s.strip()][:8]
        return out
