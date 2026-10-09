"""Agent 2 - HR Policy Expert (retrieval-augmented)."""
from __future__ import annotations

import re

from app.agents.base import Agent, WorkflowContext, best_excerpt
from app.models.schemas import PolicyExpertLLM

# Sections that must be put in front of HR for a given route, even if keyword
# retrieval ranks them lower. Each inclusion is labelled "routing rule".
ROUTE_REQUIRED = {
    "posh_ic": ["8.2", "8.3", "8.4"],
    "urgent_safety": ["7.4", "7.2"],
}
MANAGER_SUBJECT_REQUIRED = ["9.6"]

SOURCE_TYPE = {
    "policy": "Company policy (fictional Northstar handbook)",
    "escalation_table": "Company policy — roles and escalation table",
    "legal_map": "Handbook legal reference map (orientation only — not legal verification)",
}

LEGISLATION_RE = re.compile(r"[^.]*\b(Act|Code)\b,? (?:19|20)\d{2}[^.]*\.?")


class PolicyExpert(Agent):
    key = "policy_expert"
    name = "HR Policy Expert"
    responsibility = (
        "From the handbook passages retrieved for this case, select the provisions that genuinely apply and explain how "
        "each applies. Cite ONLY chunk_id values from the supplied list. If a needed topic is not covered, list it as a gap. "
        "Distinguish company policy from legislation that a policy merely mentions."
    )
    llm_model = PolicyExpertLLM
    llm_schema_text = (
        '{"findings": [{"chunk_id": str (from the list), "relevance": "high"|"medium"|"low", "how_it_applies": str}], '
        '"gaps": [str], "policy_vs_law_notes": [str]}'
    )

    def rules(self, ctx: WorkflowContext) -> dict:
        tri = ctx.outputs.get("orchestrator", {})
        category = tri.get("final_category") or ctx.inp.get("grievance_category")
        # Uploaded documents are deliberately NOT used as retrieval queries: they are
        # untrusted and could steer retrieval. The grievance itself drives retrieval.
        query = " ".join([ctx.inp.get("title", ""), ctx.inp.get("description", "")])
        s = ctx.settings
        hits, meta = ctx.index.search(query, category=category, top_k=s.retrieval_top_k, min_score=s.retrieval_min_score)

        retrieved = []
        seen = set()
        for h in hits:
            d = h.to_dict()
            d["retrieved_by"] = "keyword retrieval (BM25)"
            retrieved.append(d)
            seen.add(h.section.chunk_id)

        required = list(ROUTE_REQUIRED.get(tri.get("route", ""), []))
        if tri.get("manager_is_subject"):
            required += MANAGER_SUBJECT_REQUIRED
        for number in required:
            sec = next((x for x in ctx.index.sections if x.section_number == number), None)
            if sec and sec.chunk_id not in seen:
                retrieved.append({**sec.to_dict(), "score": None, "matched_terms": [], "excerpt": sec.text[:600], "retrieved_by": "routing rule"})
                seen.add(sec.chunk_id)

        terms = meta["query_terms"]
        findings = []
        legislation = []
        for r in retrieved:
            sec = ctx.index.get(r["chunk_id"])
            if not sec:
                continue
            quote = best_excerpt(sec.text, terms)
            findings.append({
                "chunk_id": sec.chunk_id,
                "ref": sec.ref,
                "document": sec.document,
                "quote": quote,
                "source_type": SOURCE_TYPE.get(sec.kind, sec.kind),
                "kind": sec.kind,
                "score": r.get("score"),
                "retrieved_by": r["retrieved_by"],
                "matched_terms": r.get("matched_terms", []),
                "relevance": "high" if (r.get("score") or 0) >= 20 or r["retrieved_by"] == "routing rule" else "medium",
                "how_it_applies": "",
            })
            for m in LEGISLATION_RE.finditer(sec.text):
                legislation.append({"chunk_id": sec.chunk_id, "ref": sec.ref, "quote": m.group(0).strip()})

        gaps = []
        if not findings:
            gaps.append("No supporting passage was retrieved from the handbook for this concern. Human review is required to decide which policy, if any, applies.")
        return {
            "query_terms": terms,
            "expansions": meta["expansions"],
            "min_score": meta["min_score"],
            "retrieved": retrieved,
            "findings": findings,
            "legislation_mentioned": legislation,
            "no_evidence": not findings,
            "gaps": gaps,
            "rejected_citations": [],
            "policy_vs_law_notes": [
                "Handbook provisions are fictional company policy. Where a provision mentions legislation, that mention is not verification of what the law currently requires (handbook Important Notice; §1.3 Legal precedence)."
            ],
        }

    def simulate(self, ctx: WorkflowContext, base: dict) -> dict:
        out = dict(base)
        findings = []
        for f in base["findings"]:
            f = dict(f)
            if f["retrieved_by"] == "routing rule":
                f["how_it_applies"] = "Included because the case route requires this provision to be considered."
            elif f["matched_terms"]:
                f["how_it_applies"] = f"Retrieved because the case text matches: {', '.join(f['matched_terms'][:6])}. HR should confirm it applies to the facts."
            findings.append(f)
        out["findings"] = findings
        return out

    def build_prompt(self, ctx: WorkflowContext, base: dict) -> str:
        passages = "\n\n".join(
            f"[chunk_id: {r['chunk_id']}] {r['ref']} ({r['kind']}, retrieved by {r['retrieved_by']})\n{(ctx.index.get(r['chunk_id']).text if ctx.index.get(r['chunk_id']) else r['excerpt'])[:1200]}"
            for r in base["retrieved"]
        ) or "(no passages were retrieved)"
        return (
            f"<untrusted_case_text>\n{ctx.safe_case_text()}\n</untrusted_case_text>\n\n"
            f"Retrieved handbook passages (the ONLY sources you may cite):\n{passages}\n\n"
            "Select the applicable provisions and explain how each applies. Return JSON."
        )

    def merge(self, ctx: WorkflowContext, base: dict, llm_out: PolicyExpertLLM) -> dict:
        out = dict(base)
        by_id = {f["chunk_id"]: f for f in base["findings"]}
        findings, rejected = [], []
        for lf in llm_out.findings:
            if lf.chunk_id in by_id:
                f = dict(by_id[lf.chunk_id])
                f["relevance"] = lf.relevance if lf.relevance in ("high", "medium", "low") else f["relevance"]
                f["how_it_applies"] = lf.how_it_applies.strip()
                findings.append(f)
            else:
                rejected.append(lf.chunk_id)
        # Routing-rule provisions are always kept even if the model omitted them.
        for f in base["findings"]:
            if f["retrieved_by"] == "routing rule" and f["chunk_id"] not in {x["chunk_id"] for x in findings}:
                findings.append(dict(f, how_it_applies="Included because the case route requires this provision."))
        out["findings"] = findings
        out["rejected_citations"] = rejected
        out["no_evidence"] = not findings
        out["gaps"] = base["gaps"] + [g for g in llm_out.gaps if g.strip()][:6]
        out["policy_vs_law_notes"] = base["policy_vs_law_notes"] + [n for n in llm_out.policy_vs_law_notes if n.strip()][:4]
        if not findings and base["findings"]:
            out["gaps"].append("The AI judged none of the retrieved passages applicable. Human review is required.")
        return out
