# PeopleGuard AI — Evaluation Report

_Generated automatically by `scripts/evaluate.py` on 2026-10-09 04:16 UTC. Do not edit by hand; re-run the script._

**Mode:** Demo mode: no language model is configured. Agent narratives are simulated and labelled as such.  
**Run time:** 1.5 s for 21 cases plus 7 red-team runs.

> This run used **demo mode**: no language model was called. It therefore measures the deterministic parts of the system (retrieval, triage, routing, escalation, missing-information checks and the Quality & Risk Gate). It does **not** measure the quality of AI-written narrative. Re-run with `--live` and an API key to evaluate a model.

## Summary

| Metric | Result |
|---|---|
| Cases tested | 21 (7 documented sample cases + 14 wording variants, of which 4 fresh held-out) |
| Workflow completion rate (all 6 agents completed) | 21/21 (100%) |
| Policy retrieval correctness — sample cases | 7/7 (100%) |
| Policy retrieval correctness — wording variants | 13/14 (93%) |
| Routing and urgency correct (all cases) | 20/21 (95%) |
| Escalation targets and flag correct (sample cases) | 7/7 (100%) |
| Missing-information checks correct (sample cases with an expectation) | 3/3 (100%) |
| Evidence grounding: citations that exist and were retrieved | 114/114 (100%) |
| Evidence grounding: quotes verbatim in the cited section | 114/114 (100%) |
| Reports containing raw personal identifiers | 0 of 21 |
| Quality-gate red-team: planted faults caught | 56/56 (100%) |
| Automated test suite (pytest) | 87 passed, 1 warning in 15.97s (exit code 0) |

### How each metric is measured

- **Retrieval correctness**: the report cites at least one of the handbook sections the case is expected to need (and every section marked as mandatory); for out-of-scope cases, the system must report that no evidence was found.
- **Routing / urgency**: POSH cases must go to the Internal Committee route; threats must be marked URGENT; ordinary cases must not be over-escalated.
- **Evidence grounding** is re-computed here, independently of the Quality & Risk Gate, from the final saved reports.
- **Red-team**: for each sample case, eight or nine known faults (fabricated section, unretrieved section, altered quote, guilt/penalty statement, invented statute and deadline, non-existent § reference, complaint text placed under confirmed facts, phone number, and — for POSH — removal of the IC route) are planted in a copy of the report, and the gate is run again.

## Detailed results

### Documented sample cases (data/sample_cases/sample_cases.json)

| Case | Description | Workflow | Retrieval | Route | Urgency | Escalation | Missing info | Retrieved sections | Gate |
|---|---|---|---|---|---|---|---|---|---|
| S1 | Alleged unpaid overtime | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | ✅ | ✅ | 3.1, 3.2, 3.3, 3.5, 4.3, 4.5 | warnings |
| S2 | Leave balance dispute | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | ✅ | ✅ | 5.1, 5.2, 5.3, 5.4, 5.5, 9.6 | warnings |
| S3 | Complaint about manager's behaviour | ✅ | ✅ | ✅ (standard_er) | ✅ (HIGH) | ✅ | — | 10.1, 14, 2.4, 3.1, 9.1, 9.6 | warnings |
| S4 | Sexual harassment concern (statutory routing) | ✅ | ✅ | ✅ (posh_ic) | ✅ (HIGH) | ✅ | — | 8.1, 8.2, 8.3, 8.4, 8.5, Appendix | warnings |
| S5 | Unexplained employee absence | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | ✅ | ✅ | 3.1, 5.1, 5.2, 5.3, 5.4, 5.5 | warnings |
| S6 | Threat after a safety report (urgent) | ✅ | ✅ | ✅ (urgent_safety) | ✅ (URGENT) | ✅ | — | 10.1, 7.1, 7.2, 7.3, 7.4, 8.5, 9.6 | warnings |
| S7 | Out-of-scope request with injected instructions | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | ✅ | — | none | warnings |

### Wording variants (data/sample_cases/eval_variants.json)

