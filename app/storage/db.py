"""SQLite storage for cases, agent runs and an activity log.

Every query is scoped to a workspace id (one per browser, set by a cookie), so
one visitor never sees another visitor's cases. This is isolation, not
authentication - see README "Security and privacy".
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT,
    urgency TEXT,
    route TEXT,
    escalation_flag INTEGER DEFAULT 0,
    restricted INTEGER DEFAULT 0,
    mode TEXT,
    sample_case_id TEXT,
    input_json TEXT NOT NULL,
    triage_json TEXT,
    documents_json TEXT,
    report_json TEXT,
    gate_json TEXT,
    review_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_ws ON cases(workspace, created_at);
CREATE TABLE IF NOT EXISTS agent_runs (
    case_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    seq INTEGER NOT NULL,
    name TEXT,
    status TEXT NOT NULL,
    mode TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    model TEXT,
    attempts INTEGER,
    output_json TEXT,
    notes_json TEXT,
    error TEXT,
    PRIMARY KEY (case_id, agent)
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace TEXT NOT NULL,
    case_id TEXT,
    at TEXT NOT NULL,
    event TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_ws ON activity(workspace, id);
"""

AGENT_ORDER = [
    ("orchestrator", "Employee Relations Orchestrator"),
    ("policy_expert", "HR Policy Expert"),
    ("er_case_analyst", "Employee Relations Case Analyst"),
    ("compliance_reviewer", "Labour Compliance Reviewer"),
    ("documentation_specialist", "Case Documentation Specialist"),
    ("quality_gate", "Quality & Risk Gate"),
]

