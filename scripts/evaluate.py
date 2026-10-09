#!/usr/bin/env python3
"""Evaluate PeopleGuard AI on the synthetic cases and write docs/EVALUATION.md.

Usage:
    python scripts/evaluate.py              # demo mode (no API key needed)
    python scripts/evaluate.py --with-tests # also run the pytest suite and include its result
    LLM_PROVIDER=gemini GEMINI_API_KEY=... python scripts/evaluate.py --live   # use the configured model

Every number in the report is computed from an actual run. Nothing is typed in by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.agents.base import quote_is_verbatim  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.retrieval.index import policy_index  # noqa: E402
from app.services.rules import find_pii  # noqa: E402

SAMPLES = json.loads((ROOT / "data/sample_cases/sample_cases.json").read_text(encoding="utf-8"))["cases"]
VARIANTS = json.loads((ROOT / "data/sample_cases/eval_variants.json").read_text(encoding="utf-8"))["cases"]


def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({(100 * a / b):.0f}%)" if b else "n/a"


def run_case(client: TestClient, inp: dict, document: str | None = None) -> dict:
    data = {k: v for k, v in inp.items()}
    data["sync"] = "true"
    if document:
        data["sample_document"] = document
    r = client.post("/api/cases", data=data)
    r.raise_for_status()
    cid = r.json()["case_id"]
    return client.get(f"/api/cases/{cid}").json()


def evaluate_case(result: dict, exp: dict) -> dict:
    case, agents = result["case"], result["agents"]
    rep = case.get("report") or {}
    tri = case["triage"]
    out = {"case_id": case["id"], "checks": {}}
    out["completed"] = all(a["status"] == "completed" for a in agents)
    out["agent_statuses"] = {a["agent"]: a["status"] for a in agents}

    numbers = {p["ref"].split()[0].lstrip("§") for p in rep.get("policy_provisions", [])}
    out["retrieved"] = sorted(numbers)
    if exp.get("no_evidence"):
        out["checks"]["retrieval"] = rep.get("policy_no_evidence") is True
    else:
        any_ok = bool(numbers & set(exp.get("must_retrieve_any", []))) if exp.get("must_retrieve_any") else True
        all_ok = all(n in numbers for n in exp.get("must_retrieve_all", []))
        out["checks"]["retrieval"] = any_ok and all_ok

    out["checks"]["route"] = case["route"] == exp["route"]
    if exp["urgency"] == "HIGH_OR_STANDARD":
        out["checks"]["urgency"] = tri["urgency"] in ("HIGH", "STANDARD")
    else:
        out["checks"]["urgency"] = tri["urgency"] == exp["urgency"]
    out["actual_route"], out["actual_urgency"] = case["route"], tri["urgency"]

    if "escalation_to" in exp:
        targets = {e["to"] for e in rep["escalation"]["items"]}
        out["checks"]["escalation_targets"] = all(t in targets for t in exp["escalation_to"]) and not any(t in targets for t in exp.get("escalation_not_to", []))
        out["checks"]["escalation_flag"] = case["escalation_flag"] is exp["escalation_flag"]
    if exp.get("missing_info_contains"):
        missing = " ".join(m["item"] for m in rep["missing_information"])
        out["checks"]["missing_info"] = all(i in missing for i in exp["missing_info_contains"])

    # Evidence grounding, computed independently of the gate
    cites = rep.get("policy_provisions", [])
    retrieved_ids = set()
    for a in agents:
        if a["agent"] == "policy_expert" and a["output"]:
            retrieved_ids = {r["chunk_id"] for r in a["output"]["retrieved"]}
    grounded = sum(1 for p in cites if policy_index.get(p["chunk_id"]) and p["chunk_id"] in retrieved_ids)
    verbatim = sum(1 for p in cites if policy_index.get(p["chunk_id"]) and quote_is_verbatim(p["quote"], policy_index.get(p["chunk_id"]).text))
    out["citations"], out["grounded"], out["verbatim"] = len(cites), grounded, verbatim
    out["pii_in_report"] = bool(find_pii(json.dumps(rep)))
    out["gate_verdict"] = (case.get("gate") or {}).get("verdict")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the LLM configured in the environment")
    ap.add_argument("--with-tests", action="store_true", help="run pytest and include the result")
    ap.add_argument("--out", default=str(ROOT / "docs" / "EVALUATION.md"))
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp())
    s = Settings()
    if not args.live:
        s.llm_provider = "mock"
    s.database_path = tmp / "eval.db"
    s.access_code = ""
    app = create_app(s)
    started = time.time()
    results = {"samples": [], "variants": [], "red_team": []}
    with TestClient(app) as client:
        for sc in SAMPLES:
            res = run_case(client, {**sc["input"], "sample_case_id": sc["id"]}, sc.get("document"))
            ev = evaluate_case(res, sc["expected"])
            ev.update(id=sc["id"], label=sc["label"])
            results["samples"].append(ev)
            rt = client.post(f"/api/cases/{res['case']['id']}/gate-stress-test").json()
            results["red_team"].append({"id": sc["id"], "caught": rt["caught"], "total": rt["total"], "missed": [f["fault"] for f in rt["faults"] if not f["caught"]]})
        for v in VARIANTS:
            res = run_case(client, v["input"])
            ev = evaluate_case(res, v["expected"])
            ev.update(id=v["id"], label=f"{v['input']['title']} — _{v.get('set', 'held-out')}_")
            results["variants"].append(ev)
    elapsed = time.time() - started
    mode = s.llm_status()

    tests = None
    if args.with_tests:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True)
        summary = [l for l in proc.stdout.splitlines() if re.search(r"\d+ (passed|failed)", l)]
        tests = {"returncode": proc.returncode, "summary": summary[-1] if summary else proc.stdout[-300:]}

    write_report(Path(args.out), results, mode, elapsed, tests)
    (ROOT / "docs" / "evaluation_results.json").write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": mode, "results": results, "tests": tests}, indent=2), encoding="utf-8")
    print(Path(args.out).read_text(encoding="utf-8"))
    return 0


def section(rows: list[dict], title: str) -> list[str]:
    lines = [f"### {title}", "", "| Case | Description | Workflow | Retrieval | Route | Urgency | Escalation | Missing info | Retrieved sections | Gate |", "|---|---|---|---|---|---|---|---|---|---|"]
    mark = lambda v: "—" if v is None else ("✅" if v else "❌")  # noqa: E731
    for r in rows:
        c = r["checks"]
        esc = None
        if "escalation_targets" in c:
            esc = c["escalation_targets"] and c["escalation_flag"]
        lines.append(
            f"| {r['id']} | {r['label']} | {mark(r['completed'])} | {mark(c.get('retrieval'))} | {mark(c.get('route'))} ({r['actual_route']}) | "
            f"{mark(c.get('urgency'))} ({r['actual_urgency']}) | {mark(esc)} | {mark(c.get('missing_info'))} | {', '.join(r['retrieved']) or 'none'} | {r['gate_verdict']} |"
        )
    return lines + [""]


def write_report(path: Path, results: dict, mode: dict, elapsed: float, tests: dict | None) -> None:
    S, V, RT = results["samples"], results["variants"], results["red_team"]
    allc = S + V
    completed = sum(r["completed"] for r in allc)
    ret_s = sum(r["checks"]["retrieval"] for r in S)
    ret_v = sum(r["checks"]["retrieval"] for r in V)
    route_ok = sum(r["checks"]["route"] and r["checks"]["urgency"] for r in allc)
    esc_s = [r for r in S if "escalation_targets" in r["checks"]]
    esc_ok = sum(r["checks"]["escalation_targets"] and r["checks"]["escalation_flag"] for r in esc_s)
    cites = sum(r["citations"] for r in allc)
    grounded = sum(r["grounded"] for r in allc)
    verbatim = sum(r["verbatim"] for r in allc)
    pii = sum(r["pii_in_report"] for r in allc)
    caught = sum(r["caught"] for r in RT)
    planted = sum(r["total"] for r in RT)
    miss_s = [r for r in S if "missing_info" in r["checks"]]
    miss_ok = sum(r["checks"]["missing_info"] for r in miss_s)

    failures = []
    for r in allc:
        bad = [k for k, v in r["checks"].items() if not v]
        if bad or not r["completed"]:
            failures.append(f"- **{r['id']} — {r['label']}**: failed {', '.join(bad) or 'workflow completion'} (route {r['actual_route']}, urgency {r['actual_urgency']}, retrieved {', '.join(r['retrieved']) or 'none'}).")
    for r in RT:
        if r["missed"]:
            failures.append(f"- Red-team on {r['id']}: missed {', '.join(r['missed'])}.")

    lines = [
        "# PeopleGuard AI — Evaluation Report",
        "",
        f"_Generated automatically by `scripts/evaluate.py` on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. Do not edit by hand; re-run the script._",
        "",
        f"**Mode:** {mode['message']}  ",
        f"**Run time:** {elapsed:.1f} s for {len(allc)} cases plus {len(RT)} red-team runs.",
        "",
    ]
    if mode["state"] != "live":
        lines += ["> This run used **demo mode**: no language model was called. It therefore measures the deterministic parts of the system "
                  "(retrieval, triage, routing, escalation, missing-information checks and the Quality & Risk Gate). It does **not** measure the quality "
                  "of AI-written narrative. Re-run with `--live` and an API key to evaluate a model.", ""]
    lines += [
        "## Summary",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Cases tested | {len(allc)} ({len(S)} documented sample cases + {len(V)} wording variants, of which {sum(1 for v in VARIANTS if v.get('set') == 'fresh held-out')} fresh held-out) |",
        f"| Workflow completion rate (all 6 agents completed) | {pct(completed, len(allc))} |",
        f"| Policy retrieval correctness — sample cases | {pct(ret_s, len(S))} |",
        f"| Policy retrieval correctness — wording variants | {pct(ret_v, len(V))} |",
        f"| Routing and urgency correct (all cases) | {pct(route_ok, len(allc))} |",
        f"| Escalation targets and flag correct (sample cases) | {pct(esc_ok, len(esc_s))} |",
        f"| Missing-information checks correct (sample cases with an expectation) | {pct(miss_ok, len(miss_s))} |",
        f"| Evidence grounding: citations that exist and were retrieved | {pct(grounded, cites)} |",
        f"| Evidence grounding: quotes verbatim in the cited section | {pct(verbatim, cites)} |",
        f"| Reports containing raw personal identifiers | {pii} of {len(allc)} |",
        f"| Quality-gate red-team: planted faults caught | {pct(caught, planted)} |",
    ]
    if tests:
        lines.append(f"| Automated test suite (pytest) | {tests['summary']} (exit code {tests['returncode']}) |")
    lines += [
        "",
        "### How each metric is measured",
        "",
        "- **Retrieval correctness**: the report cites at least one of the handbook sections the case is expected to need (and every section marked as mandatory); for out-of-scope cases, the system must report that no evidence was found.",
        "- **Routing / urgency**: POSH cases must go to the Internal Committee route; threats must be marked URGENT; ordinary cases must not be over-escalated.",
        "- **Evidence grounding** is re-computed here, independently of the Quality & Risk Gate, from the final saved reports.",
        "- **Red-team**: for each sample case, eight or nine known faults (fabricated section, unretrieved section, altered quote, guilt/penalty statement, invented statute and deadline, non-existent § reference, complaint text placed under confirmed facts, phone number, and — for POSH — removal of the IC route) are planted in a copy of the report, and the gate is run again.",
        "",
        "## Detailed results",
        "",
    ]
    lines += section(S, "Documented sample cases (data/sample_cases/sample_cases.json)")
    lines += section(V, "Wording variants (data/sample_cases/eval_variants.json)")
    lines += ["> **Disclosure.** V1–V10 were written before the first evaluation run. That run missed two indirect sexual-harassment descriptions (V4, V10) "
              "and wrongly retrieved benefits sections for an out-of-scope request (V8, caused by keyword substring matching). The detection patterns and "
              "keyword matching were then fixed, so V1–V10 are no longer an unbiased test. V11–V14 were written after the fixes and before this run as a "
              "fresh held-out check; any failure there is reported as-is below.", ""]
    lines += ["### Red-team results per case", "", "| Case | Caught | Missed |", "|---|---|---|"]
    lines += [f"| {r['id']} | {r['caught']}/{r['total']} | {', '.join(r['missed']) or '—'} |" for r in RT]
    lines += ["", "## Known failures", ""]
    lines += failures or ["- None in this run."]
    lines += [
        "",
        "## Known limitations",
        "",
        "- Sensitive-case detection is keyword- and pattern-based. Indirect descriptions (for example of unwelcome physical contact without sexual vocabulary) can be missed; the held-out variants above show where. A configured language model can add flags but this has not been evaluated against a real model here.",
        "- Retrieval is lexical (BM25 with an HR-vocabulary expansion table). Wording far from both the handbook and the expansion table can retrieve weaker sections.",
        "- The Compliance Reviewer has no live legal research. It names frameworks only from the handbook's own reference map, so every legal point is unverified by design.",
        "- The red-team faults are known patterns; a model could produce unsupported content the gate's rules do not recognise (for example a paraphrased guilt finding).",
        "- The test set is small and synthetic, written by the system's author. Results indicate behaviour on these cases only, not real-world accuracy.",
        "- Per-browser workspaces isolate cases between visitors but are not authentication.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
