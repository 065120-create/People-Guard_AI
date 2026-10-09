"""Quality & Risk Gate - independent checks on the assembled draft report.

The gate is deliberately NOT a rubber stamp: each check can pass, warn,
correct the report (and record exactly what it changed) or fail. A language
model, if configured, can only ADD concerns; it can never clear a failed check.
"""
from __future__ import annotations

import copy
import json
import re

from app.agents.base import Agent, WorkflowContext, best_excerpt, compact, quote_is_verbatim, sentences
from app.agents.orchestrator import escalation_row
from app.models.schemas import QualityLLM
from app.services import rules
from app.services.rules import RuleFinding

SECTION_REF_RE = re.compile(r"§\s?(\d+(?:\.\d+)?)")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


class _Corrector:
    """Applies sentence-level corrections to free-text fields and records them."""

    def __init__(self):
        self.log: list[str] = []

    def clean(self, text: str, field: str, bad: callable, reason: str) -> str:
        if not text:
            return text
        kept, removed = [], []
        for s in sentences(text) or [text]:
            (removed if bad(s) else kept).append(s)
        if not removed:
            return text
        for r in removed:
            self.log.append(f"{field}: removed “{r[:140]}” — {reason}")
        kept.append(f"[Removed by Quality & Risk Gate: {reason}]")
        return " ".join(kept)


def narrative_fields(report: dict):
    """Yield (label, container, key) for every model/template-written text field.

    The reporter's own statements are excluded: they are labelled allegations and
    may legitimately contain claims the system must not repeat as fact.
    """
    yield "executive_summary", report, "executive_summary"
    yield "reported_concern.summary", report["reported_concern"], "summary"
    for i, p in enumerate(report.get("policy_provisions", [])):
        yield f"policy_provisions[{i}].how_it_applies", p, "how_it_applies"
    for i, it in enumerate(report["compliance"].get("issues", [])):
        yield f"compliance.issues[{i}].why_relevant", it, "why_relevant"
    for i, it in enumerate(report["compliance"].get("unmapped_ai_issues", [])):
        yield f"compliance.unmapped_ai_issues[{i}].why_relevant", it, "why_relevant"
    for i, s in enumerate(report.get("next_steps", [])):
        yield f"next_steps[{i}]", s, "step"
    for i in range(len(report.get("ai_suggested_steps", []))):
        yield f"ai_suggested_steps[{i}]", report["ai_suggested_steps"], i
    for i in range(len(report.get("follow_up_questions", []))):
        yield f"follow_up_questions[{i}]", report["follow_up_questions"], i
    for i in range(len(report.get("unresolved_questions", []))):
        yield f"unresolved_questions[{i}]", report["unresolved_questions"], i
    for i in range(len(report.get("conflicts_of_interest", []))):
        yield f"conflicts_of_interest[{i}]", report["conflicts_of_interest"], i


