"""Deterministic safeguards shared by the agents and the Quality & Risk Gate.

These rules run in every mode (demo or live AI). A language model can add to
their findings but can never remove a safety flag, downgrade urgency or move a
POSH matter out of the Internal Committee route.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Sensitive-case detection
# ---------------------------------------------------------------------------

SENSITIVE_PATTERNS: dict[str, list[str]] = {
    "posh": [
        r"\bsexual(ly)?\b", r"\bgrop(e|ed|ing)\b", r"\bmolest", r"\bunwelcome (advance|touch|comment)",
        r"\bunwanted (touch|advance|sexual|physical)", r"touched (me|her|him|my)\b", r"\binappropriate(ly)? touch",
        r"\bexplicit (message|photo|image|picture|joke)", r"\bcomments? about my (body|appearance|figure)",
        r"\bsexual favou?r", r"\bposh\b", r"\bindecent", r"\bstalk(ed|ing)?\b",
        # unwelcome physical contact described without sexual vocabulary
        r"\b(pinch|grop|fondl|squeez)\w*\b.{0,25}\b(my|her|his) (waist|bottom|back|chest|breast|thigh|leg|hip)",
        r"\b(brush|rubb?)\w* (up )?against (me|her|him)\b", r"\b(kiss|hugg?)(ed)? me\b",
        r"\b(ask|asking|asked|asks) me out\b.{0,80}\b(said no|refused|declined|not interested)",
        r"\bafter i (said no|refused|declined)\b.{0,60}\b(message|text|call|follow)",
    ],
    "threat_violence": [
        r"\bthreat(en|ened|ening|s)?\b", r"\b(would|will|going to|gonna) (hurt|harm|hit|beat|kill)\b",
        r"\bkill\b", r"\bweapon\b", r"\bknife\b", r"\bgun\b", r"\bassault(ed)?\b", r"\bhit me\b",
        r"\bpushed me\b", r"\bslapp?ed\b", r"\bphysically attack", r"\bviolen(ce|t)\b", r"\bbeat (me|him|her)\b",
    ],
    "self_harm_welfare": [
        r"\bsuicid", r"\bend my life\b", r"\bkill myself\b", r"\bharm myself\b", r"\bself[- ]harm",
        r"\bdon'?t want to live\b",
    ],
    "safety_hazard": [
        r"\bunsafe\b", r"\bhazard", r"\bfire\b", r"\bgas leak", r"\bexposed wir", r"\bbroken (forklift|ladder|machine|equipment|racking)",
        r"\binjur(y|ed|ies)\b", r"\baccident\b", r"\bcollaps", r"\bnear miss",
    ],
    "retaliation": [
        r"\bretaliat", r"\bvictimi[sz]", r"\bbecause i (complained|reported|raised)", r"\bafter i (complained|reported|raised)",
        r"\bpunish(ed|ing)? (me )?for (complaining|reporting|raising)", r"\bif i complain(ed)? again\b",
        r"\bfor (complaining|reporting)\b", r"\bif i complain\b", r"\bafraid to complain\b",
    ],
    "discrimination": [
        r"\bdiscriminat", r"\bbecause (i am|i'm) (a woman|pregnant|disabled|muslim|hindu|christian|sikh|dalit|older)",
        r"\bcaste\b", r"\breligio", r"\bdisabilit", r"\bgender\b",
    ],
    "pregnancy_maternity": [r"\bpregnan", r"\bmaternity\b", r"\bnursing break", r"\bcr[eè]che\b"],
    "legal_action": [
        r"\blawyer\b", r"\badvocate\b", r"\bcourt\b", r"\blabou?r (commissioner|inspector|department|court)\b",
        r"\blegal notice\b", r"\bsue\b", r"\bpolice\b", r"\bfir\b",
    ],
    "repeated_issue": [r"\bagain\b", r"\brepeated(ly)?\b", r"\bevery month\b", r"\bsecond time\b", r"\bthird time\b", r"\bfor months\b"],
}

IMMEDIACY = [r"\bnow\b", r"\btoday\b", r"\btonight\b", r"\bimmediate", r"\bright away\b", r"\bscared to (come|go|return)", r"\bafraid to (come|go|return)", r"\bin danger\b"]

FLAG_LABELS = {
    "posh": "Possible sexual harassment (POSH) concern",
    "threat_violence": "Threat or violence indicated",
    "self_harm_welfare": "Possible risk to the employee's own safety",
    "safety_hazard": "Workplace safety hazard or injury",
    "retaliation": "Possible retaliation for raising a concern",
    "discrimination": "Possible discrimination / protected characteristic",
    "pregnancy_maternity": "Pregnancy or maternity-related",
    "legal_action": "External legal action or authority mentioned",
    "repeated_issue": "Issue described as repeated or ongoing",
    "prompt_injection": "Instruction-like text found in submitted material",
    "manager_involved": "Complaint concerns the employee's manager / supervisor",
}

URGENT_FLAGS = {"threat_violence", "self_harm_welfare"}
HIGH_FLAGS = {"posh", "retaliation", "discrimination", "safety_hazard", "legal_action", "pregnancy_maternity"}


def detect_flags(text: str) -> dict[str, list[str]]:
    """Return {flag: [matched snippets]} for every sensitive pattern found."""
    low = text.lower()
    found: dict[str, list[str]] = {}
    for flag, patterns in SENSITIVE_PATTERNS.items():
        hits = []
        for p in patterns:
            m = re.search(p, low)
            if m:
                hits.append(m.group(0))
        if hits:
            found[flag] = sorted(set(hits))
    if re.search(r"\b(my|our|the store|the|area|line|shift) (manager|supervisor|team lead|boss)\b", low) or re.search(r"\b(manager|supervisor)\b", low):
        found.setdefault("manager_mentioned", ["manager/supervisor"])
    return found


def has_immediacy(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in IMMEDIACY)


def manager_is_subject(text: str, category: str) -> bool:
    """Heuristic: the complaint is *about* a manager (conflict of interest)."""
    low = text.lower()
    if not re.search(r"\b(manager|supervisor|team lead|boss)\b", low):
        return False
    subject_patterns = [
        r"(manager|supervisor|team lead|boss) (repeatedly |often |always |has |had |keeps |kept |)?(shout|yell|humiliat|insult|abus|threat|said he would|said she would|rejected|reduced|refused|told me|made me|forced|ignored|singled)",
        r"(by|from) (my|the|our) (store |area |line |shift )?(manager|supervisor|team lead|boss)",
        r"(manager|supervisor|team lead|boss)('s)? (behaviou?r|conduct|comments)",
    ]
    return any(re.search(p, low) for p in subject_patterns) or category in {"manager_conduct"}


def urgency_level(flags: set[str], text: str) -> str:
    if "self_harm_welfare" in flags:
        return "URGENT"
    if "threat_violence" in flags:
        return "URGENT"
    if "safety_hazard" in flags and has_immediacy(text):
        return "URGENT"
    if "posh" in flags and ("threat_violence" in flags or has_immediacy(text)):
        return "URGENT"
    if flags & HIGH_FLAGS:
        return "HIGH"
    return "STANDARD"


URGENCY_RANK = {"STANDARD": 0, "HIGH": 1, "URGENT": 2}


def max_urgency(a: str, b: str) -> str:
    return a if URGENCY_RANK.get(a, 0) >= URGENCY_RANK.get(b, 0) else b


# ---------------------------------------------------------------------------
# Category inference (used to suggest / sanity-check the user's category)
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "wages_overtime": ["overtime", "payslip", "salary", "wage", "paid", "pay ", "deduct", "incentive", "extra hours", "stayed late"],
    "leave_absence": ["leave", "holiday", "absent", "absence", "missed", "shift", "ledger", "no-show", "sick"],
    "manager_conduct": ["shout", "humiliat", "insult", "yell", "rude", "bully", "abusive", "behaviour", "behavior"],
    "posh": ["sexual", "touch", "unwanted", "advances", "explicit", "stalk"],
    "safety_threat": ["threat", "unsafe", "hazard", "injur", "accident", "fire", "weapon", "hurt"],
    "discrimination": ["discriminat", "caste", "religio", "disabilit", "gender", "unfair treatment"],
    "maternity": ["pregnan", "maternity", "nursing", "creche", "crèche"],
    "disciplinary": ["warning letter", "show cause", "disciplinary", "suspend", "misconduct"],
    "separation": ["resign", "terminat", "fired", "notice period", "full and final", "final settlement", "relieving"],
    "social_security": ["provident", " pf ", "esi", "gratuity", "insurance", "bonus"],
    "data_privacy": ["confidential", "personal data", "leaked", "privacy", "shared my"],
}


def infer_category(text: str) -> tuple[str, dict[str, int]]:
    low = " " + text.lower() + " "
    # Keywords match at the start of a word, so "esi" does not match "design".
    scores = {c: sum(len(re.findall(r"(?<![a-z])" + re.escape(k.strip()), low)) for k in kws) for c, kws in CATEGORY_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return (best if scores[best] > 0 else "other"), {k: v for k, v in scores.items() if v}


# ---------------------------------------------------------------------------
# Information requirements (handbook Appendix A rule 8 and the topic sections)
# ---------------------------------------------------------------------------

LEGALLY_SENSITIVE = {"wages_overtime", "leave_absence", "maternity", "social_security", "separation", "disciplinary", "discrimination", "safety_threat"}

# Records the handbook says HR should collect for each kind of case.
REQUIRED_RECORDS: dict[str, list[tuple[str, str, list[str]]]] = {
    # (record, handbook basis, keywords that suggest it was supplied)
    "wages_overtime": [
        ("Payslip(s) for the wage period(s) concerned", "§4.5", ["payslip", "salary slip", "pay slip"]),
        ("Attendance / time records for the dates concerned", "§4.5", ["attendance", "punch", "biometric", "timesheet"]),
        ("Roster showing scheduled hours", "§4.5", ["roster", "schedule"]),
        ("Overtime approval or overtime record", "§4.5", ["overtime approval", "ot approval", "overtime record", "overtime register"]),
        ("Payroll calculation for the period", "§4.5", ["payroll calculation", "pay calculation", "computation"]),
    ],
    "leave_absence": [
        ("Leave ledger / leave balance history", "§5.5", ["ledger", "leave balance", "leave history"]),
        ("Leave request and the written response", "§5.5", ["leave request", "application", "rejected", "approval email"]),
        ("Roster and attendance records for the period", "§5.5", ["roster", "attendance"]),
    ],
    "manager_conduct": [
        ("Specific dates and examples of the conduct described", "§10.2", ["on ", "dated", "date"]),
        ("Names or roles of any witnesses (where appropriate)", "§10.2", ["witness", "colleague saw", "present"]),
        ("Any written communications relevant to the concern", "§10.2", ["email", "message", "whatsapp", "chat"]),
    ],
    "posh": [],  # The IC / designated route decides what is needed; HR does not gather evidence here.
    "safety_threat": [
        ("Incident or hazard report and the date/time of the incident", "§7.2", ["incident report", "hazard report", "reported on"]),
        ("Confirmation that the site safety / HR contact has been notified", "§7.2", ["notified", "informed safety", "safety officer"]),
    ],
    "discrimination": [
        ("Specific decisions or incidents complained of, with dates", "§10.2", ["date", "on "]),
        ("Relevant role-related criteria or records for the decision", "§2.2", ["criteria", "appraisal", "rating"]),
    ],
    "maternity": [
        ("Service dates and documentation relevant to eligibility", "§6.1", ["joining", "service", "certificate"]),
    ],
    "separation": [
        ("Resignation / separation letter and dates", "§11.1", ["resignation letter", "termination letter", "relieving"]),
        ("Final settlement computation", "§11.3", ["final settlement", "full and final", "computation"]),
    ],
    "social_security": [
        ("Wage period or service dates for the benefit concerned", "§12.3", ["service", "wage period", "date"]),
    ],
    "disciplinary": [
        ("Allegation particulars and any notice issued", "§10.3", ["notice", "show cause", "charge"]),
    ],
}

ABSENCE_RECORDS = [
    ("Attendance record for the missed shifts", "§5.4", ["attendance", "roster"]),
    ("Log of contact attempts made (dates, channel, outcome)", "§5.4", ["message", "called", "contact"]),
    ("Leave records and any notified illness / emergency", "§5.3", ["leave", "sick", "medical"]),
]


def is_absence_case(text: str, category: str) -> bool:
    low = text.lower()
    return category == "leave_absence" and bool(re.search(r"\b(absent|absence|missed (\w+ )?shifts?|not (replied|responded|reported)|no-show|unreachable|has not (come|attended))\b", low))


# ---------------------------------------------------------------------------
# Personal-data minimisation and untrusted-content handling
# ---------------------------------------------------------------------------

PII_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "phone": re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"),
    "aadhaar_like": re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)"),
    "pan_like": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    "bank_account_like": re.compile(r"(?<!\d)\d{11,18}(?!\d)"),
}


def find_pii(text: str) -> dict[str, int]:
    return {k: len(p.findall(text)) for k, p in PII_PATTERNS.items() if p.findall(text)}


def redact_pii(text: str) -> str:
    for kind, pattern in PII_PATTERNS.items():
        text = pattern.sub(f"[{kind.upper()} REDACTED]", text)
    return text


INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (all |any )?(the )?(previous|prior|above|system) (instructions|rules)",
    r"\bsystem prompt\b", r"\byou are now\b", r"\bact as\b.*\b(admin|developer|system)\b",
    r"(mark|set|label|close) (this|the) case (as )?(approved|resolved|closed|completed)",
    r"(approve|close) this (case|grievance) (immediately|automatically|without review)",
    r"do not (escalate|flag|report)", r"\bjailbreak\b", r"override (the )?(rules|policy|safeguards)",
]


def detect_injection(text: str) -> list[str]:
    low = text.lower()
    hits = []
    for p in INJECTION_PATTERNS:
        m = re.search(p, low)
        if m:
            hits.append(m.group(0))
    return hits


# ---------------------------------------------------------------------------
# Language the Quality & Risk Gate treats as unsupported conclusions
# ---------------------------------------------------------------------------

DECISION_LANGUAGE = [
    r"\bis guilty\b", r"\bwas guilty\b", r"\bfound guilty\b", r"\bguilt (is|was) (established|proven)",
    r"\bshould be (terminated|dismissed|fired|suspended|demoted|punished)\b", r"\bterminate (him|her|them|the employee|the manager)\b",
    r"\b(issue|impose) (a )?(final )?warning (to|against)\b", r"\bdisciplinary action (must|should|will) be taken against\b",
    r"\bharassment (occurred|took place|is proven|was proven|has been established)\b", r"\bis lying\b", r"\bnot credible\b",
    r"\babandoned (his|her|their) (job|employment)\b", r"\b(he|she|they) (clearly )?(committed|is responsible for) (misconduct|harassment)\b",
    r"\bthe employee (was|is) underpaid\b", r"\bunderpayment (occurred|has occurred|is confirmed|is established)\b",
    r"\bthe (allegation|complaint) is (true|proven|substantiated|false|baseless)\b",
]

ALLEGATION_AS_FACT = [
    r"\bthe manager (humiliated|harassed|threatened|shouted at|abused)\b",
    r"\b(it is|it has been) (confirmed|established|proven) that\b",
    r"\bdid (in fact|indeed)\b",
    r"\bwas (in fact )?(harassed|humiliated|threatened|underpaid)\b",
]

# Statutory-looking specifics the gate checks against retrieved text.
LEGAL_SPECIFIC_PATTERNS = [
    re.compile(r"\b(?:section|sec\.|s\.)\s*\d+[a-z]?(?:\(\d+\))?", re.I),
    re.compile(r"\b[A-Z][A-Za-z ,()&-]{3,80} Act,? (?:19|20)\d{2}\b"),
    re.compile(r"\b(?:Code on [A-Z][A-Za-z ]+|[A-Z][A-Za-z ,]+ Code),? (?:19|20)\d{2}\b"),
    re.compile(r"(?:₹|rs\.?|inr)\s?\d[\d,]*", re.I),
    re.compile(r"\b\d+\s*(?:working )?days?\b(?= of| within| after| from| notice| leave|\.|,)", re.I),
    re.compile(r"\b\d+\s*(?:weeks?|months?)\b(?= of| within| paid| maternity| notice| leave)", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|per ?cent)", re.I),
    re.compile(r"\b(?:twice|double|1\.5 times) the (?:ordinary |normal )?(?:rate|wages?)\b", re.I),
]


@dataclass
class RuleFinding:
    check: str
    status: str  # pass | warn | fail | corrected
    detail: str
    items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"check": self.check, "status": self.status, "detail": self.detail, "items": self.items}