| Case | Description | Workflow | Retrieval | Route | Urgency | Escalation | Missing info | Retrieved sections | Gate |
|---|---|---|---|---|---|---|---|---|---|
| V1 | Extra hours missing from pay — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | — | — | 3.2, 3.3, 3.5, 4.1, 4.3, 4.5 | warnings |
| V2 | Leave cancelled — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | — | — | 4.4, 5.1, 5.2, 5.3, 5.5, 6.4 | warnings |
| V3 | Supervisor belittles me — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | — | — | 2.4, 9.1, 9.6 | warnings |
| V4 | Uncomfortable behaviour by colleague — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (posh_ic) | ✅ (HIGH) | — | — | 3.4, 5.1, 5.2, 5.3, 5.4, 5.5, 8.2, 8.3, 8.4 | warnings |
| V5 | Warehouse incident — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (urgent_safety) | ✅ (URGENT) | — | — | 3.1, 3.2, 5.1, 5.3, 5.4, 5.5, 7.2, 7.4 | warnings |
| V6 | Not coming to work — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | — | — | 11.2, 14, 5.2, 5.3, 5.4, 5.5 | warnings |
| V7 | Final settlement delayed — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | — | — | 11.1, 11.2, 11.3, 11.4, 11.5, 4.1 | warnings |
| V8 | Uniform colour — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | — | — | none | warnings |
| V9 | Maternity benefit query — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (standard_er) | ✅ (HIGH) | — | — | 3.1, 3.4, 6.1, 6.2, 6.3, Appendix | warnings |
| V10 | Pinched at work — _first-round (used to fix rules)_ | ✅ | ✅ | ✅ (posh_ic) | ✅ (HIGH) | — | — | 2.4, 8.2, 8.3, 8.4, 9.1 | warnings |
| V11 | Shift manager standing too close — _fresh held-out_ | ✅ | ❌ | ❌ (standard_er) | ❌ (STANDARD) | — | — | 10.3, 14, 2.4, 3.1, 9.1, 9.6 | warnings |
| V12 | Fine deducted from wages — _fresh held-out_ | ✅ | ✅ | ✅ (standard_er) | ✅ (STANDARD) | — | — | 14, 4.1, 4.2, 4.3, 4.4, 4.5 | warnings |
| V13 | Weapon mentioned in canteen — _fresh held-out_ | ✅ | ✅ | ✅ (urgent_safety) | ✅ (URGENT) | — | — | 10.1, 14, 5.3, 7.2, 7.4, 8.5 | warnings |
| V14 | Promotion denied — _fresh held-out_ | ✅ | ✅ | ✅ (standard_er) | ✅ (HIGH) | — | — | 2.2, 5.2, 6.2, 6.5 | warnings |

> **Disclosure.** V1–V10 were written before the first evaluation run. That run missed two indirect sexual-harassment descriptions (V4, V10) and wrongly retrieved benefits sections for an out-of-scope request (V8, caused by keyword substring matching). The detection patterns and keyword matching were then fixed, so V1–V10 are no longer an unbiased test. V11–V14 were written after the fixes and before this run as a fresh held-out check; any failure there is reported as-is below.

### Red-team results per case

| Case | Caught | Missed |
|---|---|---|
| S1 | 8/8 | — |
| S2 | 8/8 | — |
| S3 | 8/8 | — |
| S4 | 9/9 | — |
| S5 | 8/8 | — |
| S6 | 8/8 | — |
| S7 | 7/7 | — |

## Known failures

- **V11 — Shift manager standing too close — _fresh held-out_**: failed retrieval, route, urgency (route standard_er, urgency STANDARD, retrieved 10.3, 14, 2.4, 3.1, 9.1, 9.6).

## Known limitations

- Sensitive-case detection is keyword- and pattern-based. Indirect descriptions (for example of unwelcome physical contact without sexual vocabulary) can be missed; the held-out variants above show where. A configured language model can add flags but this has not been evaluated against a real model here.
- Retrieval is lexical (BM25 with an HR-vocabulary expansion table). Wording far from both the handbook and the expansion table can retrieve weaker sections.
- The Compliance Reviewer has no live legal research. It names frameworks only from the handbook's own reference map, so every legal point is unverified by design.
- The red-team faults are known patterns; a model could produce unsupported content the gate's rules do not recognise (for example a paraphrased guilt finding).
- The test set is small and synthetic, written by the system's author. Results indicate behaviour on these cases only, not real-world accuracy.
- Per-browser workspaces isolate cases between visitors but are not authentication.