JSON_COLS = {"input_json": "input", "triage_json": "triage", "documents_json": "documents", "report_json": "report", "gate_json": "gate", "review_json": "review"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            c.row_factory = sqlite3.Row
            try:
                yield c
                c.commit()
            finally:
                c.close()

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _row_to_case(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for col, key in JSON_COLS.items():
            raw = d.pop(col, None)
            d[key] = json.loads(raw) if raw else None
        d["escalation_flag"] = bool(d.get("escalation_flag"))
        d["restricted"] = bool(d.get("restricted"))
        return d

    def new_case_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        while True:
            cid = f"PG-{stamp}-{secrets.token_hex(2).upper()}"
            with self.conn() as c:
                if not c.execute("SELECT 1 FROM cases WHERE id=?", (cid,)).fetchone():
                    return cid

    # -- cases ---------------------------------------------------------------
    def create_case(self, workspace: str, case_input: dict, triage: dict, documents: list[dict], mode: str) -> dict:
        cid = self.new_case_id()
        ts = now()
        with self.conn() as c:
            c.execute(
                """INSERT INTO cases (id, workspace, created_at, updated_at, status, title, category, urgency, route,
                   escalation_flag, restricted, mode, sample_case_id, input_json, triage_json, documents_json, review_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cid, workspace, ts, ts, "submitted", case_input["title"], triage["final_category"], triage["urgency"],
                    triage["route"], int(triage["escalation_flag"]), int(triage["restricted_access"]), mode,
                    case_input.get("sample_case_id"), json.dumps(case_input), json.dumps(triage), json.dumps(documents),
                    json.dumps({"status": "pending", "history": []}),
                ),
            )
            for seq, (key, name) in enumerate(AGENT_ORDER):
                c.execute("INSERT INTO agent_runs (case_id, agent, seq, name, status) VALUES (?,?,?,?,?)", (cid, key, seq, name, "pending"))
        self.log(workspace, cid, f"Case {cid} created: {case_input['title']}")
        return self.get_case(workspace, cid)

    def get_case(self, workspace: str, case_id: str) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM cases WHERE id=? AND workspace=?", (case_id, workspace)).fetchone()
        return self._row_to_case(row)

    def list_cases(self, workspace: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, created_at, updated_at, status, title, category, urgency, route, escalation_flag, restricted, mode, sample_case_id, "
                "json_extract(gate_json, '$.verdict') AS gate_verdict FROM cases WHERE workspace=? ORDER BY created_at DESC, id DESC",
                (workspace,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["escalation_flag"] = bool(d["escalation_flag"])
            d["restricted"] = bool(d["restricted"])
            out.append(d)
        return out

    def update_case(self, case_id: str, **fields) -> None:
        cols, vals = [], []
        for k, v in fields.items():
            col = next((c for c, key in JSON_COLS.items() if key == k), k)
            cols.append(f"{col}=?")
            vals.append(json.dumps(v) if col in JSON_COLS else (int(v) if isinstance(v, bool) else v))
        cols.append("updated_at=?")
        vals.append(now())
        with self.conn() as c:
            c.execute(f"UPDATE cases SET {', '.join(cols)} WHERE id=?", (*vals, case_id))

    def delete_case(self, workspace: str, case_id: str) -> bool:
        with self.conn() as c:
            cur = c.execute("DELETE FROM cases WHERE id=? AND workspace=?", (case_id, workspace))
            if cur.rowcount:
                c.execute("DELETE FROM agent_runs WHERE case_id=?", (case_id,))
                c.execute("DELETE FROM activity WHERE case_id=? AND workspace=?", (case_id, workspace))
        if cur.rowcount:
            self.log(workspace, None, f"Case {case_id} deleted")
        return bool(cur.rowcount)

    def delete_all(self, workspace: str) -> int:
        with self.conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM cases WHERE workspace=?", (workspace,)).fetchall()]
            for cid in ids:
                c.execute("DELETE FROM agent_runs WHERE case_id=?", (cid,))
            c.execute("DELETE FROM cases WHERE workspace=?", (workspace,))
            c.execute("DELETE FROM activity WHERE workspace=?", (workspace,))
        return len(ids)

    # -- agent runs ------------------------------------------------------------
    def set_agent(self, case_id: str, agent: str, **fields) -> None:
        cols, vals = [], []
        for k, v in fields.items():
            if k in ("output", "notes"):
                cols.append(f"{k}_json=?")
                vals.append(json.dumps(v, default=str))
            else:
                cols.append(f"{k}=?")
                vals.append(v)
        with self.conn() as c:
            c.execute(f"UPDATE agent_runs SET {', '.join(cols)} WHERE case_id=? AND agent=?", (*vals, case_id, agent))

    def get_agents(self, case_id: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM agent_runs WHERE case_id=? ORDER BY seq", (case_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["output"] = json.loads(d.pop("output_json")) if d.get("output_json") else None
            d["notes"] = json.loads(d.pop("notes_json")) if d.get("notes_json") else []
            out.append(d)
        return out

    def reset_agents(self, case_id: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE agent_runs SET status='pending', mode=NULL, started_at=NULL, finished_at=NULL, duration_ms=NULL, model=NULL, attempts=NULL, output_json=NULL, notes_json=NULL, error=NULL WHERE case_id=?",
                (case_id,),
            )

    # -- activity / stats --------------------------------------------------------
    def log(self, workspace: str, case_id: str | None, event: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO activity (workspace, case_id, at, event) VALUES (?,?,?,?)", (workspace, case_id, now(), event))

    def recent_activity(self, workspace: str, limit: int = 10) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT case_id, at, event FROM activity WHERE workspace=? ORDER BY id DESC LIMIT ?", (workspace, limit)).fetchall()
        return [dict(r) for r in rows]

    def stats(self, workspace: str) -> dict:
        with self.conn() as c:
            row = c.execute(
                """SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('awaiting_review','escalated_urgent') THEN 1 ELSE 0 END) AS awaiting_review,
                   SUM(CASE WHEN escalation_flag=1 AND status NOT IN ('approved') THEN 1 ELSE 0 END) AS escalated,
                   SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status IN ('submitted','running') THEN 1 ELSE 0 END) AS in_progress,
                   SUM(CASE WHEN status='returned' THEN 1 ELSE 0 END) AS returned,
                   SUM(CASE WHEN urgency='URGENT' AND status NOT IN ('approved') THEN 1 ELSE 0 END) AS urgent
                   FROM cases WHERE workspace=?""",
                (workspace,),
            ).fetchone()
        return {k: (row[k] or 0) for k in row.keys()}
