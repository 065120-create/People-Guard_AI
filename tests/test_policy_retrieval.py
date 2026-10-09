"""Policy document extraction and retrieval."""
from pathlib import Path

from app.retrieval.handbook_parser import parse_policy_file, parse_text
from app.retrieval.index import PolicyIndex

ROOT = Path(__file__).resolve().parent.parent
HANDBOOK = ROOT / "data" / "policies" / "PeopleGuard_Fictional_HR_Policy_Book_India.docx"


def test_handbook_extraction_preserves_headings():
    doc = parse_policy_file(HANDBOOK)
    numbers = {s.section_number for s in doc.sections}
    for n in ["1.1", "3.3", "4.5", "5.4", "5.5", "8.2", "8.3", "9.6", "13.5"]:
        assert n in numbers, n
    s33 = next(s for s in doc.sections if s.section_number == "3.3")
    assert s33.heading == "Overtime"
    assert s33.ref == "§3.3 Overtime"
    assert "Overtime must be approved and recorded accurately" in s33.text


def test_tables_become_citable_rows():
    doc = parse_policy_file(HANDBOOK)
    roles = [s for s in doc.sections if s.kind == "escalation_table"]
    assert {r.row_label for r in roles} >= {"Line manager", "Payroll / Benefits", "Legal / Compliance", "IC / designated statutory body"}
    legal = [s for s in doc.sections if s.kind == "legal_map" and s.row_label]
    assert any("Sexual harassment" in s.row_label for s in legal)
    assert all("Primary legal framework to verify" in s.text for s in legal)


def test_rules_and_examples_are_not_case_evidence():
    doc = parse_policy_file(HANDBOOK)
    for s in doc.sections:
        if s.kind in ("rules", "example", "meta"):
            assert s.retrievable is False, s.ref


def test_chunk_ids_unique():
    doc = parse_policy_file(HANDBOOK)
    ids = [s.chunk_id for s in doc.sections]
    assert len(ids) == len(set(ids))


def test_retrieval_finds_overtime_policy(_index):
    hits, meta = _index.search("I stayed late several days and my payslip shows no overtime", category="wages_overtime")
    refs = [h.section.section_number for h in hits]
    assert "3.3" in refs
    assert "overtime" in meta["expansions"]


def test_retrieval_everyday_language_maps_to_policy(_index):
    hits, _ = _index.search("my boss keeps shouting at me and humiliating me in front of customers")
    assert hits and hits[0].section.section_number == "2.4"


def test_retrieval_reports_no_evidence(_index):
    hits, _ = _index.search("Please change the cafeteria menu and add a reserved parking spot")
    assert hits == []


def test_search_endpoint_no_evidence_message(client):
    r = client.get("/api/policies/search", params={"q": "cafeteria menu parking"})
    body = r.json()
    assert body["results"] == []
    assert "will not invent a policy" in body["message"]


def test_search_endpoint_returns_references(client):
    body = client.get("/api/policies/search", params={"q": "Internal Committee"}).json()
    assert body["results"]
    assert body["results"][0]["ref"].startswith("§8.")
    assert body["results"][0]["document"].endswith(".docx")


def test_reindex_and_section_lookup(client):
    r = client.post("/api/policies/reindex")
    assert r.status_code == 200 and r.json()["summary"]["total_sections"] > 50
    sec = client.get("/api/policies/section/3-3-overtime").json()
    assert sec["section_number"] == "3.3"
    assert client.get("/api/policies/section/does-not-exist").status_code == 404


def test_policy_upload_disabled_without_admin_token(client):
    r = client.post("/api/policies/upload", data={"admin_token": "x"}, files={"file": ("h.txt", b"1. Test\nhello", "text/plain")})
    assert r.status_code == 403


def test_text_handbook_parsing(tmp_path):
    p = tmp_path / "mini.md"
    p.write_text("# 1. Leave\n## 1.1 Annual leave\nStaff get leave.\n## 1.2 Sick leave\nTell your manager.\n", encoding="utf-8")
    doc = parse_text(p)
    assert [s.section_number for s in doc.sections] == ["1.1", "1.2"]
    idx = PolicyIndex()
    idx.build(tmp_path)
    hits, _ = idx.search("sick leave manager", min_score=0.1)
    assert hits[0].section.section_number == "1.2"
