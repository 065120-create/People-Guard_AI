"""Data shapes shared by the API, the agents and storage."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

GRIEVANCE_CATEGORIES: dict[str, str] = {
    "wages_overtime": "Wages, pay or overtime",
    "leave_absence": "Leave, holidays or absence",
    "manager_conduct": "Manager or co-worker conduct",
    "posh": "Sexual harassment (POSH)",
    "safety_threat": "Workplace safety, threats or violence",
    "discrimination": "Discrimination or unfair treatment",
    "maternity": "Maternity or parental support",
    "disciplinary": "Conduct / disciplinary process",
    "separation": "Resignation, termination or final settlement",
    "social_security": "Social security and benefits",
    "data_privacy": "Employee data or confidentiality",
    "other": "Other / not sure",
}

EMPLOYEE_CATEGORIES = {
    "": "Not known",
    "permanent": "Permanent / regular",
    "fixed_term": "Fixed-term",
    "probationer": "Probationer",
    "trainee_apprentice": "Trainee / apprentice",
    "contract_labour": "Contract labour (via contractor)",
    "managerial": "Managerial / supervisory",
    "part_time": "Part-time",
}

ESTABLISHMENT_TYPES = {
    "": "Not known",
    "retail_store": "Retail store",
    "warehouse": "Warehouse",
    "corporate_office": "Corporate office",
}

REPORTED_BY = {
    "": "Not stated",
    "employee": "The employee concerned",
    "manager": "A manager / supervisor",
    "hr": "HR / Employee Relations",
    "colleague": "A colleague / witness",
    "anonymous": "Anonymous",
}


class CaseInput(BaseModel):
    """What a user submits. Every field except title/description may be blank."""

    title: str = Field(..., min_length=3, max_length=150)
    description: str = Field(..., min_length=20, max_length=8000)
    employee_ref: str = Field("", max_length=40)
    work_location: str = Field("", max_length=120)
    establishment_type: str = Field("", max_length=40)
    employee_category: str = Field("", max_length=40)
    grievance_category: str = Field("other", max_length=40)
    reported_by: str = Field("", max_length=40)
    relevant_dates: str = Field("", max_length=300)
    requested_outcome: str = Field("", max_length=500)
    sample_case_id: Optional[str] = Field(None, max_length=40)

    @field_validator("title", "description", "employee_ref", "work_location", "relevant_dates", "requested_outcome")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("grievance_category")
    @classmethod
    def _cat(cls, v: str) -> str:
        v = (v or "other").strip()
        if v not in GRIEVANCE_CATEGORIES:
            raise ValueError(f"Unknown grievance category '{v}'.")
        return v

    @field_validator("employee_category")
    @classmethod
    def _emp(cls, v: str) -> str:
        v = (v or "").strip()
        if v not in EMPLOYEE_CATEGORIES:
            raise ValueError(f"Unknown employee category '{v}'.")
        return v

    @field_validator("establishment_type")
    @classmethod
    def _est(cls, v: str) -> str:
        v = (v or "").strip()
        if v not in ESTABLISHMENT_TYPES:
            raise ValueError(f"Unknown establishment type '{v}'.")
        return v

    @field_validator("reported_by")
    @classmethod
    def _rep(cls, v: str) -> str:
        v = (v or "").strip()
        if v not in REPORTED_BY:
            raise ValueError(f"Unknown reporter type '{v}'.")
        return v


AgentStatus = Literal["pending", "running", "completed", "fallback", "failed", "skipped"]
AgentMode = Literal["llm", "simulated", "rules"]


class ReviewDecision(BaseModel):
    decision: Literal["approve", "return"]
    reviewer: str = Field(..., min_length=2, max_length=80)
    notes: str = Field("", max_length=2000)


# ---------------------------------------------------------------------------
# Structured outputs requested from the language model, one per agent.
# The LLM's JSON is validated against these before it is used anywhere.
# ---------------------------------------------------------------------------


class OrchestratorLLM(BaseModel):
    suggested_category: str = ""
    category_rationale: str = ""
    additional_sensitivity_flags: list[str] = []
    neutral_issue_statement: str = ""


class PolicyFinding(BaseModel):
    chunk_id: str
    relevance: str
    how_it_applies: str = ""


class PolicyExpertLLM(BaseModel):
    findings: list[PolicyFinding] = []
    gaps: list[str] = []
    policy_vs_law_notes: list[str] = []


class AnalystLLM(BaseModel):
    neutral_summary: str
    allegations: list[str] = []
    documentary_evidence: list[str] = []
    unresolved_questions: list[str] = []
    missing_information: list[str] = []
    follow_up_questions: list[str] = []
    conflicts_of_interest: list[str] = []
    recommended_process: list[str] = []


class ComplianceIssueLLM(BaseModel):
    topic: str
    framework_chunk_id: str = ""
    why_relevant: str = ""
    facts_needed: list[str] = []


class ComplianceLLM(BaseModel):
    issues: list[ComplianceIssueLLM] = []
    escalate_to_legal: bool = False
    escalation_reason: str = ""


class DocumentationLLM(BaseModel):
    executive_summary: str
    recommended_next_steps: list[str] = []


class QualityLLM(BaseModel):
    concerns: list[str] = []
