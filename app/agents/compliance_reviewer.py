"""Agent 4 - Labour Compliance Reviewer.

This agent has no live legal-research capability. It uses only the handbook's
own Legal Reference Map (Appendix C) to name which framework HR must verify,
and labels every legal point "REQUIRES VERIFICATION". It never states
deadlines, rates or entitlements.
"""
from __future__ import annotations

import re

from app.agents.base import Agent, WorkflowContext, compact
from app.models.schemas import EMPLOYEE_CATEGORIES, ESTABLISHMENT_TYPES, ComplianceLLM
from app.services import rules

# Grievance category -> Appendix C row labels (as printed in the handbook).
CATEGORY_TO_ROWS = {
    "wages_overtime": ["Wages, minimum wages", "Working conditions, safety, hours"],
    "leave_absence": ["Working conditions, safety, hours"],
    "manager_conduct": ["Industrial relations, grievance mechanisms"],
    "posh": ["Sexual harassment at workplace", "Privacy and employee records"],
    "safety_threat": ["Working conditions, safety, hours"],
    "discrimination": [],
    "maternity": ["Social security, maternity"],
    "disciplinary": ["Industrial relations, grievance mechanisms"],
    "separation": ["Industrial relations, grievance mechanisms", "Wages, minimum wages"],
    "social_security": ["Social security, maternity"],
    "data_privacy": ["Privacy and employee records"],
    "other": [],
}

TOPIC_FACTS = {
    "wages_overtime": ["work_location", "employee_category", "establishment_type", "relevant_dates"],
    "leave_absence": ["work_location", "employee_category", "establishment_type", "relevant_dates"],
    "maternity": ["work_location", "employee_category", "relevant_dates"],
    "social_security": ["work_location", "employee_category", "relevant_dates"],
    "separation": ["work_location", "employee_category", "relevant_dates"],
    "disciplinary": ["work_location", "employee_category", "relevant_dates"],
    "safety_threat": ["work_location", "establishment_type", "relevant_dates"],
    "manager_conduct": ["work_location", "employee_category"],
    "posh": ["work_location", "relevant_dates"],
    "discrimination": ["work_location", "employee_category", "relevant_dates"],
    "data_privacy": ["work_location"],
    "other": ["work_location", "employee_category"],
}

FACT_LABELS = {
    "work_location": ("Work location (state/UT)", "State/UT rules and notifications differ."),
    "employee_category": ("Employee category", "Coverage and procedure differ by category."),
    "establishment_type": ("Establishment type", "Some obligations depend on establishment type and headcount."),
    "relevant_dates": ("Relevant dates", "The law and rules in force on those dates apply, and time limits may run from them."),
}

LEGAL_ESCALATION_CATEGORIES = {"posh", "discrimination", "maternity", "separation", "disciplinary"}


def _row(ctx: WorkflowContext, prefix: str):
    for s in ctx.index.sections:
        if s.kind == "legal_map" and s.row_label.lower().startswith(prefix.lower()):
            return s
    return None


def _field(text: str, label: str) -> str:
    m = re.search(rf"{re.escape(label)}:\s*(.+)", text)
    return m.group(1).strip() if m else ""


