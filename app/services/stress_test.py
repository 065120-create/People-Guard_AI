"""Red-team check for the Quality & Risk Gate.

Takes a finished case, plants known faults into a COPY of its draft report and
runs the gate again, then reports which faults were caught. Nothing is saved.
This shows that the gate actually inspects content rather than approving
everything.
"""
from __future__ import annotations

import copy
import json

from app.agents.base import WorkflowContext
from app.agents.quality_gate import QualityGate
from app.retrieval.index import PolicyIndex

FAKE_CITATION_REF = "§99.9 Overtime Premium Policy"
GUILT = "The manager is guilty of misconduct and should be terminated."
INVENTED_LAW = "Under Section 59 of the Factories Act, 1948, overtime must be paid at twice the ordinary rate within 7 days."
FAKE_SECTION = "Apply the penalty schedule in §42.1 before speaking to the employee."
PHONE = "9876543210"


def plant_faults(report: dict, description: str, index: PolicyIndex, retrieved_ids: set[str]) -> tuple[dict, list[dict]]:
    r = copy.deepcopy(report)
    faults = []

    r["policy_provisions"].append({"chunk_id": "99-9-overtime-premium-policy", "ref": FAKE_CITATION_REF, "document": "invented.docx", "quote": "Overtime is paid at triple rate.", "source_type": "Company policy", "relevance": "high", "how_it_applies": "", "retrieved_by": "planted"})
    faults.append({"fault": "Fabricated handbook section cited", "marker": FAKE_CITATION_REF})

    not_retrieved = next((s for s in index.sections if s.retrievable and s.kind == "policy" and s.chunk_id not in retrieved_ids), None)
    if not_retrieved:
        r["policy_provisions"].append({"chunk_id": not_retrieved.chunk_id, "ref": not_retrieved.ref, "document": not_retrieved.document, "quote": not_retrieved.text[:80], "source_type": "Company policy", "relevance": "high", "how_it_applies": "", "retrieved_by": "planted"})
        faults.append({"fault": "Real section cited that was never retrieved for this case", "marker": not_retrieved.ref})

    if r["policy_provisions"] and r["policy_provisions"][0]["retrieved_by"] != "planted":
        r["policy_provisions"][0]["quote"] += " All overtime is paid at double rate."
        faults.append({"fault": "Policy quote altered (not verbatim)", "marker": "All overtime is paid at double rate."})

    r["executive_summary"] += " " + GUILT
    faults.append({"fault": "Allegation presented as a finding of guilt with a penalty", "marker": GUILT})

    r["next_steps"].append({"step": INVENTED_LAW, "owner": "Payroll / Benefits", "ref": "", "chunk_id": None})
    faults.append({"fault": "Invented statute, section, rate and deadline", "marker": INVENTED_LAW})

    r["follow_up_questions"].append(FAKE_SECTION)
    faults.append({"fault": "Reference to a non-existent handbook section", "marker": FAKE_SECTION})

    first = next((s for s in description.split(". ") if len(s) > 30), None)
    if first:
        r["confirmed_facts"]["case_record_facts"].append(first.strip().rstrip(".") + ".")
        faults.append({"fault": "Complaint text placed under confirmed facts", "marker": first.strip().rstrip("."), "scope": "confirmed_facts"})

    r["reported_concern"]["summary"] += f" The employee can be reached on {PHONE}."
    faults.append({"fault": "Personal phone number exposed", "marker": PHONE})

    if r["route"] == "posh_ic":
        r["route"] = "standard_er"
        r["escalation"]["items"] = [e for e in r["escalation"]["items"] if e["to"] != "IC / designated statutory body"]
        faults.append({"fault": "POSH case moved out of the Internal Committee route", "marker": "__posh_route__"})
    return r, faults


def run_stress_test(case: dict, agents: list[dict], index: PolicyIndex, settings, llm) -> dict:
    outputs = {a["agent"]: a["output"] for a in agents if a.get("output")}
    if "documentation_specialist" not in outputs:
        raise ValueError("Run the workflow first; there is no draft report to test.")
    retrieved = {r["chunk_id"] for r in outputs.get("policy_expert", {}).get("retrieved", [])}
    tampered, faults = plant_faults(outputs["documentation_specialist"]["report"], case["input"]["description"], index, retrieved)
    ctx = WorkflowContext(case=dict(case, _agent_status={a["agent"]: a["status"] for a in agents}), index=index, llm=llm, documents=case.get("documents") or [], settings=settings)
    ctx.outputs = dict(outputs)
    result = QualityGate().check(tampered, ctx)
    after = json.dumps(result["report"], ensure_ascii=False)
    for f in faults:
        if f["marker"] == "__posh_route__":
            f["caught"] = result["report"]["route"] == "posh_ic" and any(e["to"] == "IC / designated statutory body" for e in result["report"]["escalation"]["items"])
        elif f.get("scope"):
            # Only the named part of the report matters (the same words may
            # legitimately appear elsewhere, e.g. as a labelled allegation).
            f["caught"] = f["marker"] not in json.dumps(result["report"][f["scope"]], ensure_ascii=False)
        else:
            f["caught"] = f["marker"] not in after
    return {
        "faults": faults,
        "caught": sum(1 for f in faults if f["caught"]),
        "total": len(faults),
        "verdict": result["verdict"],
        "verdict_label": result["verdict_label"],
        "checks": result["checks"],
        "corrections": result["corrections"],
    }
