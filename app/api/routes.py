"""JSON API used by the web interface, tests and scripts."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError

from app.agents.orchestrator import triage
from app.models.schemas import (
    EMPLOYEE_CATEGORIES,
    ESTABLISHMENT_TYPES,
    GRIEVANCE_CATEGORIES,
    REPORTED_BY,
    CaseInput,
    ReviewDecision,
)
from app.services import report_export
from app.services.documents import DocumentError, extract_text
from app.services.llm import LLMClient
from app.services.stress_test import run_stress_test
from app.services.workflow import run_workflow

router = APIRouter(prefix="/api")
health_router = APIRouter()

STATUS_LABELS = {
    "submitted": "Submitted",
    "running": "Analysis running",
    "awaiting_review": "Awaiting human review",
    "escalated_urgent": "Urgent — awaiting human action",
    "approved": "Approved by reviewer",
    "returned": "Returned for rework",
    "error": "Workflow error",
}


# ---------------------------------------------------------------------------
def _ctx(request: Request):
    app = request.app
    if not hasattr(app.state, "llm"):
        app.state.llm = LLMClient(app.state.settings)
    return app.state.settings, app.state.db, app.state.index, app.state.llm, request.state.workspace


def _samples(settings) -> list[dict]:
    path = settings.sample_case_dir / "sample_cases.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def _sample_document(settings, name: str) -> dict:
    samples = {s["document"] for s in _samples(settings) if s.get("document")}
    if name not in samples:  # whitelist: only the bundled sample documents
        raise HTTPException(400, "Unknown sample document.")
    p = settings.sample_case_dir / name
    return extract_text(p.name, p.read_bytes(), int(settings.max_upload_mb * 1024 * 1024))


def _create_and_maybe_run(request: Request, background: BackgroundTasks | None, case_input: CaseInput, documents: list[dict], run: bool, sync: bool = False):
    settings, db, index, llm, ws = _ctx(request)
    if not index.ready:
        raise HTTPException(503, "No policy handbook is indexed. Add the handbook to data/policies and re-index.")
    inp = case_input.model_dump()
    tri = triage(inp, documents, index)
    case = db.create_case(ws, inp, tri, documents, "live" if llm.enabled else "demo")
    if run:
        if sync or background is None:
            run_workflow(db, ws, case["id"], settings, index, llm)
        else:
            background.add_task(run_workflow, db, ws, case["id"], settings, index, llm)
    return case, tri


def _public_case(case: dict, listing: bool = False) -> dict:
    case = dict(case)
    if listing and case.get("restricted"):
        # Lists never show the title of a restricted (POSH) case.
        case["title"] = "Restricted case"
    case["status_label"] = STATUS_LABELS.get(case["status"], case["status"])
    docs = case.get("documents") or []
    case["documents"] = [{k: d.get(k) for k in ("name", "chars", "truncated", "pii")} for d in docs]
    return case


# ---------------------------------------------------------------------------
@health_router.get("/health")
def health(request: Request):
    settings = request.app.state.settings
    index = request.app.state.index
    return {
        "status": "ok",
        "version": request.app.state.version,
        "policy_index_ready": index.ready,
        "policy_sections": len(index.sections),
        "llm": settings.llm_status(),
    }


@router.get("/config")
def get_config(request: Request):
    settings = request.app.state.settings
    return {
        "llm": settings.llm_status(),
        "categories": GRIEVANCE_CATEGORIES,
        "employee_categories": EMPLOYEE_CATEGORIES,
        "establishment_types": ESTABLISHMENT_TYPES,
        "reported_by": REPORTED_BY,
        "max_upload_mb": settings.max_upload_mb,
        "policy_upload_enabled": bool(settings.admin_token),
        "access_code_enabled": bool(settings.access_code),
    }


@router.get("/stats")
def stats(request: Request):
    _, db, _, _, ws = _ctx(request)
    cases = db.list_cases(ws)
    return {"counts": db.stats(ws), "recent_activity": db.recent_activity(ws, 8), "recent_cases": [_public_case(c, listing=True) for c in cases[:6]]}


# -- cases -------------------------------------------------------------------
@router.get("/cases")
def list_cases(request: Request):
    _, db, _, _, ws = _ctx(request)
    return {"cases": [_public_case(c, listing=True) for c in db.list_cases(ws)]}


@router.post("/cases", status_code=201)
async def create_case(
    request: Request,
    background: BackgroundTasks,
    title: str = Form(""),
    description: str = Form(""),
    employee_ref: str = Form(""),
    work_location: str = Form(""),
    establishment_type: str = Form(""),
    employee_category: str = Form(""),
    grievance_category: str = Form("other"),
    reported_by: str = Form(""),
    relevant_dates: str = Form(""),
    requested_outcome: str = Form(""),
    sample_case_id: str = Form(""),
    sample_document: str = Form(""),
    run: bool = Form(True),
    sync: bool = Form(False),
    document: UploadFile | None = File(None),
):
    settings = request.app.state.settings
    try:
        case_input = CaseInput(
            title=title, description=description, employee_ref=employee_ref, work_location=work_location,
            establishment_type=establishment_type, employee_category=employee_category, grievance_category=grievance_category or "other",
            reported_by=reported_by, relevant_dates=relevant_dates, requested_outcome=requested_outcome, sample_case_id=sample_case_id or None,
        )
    except ValidationError as exc:
        errors = [{"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"].replace("Value error, ", "")} for e in exc.errors()]
        raise HTTPException(422, {"message": "Please correct the highlighted fields.", "errors": errors})

    documents = []
    try:
        if document is not None and document.filename:
            data = await document.read(int(settings.max_upload_mb * 1024 * 1024) + 1)
            documents.append(extract_text(document.filename, data, int(settings.max_upload_mb * 1024 * 1024)))
        elif sample_document:
            documents.append(_sample_document(settings, sample_document))
    except DocumentError as exc:
        raise HTTPException(422, {"message": str(exc), "errors": [{"field": "document", "message": str(exc)}]})

    warnings = []
    from app.services.rules import find_pii

    if find_pii(" ".join([case_input.description, case_input.employee_ref])):
        warnings.append("The submission appears to contain personal identifiers (phone, email, ID or account numbers). They are redacted from AI prompts and reports. Use synthetic data only.")
    case, tri = _create_and_maybe_run(request, background, case_input, documents, run, sync)
    return {
        "case_id": case["id"],
        "urgent": tri["urgency"] == "URGENT",
        "urgency": tri["urgency"],
        "route": tri["route"],
        "route_label": tri["route_label"],
        "urgent_actions": tri["urgent_actions"],
        "flags": tri["flags"],
        "warnings": warnings,
    }


@router.get("/cases/{case_id}")
def get_case(request: Request, case_id: str):
    _, db, _, _, ws = _ctx(request)
    case = db.get_case(ws, case_id)
    if not case:
        raise HTTPException(404, "Case not found.")
    return {"case": _public_case(case), "agents": db.get_agents(case_id)}


@router.get("/cases/{case_id}/status")
def case_status(request: Request, case_id: str):
    _, db, _, _, ws = _ctx(request)
    case = db.get_case(ws, case_id)
    if not case:
        raise HTTPException(404, "Case not found.")
    agents = [{k: a[k] for k in ("agent", "name", "status", "mode", "duration_ms", "error")} for a in db.get_agents(case_id)]
    return {"status": case["status"], "status_label": STATUS_LABELS.get(case["status"]), "agents": agents}


@router.post("/cases/{case_id}/run")
def rerun_case(request: Request, case_id: str, background: BackgroundTasks):
    settings, db, index, llm, ws = _ctx(request)
    case = db.get_case(ws, case_id)
    if not case:
        raise HTTPException(404, "Case not found.")
    if case["status"] == "running":
        raise HTTPException(409, "The workflow is already running for this case.")
    db.update_case(case_id, report=None, gate=None, review={"status": "pending", "history": (case.get("review") or {}).get("history", [])})
    background.add_task(run_workflow, db, ws, case_id, settings, index, llm)
    return {"case_id": case_id, "started": True}


@router.post("/cases/{case_id}/review")
def review_case(request: Request, case_id: str, decision: ReviewDecision):
    _, db, _, _, ws = _ctx(request)
    case = db.get_case(ws, case_id)
    if not case:
        raise HTTPException(404, "Case not found.")
    if not case.get("report"):
        raise HTTPException(409, "There is no completed draft report to review yet.")
    if decision.decision == "approve" and (case.get("gate") or {}).get("verdict") == "blocked":
        raise HTTPException(409, "The Quality & Risk Gate blocked this draft. Re-run or return it instead of approving.")
    from app.storage.db import now

    entry = {"decision": decision.decision, "reviewer": decision.reviewer, "notes": decision.notes, "at": now()}
    review = case.get("review") or {"history": []}
    review["history"] = review.get("history", []) + [entry]
    review["status"] = "approved" if decision.decision == "approve" else "returned"
    report = case["report"]
    report["human_review"] = {"status": review["status"], "required": True, "reviewer": decision.reviewer, "notes": decision.notes, "decided_at": entry["at"]}
    db.update_case(case_id, review=review, report=report, status=review["status"])
    db.log(ws, case_id, f"Case {case_id} {'approved' if decision.decision == 'approve' else 'returned'} by {decision.reviewer}")
    return {"case_id": case_id, "status": review["status"]}


@router.delete("/cases/{case_id}")
def delete_case(request: Request, case_id: str):
    _, db, _, _, ws = _ctx(request)
    if not db.delete_case(ws, case_id):
        raise HTTPException(404, "Case not found.")
    return {"deleted": case_id}


@router.delete("/cases")
def delete_all_cases(request: Request):
    _, db, _, _, ws = _ctx(request)
    return {"deleted": db.delete_all(ws)}


@router.get("/cases/{case_id}/report.{fmt}")
def download_report(request: Request, case_id: str, fmt: str):
    _, db, _, _, ws = _ctx(request)
    case = db.get_case(ws, case_id)
    if not case or not case.get("report"):
        raise HTTPException(404, "No report available for this case.")
    if fmt == "docx":
        data = report_export.build_docx(case)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif fmt == "pdf":
        data = report_export.build_pdf(case)
        media = "application/pdf"
    else:
        raise HTTPException(404, "Use .pdf or .docx")
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{case_id}_HR_case_report_DRAFT.{fmt}"'})


@router.post("/cases/{case_id}/gate-stress-test")
def gate_stress_test(request: Request, case_id: str):
    settings, db, index, llm, ws = _ctx(request)
    case = db.get_case(ws, case_id)
    if not case:
        raise HTTPException(404, "Case not found.")
    try:
        return run_stress_test(case, db.get_agents(case_id), index, settings, llm)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


# -- samples -----------------------------------------------------------------
@router.get("/samples")
def list_samples(request: Request):
    return {"samples": [{k: s.get(k) for k in ("id", "label", "handbook_example", "input", "document", "expected")} for s in _samples(request.app.state.settings)]}


@router.post("/samples/load-all")
def load_all_samples(request: Request, background: BackgroundTasks):
    settings = request.app.state.settings
    created = []
    for s in _samples(settings):
        inp = CaseInput(**s["input"], sample_case_id=s["id"])
        docs = [_sample_document(settings, s["document"])] if s.get("document") else []
        case, _ = _create_and_maybe_run(request, background, inp, docs, True)
        created.append(case["id"])
    return {"created": created}


# -- policies ----------------------------------------------------------------
@router.get("/policies")
def policies(request: Request):
    index = request.app.state.index
    sections = [
        {k: s.to_dict()[k] for k in ("chunk_id", "document", "section_number", "heading", "parent", "ref", "kind", "retrievable", "row_label")}
        for s in index.sections
    ]
    return {"summary": index.summary(), "sections": sections}


@router.get("/policies/search")
def policy_search(request: Request, q: str = "", category: str = "", all_sections: bool = False):
    settings, _, index, _, _ = _ctx(request)
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(422, "Enter at least two characters to search.")
    hits, meta = index.search(q[:500], category=category or None, top_k=10, min_score=settings.retrieval_min_score, include_non_evidence=all_sections)
    results = [h.to_dict(400) for h in hits]
    message = None if results else "No relevant handbook evidence was found for this search. The system will not invent a policy; refine the search or refer the question to HR."
    return {"query": q, "results": results, "meta": meta, "message": message}


@router.get("/policies/section/{chunk_id}")
def policy_section(request: Request, chunk_id: str):
    sec = request.app.state.index.get(chunk_id)
    if not sec:
        raise HTTPException(404, "Section not found.")
    return sec.to_dict()


@router.post("/policies/reindex")
def reindex(request: Request):
    settings = request.app.state.settings
    summary = request.app.state.index.build(settings.policy_dir)
    return {"reindexed": True, "summary": summary}


@router.post("/policies/upload")
async def upload_policy(request: Request, admin_token: str = Form(""), file: UploadFile = File(...)):
    settings = request.app.state.settings
    if not settings.admin_token:
        raise HTTPException(403, "Policy upload is disabled. Set ADMIN_TOKEN on the server to enable it.")
    import hmac as _hmac

    if not _hmac.compare_digest(admin_token, settings.admin_token):
        raise HTTPException(403, "Invalid admin token.")
    name = Path(file.filename or "").name
    if not name.lower().endswith((".docx", ".pdf", ".txt", ".md")):
        raise HTTPException(422, "Upload a .docx, .pdf, .txt or .md handbook.")
    data = await file.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(422, "Handbook is larger than 10 MB.")
    import tempfile

    from app.retrieval.handbook_parser import parse_policy_file

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / name
        tmp.write_bytes(data)
        try:
            parsed = parse_policy_file(tmp)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, f"Could not read the handbook: {exc}")
        if not any(sec.retrievable for sec in parsed.sections):
            raise HTTPException(422, "No headed policy sections were found. Use Heading 1/Heading 2 styles (DOCX) or numbered headings.")
        shutil.copy(tmp, settings.policy_dir / name)
    try:
        summary = request.app.state.index.build(settings.policy_dir)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Saved, but re-indexing failed: {exc}")
    return {"uploaded": name, "summary": summary}