class ComplianceReviewer(Agent):
    key = "compliance_reviewer"
    name = "Labour Compliance Reviewer"
    responsibility = (
        "Identify which Indian employment-law questions may be relevant, using ONLY the handbook's Legal Reference Map rows "
        "supplied (cite their chunk_id). Say which facts are needed to assess applicability. You have no live legal research: "
        "never state deadlines, wage rates, entitlements or section numbers, and treat every legal point as requiring "
        "verification by a qualified human. Decide whether Legal/Compliance escalation is needed."
    )
    llm_model = ComplianceLLM
    llm_schema_text = (
        '{"issues": [{"topic": str, "framework_chunk_id": str (from the list, or ""), "why_relevant": str, "facts_needed": [str]}], '
        '"escalate_to_legal": bool, "escalation_reason": str}'
    )

    def rules(self, ctx: WorkflowContext) -> dict:
        tri = ctx.outputs.get("orchestrator", {})
        category = tri.get("final_category", "other")
        route = tri.get("route", "standard_er")
        flags = set(tri.get("flag_keys", []))
        i = ctx.inp

        facts = []
        for f in TOPIC_FACTS.get(category, TOPIC_FACTS["other"]):
            label, why = FACT_LABELS[f]
            value = i.get(f) or None
            if value and f == "employee_category":
                value = EMPLOYEE_CATEGORIES.get(value, value)
            elif value and f == "establishment_type":
                value = ESTABLISHMENT_TYPES.get(value, value)
            facts.append({"fact": label, "value": value, "why_needed": why})
        missing_facts = [f["fact"] for f in facts if not f["value"]]
        sufficiency = "insufficient" if len(missing_facts) >= 2 else ("partial" if missing_facts else "sufficient to begin verification")

        row_prefixes = list(CATEGORY_TO_ROWS.get(category, []))
        if "retaliation" in flags or "manager_involved" in flags:
            row_prefixes.append("Industrial relations, grievance mechanisms")
        if "safety_hazard" in flags or "threat_violence" in flags:
            row_prefixes.append("Working conditions, safety, hours")
        if "pregnancy_maternity" in flags:
            row_prefixes.append("Social security, maternity")
        if "posh" in flags and "Sexual harassment at workplace" not in row_prefixes:
            row_prefixes.insert(0, "Sexual harassment at workplace")

        issues, seen = [], set()
        for prefix in row_prefixes:
            sec = _row(ctx, prefix)
            if not sec or sec.chunk_id in seen:
                continue
            seen.add(sec.chunk_id)
            issues.append({
                "topic": sec.row_label,
                "chunk_id": sec.chunk_id,
                "ref": sec.ref,
                "framework_to_verify": _field(sec.text, "Primary legal framework to verify"),
                "what_hr_must_confirm": _field(sec.text, "What HR must confirm"),
                "status": "REQUIRES VERIFICATION",
                "why_relevant": "",
                "facts_needed": missing_facts,
            })
        if not issues:
            issues.append({
                "topic": tri.get("category_label", "This concern"),
                "chunk_id": "",
                "ref": "",
                "framework_to_verify": "",
                "what_hr_must_confirm": "",
                "status": "REQUIRES VERIFICATION",
                "why_relevant": "The handbook's Legal Reference Map has no row for this topic. Legal/Compliance must identify which law, if any, applies. No framework is suggested here to avoid inventing one.",
                "facts_needed": missing_facts,
            })

        statutory_route = None
        if route == "posh_ic":
            statutory_route = (
                "This matter must be routed to the Internal Committee / designated statutory mechanism (§8.2, §8.3). "
                "The ordinary grievance workflow must not substitute for that process, and this system must not conduct or decide the inquiry."
            )

        escalate = (
            category in LEGAL_ESCALATION_CATEGORIES
            or route != "standard_er"
            or bool(flags & {"retaliation", "legal_action", "discrimination", "threat_violence"})
            or (category == "wages_overtime" and "repeated_issue" in flags)
        )
        reasons = []
        if route == "posh_ic":
            reasons.append("statutory POSH process")
        if route == "urgent_safety":
            reasons.append("serious safety risk")
        if category in LEGAL_ESCALATION_CATEGORIES and route != "posh_ic":
            reasons.append(f"consequential legal questions ({tri.get('category_label', category)})")
        for f in ("retaliation", "legal_action", "discrimination"):
            if f in flags:
                reasons.append(rules.FLAG_LABELS[f].lower())
        if category == "wages_overtime" and "repeated_issue" in flags:
            reasons.append("repeated pay discrepancy (§4.5)")

        sources = []
        src = next((s for s in ctx.index.sections if s.heading.lower().startswith("official and government reference")), None)
        if src:
            for line in src.text.splitlines():
                url = re.search(r"https?://\S+", line)
                if url:
                    sources.append({"label": line.split(":")[0].lstrip("• ").strip(), "url": url.group(0).rstrip(".")})

        return {
            "category": category,
            "issues": issues,
            "applicability_facts": facts,
            "missing_facts": missing_facts,
            "fact_sufficiency": sufficiency,
            "statutory_route": statutory_route,
            "escalate_to_legal": escalate,
            "escalation_reason": "; ".join(reasons) if reasons else "",
            "verification_sources": sources,
            "verification_sources_ref": "Official and Government Reference Sources (handbook)",
            "live_legal_research": False,
            "disclaimer": (
                "No live legal-source retrieval was performed. Framework names come only from the handbook's Legal Reference Map "
                "(Appendix C), which the handbook itself describes as an orientation aid. Every legal point requires verification "
                "against current legislation, rules and state/UT notifications by a qualified professional."
            ),
        }

    def simulate(self, ctx: WorkflowContext, base: dict) -> dict:
        out = dict(base)
        issues = []
        for it in base["issues"]:
            it = dict(it)
            if not it["why_relevant"]:
                it["why_relevant"] = f"The handbook maps '{it['topic']}' matters to this framework. Applicability depends on the facts listed."
            issues.append(it)
        out["issues"] = issues
        return out

    def build_prompt(self, ctx: WorkflowContext, base: dict) -> str:
        tri = ctx.outputs.get("orchestrator", {})
        rows = [s for s in ctx.index.sections if s.kind == "legal_map" and s.row_label]
        row_text = "\n".join(f"[chunk_id: {s.chunk_id}] {s.text}" for s in rows)
        return (
            f"<untrusted_case_text>\n{ctx.safe_case_text()}\n</untrusted_case_text>\n\n"
            f"Triage: category={tri.get('final_category')}, route={tri.get('route')}, flags={tri.get('flag_keys')}\n"
            f"Handbook Legal Reference Map rows (the ONLY frameworks you may name):\n{row_text}\n\n"
            f"Rule-based assessment: {compact({k: base[k] for k in ('issues', 'missing_facts', 'escalate_to_legal')}, 2500)}\n\nReturn JSON."
        )

    def merge(self, ctx: WorkflowContext, base: dict, llm_out: ComplianceLLM) -> dict:
        out = self.simulate(ctx, base)
        by_id = {it["chunk_id"]: it for it in out["issues"] if it["chunk_id"]}
        for li in llm_out.issues:
            if li.framework_chunk_id in by_id:
                if li.why_relevant.strip():
                    by_id[li.framework_chunk_id]["why_relevant"] = li.why_relevant.strip()
            elif li.framework_chunk_id and ctx.index.get(li.framework_chunk_id) and ctx.index.get(li.framework_chunk_id).kind == "legal_map":
                sec = ctx.index.get(li.framework_chunk_id)
                out["issues"].append({
                    "topic": sec.row_label or li.topic,
                    "chunk_id": sec.chunk_id,
                    "ref": sec.ref,
                    "framework_to_verify": _field(sec.text, "Primary legal framework to verify"),
                    "what_hr_must_confirm": _field(sec.text, "What HR must confirm"),
                    "status": "REQUIRES VERIFICATION",
                    "why_relevant": li.why_relevant.strip(),
                    "facts_needed": base["missing_facts"],
                    "added_by": "AI",
                })
            elif li.topic.strip():
                out.setdefault("unmapped_ai_issues", []).append({"topic": li.topic.strip(), "why_relevant": li.why_relevant.strip(), "framework_chunk_id": li.framework_chunk_id})
        # The model may add escalation, never remove it.
        if llm_out.escalate_to_legal and not base["escalate_to_legal"]:
            out["escalate_to_legal"] = True
            out["escalation_reason"] = ("AI reviewer: " + llm_out.escalation_reason.strip()) if llm_out.escalation_reason.strip() else "AI reviewer recommended escalation."
        return out
