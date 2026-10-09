/* PeopleGuard AI front end. Plain JavaScript, no build step. */
(function () {
  "use strict";

  // ---------------------------------------------------------------- helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: "same-origin", ...opts });
    let body = null;
    const type = res.headers.get("content-type") || "";
    if (type.includes("application/json")) body = await res.json();
    if (!res.ok) {
      const detail = body && body.detail;
      const err = new Error(typeof detail === "string" ? detail : (detail && detail.message) || `Request failed (${res.status})`);
      err.status = res.status;
      err.detail = detail;
      throw err;
    }
    return body;
  }

  let toastTimer;
  function toast(msg, isErr = false) {
    const t = $("#toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.className = "toast"), 4200);
  }

  function fmtDate(iso, withTime = true) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return esc(iso);
    return d.toLocaleString(undefined, withTime ? { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" } : { day: "numeric", month: "short", year: "numeric" });
  }

  const STATUS_LABEL = {
    submitted: "Submitted", running: "Analysis running", awaiting_review: "Awaiting review",
    escalated_urgent: "Urgent", approved: "Approved", returned: "Returned", error: "Error",
  };
  const statusChip = (s, label) => `<span class="chip s-${esc(s)}">${esc(label || STATUS_LABEL[s] || s)}</span>`;
  const urgencyChip = (u) => (u && u !== "STANDARD" ? `<span class="chip u-${esc(u)}">${esc(u === "URGENT" ? "Urgent" : "High sensitivity")}</span>` : "");
  const routeChip = (r) => (r === "posh_ic" ? `<span class="chip r-posh_ic">POSH · IC route</span>` : r === "urgent_safety" ? `<span class="chip u-URGENT">Safety escalation</span>` : "");

  const SOURCE_BADGE = {
    llm: '<span class="badge b-llm" title="Written by the configured language model">AI-generated</span>',
    simulated: '<span class="badge b-sim" title="Demo mode: wording produced from templates, no model call">Simulated (demo)</span>',
    rules: '<span class="badge b-rules" title="Deterministic computation">Rule-based</span>',
    rules_fallback: '<span class="badge b-fallback" title="The AI call failed; rule-based output only">Fallback</span>',
  };
  const modeBadge = (mode, status) => {
    if (status === "fallback") return SOURCE_BADGE.rules_fallback;
    return SOURCE_BADGE[mode] || "";
  };
  const list = (items, cls = "") => (items && items.length ? `<ul class="${cls}">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : `<p class="muted small">None.</p>`);

  // ---------------------------------------------------------------- shell
  const menuBtn = $("#menuBtn");
  if (menuBtn) {
    menuBtn.addEventListener("click", () => {
      const sb = $("#sidebar");
      const open = sb.classList.toggle("open");
      menuBtn.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", (e) => {
      const sb = $("#sidebar");
      if (sb.classList.contains("open") && !sb.contains(e.target) && !menuBtn.contains(e.target)) sb.classList.remove("open");
    });
  }

  async function loadAllSamples(btn, after) {
    if (btn) { btn.disabled = true; btn.textContent = "Creating cases…"; }
    try {
      const r = await api("/api/samples/load-all", { method: "POST" });
      toast(`${r.created.length} demo cases created. The agents are running…`);
      if (after) after();
    } catch (e) {
      toast(e.message, true);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label || "Load demo cases"; }
    }
  }

  function casesTable(cases, opts = {}) {
    if (!cases.length) return `<p class="muted pad">No cases yet.</p>`;
    const rows = cases.map((c) => `
      <tr class="clickable" data-href="/cases/${esc(c.id)}">
        <td data-l="Case"><span class="case-id">${esc(c.id)}</span></td>
        <td data-l="Title" class="title-cell">${c.restricted ? '<span class="chip restricted">Restricted</span> ' : ""}${esc(c.restricted ? "Restricted case — details hidden" : c.title)}
          <small>${esc(c.sample_case_id ? "Sample " + c.sample_case_id + " · " : "")}${fmtDate(c.created_at)}</small></td>
        <td data-l="Status">${statusChip(c.status, c.status_label)} ${urgencyChip(c.urgency)} ${routeChip(c.route)}</td>
        ${opts.full ? `<td data-l="Gate">${c.gate_verdict ? `<span class="badge b-${c.gate_verdict === "passed" ? "pass" : c.gate_verdict === "warnings" ? "warn" : c.gate_verdict === "corrected" ? "corrected" : "fail"}">${esc(c.gate_verdict)}</span>` : "—"}</td>
        <td data-l="Mode">${c.mode === "live" ? SOURCE_BADGE.llm : SOURCE_BADGE.simulated}</td>
        <td><button class="icon-btn" data-del="${esc(c.id)}" aria-label="Delete case ${esc(c.id)}" title="Delete"><svg viewBox="0 0 24 24"><path d="M9 3h6l1 2h4v2H4V5h4l1-2Zm-3 6h12l-1 12H7L6 9Z"/></svg></button></td>` : ""}
      </tr>`).join("");
    return `<table class="table responsive"><thead><tr><th>Case</th><th>Title</th><th>Status</th>${opts.full ? "<th>Gate</th><th>Mode</th><th></th>" : ""}</tr></thead><tbody>${rows}</tbody></table>`;
  }

  function wireRowLinks(root) {
    $$("tr.clickable", root).forEach((tr) => tr.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      location.href = tr.dataset.href;
    }));
  }

  // ---------------------------------------------------------------- dashboard
  async function initDashboard() {
    const loadBtn = $("#loadAll");
    if (loadBtn) loadBtn.dataset.label = loadBtn.textContent;
    let timer;
    async function refresh() {
      const s = await api("/api/stats");
      for (const [k, v] of Object.entries(s.counts)) $$(`[data-k="${k}"]`).forEach((el) => (el.textContent = v));
      $("#emptyHero").hidden = s.counts.total > 0;
      $("#recentCases").innerHTML = casesTable(s.recent_cases);
      wireRowLinks($("#recentCases"));
      $("#activity").innerHTML = s.recent_activity.length
        ? s.recent_activity.map((a) => `<li>${a.case_id ? `<a href="/cases/${esc(a.case_id)}" class="link">${esc(a.event)}</a>` : esc(a.event)}<time>${fmtDate(a.at)}</time></li>`).join("")
        : '<li class="muted">No activity yet.</li>';
      clearTimeout(timer);
      if (s.counts.in_progress > 0) timer = setTimeout(refresh, 2000);
    }
    loadBtn && loadBtn.addEventListener("click", () => loadAllSamples(loadBtn, refresh));
    refresh().catch((e) => toast(e.message, true));
  }

  // ---------------------------------------------------------------- new case
  async function initNewCase() {
    const form = $("#caseForm");
    const [cfg, samples] = await Promise.all([api("/api/config"), api("/api/samples")]);
    const fill = (id, obj) => ($(id).innerHTML = Object.entries(obj).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join(""));
    fill("#f_reported_by", cfg.reported_by);
    fill("#f_establishment_type", cfg.establishment_types);
    fill("#f_employee_category", cfg.employee_categories);
    fill("#f_grievance_category", cfg.categories);
    $("#f_grievance_category").value = "other";
    $("#maxMb").textContent = cfg.max_upload_mb;

    const sel = $("#sampleSelect");
    samples.samples.forEach((s) => sel.insertAdjacentHTML("beforeend", `<option value="${esc(s.id)}">${esc(s.id)} — ${esc(s.label)}</option>`));
    const desc = $("#f_description");
    const count = () => ($("#descCount").textContent = `${desc.value.length} / 8000`);
    desc.addEventListener("input", count);

    function clearForm() {
      form.reset();
      $("#f_grievance_category").value = "other";
      $("#f_sample_case_id").value = "";
      $("#f_sample_document").value = "";
      $("#docHint").textContent = "Documents are treated as unverified data. Instructions inside them are ignored.";
      $("#sampleNote").textContent = "";
      sel.value = "";
      count();
    }
    $("#clearForm").addEventListener("click", clearForm);
    sel.addEventListener("change", () => {
      const s = samples.samples.find((x) => x.id === sel.value);
      if (!s) return clearForm();
      for (const [k, v] of Object.entries(s.input)) {
        const el = $(`#f_${k}`);
        if (el) el.value = v ?? "";
      }
      $("#f_sample_case_id").value = s.id;
      $("#f_sample_document").value = s.document || "";
      $("#f_document").value = "";
      $("#docHint").innerHTML = s.document ? `Sample document attached: <span class="mono">${esc(s.document)}</span> (choose a file to replace it).` : "No document for this sample.";
      $("#sampleNote").textContent = s.handbook_example ? `Based on handbook ${s.handbook_example}.` : "Additional test case (not in the handbook appendix).";
      count();
    });
    $("#f_document").addEventListener("change", () => {
      if ($("#f_document").files.length) $("#f_sample_document").value = "";
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      $$(".err", form).forEach((x) => (x.textContent = ""));
      $$(".invalid", form).forEach((x) => x.classList.remove("invalid"));
      let bad = false;
      const flag = (name, msg) => {
        bad = true;
        const el = $(`[name="${name}"]`, form);
        el && el.classList.add("invalid");
        const p = $(`[data-err="${name}"]`, form);
        if (p) p.textContent = msg;
      };
      if ($("#f_title").value.trim().length < 3) flag("title", "Enter a title of at least 3 characters.");
      if (desc.value.trim().length < 20) flag("description", "Describe the issue in at least 20 characters.");
      const file = $("#f_document").files[0];
      if (file && file.size > cfg.max_upload_mb * 1024 * 1024) flag("document", `The file is larger than ${cfg.max_upload_mb} MB.`);
      if (bad) return;

      const btn = $("#submitBtn");
      btn.disabled = true;
      btn.textContent = "Running intake triage…";
      try {
        const fd = new FormData(form);
        if (!file) fd.delete("document");
        const r = await api("/api/cases", { method: "POST", body: fd });
        if (r.warnings && r.warnings.length) toast(r.warnings[0]);
        if (r.urgent) {
          const panel = $("#urgentPanel");
          panel.className = "urgent";
          panel.innerHTML = `<h2>⚠ Urgent risk detected — act now</h2>
            <p>Intake triage flagged <strong>${esc(r.flags.map((f) => f.label).join("; "))}</strong>. The agent analysis has started in the background, but <strong>do not wait for it</strong>.</p>
            <ul>${r.urgent_actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
            <p style="margin-top:.75rem"><a class="btn btn-primary" href="/cases/${esc(r.case_id)}">Open case ${esc(r.case_id)}</a></p>`;
          panel.hidden = false;
          form.hidden = true;
          window.scrollTo({ top: 0, behavior: "smooth" });
        } else {
          location.href = `/cases/${encodeURIComponent(r.case_id)}`;
        }
      } catch (err) {
        const errors = err.detail && err.detail.errors;
        if (errors) errors.forEach((x) => flag(x.field, x.message));
        $("#formErr").textContent = err.message;
      } finally {
        btn.disabled = false;
        btn.textContent = "Submit for analysis";
      }
    });
  }

  // ---------------------------------------------------------------- case page
  const AGENT_ROLES = {
    orchestrator: "Classifies, detects sensitive and urgent risk, sets the route and escalations",
    policy_expert: "Retrieves handbook provisions and quotes them with section references",
    er_case_analyst: "Separates allegations from facts, identifies gaps and neutral questions",
    compliance_reviewer: "Flags legal questions that require verification and statutory routing",
    documentation_specialist: "Assembles the draft HR case report",
    quality_gate: "Independently checks grounding, neutrality, invented law, data exposure and escalation",
  };
  const STATUS_TEXT = { pending: "Waiting", running: "Running…", completed: "Completed", fallback: "Completed with fallback", failed: "Failed", skipped: "Not run" };

  function renderAgentBody(a) {
    const o = a.output || {};
    const src = SOURCE_BADGE[o.narrative_source] || "";
    let h = "";
    (a.notes || []).forEach((n) => (h += `<p class="note-line">${esc(n)}</p>`));
    if (a.error) h += `<p class="note-line ${a.status === "fallback" ? "warn" : "err"}">${esc(a.error)}</p>`;
    if (!a.output) return h || `<p class="muted small">No output — this agent has not produced results.</p>`;

    switch (a.agent) {
      case "orchestrator":
        h += `<dl class="kv"><dt>Case ID</dt><dd class="mono">${esc(o.case_id)}</dd>
          <dt>Category</dt><dd>${esc(o.category_label)}${o.user_category !== o.final_category ? ` <span class="muted small">(selected: ${esc(o.user_category)})</span>` : ""}</dd>
          <dt>Urgency</dt><dd>${esc(o.urgency)}</dd><dt>Route</dt><dd>${esc(o.route_label)}</dd>
          <dt>Access</dt><dd>${o.restricted_access ? "Restricted" : "Standard"}</dd></dl>`;
        if (o.category_note) h += `<p class="note-line">${esc(o.category_note)}</p>`;
        h += `<h4>Issue statement ${src}</h4><p>${esc(o.issue_statement)}</p>`;
        h += `<h4>Sensitivity flags <span class="badge b-rules">Rule-based</span></h4>` + (o.flags.length ? `<ul>${o.flags.map((f) => `<li><strong>${esc(f.label)}</strong> <span class="muted small">— matched: ${esc((f.evidence || []).join(", "))}</span></li>`).join("")}</ul>` : `<p class="muted small">No sensitive indicators found.</p>`);
        if (o.llm_additional_flags && o.llm_additional_flags.length) h += `<h4>Additional concerns raised by AI ${SOURCE_BADGE.llm}</h4>${list(o.llm_additional_flags)}`;
        h += `<h4>Escalations set</h4>` + (o.escalations.length ? `<ul>${o.escalations.map((e) => `<li><strong>${esc(e.to)}</strong> (${esc(e.priority)}): ${esc(e.reason)}</li>`).join("")}</ul>` : `<p class="muted small">None at intake.</p>`);
        h += `<h4>Workflow plan</h4><ol>${o.workflow_plan.map((p) => `<li>${esc(p.purpose)}</li>`).join("")}</ol>`;
        break;
      case "policy_expert":
        h += `<h4>Retrieval</h4><p class="small muted">Query terms (after HR-vocabulary expansion): ${esc((o.query_terms || []).slice(0, 30).join(", "))}${o.expansions && o.expansions.length ? ` · expansions triggered by: ${esc(o.expansions.join(", "))}` : ""}</p>`;
        if (o.no_evidence) h += `<p class="note-line warn">No handbook passage scored above the relevance threshold (${esc(o.min_score)}). The system reports this instead of inventing a policy.</p>`;
        else h += `<table class="table compact"><thead><tr><th>Section</th><th>Score</th><th>Retrieved by</th><th>How it applies ${src}</th></tr></thead><tbody>${o.findings.map((f) => `<tr><td><a class="link" href="/policies?section=${encodeURIComponent(f.chunk_id)}">${esc(f.ref)}</a></td><td>${f.score == null ? "—" : esc(f.score.toFixed ? f.score.toFixed(1) : f.score)}</td><td>${esc(f.retrieved_by)}</td><td>${esc(f.how_it_applies)}</td></tr>`).join("")}</tbody></table>`;
        if (o.rejected_citations && o.rejected_citations.length) h += `<p class="note-line warn">Citations rejected because they were not among the retrieved passages: ${esc(o.rejected_citations.join(", "))}</p>`;
        if (o.legislation_mentioned && o.legislation_mentioned.length) h += `<h4>Legislation mentioned in policy text (not verification)</h4>${list(o.legislation_mentioned.map((l) => `${l.ref}: “${l.quote}”`))}`;
        h += `<h4>Policy vs law</h4>${list(o.policy_vs_law_notes)}`;
        if (o.gaps && o.gaps.length) h += `<h4>Gaps</h4>${list(o.gaps)}`;
        break;
      case "er_case_analyst":
        h += `<h4>Neutral summary ${src}</h4><p>${esc(o.neutral_summary)}</p>`;
        h += `<h4><span class="label-tag lt-allegation">Allegations</span> Reported account</h4>${o.posh_minimal_mode ? '<p class="muted small">Withheld — POSH matters are not summarised in the ER workflow.</p>' : list(o.reported_account)}`;
        h += `<h4><span class="label-tag lt-fact">Facts</span> Case-record facts</h4>${list(o.case_record_facts)}`;
        h += `<h4>Documentary evidence</h4>${o.documentary_evidence.length ? list(o.documentary_evidence.map((d) => `${d.name} — ${d.status}`)) : '<p class="muted small">No documents uploaded.</p>'}`;
        h += `<h4>Missing information</h4>${o.missing_information.length ? `<ul>${o.missing_information.map((m) => `<li><strong>${esc(m.item)}</strong> <span class="muted small">(${esc(m.severity)} · ${esc(m.basis)})</span></li>`).join("")}</ul>` : '<p class="muted small">None.</p>'}`;
        h += `<h4>Neutral follow-up questions</h4>${list(o.follow_up_questions)}`;
        h += `<h4>Conflicts of interest</h4>${list(o.conflicts_of_interest)}`;
        h += `<h4>Unresolved questions</h4>${list(o.unresolved_questions)}`;
        break;
      case "compliance_reviewer":
        h += `<p class="note-line">${esc(o.disclaimer)}</p>`;
        if (o.statutory_route) h += `<p class="note-line warn"><strong>Statutory route:</strong> ${esc(o.statutory_route)}</p>`;
        h += `<h4>Applicability facts — ${esc(o.fact_sufficiency)}</h4><table class="table compact"><tbody>${o.applicability_facts.map((f) => `<tr><td>${esc(f.fact)}</td><td>${f.value ? esc(f.value) : '<span class="badge b-warn">Missing</span>'}</td><td class="muted small">${esc(f.why_needed)}</td></tr>`).join("")}</tbody></table>`;
        h += `<h4><span class="label-tag lt-legal">Legal issues for verification</span></h4><ul>${o.issues.map((i) => `<li><strong>${esc(i.topic)}</strong> — ${esc(i.framework_to_verify || "not mapped in the handbook")} <span class="badge b-warn">${esc(i.status)}</span><br><span class="small">${esc(i.why_relevant)}</span>${i.ref ? ` <span class="muted small">(${esc(i.ref)})</span>` : ""}</li>`).join("")}</ul>`;
        h += `<h4>Legal / Compliance escalation</h4><p>${o.escalate_to_legal ? `<strong>Yes</strong> — ${esc(o.escalation_reason)}` : "Not required on current facts."}</p>`;
        break;
      case "documentation_specialist": {
        const r = o.report || {};
        h += `<h4>Executive summary ${src}</h4><p>${esc(r.executive_summary)}</p>`;
        h += `<p class="small muted">Assembled ${(r.policy_provisions || []).length} policy citation(s), ${(r.missing_information || []).length} missing-information item(s), ${(r.next_steps || []).length} next step(s) and ${(r.escalation && r.escalation.items || []).length} escalation(s). See the Case report tab for the version after quality checks.</p>`;
        break;
      }
      case "quality_gate":
        h += `<p><strong>${esc(o.verdict_label)}</strong></p><div class="gate-checks">${o.checks.map((c) => `<div class="gate-check"><span class="badge b-${esc(c.status)}">${esc(c.status)}</span><div><strong>${esc(c.check)}</strong><br><span class="muted small">${esc(c.detail)}</span>${c.items && c.items.length ? `<ul class="small">${c.items.slice(0, 6).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}</div></div>`).join("")}</div>`;
        if (o.corrections && o.corrections.length) h += `<h4>Corrections applied</h4>${list(o.corrections)}`;
        break;
    }
    h += `<details class="raw"><summary>Raw structured output (JSON)</summary><pre>${esc(JSON.stringify(o, null, 2))}</pre></details>`;
    return h;
  }

  function renderPipeline(agents, openKeys) {
    return agents.map((a, i) => {
      const dot = a.status === "completed" ? "✓" : a.status === "fallback" ? "!" : a.status === "failed" || a.status === "skipped" ? "×" : String(i + 1);
      const meta = [a.status === "completed" || a.status === "fallback" ? modeBadge(a.mode, a.status) : "", a.duration_ms != null ? `${a.duration_ms} ms` : "", a.model ? esc(a.model) : ""].filter(Boolean).join(" · ");
      return `<li class="agent st-${esc(a.status)}"><span class="agent-dot">${dot}</span>
        <details class="agent-card" data-agent="${esc(a.agent)}" ${openKeys.has(a.agent) ? "open" : ""}>
          <summary><span class="name">${i + 1}. ${esc(a.name)}</span><span class="chip">${esc(STATUS_TEXT[a.status] || a.status)}</span>
            <span class="meta">${meta}<svg class="chev" viewBox="0 0 24 24"><path d="m7 10 5 5 5-5H7Z"/></svg></span>
            <span class="role">${esc(AGENT_ROLES[a.agent] || "")}</span></summary>
          <div class="agent-body">${renderAgentBody(a)}</div>
        </details></li>`;
    }).join("");
  }

  function sec(num, title, body) {
    return `<section class="rsec"><h3><span class="num">${num}</span>${esc(title)}</h3>${body}</section>`;
  }

  function renderReport(c) {
    const r = c.report;
    const gate = c.gate || {};
    if (!r) return `<div class="empty">${c.status === "error" ? esc(c.error || "The workflow did not complete.") : "The report will appear here when the agents finish."}</div>`;
    let h = "";
    h += `<div class="report-toolbar"><div class="chips">${modeBadgeForCase(c)}</div>
      <div class="row-actions"><a class="btn btn-ghost btn-sm" href="/api/cases/${esc(c.id)}/report.pdf">Download PDF</a><a class="btn btn-ghost btn-sm" href="/api/cases/${esc(c.id)}/report.docx">Download DOCX</a></div></div>`;

    h += `<div class="gate-box v-${esc(gate.verdict)}"><div class="card-head" style="margin:0"><h2>Quality &amp; Risk Gate: ${esc(gate.verdict_label || "")}</h2>
      <button class="btn btn-ghost btn-sm" id="stressBtn" title="Plant known faults in a copy of this report and check the gate catches them">Run red-team check</button></div>
      <p class="small muted" style="margin:.35rem 0 0">${Object.entries(gate.counts || {}).map(([k, v]) => `${v} ${k}`).join(" · ")}${gate.corrections && gate.corrections.length ? ` · ${gate.corrections.length} correction(s) applied` : ""}</p>
      <div id="stressOut" class="stress"></div></div>`;

    h += `<article class="report-doc"><div class="report-banner"><h2>HR Case Report · <span class="mono">${esc(r.case_id)}</span></h2><span class="draft">Draft for HR review</span></div>`;
    h += sec("1", "Case ID and case category", `<dl class="kv"><dt>Case ID</dt><dd class="mono">${esc(r.case_id)}</dd><dt>Title</dt><dd>${esc(r.title)}</dd><dt>Category</dt><dd>${esc(r.category_label)}</dd><dt>Urgency</dt><dd>${esc(r.urgency)}</dd><dt>Route</dt><dd>${esc(r.route_label)}</dd><dt>Access</dt><dd>${r.restricted_access ? "<strong>Restricted</strong> — designated HR/IC only" : "Standard HR access"}</dd><dt>Generated</dt><dd>${fmtDate(r.generated_at)}</dd></dl>${r.category_note ? `<p class="note-line" style="margin-top:.75rem">${esc(r.category_note)}</p>` : ""}`);
    h += sec("2", "Executive summary", `<span class="label-tag lt-rec">HR draft</span><p>${esc(r.executive_summary)}</p>`);
    const rc = r.reported_concern;
    h += sec("3", "Employee's reported concern", `<span class="label-tag lt-allegation">${esc(rc.label)}</span><p>${esc(rc.summary)}</p>${rc.statements && rc.statements.length ? `<ul>${rc.statements.map((s) => `<li class="statement">The reporter states: “${esc(s)}”</li>`).join("")}</ul>` : ""}${rc.requested_outcome ? `<p class="small"><strong>Requested outcome (as stated):</strong> ${esc(rc.requested_outcome)}</p>` : ""}`);
    const cf = r.confirmed_facts;
    h += sec("4", "Confirmed facts and available evidence", `<span class="label-tag lt-fact">Case-record facts</span><p class="small muted">${esc(cf.note)}</p>${list(cf.case_record_facts)}${cf.documentary_evidence.length ? `<h4 style="margin-top:.75rem">Documents (unauthenticated)</h4><ul>${cf.documentary_evidence.map((d) => `<li><strong>${esc(d.name)}</strong> — ${esc(d.status)}${d.notable_lines.length ? `<ul>${d.notable_lines.map((l) => `<li class="small">“${esc(l)}”</li>`).join("")}</ul>` : ""}</li>`).join("")}</ul>` : ""}`);
    h += sec("5", "Relevant HR policy provisions", `<span class="label-tag lt-policy">Company policy — fictional handbook</span>` + (r.policy_provisions.length ? r.policy_provisions.map((p) => `<div class="prov"><div class="prov-head"><a href="/policies?section=${encodeURIComponent(p.chunk_id)}">${esc(p.ref)}</a><span class="muted small">${esc(p.document)}</span>${p.retrieved_by === "routing rule" ? '<span class="badge b-rules">routing rule</span>' : ""}</div><p class="quote">“${esc(p.quote)}”</p>${p.how_it_applies ? `<p class="small">${esc(p.how_it_applies)}</p>` : ""}</div>`).join("") : `<div class="empty">No supporting handbook passage was retrieved. Human review must decide which policy, if any, applies.</div>`) + (r.policy_vs_law_notes || []).map((n) => `<p class="small muted" style="margin-top:.5rem">${esc(n)}</p>`).join(""));
    h += sec("6", "Missing information", r.missing_information.length ? `<table class="table compact responsive"><thead><tr><th>Item</th><th>Priority</th><th>Basis</th></tr></thead><tbody>${r.missing_information.map((m) => `<tr><td data-l="Item">${esc(m.item)}</td><td data-l="Priority">${m.severity === "required" ? '<span class="badge b-warn">required</span>' : esc(m.severity)}</td><td data-l="Basis" class="small">${esc(m.basis)}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">No gaps identified.</p>`);
    const comp = r.compliance;
    h += sec("7", "Compliance issues requiring verification", `<span class="label-tag lt-legal">Legal issues — require verification</span><p class="small muted">${esc(comp.disclaimer)}</p>${comp.statutory_route ? `<p class="note-line warn">${esc(comp.statutory_route)}</p>` : ""}<ul>${comp.issues.map((i) => `<li><strong>${esc(i.topic)}</strong>: verify against ${esc(i.framework_to_verify || "(no framework mapped in the handbook — Legal/Compliance to identify)")}.${i.what_hr_must_confirm ? ` HR must confirm: ${esc(i.what_hr_must_confirm)}` : ""} ${i.ref ? `<span class="muted small">(${esc(i.ref)})</span>` : ""}${i.why_relevant ? `<br><span class="small">${esc(i.why_relevant)}</span>` : ""}</li>`).join("")}</ul>${comp.verification_sources && comp.verification_sources.length ? `<p class="small" style="margin-top:.5rem"><strong>Where to verify</strong> (listed in the handbook):</p><ul class="small">${comp.verification_sources.map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a></li>`).join("")}</ul>` : ""}`);
    h += sec("8", "Suggested follow-up questions", `<span class="label-tag lt-rec">Neutral questions</span>${list(r.follow_up_questions)}${r.conflicts_of_interest.length ? `<h4 style="margin-top:.75rem">Conflicts of interest</h4>${list(r.conflicts_of_interest)}` : ""}`);
    h += sec("9", "Recommended procedural next steps", `<span class="label-tag lt-rec">HR recommendation — draft</span><table class="table compact responsive"><thead><tr><th>Step</th><th>Responsible</th><th>Basis</th></tr></thead><tbody>${r.next_steps.map((s) => `<tr><td data-l="Step">${esc(s.step)}</td><td data-l="Responsible">${esc(s.owner)}</td><td data-l="Basis">${s.chunk_id ? `<a class="link" href="/policies?section=${encodeURIComponent(s.chunk_id)}">${esc(s.ref)}</a>` : esc(s.ref || "")}</td></tr>`).join("")}</tbody></table>${r.ai_suggested_steps && r.ai_suggested_steps.length ? `<h4 style="margin-top:.75rem">Additional AI-suggested steps ${SOURCE_BADGE.llm}</h4>${list(r.ai_suggested_steps)}` : ""}${r.evidence_checklist.length ? `<h4 style="margin-top:.75rem">Evidence checklist</h4><ul>${r.evidence_checklist.map((e) => `<li>☐ ${esc(e.item)} <span class="muted small">— ${esc(e.status)} (${esc(e.basis)})</span></li>`).join("")}</ul>` : ""}<p class="small" style="margin-top:.75rem"><strong>Responsible functions:</strong> ${esc(r.responsible_functions.join(", "))}</p>`);
    const esc_ = r.escalation;
    h += sec("10", "Escalation requirements", `${esc_.urgent_actions && esc_.urgent_actions.length ? `<div class="urgent"><h2>⚠ Act now — do not wait for this report</h2><ul>${esc_.urgent_actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>` : ""}<ul>${esc_.items.map((e) => `<li><strong>${esc(e.to)}</strong> <span class="chip ${e.priority === "URGENT" ? "u-URGENT" : e.priority === "HIGH" ? "u-HIGH" : ""}">${esc(e.priority)}</span><br>${esc(e.reason)} ${e.basis.length ? `<span class="muted small">(${e.basis.map((b) => esc(b.ref)).join("; ")})</span>` : ""}</li>`).join("")}</ul>`);
    h += sec("11", "Limitations and uncertainty", `${list(r.limitations)}${r.unresolved_questions.length ? `<h4 style="margin-top:.75rem">Unresolved questions</h4>${list(r.unresolved_questions)}` : ""}`);
    const hr = r.human_review;
    h += sec("12", "Human review and approval status", `<dl class="kv"><dt>Status</dt><dd>${hr.status === "approved" ? '<span class="chip s-approved">Approved</span>' : hr.status === "returned" ? '<span class="chip s-returned">Returned</span>' : '<span class="chip">Pending — not approved</span>'}</dd><dt>Reviewer</dt><dd>${esc(hr.reviewer || "—")}</dd><dt>Decided</dt><dd>${fmtDate(hr.decided_at)}</dd><dt>Notes</dt><dd>${esc(hr.notes || "—")}</dd></dl><p class="small muted" style="margin-top:.6rem">Approval confirms the draft for further HR handling only. It is not a finding on the allegation and not a disciplinary decision.</p>`);
    h += `</article>`;
    return h;
  }

  function modeBadgeForCase(c) {
    return c.mode === "live" ? `${SOURCE_BADGE.llm}<span class="muted small">Narrative written by the configured model</span>` : `${SOURCE_BADGE.simulated}<span class="muted small">Demo mode — narrative from templates; retrieval and checks are real</span>`;
  }

  function renderReview(c) {
    const review = c.review || { history: [] };
    const blocked = c.gate && c.gate.verdict === "blocked";
    const ready = !!c.report;
    const hist = (review.history || []).slice().reverse().map((h) => `<li><strong>${esc(h.decision === "approve" ? "Approved" : "Returned")}</strong> by ${esc(h.reviewer)}<time>${fmtDate(h.at)}</time>${h.notes ? `<div class="small">${esc(h.notes)}</div>` : ""}</li>`).join("");
    return `<div class="review-grid">
      <form class="card" id="reviewForm">
        <div class="card-head"><h2>Record a review decision</h2></div>
        ${!ready ? '<p class="muted">The draft report is not ready yet.</p>' : ""}
        ${blocked ? '<p class="note-line err">The Quality &amp; Risk Gate blocked this draft. It can be returned, not approved.</p>' : ""}
        <p class="small muted">Approving confirms the draft is fit for further HR handling. It does not decide the grievance, find anyone at fault or authorise any action against an employee.</p>
        ${(c.gate && c.gate.review_reasons || []).length ? `<h4 class="small" style="margin:.5rem 0 .25rem">Why human review is required</h4>${list(c.gate.review_reasons, "small")}` : ""}
        <div class="field" style="margin-top:.75rem"><label for="rv_name">Reviewer name / role</label><input id="rv_name" name="reviewer" maxlength="80" placeholder="e.g. HR Manager (ER)" required></div>
        <div class="field" style="margin-top:.75rem"><label for="rv_notes">Notes</label><textarea id="rv_notes" name="notes" rows="4" maxlength="2000" placeholder="What was checked, what needs changing"></textarea></div>
        <div class="row-actions" style="margin-top:1rem">
          <button class="btn btn-ok" type="submit" data-decision="approve" ${!ready || blocked ? "disabled" : ""}>Approve draft</button>
          <button class="btn btn-ghost" type="submit" data-decision="return" ${!ready ? "disabled" : ""}>Return for rework</button>
        </div>
        <p class="err" id="rvErr"></p>
      </form>
      <section class="card"><div class="card-head"><h2>Review history</h2></div>${hist ? `<ol class="timeline">${hist}</ol>` : '<p class="muted">No decisions recorded yet.</p>'}</section>
    </div>`;
  }

  async function initCase() {
    const id = document.body.dataset.caseId;
    let lastSig = "";
    let timer;
    const openKeys = new Set();

    async function load() {
      let data;
      try {
        data = await api(`/api/cases/${encodeURIComponent(id)}`);
      } catch (e) {
        $("#caseRoot").innerHTML = `<div class="card"><h1>Case not found</h1><p class="muted">${esc(e.message)} Cases are only visible in the browser where they were created.</p><a class="btn btn-primary" href="/cases">Back to case history</a></div>`;
        return;
      }
      const c = data.case;
      const running = ["submitted", "running"].includes(c.status);
      const sig = JSON.stringify([c.status, c.updated_at, data.agents.map((a) => a.status)]);
      if (sig !== lastSig) {
        lastSig = sig;
        $$(".agent-card[open]").forEach((d) => openKeys.add(d.dataset.agent));
        $$(".agent-card:not([open])").forEach((d) => openKeys.delete(d.dataset.agent));
        $("#caseTitle").textContent = c.restricted ? `Restricted case ${c.id}` : c.title;
        $("#caseChips").innerHTML = `${statusChip(c.status, c.status_label)} ${urgencyChip(c.urgency)} ${routeChip(c.route)} ${c.restricted ? '<span class="chip restricted">Restricted access</span>' : ""} <span class="chip">${esc((c.triage && c.triage.category_label) || c.category)}</span> <span class="chip">Created ${fmtDate(c.created_at)}</span>`;
        $("#caseActions").innerHTML = `<button class="btn btn-ghost" id="rerunBtn" ${running ? "disabled" : ""}>Re-run agents</button><button class="btn btn-danger-ghost" id="delBtn">Delete case</button>`;
        const tri = c.triage || {};
        $("#urgentBox").innerHTML = tri.urgency === "URGENT" ? `<div class="urgent"><h2>⚠ Urgent risk flagged at intake</h2><p>${esc((tri.flags || []).map((f) => f.label).join("; "))}. Immediate human action is required — this analysis must not delay it.</p><ul>${(tri.urgent_actions || []).map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>` : "";
        $("#restrictedBox").innerHTML = c.route === "posh_ic" ? `<div class="restricted-box"><strong>POSH — statutory route.</strong> This concern must be handled by the Internal Committee / designated statutory process. Complaint details are withheld from the ER report and access is restricted. PeopleGuard does not investigate or decide POSH matters.</div>` : "";
        const done = data.agents.filter((a) => ["completed", "fallback"].includes(a.status)).length;
        $("#pipelineNote").textContent = running ? `Agents running — ${done} of 6 complete…` : `${done} of 6 agents completed · mode: ${c.mode === "live" ? "live AI" : "demo"}`;
        if (!openKeys.size && !running) openKeys.add("quality_gate");
        $("#pipeline").innerHTML = renderPipeline(data.agents, openKeys);
        $("#report").innerHTML = renderReport(c);
        $("#review").innerHTML = renderReview(c);
        wireCase(c);
      }
      clearTimeout(timer);
      if (running) timer = setTimeout(load, 1200);
    }

    function wireCase(c) {
      $("#delBtn").onclick = async () => {
        if (!confirm(`Delete case ${c.id}? This cannot be undone.`)) return;
        await api(`/api/cases/${encodeURIComponent(c.id)}`, { method: "DELETE" });
        location.href = "/cases";
      };
      $("#rerunBtn").onclick = async () => {
        try {
          await api(`/api/cases/${encodeURIComponent(c.id)}/run`, { method: "POST" });
          toast("Re-running all six agents…");
          lastSig = "";
          setTimeout(load, 300);
        } catch (e) { toast(e.message, true); }
      };
      const stress = $("#stressBtn");
      if (stress) stress.onclick = async () => {
        stress.disabled = true;
        try {
          const r = await api(`/api/cases/${encodeURIComponent(c.id)}/gate-stress-test`, { method: "POST" });
          $("#stressOut").innerHTML = `<p><strong>Red-team check: ${r.caught} of ${r.total} planted faults caught</strong> <span class="muted small">(on a copy — the saved report is unchanged). Gate verdict on the tampered copy: ${esc(r.verdict_label)}</span></p>${r.faults.map((f) => `<div class="stress-row"><span class="badge ${f.caught ? "b-pass" : "b-fail"}">${f.caught ? "caught" : "missed"}</span><span>${esc(f.fault)}</span></div>`).join("")}`;
        } catch (e) { toast(e.message, true); } finally { stress.disabled = false; }
      };
      const form = $("#reviewForm");
      if (form) form.onsubmit = async (e) => {
        e.preventDefault();
        const decision = e.submitter && e.submitter.dataset.decision;
        const reviewer = $("#rv_name").value.trim();
        if (reviewer.length < 2) { $("#rvErr").textContent = "Enter the reviewer's name or role."; return; }
        try {
          await api(`/api/cases/${encodeURIComponent(c.id)}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, reviewer, notes: $("#rv_notes").value }) });
          toast(decision === "approve" ? "Draft approved." : "Draft returned for rework.");
          lastSig = "";
          load();
        } catch (err) { $("#rvErr").textContent = err.message; }
      };
    }

    $$(".tab").forEach((t) => t.addEventListener("click", () => {
      $$(".tab").forEach((x) => { x.classList.toggle("active", x === t); x.setAttribute("aria-selected", String(x === t)); });
      $$(".tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== t.dataset.tab));
      history.replaceState(null, "", `#${t.dataset.tab}`);
    }));
    const hash = location.hash.replace("#", "");
    if (hash) { const t = $(`.tab[data-tab="${hash}"]`); t && t.click(); }
    load();
  }

  // ---------------------------------------------------------------- history
  async function initHistory() {
    let all = [];
    const loadBtn = $("#loadAll");
    loadBtn.dataset.label = loadBtn.textContent;
    let timer;
    async function refresh() {
      all = (await api("/api/cases")).cases;
      render();
      clearTimeout(timer);
      if (all.some((c) => ["submitted", "running"].includes(c.status))) timer = setTimeout(refresh, 2000);
    }
    function render() {
      const q = $("#filterText").value.toLowerCase();
      const st = $("#filterStatus").value;
      const fl = $("#filterFlag").checked;
      const rows = all.filter((c) => (!q || c.id.toLowerCase().includes(q) || (!c.restricted && c.title.toLowerCase().includes(q))) && (!st || c.status === st) && (!fl || c.escalation_flag));
      $("#caseTable").innerHTML = all.length ? casesTable(rows, { full: true }) : `<div class="empty">No cases yet. <a href="/cases/new">Create one</a> or load the demo cases.</div>`;
      wireRowLinks($("#caseTable"));
      $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
        if (!confirm(`Delete case ${b.dataset.del}?`)) return;
        await api(`/api/cases/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
        toast(`Case ${b.dataset.del} deleted.`);
        refresh();
      }));
    }
    ["#filterText", "#filterStatus", "#filterFlag"].forEach((s) => $(s).addEventListener("input", render));
    loadBtn.addEventListener("click", () => loadAllSamples(loadBtn, refresh));
    $("#deleteAll").addEventListener("click", async () => {
      if (!confirm("Delete ALL cases in this browser workspace?")) return;
      const r = await api("/api/cases", { method: "DELETE" });
      toast(`${r.deleted} case(s) deleted.`);
      refresh();
    });
    refresh().catch((e) => toast(e.message, true));
  }

  // ---------------------------------------------------------------- policies
  async function initPolicies() {
    const dlg = $("#sectionDialog");
    async function openSection(id) {
      try {
        const s = await api(`/api/policies/section/${encodeURIComponent(id)}`);
        $("#sectionBody").innerHTML = `<p class="eyebrow">${esc(s.document)}</p><h2 style="margin-bottom:.25rem">${esc(s.ref)}</h2>${s.parent ? `<p class="muted small">Under: ${esc(s.parent)}</p>` : ""}<p>${s.retrievable ? '<span class="badge b-pass">Used as case evidence</span>' : '<span class="badge b-sim">Reference only — not used as case evidence</span>'} <span class="badge b-rules">${esc(s.kind)}</span></p><div class="section-text">${esc(s.text)}</div>`;
        dlg.showModal();
      } catch (e) { toast(e.message, true); }
    }
    async function load() {
      const [p, cfg] = await Promise.all([api("/api/policies"), api("/api/config")]);
      const s = p.summary;
      $("#docList").innerHTML = s.documents.length ? s.documents.map((d) => `<div class="doc-item"><strong>${esc(d.name)}</strong><div class="muted small">${esc(d.title)}</div><div class="small">${d.sections} sections indexed · ${d.evidence_sections} used as case evidence · indexed ${fmtDate(new Date(s.indexed_at * 1000).toISOString())}</div>${d.warnings.map((w) => `<p class="note-line warn">${esc(w)}</p>`).join("")}</div>`).join("") + (s.errors || []).map((e) => `<p class="note-line err">${esc(e)}</p>`).join("")
        : `<div class="empty">No handbook is indexed. Place <span class="mono">PeopleGuard_Fictional_HR_Policy_Book_India.docx</span> in <span class="mono">data/policies/</span> and click Re-index.</div>`;
      if (!cfg.policy_upload_enabled) $("#uploadBox").innerHTML = `<p class="muted small" style="margin:0">Uploading a replacement handbook is disabled on this deployment (set <code>ADMIN_TOKEN</code> on the server to enable it). Re-index reloads the files already in <code>data/policies/</code>.</p>`;
      $("#sectionCount").textContent = `${p.sections.length} sections`;
      const groups = new Map();
      p.sections.forEach((x) => {
        const key = x.parent || x.ref.split(" (row")[0];
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(x);
      });
      $("#sectionTree").innerHTML = Array.from(groups.entries()).map(([g, items]) => `<details><summary>${esc(g)} <span class="muted small">(${items.length})</span></summary><ul>${items.map((x) => `<li><button data-sec="${esc(x.chunk_id)}">${esc(x.ref)}</button>${x.retrievable ? "" : '<span class="badge b-sim">reference only</span>'}</li>`).join("")}</ul></details>`).join("");
      $$("[data-sec]").forEach((b) => b.addEventListener("click", () => openSection(b.dataset.sec)));
    }
    $("#searchForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const q = $("#searchQ").value.trim();
      const out = $("#searchResults");
      if (q.length < 2) { out.innerHTML = '<p class="err">Enter at least two characters.</p>'; return; }
      try {
        const r = await api(`/api/policies/search?q=${encodeURIComponent(q)}&all_sections=${$("#searchAll").checked}`);
        out.innerHTML = r.results.length ? r.results.map((x) => `<div class="result"><div class="result-head"><button data-sec="${esc(x.chunk_id)}">${esc(x.ref)}</button><span class="muted small">score ${x.score.toFixed(1)} · ${esc(x.document)}</span></div><p>${esc(x.excerpt)}</p><p class="muted small">Matched: ${esc(x.matched_terms.join(", "))}</p></div>`).join("") : `<div class="empty">${esc(r.message)}</div>`;
        $$("[data-sec]", out).forEach((b) => b.addEventListener("click", () => openSection(b.dataset.sec)));
      } catch (err) { out.innerHTML = `<p class="err">${esc(err.message)}</p>`; }
    });
    $("#reindexBtn").addEventListener("click", async () => {
      const b = $("#reindexBtn");
      b.disabled = true;
      try {
        const r = await api("/api/policies/reindex", { method: "POST" });
        toast(`Re-indexed ${r.summary.total_sections} sections.`);
        load();
      } catch (e) { toast(e.message, true); } finally { b.disabled = false; }
    });
    const up = $("#uploadForm");
    if (up) up.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const r = await api("/api/policies/upload", { method: "POST", body: new FormData(e.target) });
        toast(`Uploaded ${r.uploaded}; ${r.summary.total_sections} sections indexed.`);
        $("#uploadErr").textContent = "";
        load();
      } catch (err) { $("#uploadErr").textContent = err.message; }
    });
    await load();
    const want = new URLSearchParams(location.search).get("section");
    if (want) openSection(want);
  }

  // ---------------------------------------------------------------- boot
  const page = document.body.dataset.page;
  const init = { dashboard: initDashboard, new_case: initNewCase, case: initCase, history: initHistory, policies: initPolicies }[page];
  if (init) Promise.resolve(init()).catch((e) => toast(e.message || "Something went wrong.", true));
})();
