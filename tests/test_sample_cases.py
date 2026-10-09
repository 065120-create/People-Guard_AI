"""Each synthetic sample case must produce its documented expected behaviour."""
import json

import pytest

from app.services.rules import find_pii
from conftest import SAMPLES, get_case, submit


@pytest.mark.parametrize("sc", SAMPLES, ids=[s["id"] for s in SAMPLES])
def test_sample_case_expectations(client, sc):
    exp = sc["expected"]
    res = submit(client, sc["id"])
    data = get_case(client, res["case_id"])
    case, rep = data["case"], data["case"]["report"]
    tri = case["triage"]

    assert all(a["status"] == "completed" for a in data["agents"])
    assert case["route"] == exp["route"]
    assert tri["urgency"] == exp["urgency"]
    assert case["escalation_flag"] is exp["escalation_flag"]

    numbers = {p["ref"].split()[0].lstrip("§") for p in rep["policy_provisions"]}
    if exp["must_retrieve_any"]:
        assert numbers & set(exp["must_retrieve_any"]), numbers
    for n in exp["must_retrieve_all"]:
        assert n in numbers, (n, numbers)
    if exp.get("no_evidence"):
        assert rep["policy_provisions"] == [] and rep["policy_no_evidence"] is True

    missing = " ".join(m["item"] for m in rep["missing_information"])
    for item in exp["missing_info_contains"]:
        assert item in missing, (item, missing)

    targets = {e["to"] for e in rep["escalation"]["items"]}
    for t in exp["escalation_to"]:
        assert t in targets, (t, targets)
    for t in exp["escalation_not_to"]:
        assert t not in targets, (t, targets)

    for f in exp.get("flags_include", []):
        assert f in tri["flag_keys"], (f, tri["flag_keys"])
    if exp.get("restricted"):
        assert case["restricted"] is True
    if exp.get("statements_hidden"):
        assert rep["reported_concern"]["statements"] == []
    if exp.get("urgent_actions"):
        assert rep["escalation"]["urgent_actions"]
    if exp.get("no_raw_pii_in_report"):
        assert not find_pii(json.dumps(rep))
    for phrase in exp.get("report_must_not_conclude", []):
        narrative = rep["executive_summary"] + " " + " ".join(s["step"] for s in rep["next_steps"])
        assert phrase not in narrative

    legal_topics = {i["topic"] for i in rep["compliance"]["issues"]}
    for row in exp["legal_rows"]:
        assert row in legal_topics, (row, legal_topics)
    assert case["gate"]["verdict"] != "blocked"
