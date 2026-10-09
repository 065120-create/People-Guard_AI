"""Case creation, validation, isolation, review, deletion and report generation."""
from fastapi.testclient import TestClient

from conftest import get_case, submit

AGENT_KEYS = ["orchestrator", "policy_expert", "er_case_analyst", "compliance_reviewer", "documentation_specialist", "quality_gate"]


def test_create_case_runs_all_six_agents(client):
    res = submit(client, "S1")
    assert res["case_id"].startswith("PG-")
    data = get_case(client, res["case_id"])
    assert [a["agent"] for a in data["agents"]] == AGENT_KEYS
    assert all(a["status"] == "completed" for a in data["agents"])
    assert data["case"]["status"] == "awaiting_review"
    assert data["case"]["report"]["case_id"] == res["case_id"]


def test_blank_optional_fields_are_reported_missing(client):
    res = submit(client, title="Pay query", description="My salary for August seems lower than expected and I do not know why.", grievance_category="wages_overtime")
    rep = get_case(client, res["case_id"])["case"]["report"]
    missing = " ".join(m["item"] for m in rep["missing_information"])
    for item in ["Work location", "Employee category", "Relevant dates", "Payslip"]:
        assert item in missing
    facts = rep["compliance"]["applicability_facts"]
    assert any(f["value"] is None for f in facts)


def test_invalid_inputs_rejected(client):
    r = client.post("/api/cases", data={"title": "Hi", "description": "too short"})
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["detail"]["errors"]}
    assert {"title", "description"} <= fields
    r = client.post("/api/cases", data={"title": "Valid title", "description": "A long enough description of the issue.", "grievance_category": "nonsense"})
    assert r.status_code == 422
    r = client.post("/api/cases", data={"title": "Valid title", "description": "A long enough description of the issue."}, files={"document": ("x.exe", b"MZ...", "application/octet-stream")})
    assert r.status_code == 422 and "Unsupported file type" in r.text
    r = client.post("/api/cases", data={"title": "Valid title", "description": "A long enough description of the issue.", "sample_document": "../../app/config.py"})
    assert r.status_code == 400


def test_uploaded_document_is_extracted(client):
    res = submit(client, title="Overtime", description="I worked overtime on 3 September and it was not paid in my payslip.",
                 grievance_category="wages_overtime")
    files = {"document": ("note.txt", b"Timesheet: 03 Sep 2026 worked until 22:00. Overtime not paid.", "text/plain")}
    r = client.post("/api/cases", data={"title": "Overtime with doc", "description": "I worked overtime on 3 September and it was not paid.", "grievance_category": "wages_overtime", "sync": "true"}, files=files)
    assert r.status_code == 201
    case = get_case(client, r.json()["case_id"])["case"]
    assert case["documents"][0]["name"] == "note.txt"
    assert "text" not in case["documents"][0]  # raw text is not sent back to the browser
    ev = case["report"]["confirmed_facts"]["documentary_evidence"]
    assert ev and "not authenticated" in ev[0]["status"]
    assert res["case_id"] != r.json()["case_id"]


def test_workspace_isolation(client):
    res = submit(client, "S2")
    other = TestClient(client.app)
    with other:
        assert other.get(f"/api/cases/{res['case_id']}").status_code == 404
        assert other.get("/api/cases").json()["cases"] == []
        assert other.delete(f"/api/cases/{res['case_id']}").status_code == 404
    assert client.get(f"/api/cases/{res['case_id']}").status_code == 200


def test_dashboard_counts_are_real(client):
    assert client.get("/api/stats").json()["counts"]["total"] == 0
    submit(client, "S1")
    urgent = submit(client, "S6")
    posh = submit(client, "S4")
    c = client.get("/api/stats").json()["counts"]
    assert c["total"] == 3
    assert c["awaiting_review"] == 3
    assert c["escalated"] == 2
    assert c["urgent"] == 1
    assert c["completed"] == 0
    client.post(f"/api/cases/{urgent['case_id']}/review", json={"decision": "approve", "reviewer": "HR Manager"})
    c = client.get("/api/stats").json()["counts"]
    assert c["completed"] == 1 and c["escalated"] == 1
    assert posh["route"] == "posh_ic"


def test_restricted_case_title_hidden_in_lists(client):
    res = submit(client, "S4")
    listed = client.get("/api/cases").json()["cases"]
    row = next(c for c in listed if c["id"] == res["case_id"])
    assert row["title"] == "Restricted case"


def test_review_approve_and_return(client):
    res = submit(client, "S2")
    cid = res["case_id"]
    r = client.post(f"/api/cases/{cid}/review", json={"decision": "return", "reviewer": "ER Lead", "notes": "Add ledger"})
    assert r.json()["status"] == "returned"
    r = client.post(f"/api/cases/{cid}/review", json={"decision": "approve", "reviewer": "ER Lead"})
    assert r.json()["status"] == "approved"
    case = get_case(client, cid)["case"]
    assert case["report"]["human_review"]["status"] == "approved"
    assert len(case["review"]["history"]) == 2
    assert client.post(f"/api/cases/{cid}/review", json={"decision": "approve", "reviewer": ""}).status_code == 422


def test_delete_case_and_delete_all(client):
    a = submit(client, "S1")["case_id"]
    submit(client, "S2")
    assert client.delete(f"/api/cases/{a}").status_code == 200
    assert client.get(f"/api/cases/{a}").status_code == 404
    assert client.delete("/api/cases").json()["deleted"] == 1
    assert client.get("/api/cases").json()["cases"] == []


def test_report_downloads(client):
    cid = submit(client, "S3")["case_id"]
    pdf = client.get(f"/api/cases/{cid}/report.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and len(pdf.content) > 3000
    docx = client.get(f"/api/cases/{cid}/report.docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    import io

    from docx import Document

    text = "\n".join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)
    for heading in ["1. Case ID and category", "5. Relevant HR policy provisions", "10. Escalation requirements", "12. Human review and approval status"]:
        assert heading in text
    assert client.get(f"/api/cases/{cid}/report.txt").status_code == 404


def test_report_has_twelve_sections(client):
    from app.services.report_export import report_sections

    cid = submit(client, "S1")["case_id"]
    case = get_case(client, cid)["case"]
    titles = [t for t, _ in report_sections(case)]
    assert len(titles) == 12
    assert titles[0].startswith("1. ") and titles[-1].startswith("12. ")


def test_rerun_workflow(client):
    cid = submit(client, "S5")["case_id"]
    r = client.post(f"/api/cases/{cid}/run")
    assert r.status_code == 200
    data = get_case(client, cid)
    assert all(a["status"] == "completed" for a in data["agents"])


def test_load_all_samples(client):
    created = client.post("/api/samples/load-all").json()["created"]
    assert len(created) == 7
    statuses = {c["status"] for c in client.get("/api/cases").json()["cases"]}
    assert statuses <= {"awaiting_review", "escalated_urgent"}
