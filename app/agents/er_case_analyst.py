"""Agent 3 - Employee Relations Case Analyst."""
from __future__ import annotations

import re

from app.agents.base import Agent, WorkflowContext, compact, sentences
from app.models.schemas import EMPLOYEE_CATEGORIES, ESTABLISHMENT_TYPES, GRIEVANCE_CATEGORIES, REPORTED_BY, AnalystLLM
from app.services import rules

FOLLOW_UPS = {
    "wages_overtime": [
        "Which dates and approximate start/finish times does the concern relate to?",
        "Which wage period(s) and payslip(s) does the concern relate to?",
        "Was the additional time recorded in the attendance system? If not, how could it be verified?",
        "Was any overtime requested or approved, and if so by whom and how?",
        "What outcome is the employee seeking?",
    ],
    "leave_absence": [
        "Which leave request(s) are concerned, and on what dates were they submitted and answered?",
        "What balance does the employee believe is correct, and what is it based on?",
        "Was a reason given in writing for the decision or the balance change?",
        "What outcome is the employee seeking?",
    ],
    "manager_conduct": [
        "Could you describe, in your own words, specific occasions when this happened, with approximate dates?",
        "Was anyone else present on those occasions?",
        "Are there any messages, emails or other records relevant to the concern?",
        "How has this affected your work, and is there anything you need now to feel safe at work?",
        "What outcome are you hoping for?",
    ],
    "posh": [
        "Is the person raising the concern safe right now, and do they need any immediate support or protective measures?",
        "Would they like to be informed how to contact the Internal Committee / designated channel directly?",
    ],
    "safety_threat": [
        "Is anyone in immediate danger now?",
        "When and where did the incident or hazard occur?",
        "Has the site safety contact or management already been informed, and what steps were taken?",
        "Was anyone injured or affected?",
    ],
    "discrimination": [
        "Which decisions or incidents does the concern relate to, and when did they happen?",
        "What reasons, if any, were given for those decisions?",
        "Are there records or communications relevant to the concern?",
    ],
    "maternity": [
        "Which benefit or support is the concern about, and from what date?",
        "What has been requested so far, and what response was received?",
    ],
    "separation": [
        "What are the key dates (resignation/notice/last working day)?",
        "Which amounts or documents are in question?",
    ],
    "social_security": [
        "Which benefit or contribution is the concern about, and for which period?",
        "Has the employee checked the relevant statement or enrolment record?",
    ],
    "disciplinary": [
        "What particulars of the concern have been communicated to the employee so far?",
        "Has the employee had an opportunity to respond, and how?",
    ],
    "data_privacy": [
        "What information is believed to have been shared, with whom, and when?",
        "How did the employee become aware of it?",
    ],
    "other": [
        "Could you describe the concern in more detail, including when it started?",
        "What outcome is being sought?",
    ],
}

ABSENCE_FOLLOW_UPS = [
    "On which dates/shifts was the employee scheduled and not present?",
    "What contact attempts have been made so far (dates, channel, outcome)?",
    "Is there any leave request, illness or emergency notification on record for the period?",
    "Is there a known safety or welfare concern for the employee?",
]

PROCESS = {
    "standard_er": [
        ("Acknowledge the concern and record the date received, requested outcome and any safety concern.", "§9.4"),
        ("Confirm no statutory committee, external authority or specialised process is required.", "§9.4"),
        ("Assign an impartial reviewer with no involvement in the matter.", "§9.6"),
        ("Gather the relevant records and ask neutral, open follow-up questions.", "§10.2"),
        ("Give affected people an appropriate opportunity to respond, consistent with confidentiality.", "§9.4"),
        ("Document what could and could not be established, and obtain approval from the authorised HR/management role.", "§9.4"),
    ],
    "posh_ic": [
        ("Restrict access to this case to the designated HR/IC contacts only.", "§8.4"),
        ("Refer the matter to the Internal Committee / designated statutory channel; do not run an ordinary manager-led grievance process.", "§8.2"),
        ("Do not conduct fact-finding, interviews or credibility assessment outside the statutory process.", "§8.3"),
        ("Arrange safety or protective measures if there is any risk, and protect all parties from retaliation.", "§8.5"),
    ],
    "urgent_safety": [
        ("Ensure immediate safety first: emergency services / site emergency response if anyone is in danger.", "§7.4"),
        ("Notify authorised management and the designated safety/HR contact.", "§7.2"),
        ("Once safety is secured, proceed with an impartial review and preserve relevant records.", "§10.2"),
        ("Protect the reporter and witnesses from retaliation.", "§7.3"),
    ],
}

