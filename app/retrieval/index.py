"""Lightweight local policy retrieval (BM25 + HR vocabulary expansion).

Why not a vector database? The handbook is ~90 short sections. A lexical BM25
index builds in milliseconds, needs no embedding model or external service,
runs on any free hosting tier and gives scores that can be explained to an
evaluator (which words matched). An HR-vocabulary expansion table bridges
everyday complaint language ("stayed late", "shouted at me") to the
handbook's terminology ("overtime", "humiliating conduct").
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.retrieval.handbook_parser import ParsedDocument, PolicySection, parse_policy_file

STOPWORDS = set(
    """a about above after again against all am an and any are as at be because been before being below between both
    but by can could did do does doing down during each few for from further had has have having he her here hers him
    his how i if in into is it its itself just me more most my myself no nor not now of off on once only or other our
    ours out over own same she should so some such than that the their theirs them then there these they this those
    through to too under until up very was we were what when where which while who whom why will with would you your
    yours also may must per via etc any every one two three several since still even get got told say said says
    """.split()
)
# Words so common in an HR handbook that they carry no retrieval signal.
DOMAIN_STOPWORDS = {
    "employee", "employees", "northstar", "hr", "policy", "company", "work", "worked", "working", "staff",
    "team", "please", "issue", "concern", "case", "matter", "person", "people", "time", "day", "days", "week",
    "weeks", "month", "months", "date", "dates", "store", "office", "warehouse", "applicable", "relevant",
}

# Everyday phrasing -> handbook vocabulary. Keys are matched as substrings of the
# lower-cased query; values are appended to the query terms.
EXPANSIONS: dict[str, list[str]] = {
    "overtime": ["overtime", "hours", "payslip", "payroll", "rate", "attendance", "off-the-clock"],
    "extra hours": ["overtime", "hours", "attendance"],
    "stayed late": ["overtime", "hours", "attendance", "off-the-clock"],
    "beyond scheduled": ["overtime", "hours", "roster"],
    "beyond my shift": ["overtime", "hours", "roster"],
    "not paid": ["wages", "payslip", "pay", "discrepancy", "payroll"],
    "unpaid": ["wages", "payslip", "pay", "discrepancy", "payroll"],
    "payslip": ["payslip", "wage", "statement", "discrepancy"],
    "salary": ["wages", "salary", "payroll", "payment"],
    "deduct": ["deductions", "wages", "fines", "withhold"],
    "fine": ["deductions", "fines"],
    "minimum wage": ["minimum", "wages", "rates"],
    "leave": ["leave", "ledger", "balance", "request", "entitlements"],
    "earned leave": ["leave", "earned", "privilege", "ledger", "balance", "accrual"],
    "leave balance": ["leave", "balance", "ledger", "dispute"],
    "holiday": ["holiday", "leave", "festival"],
    "sick": ["sick", "illness", "leave", "medical"],
    "absent": ["absence", "notice", "welfare", "contact", "attendance", "abandonment"],
    "absence": ["absence", "notice", "welfare", "contact", "attendance", "abandonment"],
    "missed": ["absence", "attendance", "shifts", "welfare"],
    "no-show": ["absence", "notice", "welfare"],
    "not responded": ["absence", "contact", "welfare"],
    "not replied": ["absence", "contact", "welfare"],
    "humiliat": ["humiliating", "respectful", "conduct", "abusive", "dignity", "grievance", "privately"],
    "shout": ["humiliating", "abusive", "respectful", "conduct"],
    "yell": ["humiliating", "abusive", "respectful", "conduct"],
    "insult": ["humiliating", "abusive", "respectful", "conduct", "dignity"],
    "bully": ["humiliating", "abusive", "intimidation", "respectful", "conduct"],
    "abus": ["abusive", "threatening", "humiliating", "conduct"],
    "in front of": ["humiliating", "privately", "respectfully"],
    "manager": ["manager", "managers", "line", "conflict", "impartial"],
    "sexual": ["sexual", "harassment", "posh", "internal", "committee", "ic"],
    "harass": ["harassment", "posh", "internal", "committee", "intimidation"],
    "touch": ["sexual", "harassment", "posh", "internal", "committee"],
    "unwanted": ["sexual", "harassment", "posh"],
    "inappropriate": ["harassment", "conduct", "posh"],
    "posh": ["posh", "sexual", "harassment", "internal", "committee"],
    "threat": ["threats", "violence", "emergency", "serious", "risk", "safety", "immediate"],
    "violen": ["violence", "threats", "emergency", "serious", "risk"],
    "weapon": ["violence", "threats", "emergency", "serious", "risk"],
    "hurt": ["harm", "risk", "safety", "injuries"],
    "kill": ["threats", "violence", "emergency", "serious", "harm"],
    "unsafe": ["safety", "hazards", "unsafe", "equipment", "risk"],
    "injur": ["injuries", "safety", "incidents", "hazards"],
    "fire": ["fire", "hazards", "safety"],
    "retaliat": ["retaliation", "retaliate", "punished", "good", "faith"],
    "punish": ["retaliation", "punished"],
    "complained": ["retaliation", "good", "faith", "punished"],
    "complain again": ["retaliation", "punished", "threats"],
    "reported": ["reporting", "retaliation", "good", "faith"],
    "scared": ["safety", "risk", "threats", "harm"],
    "afraid": ["safety", "risk", "threats", "harm"],
    "victimi": ["retaliation", "intimidation"],
    "discriminat": ["discrimination", "equal", "opportunity", "unlawful"],
    "pregnan": ["pregnancy", "maternity", "benefits", "fairness"],
    "maternity": ["maternity", "benefits", "nursing", "pregnancy"],
    "resign": ["resignation", "notice", "separation", "final"],
    "terminat": ["termination", "retrenchment", "separation"],
    "fired": ["termination", "separation"],
    "dismiss": ["termination", "disciplinary", "separation"],
    "final settlement": ["final", "wages", "dues", "settlement"],
    "provident": ["provident", "fund", "social", "security", "benefits"],
    " pf": ["provident", "fund", "social", "security"],
    "esi": ["employee", "state", "insurance", "social", "security"],
    "gratuity": ["gratuity", "social", "security", "benefits"],
    "confidential": ["confidentiality", "data", "access", "records"],
    "personal data": ["data", "minimisation", "confidentiality", "records"],
    "warning": ["disciplinary", "outcomes", "warning", "notice"],
    "disciplinary": ["disciplinary", "fact-finding", "notice", "response", "outcomes"],
    "suspend": ["interim", "measures", "suspension"],
    "roster": ["roster", "schedules", "shift"],
    "shift": ["shift", "roster", "schedules", "attendance"],
    "night": ["night", "work", "rest", "safeguards"],
}

CATEGORY_TERMS: dict[str, list[str]] = {
    "wages_overtime": ["overtime", "wages", "payslip", "payroll", "attendance", "hours", "discrepancy"],
    "leave_absence": ["leave", "ledger", "balance", "absence", "attendance"],
    "manager_conduct": ["respectful", "conduct", "humiliating", "grievance", "impartial", "retaliation"],
    "posh": ["sexual", "harassment", "posh", "internal", "committee", "confidentiality"],
    "safety_threat": ["safety", "hazards", "emergency", "threats", "violence", "risk"],
    "discrimination": ["discrimination", "equal", "opportunity", "retaliation"],
    "maternity": ["maternity", "pregnancy", "benefits", "nursing"],
    "separation": ["resignation", "termination", "separation", "final", "dues"],
    "social_security": ["social", "security", "provident", "insurance", "gratuity", "benefits"],
    "disciplinary": ["disciplinary", "fact-finding", "notice", "response", "outcomes"],
    "data_privacy": ["confidentiality", "data", "access", "records"],
    "other": [],
}

_TOKEN = re.compile(r"[a-z][a-z\-]+|\d+(?:\.\d+)?")


def stem(word: str) -> str:
    """Very small suffix stripper; enough to match overtime/overtimes etc."""
    for suffix in ("ations", "ation", "ments", "ment", "ingly", "ings", "ing", "edly", "ies", "ied", "ed", "es", "s", "ly"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            base = word[: -len(suffix)]
            if suffix in ("ies", "ied"):
                base += "y"
            return base
    return word


def tokenize(text: str, *, query: bool = False) -> list[str]:
    out = []
    for tok in _TOKEN.findall(text.lower()):
        tok = tok.strip("-")
        if not tok or tok in STOPWORDS or (query and tok in DOMAIN_STOPWORDS):
            continue
        if len(tok) < 2:
            continue
        out.append(stem(tok))
    return out


def expand_query(text: str, category: str | None = None) -> tuple[list[str], list[str]]:
    """Return (query tokens, list of expansion triggers that fired)."""
    low = " " + text.lower() + " "
    terms = tokenize(text, query=True)
    fired = []
    for key, extra in EXPANSIONS.items():
        # Keys match at the start of a word ("harass" matches "harassed",
        # but "esi" must not match "design").
        if re.search(r"(?<![a-z])" + re.escape(key.strip()), low):
            fired.append(key.strip())
            terms.extend(stem(t) for t in extra)
    if category and category in CATEGORY_TERMS:
        terms.extend(stem(t) for t in CATEGORY_TERMS[category])
    return terms, fired


@dataclass
class SearchHit:
    section: PolicySection
    score: float
    matched_terms: list[str]

    def to_dict(self, excerpt_chars: int = 600) -> dict:
        d = self.section.to_dict()
        d["score"] = round(self.score, 3)
        d["matched_terms"] = self.matched_terms
        d["excerpt"] = self.section.text[:excerpt_chars] + ("…" if len(self.section.text) > excerpt_chars else "")
        return d


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.6):
        self.k1, self.b = k1, b
        self.docs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avgdl = (sum(self.lens) / len(self.lens)) if self.lens else 1.0
        n = len(docs)
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def score(self, query: list[str], idx: int) -> tuple[float, list[str]]:
        doc, dl = self.docs[idx], self.lens[idx]
        total, matched = 0.0, []
        qcounts = Counter(query)
        for term, qtf in qcounts.items():
            tf = doc.get(term)
            if not tf:
                continue
            idf = self.idf.get(term, 0.0)
            denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            # dampen repeated query terms (from expansion) with a log factor
            total += idf * (tf * (self.k1 + 1) / denom) * (1 + math.log(qtf))
            matched.append(term)
        return total, matched


class PolicyIndex:
    """In-memory index over every policy file in the policy directory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.documents: list[ParsedDocument] = []
        self.sections: list[PolicySection] = []
        self.by_id: dict[str, PolicySection] = {}
        self._bm25: BM25 | None = None
        self._retrievable: list[int] = []
        self.indexed_at: float | None = None
        self.errors: list[str] = []

    def build(self, policy_dir: Path) -> dict:
        documents, errors = [], []
        files = sorted(p for p in policy_dir.glob("*") if p.suffix.lower() in {".docx", ".pdf", ".txt", ".md"})
        for f in files:
            try:
                documents.append(parse_policy_file(f))
            except Exception as exc:  # noqa: BLE001 - report any parse failure
                errors.append(f"{f.name}: {exc}")
        sections = [s for d in documents for s in d.sections]
        token_docs = [tokenize(f"{s.heading} {s.heading} {s.row_label} {s.parent} {s.text}") for s in sections]
        with self._lock:
            self.documents = documents
            self.sections = sections
            self.by_id = {s.chunk_id: s for s in sections}
            self._bm25 = BM25(token_docs) if sections else None
            self._retrievable = [i for i, s in enumerate(sections) if s.retrievable]
            self.indexed_at = time.time()
            self.errors = errors
        return self.summary()

    def summary(self) -> dict:
        return {
            "documents": [
                {
                    "name": d.name,
                    "title": d.title,
                    "sections": len(d.sections),
                    "evidence_sections": sum(1 for s in d.sections if s.retrievable),
                    "warnings": d.warnings,
                }
                for d in self.documents
            ],
            "total_sections": len(self.sections),
            "indexed_at": self.indexed_at,
            "errors": self.errors,
        }

    @property
    def ready(self) -> bool:
        return bool(self.sections)

    def search(
        self,
        text: str,
        *,
        category: str | None = None,
        top_k: int = 6,
        min_score: float = 6.0,
        include_non_evidence: bool = False,
        relative_floor: float = 0.2,
    ) -> tuple[list[SearchHit], dict]:
        terms, fired = expand_query(text, category)
        meta = {"query_terms": sorted(set(terms)), "expansions": fired, "min_score": min_score}
        if not self._bm25 or not terms:
            return [], meta
        candidates = range(len(self.sections)) if include_non_evidence else self._retrievable
        hits = []
        for i in candidates:
            score, matched = self._bm25.score(terms, i)
            if score >= min_score:
                hits.append(SearchHit(self.sections[i], score, matched))
        hits.sort(key=lambda h: h.score, reverse=True)
        if hits:
            # Drop weak tail matches relative to the best match.
            floor = hits[0].score * relative_floor
            hits = [h for h in hits if h.score >= floor]
        return hits[:top_k], meta

    def get(self, chunk_id: str) -> PolicySection | None:
        return self.by_id.get(chunk_id)

    def find_by_ref(self, ref: str) -> PolicySection | None:
        """Resolve a citation label (e.g. '§3.3' or '§3.3 Overtime') to a section."""
        ref = ref.strip()
        for s in self.sections:
            if ref == s.ref or ref == s.chunk_id:
                return s
        m = re.match(r"^§?\s*(\d+\.\d+)\b", ref)
        if m:
            for s in self.sections:
                if s.section_number == m.group(1):
                    return s
        return None


policy_index = PolicyIndex()