class QualityGate(Agent):
    key = "quality_gate"
    name = "Quality & Risk Gate"
    deterministic_in_demo = True
    responsibility = (
        "Act as an independent reviewer of a draft HR case report. List concrete problems only: allegations presented as "
        "facts, conclusions about guilt or penalties, legal claims not supported by the cited handbook text, missing "
        "escalation, unnecessary personal data, or unsupported recommendations. If there are none, return an empty list."
    )
    llm_model = QualityLLM
    llm_schema_text = '{"concerns": [str]}'

    # ------------------------------------------------------------------
    def rules(self, ctx: WorkflowContext) -> dict:
        report = copy.deepcopy(ctx.outputs["documentation_specialist"]["report"])
        return self.check(report, ctx)

    def check(self, report: dict, ctx: WorkflowContext) -> dict:
        index = ctx.index
        tri = ctx.outputs.get("orchestrator", {})
        pol = ctx.outputs.get("policy_expert", {})
        agent_status = ctx.case.get("_agent_status", {})
        findings: list[RuleFinding] = []
        fix = _Corrector()
        corpus = _norm(" ".join(s.text + " " + s.ref for s in index.sections))
        valid_numbers = {s.section_number for s in index.sections if s.section_number}

        # 1. Agent completion -------------------------------------------------
        failed = [a for a, st in agent_status.items() if st == "failed"]
        fallback = [a for a, st in agent_status.items() if st == "fallback"]
        if failed:
            findings.append(RuleFinding("Agent completion", "fail", "One or more agents failed; the report is incomplete.", failed))
        elif fallback:
            findings.append(RuleFinding("Agent completion", "warn", "Some agents fell back to rule-based output after an AI error.", fallback))
        else:
            findings.append(RuleFinding("Agent completion", "pass", "All upstream agents completed."))

        # 2. Citations exist in the indexed handbook ---------------------------
        retrieved_ids = {r["chunk_id"] for r in pol.get("retrieved", [])}
        bad_exist, bad_retr = [], []
        kept = []
        for p in report.get("policy_provisions", []):
            if not index.get(p.get("chunk_id") or ""):
                bad_exist.append(p.get("ref") or p.get("chunk_id"))
            elif p["chunk_id"] not in retrieved_ids:
                bad_retr.append(p.get("ref"))
            else:
                kept.append(p)
        report["policy_provisions"] = kept
        for e in report["escalation"]["items"]:
            good = [b for b in e.get("basis", []) if b and index.get(b.get("chunk_id", ""))]
            if len(good) != len(e.get("basis", [])):
                bad_exist.append(f"escalation basis for {e['to']}")
            e["basis"] = good
        for it in report["compliance"]["issues"]:
            if it.get("chunk_id") and not index.get(it["chunk_id"]):
                bad_exist.append(it.get("ref") or it["chunk_id"])
                it["chunk_id"], it["ref"], it["framework_to_verify"] = "", "", ""
        if bad_exist or bad_retr:
            items = [f"not in handbook: {x}" for x in bad_exist] + [f"not among retrieved passages: {x}" for x in bad_retr]
            for x in items:
                fix.log.append(f"citation removed — {x}")
            findings.append(RuleFinding("Citations match retrieved material", "corrected", "Citations that did not correspond to retrieved handbook sections were removed.", items))
        else:
            findings.append(RuleFinding("Citations match retrieved material", "pass", f"All {len(kept)} policy citations exist in the handbook and were retrieved for this case."))

        # 3. Quotes are verbatim -------------------------------------------------
        fixed_quotes = []
        for p in report["policy_provisions"]:
            sec = index.get(p["chunk_id"])
            if sec and not quote_is_verbatim(p.get("quote"), sec.text):
                fixed_quotes.append(p["ref"])
                p["quote"] = best_excerpt(sec.text, pol.get("query_terms", []))
        findings.append(
            RuleFinding("Policy quotes are verbatim", "corrected", "Quotes that did not match the source text were replaced with verbatim excerpts.", fixed_quotes)
            if fixed_quotes else RuleFinding("Policy quotes are verbatim", "pass", "Every quoted excerpt appears word-for-word in its cited section.")
        )

        # 4. Section numbers mentioned in narrative exist -------------------------
        def bad_ref(sentence: str) -> bool:
            return any(n not in valid_numbers for n in SECTION_REF_RE.findall(sentence))

        before = len(fix.log)
        for label, container, key in list(narrative_fields(report)):
            val = container[key]
            if isinstance(val, str) and SECTION_REF_RE.search(val):
                container[key] = fix.clean(val, label, bad_ref, "cited a handbook section that does not exist")
        bad_section_refs = fix.log[before:]
        findings.append(
            RuleFinding("No invented section numbers", "corrected", "Sentences citing non-existent handbook sections were removed.", bad_section_refs)
            if bad_section_refs else RuleFinding("No invented section numbers", "pass", "All § references in the narrative exist in the handbook.")
        )

        # 5. No invented legal specifics -----------------------------------------
        def unsupported_legal(sentence: str) -> bool:
            for pat in rules.LEGAL_SPECIFIC_PATTERNS:
                for m in pat.finditer(sentence):
                    if _norm(m.group(0)) not in corpus:
                        return True
            return False

        before = len(fix.log)
        for label, container, key in list(narrative_fields(report)):
            val = container[key]
            if isinstance(val, str):
                container[key] = fix.clean(val, label, unsupported_legal, "legal specific (statute, section, amount, rate or time limit) not found in the handbook")
        legal_fixes = fix.log[before:]
        findings.append(
            RuleFinding("No invented legal or policy specifics", "corrected", "Statements with statutory specifics not supported by the handbook were removed.", legal_fixes)
            if legal_fixes else RuleFinding("No invented legal or policy specifics", "pass", "No statute names, section numbers, amounts, rates or deadlines appear that are absent from the handbook.")
        )

        # 6. Allegations not presented as fact / no outcome decisions ------------
        def judgmental(sentence: str) -> bool:
            low = sentence.lower()
            return any(re.search(p, low) for p in rules.DECISION_LANGUAGE + rules.ALLEGATION_AS_FACT)

        before = len(fix.log)
        for label, container, key in list(narrative_fields(report)):
            val = container[key]
            if isinstance(val, str):
                container[key] = fix.clean(val, label, judgmental, "presented an allegation as fact or stated an outcome/penalty")
        desc_sents = {_norm(s) for s in sentences(ctx.inp.get("description", "")) if len(s) > 25}
        facts = report["confirmed_facts"]["case_record_facts"]
        leaked = [f for f in facts if any(d in _norm(f) for d in desc_sents)]
        if leaked:
            report["confirmed_facts"]["case_record_facts"] = [f for f in facts if f not in leaked]
            fix.log.extend(f"confirmed_facts: moved complaint text out of confirmed facts — “{f[:100]}”" for f in leaked)
        judg_fixes = fix.log[before:]
        findings.append(
            RuleFinding("Allegations not presented as facts; no outcome decided", "corrected", "Judgmental or outcome language was removed from the draft.", judg_fixes)
            if judg_fixes else RuleFinding("Allegations not presented as facts; no outcome decided", "pass", "The report keeps allegations labelled as such and decides no outcome or penalty.")
        )

        # 7. Required information ------------------------------------------------
        req = [m["item"] for m in report.get("missing_information", []) if m.get("severity") == "required"]
        findings.append(
            RuleFinding("Required case information", "warn", f"{len(req)} required item(s) missing; conclusions are limited until obtained.", req)
            if req else RuleFinding("Required case information", "pass", "Key applicability facts and records are present or requested.")
        )

        # 8. Policy evidence present ---------------------------------------------
        if not report["policy_provisions"]:
            report["policy_no_evidence"] = True
            if not any("No supporting handbook passage" in l for l in report["limitations"]):
                report["limitations"].append("No supporting handbook passage was retrieved. Human review must decide which policy, if any, applies.")
            findings.append(RuleFinding("Policy evidence found", "warn", "No supporting handbook passage was retrieved; the report says so and requests human review."))
        else:
            findings.append(RuleFinding("Policy evidence found", "pass", f"{len(report['policy_provisions'])} handbook provision(s) support the report."))

        # 9. Escalation consistency ----------------------------------------------
        esc_fixes = []
        posh = "posh" in tri.get("flag_keys", []) or tri.get("final_category") == "posh"
        targets = {e["to"] for e in report["escalation"]["items"]}
        if posh:
            if report["route"] != "posh_ic":
                report["route"], report["route_label"] = "posh_ic", "POSH — route to Internal Committee / designated statutory process"
                esc_fixes.append("route set to Internal Committee")
            if "IC / designated statutory body" not in targets:
                row = escalation_row(index, "IC / designated statutory body")
                report["escalation"]["items"].insert(0, {"to": "IC / designated statutory body", "reason": "POSH indicators present.", "priority": "HIGH", "basis": [row] if row else []})
                esc_fixes.append("added Internal Committee escalation")
            if not report["compliance"].get("statutory_route"):
                report["compliance"]["statutory_route"] = "Route to the Internal Committee / designated statutory mechanism (§8.2, §8.3)."
                esc_fixes.append("added statutory routing statement")
            report["restricted_access"] = True
        if tri.get("urgency") == "URGENT" and not report["escalation"].get("urgent_actions"):
            report["escalation"]["urgent_actions"] = tri.get("urgent_actions") or ["Immediate human intervention required (§7.4)."]
            esc_fixes.append("added urgent actions")
        if tri.get("urgency") == "URGENT" or posh or report["compliance"].get("escalate_to_legal"):
            report["escalation"]["flagged"] = True
        if esc_fixes:
            findings.append(RuleFinding("Escalation requirements", "corrected", "Escalation was made consistent with the detected risk.", esc_fixes))
        else:
            detail = f"Escalation is consistent with urgency '{tri.get('urgency')}' and route '{tri.get('route')}'."
            status = "warn" if tri.get("urgency") == "URGENT" or posh else "pass"
            items = ["Immediate human attention required"] if tri.get("urgency") == "URGENT" else (["Statutory POSH route"] if posh else [])
            findings.append(RuleFinding("Escalation requirements", status, detail, items))

        # 10. Sensitive data exposure --------------------------------------------
        serial = json.dumps(report, ensure_ascii=False)
        pii = rules.find_pii(serial)
        exposure = []
        if pii:
            report = json.loads(rules.redact_pii(serial))
            exposure.append("Redacted: " + ", ".join(f"{k} ×{v}" for k, v in pii.items()))
        if report["route"] == "posh_ic" and (report["reported_concern"]["statements"] or report["reported_concern"].get("requested_outcome")):
            report["reported_concern"]["statements"] = []
            report["reported_concern"]["requested_outcome"] = ""
            exposure.append("Removed POSH complaint details from the ER report (restricted to the IC route).")
        findings.append(
            RuleFinding("Sensitive data minimised", "corrected", "Personal data was redacted from the report.", exposure)
            if exposure else RuleFinding("Sensitive data minimised", "pass", "No phone numbers, emails, ID or account numbers found in the report.")
        )

        # 11. Untrusted instructions ------------------------------------------------
        inj = tri.get("injection_detected") or []
        if inj:
            ok = report["human_review"]["status"] == "pending"
            findings.append(RuleFinding("Untrusted instructions ignored", "warn" if ok else "fail", "Instruction-like text was found in the submission and was treated as data; the case still requires human review.", inj))
        else:
            findings.append(RuleFinding("Untrusted instructions ignored", "pass", "No instruction-like text found in submitted material."))

        return self._summarise(report, findings, fix.log)

    def _summarise(self, report: dict, findings: list[RuleFinding], corrections: list[str], ai_concerns: list[str] | None = None) -> dict:
        statuses = [f.status for f in findings]
        if "fail" in statuses:
            verdict, label = "blocked", "Blocked — fix the failed checks before using this draft"
        elif "corrected" in statuses:
            verdict, label = "corrected", "Corrected by the gate — review the changes, then approve or return"
        elif "warn" in statuses or ai_concerns:
            verdict, label = "warnings", "Passed with warnings — human review required"
        else:
            verdict, label = "passed", "Passed — ready for human review"
        reasons = ["Every PeopleGuard report is a draft and requires approval by an authorised HR reviewer (§13.5, Appendix A rule 10)."]
        reasons += [f"{f.check}: {f.detail}" for f in findings if f.status in ("fail", "warn")]
        report["quality_gate"] = {"verdict": verdict, "label": label}
        return {
            "checks": [f.to_dict() for f in findings],
            "corrections": corrections,
            "ai_concerns": ai_concerns or [],
            "verdict": verdict,
            "verdict_label": label,
            "counts": {s: statuses.count(s) for s in ("pass", "warn", "corrected", "fail")},
            "requires_human_review": True,
            "review_reasons": reasons,
            "report": report,
        }

    def simulate(self, ctx: WorkflowContext, base: dict) -> dict:
        return base

    def build_prompt(self, ctx: WorkflowContext, base: dict) -> str:
        r = base["report"]
        view = {k: r.get(k) for k in ("executive_summary", "urgency", "route_label", "next_steps", "follow_up_questions", "escalation", "limitations")}
        view["reported_concern_summary"] = r["reported_concern"]["summary"]
        view["policy_refs"] = [{"ref": p["ref"], "quote": p["quote"], "how": p.get("how_it_applies")} for p in r["policy_provisions"]]
        view["legal_issues"] = [{"topic": i["topic"], "why": i.get("why_relevant")} for i in r["compliance"]["issues"]]
        return f"Draft report to review:\n{compact(view, 6000)}\n\nList concrete problems (or none). Return JSON."

    def merge(self, ctx: WorkflowContext, base: dict, llm_out: QualityLLM) -> dict:
        concerns = [c.strip() for c in llm_out.concerns if c.strip()][:8]
        if not concerns:
            return base
        out = dict(base)
        out["ai_concerns"] = concerns
        out["checks"] = base["checks"] + [{"check": "AI reviewer concerns", "status": "warn", "detail": "An AI reviewer raised points for the human reviewer to consider.", "items": concerns}]
        out["counts"] = dict(base["counts"], warn=base["counts"].get("warn", 0) + 1)
        if out["verdict"] == "passed":
            out["verdict"], out["verdict_label"] = "warnings", "Passed with warnings — human review required"
            out["report"]["quality_gate"] = {"verdict": out["verdict"], "label": out["verdict_label"]}
        out["review_reasons"] = base["review_reasons"] + ["AI reviewer concerns: " + "; ".join(concerns)]
        return out