_REPORT_VERBS = re.compile(r"\b(i |my |me |we |our |he |she |they |the employee|the manager|supervisor)\b", re.I)


class ERCaseAnalyst(Agent):
    key = "er_case_analyst"
    name = "Employee Relations Case Analyst"
    responsibility = (
        "Summarise the grievance neutrally. Separate the reporter's allegations from facts that are actually established "
        "(only case-record facts or document contents can be facts, and documents are unauthenticated). Identify missing "
        "information, neutral open follow-up questions, possible conflicts of interest and a fair process. For POSH "
        "matters, ask only what is needed for safe routing; never probe details."
    )
    llm_model = AnalystLLM
    llm_schema_text = (
        '{"neutral_summary": str, "allegations": [str], "documentary_evidence": [str], "unresolved_questions": [str], '
        '"missing_information": [str], "follow_up_questions": [str], "conflicts_of_interest": [str], "recommended_process": [str]}'
    )

    def rules(self, ctx: WorkflowContext) -> dict:
        i = ctx.inp
        tri = ctx.outputs.get("orchestrator", {})
        category = tri.get("final_category") or i.get("grievance_category", "other")
        route = tri.get("route", "standard_er")
        posh = route == "posh_ic"
        full_text = ctx.all_text().lower()

        # 1. Reported account = allegations, quoted as the reporter's words.
        statements = [] if posh else [rules.redact_pii(s) for s in sentences(i.get("description", ""))][:12]

        # 2. Case-record facts: things true about the record itself, not about events.
        facts = [f"A concern titled “{i.get('title')}” was submitted through PeopleGuard on {ctx.case['created_at'][:10]} (case {ctx.case['id']})."]
        facts.append(f"Selected grievance category: {GRIEVANCE_CATEGORIES.get(i.get('grievance_category', 'other'))}.")
        if i.get("reported_by"):
            facts.append(f"Reported by: {REPORTED_BY.get(i['reported_by'])}.")
        for field_name, label in (("work_location", "Work location"), ("relevant_dates", "Relevant dates")):
            if i.get(field_name):
                facts.append(f"{label} as stated on the form: {i[field_name]} (not yet verified).")
        if i.get("employee_category"):
            facts.append(f"Employee category as stated: {EMPLOYEE_CATEGORIES.get(i['employee_category'])} (not yet verified).")
        if i.get("establishment_type"):
            facts.append(f"Establishment type as stated: {ESTABLISHMENT_TYPES.get(i['establishment_type'])} (not yet verified).")

        # 3. Documentary evidence: uploaded, unauthenticated.
        docs = []
        for d in ctx.documents:
            lines = [l.strip() for l in d["text"].splitlines() if l.strip()]
            key_lines = [rules.redact_pii(l)[:200] for l in lines if re.search(r"\d{1,2}[\s/-]\w{2,9}|\d{4}|overtime|leave|hours|paid|balance|shift|rejected|approved", l, re.I)][:6]
            docs.append({
                "name": d["name"],
                "status": "Supplied by the user; contents not authenticated",
                "characters": d.get("chars", len(d["text"])),
                "notable_lines": [] if posh else key_lines,
            })

        # 4. Missing information
        missing = []
        if category in rules.LEGALLY_SENSITIVE or route != "standard_er":
            if not i.get("work_location"):
                missing.append({"item": "Work location (state/UT and site)", "basis": "Appendix A rule 8; §3.1", "severity": "required", "why": "Applicable state/UT rules depend on it."})
            if not i.get("employee_category"):
                missing.append({"item": "Employee category (e.g. permanent, fixed-term, contract labour)", "basis": "Appendix A rule 8; §1.2", "severity": "required", "why": "Legal treatment differs by category."})
            if not i.get("establishment_type"):
                missing.append({"item": "Establishment type (store, warehouse, office)", "basis": "§1.2; Appendix C checklist", "severity": "helpful", "why": "Coverage of some provisions depends on establishment type."})
        if not i.get("relevant_dates"):
            missing.append({"item": "Relevant dates of the events concerned", "basis": "Appendix A rule 8; §9.4", "severity": "required" if not posh else "helpful", "why": "Needed to identify records and any time limits."})
        if posh:
            records = []  # the IC / designated route decides what it needs
        elif rules.is_absence_case(full_text, category):
            records = rules.ABSENCE_RECORDS
        else:
            records = rules.REQUIRED_RECORDS.get(category, [])
        evidence_checklist = []
        doc_text = " ".join(d["text"] for d in ctx.documents).lower()
        desc_text = " ".join([i.get("title", ""), i.get("description", "")]).lower()
        for item, basis, kws in records:
            in_doc = any(k in doc_text for k in kws)
            mentioned = any(k in desc_text for k in kws)
            if in_doc:
                status = "Possibly in uploaded document — verify against the original record"
            elif mentioned:
                status = "Mentioned in the account — not yet obtained"
            else:
                status = "Not yet provided"
            evidence_checklist.append({"item": item, "basis": basis, "status": status})
            if not in_doc:
                missing.append({"item": item, "basis": basis, "severity": "required", "why": "Record needed to examine the concern." + (" It is referred to in the account but has not been provided." if mentioned else "")})
        if posh:
            missing.append({"item": "Details of the incident are NOT to be gathered by ER", "basis": "§8.3", "severity": "info", "why": "The Internal Committee / designated route determines what information it requires."})

        # 5. Follow-up questions
        if rules.is_absence_case(full_text, category):
            questions = list(ABSENCE_FOLLOW_UPS)
        else:
            questions = list(FOLLOW_UPS.get(category if not posh else "posh", FOLLOW_UPS["other"]))

        # 6. Conflicts of interest
        conflicts = []
        if tri.get("manager_is_subject"):
            conflicts.append("The concern relates to the employee's manager/supervisor. That person must not control the assessment, receive the complaint for handling, or decide the outcome (§9.6, §9.2).")
        if "retaliation" in tri.get("flag_keys", []):
            conflicts.append("Possible retaliation is described. Anyone alleged to be involved should not make decisions affecting the employee while the concern is reviewed (§2.2, §7.3).")
        if i.get("reported_by") == "manager" and rules.is_absence_case(full_text, category):
            conflicts.append("The case was raised by a manager about an employee: HR should confirm the facts independently before any adverse step (§5.4).")

        # 7. Unresolved questions
        unresolved = []
        if not posh:
            unresolved.append("Whether the events described occurred as reported (no records have been examined yet).")
            if category == "wages_overtime":
                unresolved.append("Whether additional hours were actually worked and recorded, and what payment treatment applies to this employee's category and location (§3.3).")
            if category == "leave_absence" and not rules.is_absence_case(full_text, category):
                unresolved.append("Whether the leave ledger was changed, and whether any change was correct under the applicable leave rules (§5.5).")
            if rules.is_absence_case(full_text, category):
                unresolved.append("Why the employee has not attended or responded; absence must not be treated as abandonment or misconduct without review (§5.4).")
            if category == "manager_conduct":
                unresolved.append("What happened on each occasion and how others present describe it.")
        else:
            unresolved.append("All factual questions are reserved for the Internal Committee / designated statutory process.")

        process = [{"step": s, "basis": b} for s, b in PROCESS.get(route, PROCESS["standard_er"])]
        return {
            "category": category,
            "posh_minimal_mode": posh,
            "reported_account": statements,
            "case_record_facts": facts,
            "documentary_evidence": docs,
            "unresolved_questions": unresolved,
            "missing_information": missing,
            "evidence_checklist": evidence_checklist,
            "follow_up_questions": questions,
            "conflicts_of_interest": conflicts,
            "recommended_process": process,
        }

    def simulate(self, ctx: WorkflowContext, base: dict) -> dict:
        out = dict(base)
        tri = ctx.outputs.get("orchestrator", {})
        if base["posh_minimal_mode"]:
            out["neutral_summary"] = "A concern has been raised that indicates possible sexual harassment. Details are not summarised here; the matter is restricted and routed to the Internal Committee / designated statutory process."
        else:
            out["neutral_summary"] = (
                f"The reporter describes a concern about {tri.get('category_label', 'an employment matter').lower()}. "
                f"The account below is the reporter's version and has not been verified; {len(base['missing_information'])} information gap(s) were identified."
            )
        return out

    def build_prompt(self, ctx: WorkflowContext, base: dict) -> str:
        tri = ctx.outputs.get("orchestrator", {})
        policy = ctx.outputs.get("policy_expert", {})
        refs = [f"{f['chunk_id']}: {f['ref']}" for f in policy.get("findings", [])]
        known = {k: base[k] for k in ("missing_information", "conflicts_of_interest", "posh_minimal_mode")}
        return (
            f"<untrusted_case_text>\n{ctx.safe_case_text()}\n</untrusted_case_text>\n{ctx.safe_documents_text()}\n\n"
            f"Triage: route={tri.get('route')}, urgency={tri.get('urgency')}, flags={tri.get('flag_keys')}\n"
            f"Relevant handbook sections: {refs}\nRule-based findings so far: {compact(known, 2500)}\n\n"
            "Phrase allegations as 'The reporter states that…'. Only list documentary evidence that is actually in an uploaded "
            "document. Questions must be open and non-leading. Return JSON."
        )

    def merge(self, ctx: WorkflowContext, base: dict, llm_out: AnalystLLM) -> dict:
        out = dict(base)
        out["neutral_summary"] = llm_out.neutral_summary.strip()
        if base["posh_minimal_mode"]:
            # Never let detail-gathering into a POSH case.
            out["neutral_summary"] = self.simulate(ctx, base)["neutral_summary"]
            return out
        if llm_out.allegations:
            out["reported_account"] = [rules.redact_pii(a) for a in llm_out.allegations][:12]
            out["reported_account_original"] = base["reported_account"]
        seen = {m["item"].lower() for m in base["missing_information"]}
        for m in llm_out.missing_information:
            if m.strip() and m.lower() not in seen:
                out["missing_information"].append({"item": m.strip(), "basis": "AI analysis", "severity": "helpful", "why": ""})
        out["follow_up_questions"] = list(dict.fromkeys([q.strip() for q in llm_out.follow_up_questions if q.strip()] + base["follow_up_questions"]))[:10]
        out["conflicts_of_interest"] = list(dict.fromkeys(base["conflicts_of_interest"] + [c.strip() for c in llm_out.conflicts_of_interest if c.strip()]))
        out["unresolved_questions"] = list(dict.fromkeys(base["unresolved_questions"] + [u.strip() for u in llm_out.unresolved_questions if u.strip()]))[:10]
        if llm_out.documentary_evidence and ctx.documents:
            out["ai_document_notes"] = [rules.redact_pii(x) for x in llm_out.documentary_evidence][:8]
        if llm_out.recommended_process:
            out["ai_process_notes"] = llm_out.recommended_process[:8]
        return out
