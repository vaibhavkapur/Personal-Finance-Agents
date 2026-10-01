/* Customer journey + operations view. Plain JS talking to the application API. Fixture tokens only. */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const state = { token: localStorage.getItem("token") || "tok_customer_demo_3", caseId: localStorage.getItem("caseId") || null, view: "customer", me: null, docs: [] };

  const money = (minor, cur = "USD") => (minor === null || minor === undefined) ? "—" : `${(minor / 100).toFixed(2)} ${cur}`;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const badge = (text, cls) => `<span class="badge ${cls || text}">${esc(text)}</span>`;

  async function api(method, path, body) {
    const res = await fetch(path, { method, headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}` }, body: body ? JSON.stringify(body) : undefined });
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch { data = { message: text }; }
    if (!res.ok) { const err = new Error(data.message || (data.detail && (data.detail.message || JSON.stringify(data.detail))) || res.statusText); err.status = res.status; err.code = data.code; throw err; }
    return data;
  }
  const setMsg = (el, text, cls) => { el.textContent = text; el.className = `msg ${cls || ""}`; };

  // ------------------------------------------------------------------ session
  $("#token-select").value = state.token;
  $("#token-select").addEventListener("change", (e) => { state.token = e.target.value; localStorage.setItem("token", state.token); boot(); });
  document.querySelectorAll("nav .tab").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));

  function switchView(view) {
    state.view = view;
    document.querySelectorAll("nav .tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    $("#customer-view").hidden = view !== "customer";
    $("#operator-view").hidden = view !== "operator";
    if (view === "operator") loadOperator();
  }

  async function boot() {
    try {
      const health = await fetch("/health").then((r) => r.json());
      $("#env-badge").textContent = `${health.adapter.environment} · ${health.adapter.adapter}`;
      $("#clock-now").textContent = `clock ${health.clock}${health.clock_frozen ? " (frozen)" : ""}`;
      state.me = await api("GET", "/v1/me");
      state.docs = (await api("GET", "/v1/documents")).items;
      fillDocSelect($("#doc-select"), state.docs);
      await loadCases();
      if (state.caseId) await loadCase(state.caseId).catch(() => { state.caseId = null; renderEmpty(); });
      else renderEmpty();
    } catch (e) { renderEmpty(`Cannot reach the API: ${e.message}`); }
  }

  function fillDocSelect(sel, docs) {
    sel.innerHTML = docs.map((d) => `<option value="${esc(d.id)}">${esc(d.doc_type)} — ${esc(d.id)}</option>`).join("");
  }

  async function loadCases() {
    const items = (await api("GET", "/v1/claims")).items;
    $("#case-list").innerHTML = items.length ? items.map((c) => `<li class="selectable ${c.id === state.caseId ? "active" : ""}" data-id="${esc(c.id)}"><strong>${esc(c.id)}</strong><br><span class="muted small">${esc(c.status)} · v${c.version} · loss ${esc(c.loss_at)}</span></li>`).join("") : `<li class="muted">No cases yet.</li>`;
    document.querySelectorAll("#case-list li.selectable").forEach((li) => li.addEventListener("click", () => loadCase(li.dataset.id)));
  }

  $("#create-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const document_ids = [...$("#doc-select").selectedOptions].map((o) => o.value);
    try {
      const res = await api("POST", "/v1/claims", { customer_id: state.me.customer_id, policy_id: "travel_policy_demo_1", loss_type: "baggage_delay", loss_at: f.get("loss_at"), document_ids, mock_scenario: f.get("mock_scenario") || null });
      $("#new-case").open = false;
      await loadCases();
      await loadCase(res.id);
    } catch (err) { alert(err.message); }
  });

  function renderEmpty(msg) {
    $("#case-detail").hidden = true;
    $("#case-empty").hidden = false;
    if (msg) $("#case-empty").textContent = msg;
  }

  // ------------------------------------------------------------------ case detail
  async function loadCase(id) {
    state.caseId = id;
    localStorage.setItem("caseId", id);
    const [view, timeline] = await Promise.all([api("GET", `/v1/claims/${id}`), api("GET", `/v1/claims/${id}/timeline`)]);
    state.view_data = view;
    render(view, timeline);
    await loadCases();
    if (state.view === "operator") loadOperator();
  }

  function render(v, timeline) {
    $("#case-empty").hidden = true;
    $("#case-detail").hidden = false;
    const c = v.case;
    const cur = v.totals.currency;
    $("#case-title").textContent = `${c.id} · ${c.loss_type.replace("_", " ")}`;
    $("#case-meta").textContent = `${v.claimant.full_name} · policy ${v.policy.policy_id} v${v.policy.version} (${v.policy.insurer_name}) · loss ${c.loss_at} · case version ${c.version}${c.external_claim_ref ? ` · insurer ref ${c.external_claim_ref}` : ""}`;
    $("#case-status").textContent = c.status;
    $("#case-waiting").textContent = `waiting on ${v.waiting_on}`;
    $("#next-step").textContent = v.next_step;

    $("#checklist").innerHTML = v.checklist.map((i) => `<li>${badge(i.status)} <strong>${esc(i.id.replace(/_/g, " "))}</strong>${i.clause ? ` <span class="muted small">(${esc(i.clause)})</span>` : ""}<br><span class="small">${esc(i.detail)}</span>${i.evidence.length ? `<br><span class="mono muted">${i.evidence.map(esc).join(", ")}</span>` : ""}</li>`).join("");
    $("#evidence-checklist").innerHTML = v.evidence_checklist.map((e) => `<li>${badge(e.present ? "present" : (e.alternative_present.length ? "alternative" : "missing"), e.present ? "ok" : (e.alternative_present.length ? "warn" : "bad"))} ${esc(e.doc_type)} ${e.document_id ? `<span class="mono muted">${esc(e.document_id)}</span>` : ""}</li>`).join("");
    const attached = new Set(v.documents.map((d) => d.id));
    fillDocSelect($("#attach-select"), state.docs.filter((d) => !attached.has(d.id)));

    const t = v.totals;
    $("#totals-table").innerHTML = [
      ["Requested (all receipts)", money(t.requested_minor, cur)], ["Supported by fixture rules", money(t.supported_minor, cur)], ["Excluded", money(t.excluded_minor, cur)], ["Uncertain", money(t.uncertain_minor, cur)], ["Duplicate", money(t.duplicate_minor, cur)],
      ["Policy cap", money(t.cap_minor, cur)], ["Estimated payable (not a decision)", money(t.estimated_payable_minor, cur)], ["Approved by insurer", money(t.approved_minor, cur)], ["Paid", money(t.paid_minor, cur)], ["Outstanding", money(t.outstanding_minor, cur)],
    ].map(([k, val]) => `<tr><th>${k}</th><td>${val}</td></tr>`).join("");

    $("#questions").innerHTML = v.open_questions.length ? v.open_questions.map(renderQuestion).join("") : `<p class="muted">No open questions.</p>`;
    document.querySelectorAll("form.answer").forEach((f) => f.addEventListener("submit", onAnswer));

    $("#expenses-table").innerHTML = `<tr><th>Receipt</th><th>Merchant</th><th>Purchased</th><th>Category</th><th>Requested</th><th>Supported</th><th>Approved</th><th>Status</th><th>Reason</th></tr>` +
      v.expenses.map((e) => `<tr><td class="mono">${esc(e.receipt_id)}<br><span class="muted">${esc(e.source_locator)}</span></td><td>${esc(e.merchant)}<br><span class="muted small">${esc(e.description)}</span></td><td>${esc(e.purchased_at || "unknown")}</td><td>${esc(e.category)}</td><td>${money(e.requested_minor, e.currency)}</td><td>${money(e.supported_minor, e.currency)}</td><td>${money(e.approved_minor, e.currency)}</td><td>${badge(e.eligibility_status)}</td><td class="small">${esc(e.exclusion_reason || "")}${e.rule_id ? ` <span class="mono muted">${esc(e.rule_id)}</span>` : ""}</td></tr>`).join("");

    renderAction(v);
    $("#btn-export").href = `/v1/claims/${c.id}/export`;
    $("#btn-export").onclick = async (e) => { e.preventDefault(); const data = await api("GET", `/v1/claims/${c.id}/export`); const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }); const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${c.id}-history.json`; a.click(); };

    $("#decisions").innerHTML = v.decisions.length ? v.decisions.map((d) => {
      const ex = d.explanation || {};
      return `<div class="question"><strong>Decision v${d.decision_version}: ${badge(d.outcome, d.outcome === "approved" ? "ok" : d.outcome === "denied" ? "bad" : "warn")}</strong> accepted ${money(d.accepted_minor, d.currency)}, not paid ${money(d.rejected_minor, d.currency)} · ${esc(d.decided_at)} · appeal: ${esc(d.appeal_status)}<p>${esc(ex.summary || "")}</p>
        <table>${(ex.items || []).filter((i) => i.rejected_minor).map((i) => `<tr><td class="mono">${esc(i.expense_id || "(aggregate)")}</td><td>${esc(i.insurer_reason_code)}<br><span class="muted small">${esc(i.insurer_reason_text)}</span></td><td>${money(i.rejected_minor, d.currency)}</td><td>${badge(i.policy_view, i.policy_view === "contradicted_by_evidence" ? "ok" : i.policy_view === "supported_by_policy" ? "info" : "warn")}<br><span class="small">${esc(i.explanation || "")}</span>${i.citation ? `<br><span class="mono muted">${esc(i.citation.clause_id)} v${esc(i.citation.policy_version)}</span>` : ""}${i.fact_locators ? `<br><span class="mono muted">${i.fact_locators.map(esc).join(", ")}</span>` : ""}</td></tr>`).join("")}</table></div>`;
    }).join("") : `<p class="muted">No insurer decision yet.</p>`;

    $("#requests").innerHTML = v.insurer_requests.length ? v.insurer_requests.map((r) => `<li>${badge(r.status, r.status === "open" ? "warn" : r.status === "satisfied" ? "ok" : "bad")} <strong>${esc(r.requirement.document_type)}</strong> — ${esc(r.requirement.message || "")}<br><span class="muted small">request ${esc(r.provider_request_id)} · due ${esc(r.due_at)} · deadline source: ${esc(r.deadline_source)}${r.satisfied_by_submission_id ? ` · satisfied by ${esc(r.satisfied_by_submission_id)}` : ""}</span></li>`).join("") : `<li class="muted">None.</li>`;

    const s = v.settlement;
    $("#settlement").innerHTML = s ? `<p>${badge(s.status, s.status === "paid_in_full" ? "ok" : s.status === "unpaid" ? "warn" : "info")} accepted ${money(s.accepted_minor, s.currency)} · paid ${money(s.paid_minor, s.currency)} · outstanding ${money(s.outstanding_minor, s.currency)}</p>
      <table><tr><th>Payment</th><th>Amount</th><th>Posted</th><th>Reconciliation</th></tr>${v.payments.map((p) => `<tr><td class="mono">${esc(p.provider_payment_ref)}<br><span class="muted">${esc(p.claim_reference)} · ${esc(p.payee_id)}</span></td><td>${money(p.amount_minor, p.currency)} (${esc(p.payment_status)})</td><td>${esc(p.posted_at)}</td><td>${esc(p.reconciliation_status)}</td></tr>`).join("")}</table>` : `<p class="muted">No decision, so nothing to reconcile yet.</p>`;

    $("#timeline").innerHTML = timeline.events.slice().reverse().map((e) => `<li><span class="t">${esc(e.occurred_at)} · #${e.sequence} · ${esc(e.actor)}</span><br><strong>${esc(e.type)}</strong>${e.next_state ? ` ${esc(e.previous_state)} → ${esc(e.next_state)}` : ""}${e.source_event_id ? ` <span class="mono muted">${esc(e.source_event_id)}</span>` : ""}</li>`).join("");
  }

  function renderQuestion(q) {
    const inputs = {
      baggage_delivered_at: `<input name="delivered_at" placeholder="2026-09-12T16:30:00Z" required />`,
      delay_start_time: `<input name="delay_start_at" placeholder="2026-09-10T14:00:00Z" required />`,
      purchased_at: `<input name="purchased_at" placeholder="2026-09-11T15:00:00Z" required />`,
      loss_date: `<input name="loss_at" placeholder="2026-09-10T14:00:00Z" required />`,
      category: `<select name="category"><option>clothing</option><option>toiletries</option><option>essentials</option><option>electronics</option><option>alcohol</option><option>jewelry</option><option>luxury</option><option value="other">other (not covered)</option></select>`,
      distinct_purchase: `<select name="distinct"><option value="true">Yes, a separate purchase</option><option value="false">No, same purchase</option></select>`,
      destination_confirmation: `<select name="not_home"><option value="true">Yes, away from home</option><option value="false">No, this was my home city</option></select>`,
      confirm_fact: `<select name="confirm"><option value="true">Confirm</option><option value="false">Reject</option></select>`,
      receipt_confirmation: `<select name="confirm"><option value="true">Confirm</option><option value="false">Reject</option></select>`,
    }[q.field] || `<input name="value" />`;
    return `<form class="answer question" data-q="${esc(q.id)}" data-field="${esc(q.field)}"><div><span class="muted small">${esc(q.field)}${q.expense_id ? ` · ${esc(q.expense_id)}` : ""}</span><br>${esc(q.question)}</div><div class="inline" style="display:flex;gap:8px;margin-top:6px">${inputs}<button type="submit">Answer</button></div></form>`;
  }

  async function onAnswer(e) {
    e.preventDefault();
    const form = e.target;
    const answer = {};
    for (const [k, val] of new FormData(form).entries()) answer[k] = val === "true" ? true : val === "false" ? false : val;
    try { await api("POST", `/v1/claims/${state.caseId}/questions/${form.dataset.q}/answer`, { answer }); await loadCase(state.caseId); }
    catch (err) { alert(err.message); }
  }

  function renderAction(v) {
    const a = v.pending_action;
    const card = $("#action-card");
    if (!a) { card.hidden = true; return; }
    card.hidden = false;
    const r = a.review_summary || {};
    const cur = r.currency || "USD";
    const canApprove = a.status === "proposed" && state.me && state.me.role !== "operator";
    card.querySelector("#action-review").innerHTML = `<div class="review">
      <dl>
        <dt>Action</dt><dd>${esc(a.action_type)} (${esc(a.packet_type)}) ${badge(a.status, a.status === "proposed" ? "warn" : a.status === "succeeded" ? "ok" : "info")}</dd>
        <dt>Destination</dt><dd>${esc(r.destination)} ${r.simulator_scenario ? badge(`simulator: ${r.simulator_scenario}`, "mock") : ""}</dd>
        <dt>Documents shared</dt><dd>${(r.documents || []).map((d) => `<div class="mono">${esc(d.doc_type)} · ${esc(d.document_id)} · ${esc(d.content_hash.slice(0, 23))}…</div>`).join("") || "none"}</dd>
        <dt>Amount requested</dt><dd>${money(r.requested_total_minor, cur)} (policy cap makes ${money(r.expected_maximum_minor, cur)} the most the fixture rules support)</dd>
        <dt>Statements of fact</dt><dd>${(r.statements || []).map((s) => `<div>• ${esc(s)}</div>`).join("")}</dd>
        <dt>Excluded &amp; disclosed</dt><dd>${(r.excluded_items || []).map((x) => `<div>${esc(x.receipt_id)} ${money(x.amount_minor, cur)} — ${esc(x.reason)}</div>`).join("") || "none"}</dd>
        ${(r.challenges || []).length ? `<dt>Challenges</dt><dd>${r.challenges.map((c) => `<div><strong>${esc(c.reason_code)}</strong> on ${esc(c.expense_id)} (${money(c.challenged_amount_minor, cur)}) — ${esc(c.clause_id)}: ${esc(c.argument)}<br><span class="mono muted">${c.fact_locators.map(esc).join(", ")}</span></div>`).join("")}</dd>` : ""}
        <dt>Irreversible effect</dt><dd class="irreversible">${esc(r.irreversible_effect)}</dd>
        <dt>Packet hash</dt><dd class="mono">${esc(a.content_hash)}</dd>
        <dt>Approval challenge</dt><dd class="mono">${esc(a.approval_challenge_id)} (${esc(a.challenge_status)}, expires ${esc(a.challenge_expires_at)})</dd>
        <dt>Case version bound</dt><dd>${a.expected_case_version}</dd>
        ${a.provider_ref ? `<dt>Provider reference</dt><dd class="mono">${esc(a.provider_ref)}</dd>` : ""}
      </dl>
      <div class="buttons" style="margin-top:10px">
        <button id="btn-approve" ${canApprove ? "" : "disabled"}>Approve exactly this packet</button>
        ${!canApprove && a.status === "proposed" ? `<span class="muted small">Only the claimant or an authorized representative can approve.</span>` : ""}
      </div>
    </div>`;
    const btn = $("#btn-approve");
    if (btn) btn.onclick = async () => {
      try {
        const res = await api("POST", `/v1/actions/${a.action_id}/approve`, { expected_case_version: a.expected_case_version, action_payload_hash: a.content_hash, approval_challenge_id: a.approval_challenge_id });
        setMsg($("#action-msg"), `Approved (${res.approval_id}). The background worker executes it; run the worker from the Operator tab or wait for the service worker.`, "ok");
        await loadCase(state.caseId);
      } catch (err) { setMsg($("#action-msg"), `${err.status} ${err.code || ""}: ${err.message}`, "error"); }
    };
  }

  const act = (label, fn) => async () => { try { const res = await fn(); setMsg($("#action-msg"), typeof res === "string" ? res : `${label}: ok`, "ok"); await loadCase(state.caseId); } catch (err) { setMsg($("#action-msg"), `${label} failed (${err.status} ${err.code || ""}): ${err.message}`, "error"); } };
  $("#btn-evaluate").onclick = act("Evaluate", () => api("POST", `/v1/claims/${state.caseId}/evaluate`));
  $("#btn-draft").onclick = act("Draft", async () => { const d = await api("POST", `/v1/claims/${state.caseId}/submission-drafts`, {}); return `Draft ${d.packet_type} ready for review: ${d.action_id}`; });
  $("#btn-appeal").onclick = act("Appeal draft", async () => { const d = await api("POST", `/v1/claims/${state.caseId}/appeal-drafts`, {}); return `Appeal draft ready for review: ${d.action_id}`; });
  $("#btn-accept").onclick = act("Accept decision", () => api("POST", `/v1/claims/${state.caseId}/decision/accept`));
  $("#btn-reconcile").onclick = act("Reconcile", async () => { const r = await api("POST", `/v1/claims/${state.caseId}/reconcile`); return `Settlement: ${r.settlement.status} (paid ${money(r.settlement.paid_minor)}, outstanding ${money(r.settlement.outstanding_minor)})`; });
  $("#attach-form").addEventListener("submit", (e) => { e.preventDefault(); const ids = [...$("#attach-select").selectedOptions].map((o) => o.value); if (!ids.length) return; act("Attach", () => api("POST", `/v1/claims/${state.caseId}/documents`, { document_ids: ids }))(); });

  $("#agent-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#agent-input").value.trim();
    const log = $("#agent-log");
    log.insertAdjacentHTML("beforeend", `<div class="turn"><div class="who">You</div>${esc(msg || "(status check)")}</div>`);
    try {
      const turn = await api("POST", `/v1/claims/${state.caseId}/agent/turns`, { message: msg });
      log.insertAdjacentHTML("beforeend", `<div class="turn"><div class="who">Advocate · ${esc(turn.planner)} · tools: ${turn.tools_used.map(esc).join(", ") || "none"}</div>${esc(turn.message)}</div>`);
      $("#agent-input").value = "";
      await loadCase(state.caseId);
    } catch (err) { log.insertAdjacentHTML("beforeend", `<div class="turn msg error">${esc(err.message)}</div>`); }
    log.scrollTop = log.scrollHeight;
  });

  // ------------------------------------------------------------------ operator
  async function loadOperator() {
    const msg = $("#op-msg");
    if (!state.me || state.me.role !== "operator") { setMsg(msg, "Sign in as the Operations reviewer to use this view.", "error"); return; }
    try {
      const [metrics, caps, jobs, inbox, mock] = await Promise.all([api("GET", "/v1/dev/metrics"), api("GET", "/v1/provider/capabilities"), api("GET", "/v1/dev/jobs"), api("GET", "/v1/dev/inbox"), api("GET", "/v1/dev/mock-insurer/claims")]);
      $("#metrics-table").innerHTML = Object.entries(metrics).map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("");
      $("#capabilities").textContent = JSON.stringify(caps, null, 2);
      $("#op-jobs").innerHTML = `<tr><th>Job</th><th>Type</th><th>Status</th><th>Run at</th><th>Attempts</th><th>Lease</th><th>Error</th></tr>` + jobs.items.map((j) => `<tr><td class="mono">${esc(j.id)}</td><td>${esc(j.type)}</td><td>${badge(j.status, j.status === "done" ? "ok" : j.status === "failed" ? "bad" : "warn")}</td><td>${esc(j.run_at)}</td><td>${j.attempts}</td><td class="mono">${esc(j.lease_until || "")}</td><td class="small">${esc(j.last_error || "")}</td></tr>`).join("");
      $("#op-inbox").innerHTML = `<tr><th>Provider</th><th>Event</th><th>Type</th><th>Claim ref</th><th>Seq</th><th>Outcome</th><th>Received</th></tr>` + inbox.items.map((i) => `<tr><td>${esc(i.provider)}</td><td class="mono">${esc(i.provider_event_id)}</td><td>${esc(i.type)}</td><td class="mono">${esc(i.claim_reference || "")}</td><td>${esc(i.sequence ?? "")}</td><td>${esc(i.outcome)}</td><td>${esc(i.received_at)}</td></tr>`).join("");
      $("#op-mock").textContent = JSON.stringify(mock.items, null, 2);
      if (state.caseId) {
        const ops = await api("GET", `/v1/claims/${state.caseId}/operator`);
        $("#op-actions").innerHTML = `<tr><th>Action</th><th>Type</th><th>Status</th><th>Payload hash</th><th>Idempotency key</th><th>Provider ref</th><th>Result</th></tr>` + ops.actions.map((a) => `<tr><td class="mono">${esc(a.action_id)}</td><td>${esc(a.action_type)}</td><td>${badge(a.status, a.status === "succeeded" ? "ok" : a.status === "failed" ? "bad" : "warn")}</td><td class="mono">${esc(a.content_hash.slice(0, 30))}…</td><td class="mono">${esc(a.idempotency_key)}</td><td class="mono">${esc(a.provider_ref || "")}</td><td class="small">${esc(JSON.stringify(a.result || {})).slice(0, 160)}</td></tr>`).join("");
        $("#op-adapter").innerHTML = `<tr><th>When</th><th>Adapter</th><th>Operation</th><th>Request ref</th><th>Outcome</th><th>Env</th><th>Detail</th></tr>` + ops.adapter_requests.map((r) => `<tr><td>${esc(r.created_at)}</td><td>${esc(r.adapter)}</td><td>${esc(r.operation)}</td><td class="mono">${esc(r.request_ref || "")}</td><td>${badge(r.outcome, r.outcome === "accepted" || r.outcome === "found" ? "ok" : r.outcome === "declined" ? "bad" : "warn")}</td><td>${esc(r.environment)}</td><td class="small">${esc(JSON.stringify(r.detail))}</td></tr>`).join("");
        $("#op-transitions").innerHTML = `<tr><th>Seq</th><th>When</th><th>Event</th><th>From</th><th>To</th><th>Actor</th><th>Expected version</th><th>Source event</th></tr>` + ops.transitions.map((e) => `<tr><td>${e.sequence}</td><td>${esc(e.occurred_at)}</td><td>${esc(e.type)}</td><td>${esc(e.previous_state)}</td><td>${esc(e.next_state)}</td><td>${esc(e.actor)}</td><td>${e.expected_case_version}</td><td class="mono">${esc(e.source_event_id || "")}</td></tr>`).join("");
        $("#op-tools").innerHTML = `<tr><th>When</th><th>Tool</th><th>Outcome</th><th>Latency</th><th>Input ref</th><th>Output ref</th><th>Input (redacted)</th></tr>` + ops.tool_runs.map((t) => `<tr><td>${esc(t.created_at)}</td><td>${esc(t.tool)}</td><td>${badge(t.outcome, t.outcome === "ok" ? "ok" : "bad")}</td><td>${t.latency_ms} ms</td><td class="mono">${esc(t.input_ref.slice(0, 22))}…</td><td class="mono">${esc(t.output_ref.slice(0, 22))}…</td><td class="small">${esc(JSON.stringify(t.input_summary))}</td></tr>`).join("");
      }
      setMsg(msg, "", "");
    } catch (err) { setMsg(msg, err.message, "error"); }
  }

  const opAct = (fn) => async () => { try { const r = await fn(); setMsg($("#op-msg"), typeof r === "string" ? r : JSON.stringify(r), "ok"); await boot(); if (state.caseId) await loadCase(state.caseId); await loadOperator(); } catch (err) { setMsg($("#op-msg"), `${err.status} ${err.code || ""}: ${err.message}`, "error"); } };
  document.querySelectorAll("[data-op=advance]").forEach((b) => b.onclick = opAct(() => api("POST", "/v1/dev/clock", { advance_hours: Number(b.dataset.hours) })));
  document.querySelectorAll("[data-op=worker]").forEach((b) => b.onclick = opAct(async () => { const r = await api("POST", "/v1/dev/worker/run-until-idle"); return `worker ran ${r.rounds} round(s): ${JSON.stringify(r.stats[0])}`; }));
  document.querySelectorAll("[data-fault]").forEach((b) => b.onclick = opAct(() => api("POST", "/v1/dev/mock-insurer/faults", { fault: b.dataset.fault })));
  document.querySelectorAll("[data-chaos]").forEach((b) => b.onclick = opAct(async () => { const cur = (await api("POST", "/v1/dev/mock-insurer/faults", {})).chaos; const body = {}; body[b.dataset.chaos] = !cur[b.dataset.chaos]; return api("POST", "/v1/dev/mock-insurer/faults", body); }));
  document.querySelectorAll("[data-pay]").forEach((b) => b.onclick = opAct(() => { if (!state.caseId) throw new Error("select a case first"); return api("POST", "/v1/dev/mock-payments/emit", { case_id: state.caseId, mode: b.dataset.pay }); }));

  boot();
})();
