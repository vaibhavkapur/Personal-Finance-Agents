/* Loan Negotiation Agent - dependency-free customer and operator views. */
(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const state = { me: null, caseId: null, view: null, timer: null };

  const usd = (minor) => {
    if (minor === null || minor === undefined) return "—";
    const sign = minor < 0 ? "-" : "";
    const abs = Math.abs(minor);
    return `${sign}$${(Math.floor(abs / 100)).toLocaleString()}.${String(abs % 100).padStart(2, "0")}`;
  };
  const pct = (rate) => (rate === null || rate === undefined ? "—" : `${(parseFloat(rate) * 100).toFixed(3).replace(/0+$/, "").replace(/\.$/, "")}%`);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const tokenHeaders = (op) => ({ Authorization: `Bearer ${op ? $("#op-token").value : $("#token").value}`, "Content-Type": "application/json" });

  async function api(path, opts = {}, op = false) {
    const res = await fetch(path, { ...opts, headers: { ...tokenHeaders(op), ...(opts.headers || {}) } });
    const text = await res.text();
    let body = {};
    try { body = text ? JSON.parse(text) : {}; } catch (_) { body = { detail: text }; }
    if (!res.ok) throw new Error(`${res.status}: ${typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body)}`);
    return body;
  }

  // ------------------------------------------------------------- navigation
  $$("nav button").forEach((b) => b.addEventListener("click", () => {
    $$("nav button").forEach((x) => x.classList.toggle("active", x === b));
    const view = b.dataset.view;
    $("#view-customer").hidden = view !== "customer";
    $("#view-operator").hidden = view !== "operator";
    if (view === "operator") refreshOps();
  }));

  // --------------------------------------------------------------- customer
  async function loadMe() {
    try {
      state.me = await api("/v1/me");
    } catch (e) {
      $("#case-panel").innerHTML = `<p class="notice">${esc(e.message)}. Check the borrower token.</p>`;
      return;
    }
    $("#env-tag").textContent = state.me.environment;
    $("#clock").textContent = `fixture clock: ${state.me.clock_now}`;
    $("#mortgage-select").innerHTML = state.me.mortgages.map((m) => `<option value="${m.id}">${m.id} — ${usd(m.balance_minor)} @ ${pct(m.note_rate_decimal)}, ${m.remaining_months} mo</option>`).join("");
    $("#offer-docs").innerHTML = state.me.documents.filter((d) => d.kind === "loan_estimate").map((d) => `<label><input type="checkbox" value="${d.id}" ${/_(a|b|c)$/.test(d.id) ? "checked" : ""}/> ${d.id} <span class="muted">(${d.source})</span></label>`).join("");
    $("#case-list").innerHTML = state.me.cases.map((c) => `<li data-id="${c.id}" class="${c.id === state.caseId ? "active" : ""}"><span>${c.id}</span><span class="tag state">${c.status}</span></li>`).join("") || "<li class='muted'>No cases yet.</li>";
    $$("#case-list li[data-id]").forEach((li) => li.addEventListener("click", () => selectCase(li.dataset.id)));
  }

  $("#create-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const docs = $$("#offer-docs input:checked").map((i) => i.value);
    try {
      const res = await api("/v1/loan-cases", {
        method: "POST",
        body: JSON.stringify({
          customer_id: state.me.customer.id,
          mortgage_id: $("#mortgage-select").value,
          holding_horizon_months: parseInt($("#horizon").value, 10),
          maximum_cash_to_close_minor: Math.round(parseFloat($("#max-cash").value || "0") * 100),
          offer_document_ids: docs,
        }),
      });
      await loadMe();
      selectCase(res.id);
    } catch (e) { alert(e.message); }
  });

  async function selectCase(id) {
    state.caseId = id;
    await refreshCase();
    await loadMe();
  }

  async function refreshCase() {
    if (!state.caseId) return;
    try {
      const [view, msgs, tl] = await Promise.all([
        api(`/v1/loan-cases/${state.caseId}`),
        api(`/v1/loan-cases/${state.caseId}/messages`),
        api(`/v1/loan-cases/${state.caseId}/timeline`),
      ]);
      state.view = view;
      renderCase(view, msgs.messages, tl.events);
      const li = $(`#case-list li[data-id="${view.id}"] .tag`);
      if (li) li.textContent = view.status;
    } catch (e) {
      $("#case-panel").innerHTML = `<p class="notice">${esc(e.message)}</p>`;
    }
  }

  function renderCase(v, messages, events) {
    const panel = $("#case-panel");
    const tpl = $("#tpl-case").content.cloneNode(true);
    tpl.querySelector(".case-id").textContent = v.id;
    tpl.querySelector(".state").textContent = v.status;
    tpl.querySelector(".version").textContent = v.version;
    tpl.querySelector(".next-decision").innerHTML = `<strong>Next:</strong> ${esc(v.next_decision.text)}`;

    const m = v.mortgage;
    tpl.querySelector(".mortgage").innerHTML = `<dl class="kv">
      <dt>Balance</dt><dd>${usd(m.balance_minor)} <span class="muted">as of ${m.as_of ? m.as_of.slice(0, 10) : "?"}${m.balance_confirmed_at ? " (confirmed)" : " (unconfirmed)"}</span></dd>
      <dt>Note rate</dt><dd>${pct(m.note_rate_decimal)}</dd>
      <dt>Remaining</dt><dd>${m.remaining_months} months</dd>
      <dt>P&amp;I</dt><dd>${usd(m.monthly_pi_minor)} <span class="muted">escrow ${usd(m.escrow_minor)}; includes escrow: ${m.payment_includes_escrow === null ? "unknown" : m.payment_includes_escrow}</span></dd>
      <dt>Horizon</dt><dd>${v.holding_horizon_months ?? "—"} months; max cash ${usd(v.maximum_cash_to_close_minor)}</dd>
      <dt>Status</dt><dd>Existing loan remains active throughout comparison and application.</dd>
    </dl>`;

    const qs = v.outstanding_questions || [];
    tpl.querySelector(".questions").innerHTML = qs.length ? qs.map((q) => `<div class="row"><span>${esc(q.question)}</span>${questionControl(q)}</div>`).join("") : "<p class='muted'>Nothing outstanding.</p>";

    tpl.querySelector(".comparison").innerHTML = renderComparison(v);
    tpl.querySelector(".pending").innerHTML = renderPending(v);
    if (!(v.pending_actions || []).length) tpl.querySelector(".approvals").style.display = "none";
    tpl.querySelector(".offers").innerHTML = renderOffers(v);
    tpl.querySelector(".lender-requests").innerHTML = renderLenderRequests(v);
    tpl.querySelector(".applications").innerHTML = renderApplications(v);
    tpl.querySelector(".messages").innerHTML = messages.map((mm) => `<div class="msg ${mm.role}">${esc(mm.content)}<div class="meta">${mm.role} · ${mm.created_at}${mm.data && mm.data.tool_calls ? " · tools: " + mm.data.tool_calls.map((t) => t.tool + (t.ok ? "" : "(error)")).join(", ") : ""}</div></div>`).join("") || "<p class='muted'>Say hello to the agent.</p>";
    tpl.querySelector(".timeline").innerHTML = events.slice().reverse().map((e) => `<div><span class="muted">#${e.sequence} ${e.occurred_at}</span> <strong>${esc(e.type)}</strong> by ${esc(e.actor)}${e.previous_state ? ` (${e.previous_state} → ${e.next_state})` : ""}</div>`).join("");

    panel.innerHTML = "";
    panel.appendChild(tpl);
    const msgBox = $(".messages", panel);
    msgBox.scrollTop = msgBox.scrollHeight;

    $(".chat-form", panel).addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const input = $(".chat-input", panel);
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      await chat(text);
    });
    $$(".quick button", panel).forEach((b) => b.addEventListener("click", () => chat(b.dataset.msg)));
    $$("[data-approve]", panel).forEach((b) => b.addEventListener("click", () => approve(b.dataset.approve)));
    $$("[data-decline]", panel).forEach((b) => b.addEventListener("click", () => decline(b.dataset.decline)));
    $$("[data-answer]", panel).forEach((b) => b.addEventListener("click", () => answer(b)));
    $$("[data-supply]", panel).forEach((b) => b.addEventListener("click", () => supplyField(b)));
    $$("[data-release]", panel).forEach((b) => b.addEventListener("click", () => releaseDocs(b.dataset.release)));
    $$("[data-close]", panel).forEach((b) => b.addEventListener("click", () => requestClosing(b.dataset.close)));
    $$("[data-finance]", panel).forEach((cb) => cb.addEventListener("change", () => toggleFinance(cb.dataset.finance, cb.checked)));
  }

  function questionControl(q) {
    if (q.kind === "confirm") return `<button class="secondary" data-answer='${JSON.stringify({ current_balance_confirmed: true })}'>Confirm</button>`;
    if (q.kind === "yes_no") return `<button class="secondary" data-answer='${JSON.stringify({ [q.field]: true })}'>Yes</button><button class="secondary" data-answer='${JSON.stringify({ [q.field]: false })}'>No</button>`;
    if (q.kind === "integer") return `<input type="number" class="q-input" style="width:90px" placeholder="months" /><button class="secondary" data-answer='{"__field":"${q.field}"}'>Save</button>`;
    if (q.kind === "integer_optional") return `<input type="number" class="q-input" style="width:110px" placeholder="USD" /><button class="secondary" data-answer='{"__field":"${q.field}","__usd":true}'>Save</button>`;
    if (q.kind === "offer_field" && q.field.endsWith("rate_lock")) return `<button class="secondary" data-supply="${q.offer_id}" data-field="rate_lock">Use: not locked, 45 days</button>`;
    return "";
  }

  async function answer(btn) {
    let facts = JSON.parse(btn.dataset.answer);
    if (facts.__field) {
      const input = btn.parentElement.querySelector(".q-input");
      let val = parseFloat(input.value);
      if (Number.isNaN(val)) return;
      if (facts.__usd) val = Math.round(val * 100);
      facts = { [facts.__field]: val };
    }
    try { await api(`/v1/loan-cases/${state.caseId}/facts`, { method: "POST", body: JSON.stringify(facts) }); await refreshCase(); } catch (e) { alert(e.message); }
  }

  async function supplyField(btn) {
    try {
      await api(`/v1/loan-cases/${state.caseId}/offers/${btn.dataset.supply}/fields`, { method: "POST", body: JSON.stringify({ field: "rate_lock", value: { locked: false, lock_period_days: 45, note: "Supplied by borrower from lender email" }, source: "borrower: lender email" }) });
      await refreshCase();
    } catch (e) { alert(e.message); }
  }

  async function toggleFinance(offerId, flag) {
    try {
      await api(`/v1/loan-cases/${state.caseId}/facts`, { method: "POST", body: JSON.stringify({ finance_costs: { [offerId]: flag } }) });
      await api(`/v1/loan-cases/${state.caseId}/compare`, { method: "POST", body: "{}" });
      await refreshCase();
    } catch (e) { alert(e.message); await refreshCase(); }
  }

  function renderComparison(v) {
    const c = v.comparison;
    if (!c) return "<p class='muted'>No comparison yet. Confirm the facts and ask the agent whether refinancing is worth it.</p>";
    const rows = [c.keep, ...c.offers];
    const best = c.recommendation.best_offer_id;
    const H = c.horizon_months;
    const html = [`<p><strong>${esc(c.recommendation.decision.replace("_", " "))}:</strong> ${esc(c.recommendation.reason)}</p>`,
      `<table><thead><tr><th>Scenario</th><th class="num">Monthly P&amp;I</th><th class="num">Δ monthly</th><th class="num">Upfront costs</th><th class="num">Financed</th><th class="num">Paid through m${H}</th><th class="num">Owed at m${H}</th><th class="num">Cost vs keep</th><th>Break-even</th><th>Cash to close</th><th>Finance costs?</th></tr></thead><tbody>`];
    rows.forEach((r) => {
      const cls = r.scenario_id === best ? "best" : (!r.rankable ? "unranked" : "");
      const diff = r.economic_difference_vs_keep_minor;
      const be = r.economic_break_even_month ? `month ${r.economic_break_even_month}${r.simple_break_even_months ? ` (simple ${r.simple_break_even_months} mo)` : ""}` : (r.scenario_id === "keep_current" ? "—" : "never");
      const ctc = r.cash_to_close && r.cash_to_close.total_minor !== undefined ? `${usd(r.cash_to_close.total_minor)}<div class="muted small">prepaids ${usd(r.cash_to_close.prepaids_minor)}, escrow ${usd(r.cash_to_close.escrow_minor)}${r.cash_to_close.exceeds_maximum ? " · exceeds max" : ""}</div>` : "—";
      const fin = r.scenario_id !== "keep_current" && r.rankable ? `<input type="checkbox" data-finance="${r.scenario_id}" ${(v.finance_costs || {})[r.scenario_id] ? "checked" : ""} ${v.status !== "awaiting_decision" ? "disabled" : ""}/>` : "";
      html.push(`<tr class="${cls}"><td>${esc(r.label)}${r.rankable ? "" : `<div class="muted small">not ranked: ${esc((r.missing_fields || []).concat(r.contradictions || []).join("; ") || (r.expired ? "expired" : ""))}</div>`}${(r.notes || []).map((n) => `<div class="muted small">${esc(n)}</div>`).join("")}</td>
        <td class="num">${r.rankable || r.scenario_id === "keep_current" ? usd(r.monthly_pi_minor) : "—"}</td>
        <td class="num ${r.monthly_change_vs_keep_minor < 0 ? "neg" : r.monthly_change_vs_keep_minor > 0 ? "pos" : ""}">${r.scenario_id === "keep_current" || !r.rankable ? "—" : usd(r.monthly_change_vs_keep_minor)}</td>
        <td class="num">${usd(r.upfront_incremental_costs_minor)}</td><td class="num">${usd(r.financed_costs_minor)}</td>
        <td class="num">${usd(r.cumulative_pi_at_horizon_minor)}</td><td class="num">${usd(r.remaining_balance_at_horizon_minor)}</td>
        <td class="num ${diff < 0 ? "neg" : diff > 0 ? "pos" : ""}">${r.scenario_id === "keep_current" ? "baseline" : (r.rankable ? usd(diff) : "—")}</td>
        <td>${be}</td><td>${ctc}</td><td>${fin}</td></tr>`);
    });
    html.push("</tbody></table>");
    html.push(`<details class="small"><summary>Assumptions and sensitivity</summary><ul>${c.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>${c.offers.filter((o) => o.rankable).map((o) => `<div><strong>${esc(o.label)}</strong> — cost vs keep by horizon: ${Object.entries(o.sensitivity.horizon_difference_minor || {}).map(([h, d]) => `m${h}: ${usd(d)}`).join(", ")}; fees ±$1,000: ${Object.entries(o.sensitivity.cost_shift_difference_minor || {}).map(([s, d]) => `${s > 0 ? "+" : ""}${usd(parseInt(s, 10))} → ${usd(d)}`).join(", ")}; financed vs cash: ${usd(o.sensitivity.financed_costs_difference_minor)} / ${usd(o.sensitivity.cash_costs_difference_minor)}</div>`).join("")}</details>`);
    html.push(`<p class="muted small">Calculation ${esc(c.calculation_version)} · computed ${esc(c.computed_at)} · authority: ${esc(c.authority)} · not a financing commitment.</p>`);
    return html.join("");
  }

  function renderPending(v) {
    const pend = v.pending_actions || [];
    if (!pend.length) return "";
    return pend.map((a) => {
      const r = a.review || {};
      const dest = r.destination || {};
      const canApprove = a.status === "proposed" && a.approval_challenge_id;
      return `<div class="review">
        <h4>${esc(r.title || a.action_type)} <span class="tag warn">${esc(a.status)}</span></h4>
        <dl class="kv">
          <dt>Destination</dt><dd>${esc(dest.lender_name || dest.lender_id || "—")} · environment <strong>${esc(dest.environment || "mock")}</strong></dd>
          ${r.terms ? `<dt>Terms</dt><dd>${pct(r.terms.note_rate_decimal)} for ${r.terms.term_months} months · principal ${usd(r.terms.principal_minor)} · P&amp;I ${usd(r.terms.monthly_pi_minor)} · financed ${usd(r.terms.financed_costs_minor)} · status ${esc(r.terms.status)}</dd>` : ""}
          ${r.final_terms ? `<dt>Final terms</dt><dd>${pct(r.final_terms.note_rate_decimal)} for ${r.final_terms.term_months} months · principal ${usd(r.final_terms.principal_minor)} · P&amp;I ${usd(r.final_terms.monthly_pi_minor)} · financed ${usd(r.final_terms.financed_costs_minor)} (${esc(r.final_terms.final_terms_id)})</dd>` : ""}
          ${r.differences_from_approved_offer ? `<dt>Changes</dt><dd>${(r.differences_from_approved_offer.differences || []).map((d) => `<div class="${d.material ? "diff-material" : ""}">${esc(d.field)}: ${esc(JSON.stringify(d.earlier))} → ${esc(JSON.stringify(d.final))}${d.material ? " (material)" : ""}</div>`).join("") || "none"}</dd>` : ""}
          ${r.terms_shared ? `<dt>Shared</dt><dd>your rate ${pct(r.terms_shared.your_current_rate)}${r.terms_shared.competing_offer ? `; competing offer ${esc(r.terms_shared.competing_offer.lender_name)} ${pct(r.terms_shared.competing_offer.note_rate_decimal)} / ${r.terms_shared.competing_offer.term_months} mo, costs ${usd(r.terms_shared.competing_offer.incremental_costs_net_minor)}` : ""}</dd>` : ""}
          <dt>Documents</dt><dd>${(r.documents_shared || []).map((d) => esc(d.id)).join(", ") || "none"}</dd>
          <dt>Effect</dt><dd>${esc(r.irreversible_effect || "")}</dd>
          ${r.does_not_authorize ? `<dt>Does not authorize</dt><dd>${r.does_not_authorize.map(esc).join(", ")}</dd>` : ""}
          <dt>Binding</dt><dd class="small">hash ${esc(a.action_payload_hash)} · case v${a.expected_case_version} · challenge ${esc(a.approval_challenge_id || "—")} expires ${esc(a.challenge_expires_at || "—")}</dd>
        </dl>
        ${r.message_preview ? `<pre>${esc(r.message_preview)}</pre>` : ""}
        <div class="row">${canApprove ? `<button data-approve="${a.action_id}">Approve exactly this</button><button class="danger" data-decline="${a.action_id}">Decline</button>` : `<span class="muted">${a.status === "approved" || a.status === "executing" ? "Approved; the background executor is performing it." : a.status === "uncertain" ? "Outcome unknown; being reconciled by original request reference." : ""}</span>`}</div>
      </div>`;
    }).join("");
  }

  async function approve(actionId) {
    const a = (state.view.pending_actions || []).find((x) => x.action_id === actionId);
    try {
      await api(`/v1/actions/${actionId}/approve`, { method: "POST", body: JSON.stringify({ expected_case_version: a.expected_case_version, action_payload_hash: a.action_payload_hash, approval_challenge_id: a.approval_challenge_id }) });
      await refreshCase();
      scheduleRefresh();
    } catch (e) { alert(e.message); await refreshCase(); }
  }
  async function decline(actionId) {
    try { await api(`/v1/actions/${actionId}/decline`, { method: "POST" }); await refreshCase(); } catch (e) { alert(e.message); }
  }

  function renderOffers(v) {
    const offers = (v.offers || []).slice().sort((a, b) => (a.lender_id + a.version).localeCompare(b.lender_id + b.version));
    if (!offers.length) return "<p class='muted'>No offers attached.</p>";
    return `<table><thead><tr><th>Lender</th><th>v</th><th>Status</th><th class="num">Rate</th><th class="num">Term</th><th class="num">Net costs</th><th>Expires</th></tr></thead><tbody>${offers.map((o) => {
      const n = o.normalized || {};
      const badge = o.status === "superseded" || o.status === "expired" ? "warn" : "";
      return `<tr><td>${esc(o.lender_name)}<div class="muted small">${esc(o.id)}${n.missing_fields && n.missing_fields.length ? ` · missing ${esc(n.missing_fields.join(", "))}` : ""}${n.contradictions && n.contradictions.length ? ` · <span class="pos">${esc(n.contradictions.join("; "))}</span>` : ""}</div></td><td>${o.version}</td><td><span class="tag ${badge}">${esc(o.status)}</span></td><td class="num">${pct(o.note_rate_decimal)}</td><td class="num">${o.term_months ?? "—"}</td><td class="num">${usd(n.incremental_costs_net_minor)}<div class="muted small">credits ${usd(n.lender_credits_minor || 0)}</div></td><td class="small">${o.expires_at ? o.expires_at.slice(0, 10) : "—"}</td></tr>`;
    }).join("")}</tbody></table>`;
  }

  function renderLenderRequests(v) {
    const lrs = v.lender_requests || [];
    if (!lrs.length) return "";
    return `<h4 class="muted">Lender requests</h4>${lrs.map((r) => `<div class="notice"><strong>${esc(r.lender_id)}</strong> · ${esc(r.status)}${r.external_request_ref ? ` · ref ${esc(r.external_request_ref)}` : ""}<br/>${r.response && r.response.outcome ? `Reply: <em>${esc(r.response.outcome)}</em> — ${esc(r.response.reason || "")}` : "Awaiting reply."}</div>`).join("")}`;
  }

  function renderApplications(v) {
    const apps = v.applications || [];
    if (!apps.length) return "<p class='muted'>No application drafted. Applying requires your approval and is a mock submission.</p>";
    const incomeDocs = (state.me.documents || []).filter((d) => ["income_evidence", "pay_stub", "w2", "tax_return"].includes(d.kind));
    return apps.map((a) => {
      const open = (a.conditions || []).filter((c) => c.status === "open");
      const ft = a.final_terms;
      return `<dl class="kv">
        <dt>Application</dt><dd>${esc(a.id)} · <span class="tag ${a.status === "declined" ? "bad" : ""}">${esc(a.status)}</span> · lender ${esc(a.lender_id)}</dd>
        <dt>Provider ref</dt><dd>${esc(a.external_application_ref || "—")} <span class="muted small">client ref ${esc(a.client_request_ref || "—")}</span></dd>
        <dt>Documents</dt><dd>${(a.document_manifest || []).map((d) => esc(d.id)).join(", ") || "none"}</dd>
        <dt>Conditions</dt><dd>${(a.conditions || []).map((c) => `<div>${c.status === "open" ? "⏳" : "✓"} ${esc(c.id)} — ${esc(c.description || "")}</div>`).join("") || "none"}</dd>
        ${ft ? `<dt>Final terms</dt><dd>${pct(ft.note_rate_decimal)} for ${ft.term_months} months · principal ${usd(ft.principal_minor)} · P&amp;I ${usd(ft.monthly_pi_minor)} · financed ${usd(ft.financed_costs_minor || 0)} · ${esc(ft.final_terms_id)} <span class="tag">approved offer, not funded</span></dd>` : ""}
        ${a.closing_evidence ? `<dt>Mock closing</dt><dd>${esc(a.closing_evidence.closing_record_id)} at ${esc(a.closing_evidence.closed_at)} · payoff record ${esc(a.closing_evidence.payoff_record_id)}<div class="muted small">${esc(a.closing_evidence.note)}</div></dd>` : ""}
      </dl>
      ${open.length && v.status === "conditions_outstanding" ? `<div class="docs"><strong>Release documents</strong>${incomeDocs.length ? incomeDocs.map((d) => `<label><input type="checkbox" class="rel-doc" value="${d.id}"/> ${d.id} (${d.kind})</label>`).join("") : "<p class='muted small'>No income evidence document on file. Add one via the API (see docs/demo.md).</p>"}<button data-release="${a.id}">Prepare release for approval</button></div>` : ""}
      ${v.status === "final_review" && ft ? `<button data-close="${a.id}">Review final terms and prepare closing request</button>` : ""}
      ${(v.term_reviews || []).filter((t) => t.application_id === a.id).map((t) => `<div class="notice"><strong>Final-term review</strong> (${t.reviewed_at}): ${esc(t.differences.summary)}${(t.differences.differences || []).map((d) => `<div class="${d.material ? "diff-material" : ""}">${esc(d.field)}: ${esc(JSON.stringify(d.earlier))} → ${esc(JSON.stringify(d.final))}</div>`).join("")}</div>`).join("")}`;
    }).join("<hr/>");
  }

  async function releaseDocs(appId) {
    const ids = $$(".rel-doc:checked").map((i) => i.value);
    if (!ids.length) { alert("Select at least one document."); return; }
    try { await api(`/v1/loan-cases/${state.caseId}/applications/${appId}/document-releases`, { method: "POST", body: JSON.stringify({ document_ids: ids }) }); await refreshCase(); } catch (e) { alert(e.message); }
  }
  async function requestClosing(appId) {
    try { await api(`/v1/loan-cases/${state.caseId}/applications/${appId}/closing-requests`, { method: "POST" }); await refreshCase(); } catch (e) { alert(e.message); }
  }

  async function chat(text) {
    try {
      await api(`/v1/loan-cases/${state.caseId}/messages`, { method: "POST", body: JSON.stringify({ message: text }) });
      await refreshCase();
    } catch (e) { alert(e.message); }
  }

  function scheduleRefresh() {
    clearTimeout(state.timer);
    let n = 0;
    const tick = async () => { await refreshCase(); if (++n < 6) state.timer = setTimeout(tick, 2500); };
    state.timer = setTimeout(tick, 2500);
  }

  // --------------------------------------------------------------- operator
  async function refreshOps() {
    try {
      const [metrics, cases, jobs, adapter, clock] = await Promise.all([
        api("/v1/ops/metrics", {}, true), api("/v1/ops/cases", {}, true), api("/v1/ops/jobs", {}, true), api("/v1/ops/adapter", {}, true), api("/v1/ops/clock", {}, true),
      ]);
      $("#clock").textContent = `fixture clock: ${clock.clock_now}`;
      $("#op-metrics").textContent = JSON.stringify(metrics, null, 2);
      $("#op-cases").innerHTML = `<table><thead><tr><th>Case</th><th>Tenant</th><th>State</th><th>v</th><th></th></tr></thead><tbody>${cases.cases.map((c) => `<tr><td>${esc(c.id)}</td><td>${esc(c.tenant_id)}</td><td><span class="tag state">${esc(c.status)}</span></td><td>${c.version}</td><td><button class="secondary" data-opcase="${c.id}">Inspect</button></td></tr>`).join("")}</tbody></table>`;
      $$("[data-opcase]").forEach((b) => b.addEventListener("click", async () => {
        const d = await api(`/v1/ops/cases/${b.dataset.opcase}`, {}, true);
        $("#op-case-detail").innerHTML = `<h4>${esc(d.id)} · ${esc(d.status)} v${d.version}</h4>
          <strong>Transitions</strong>${d.timeline.filter((e) => e.next_state).map((e) => `<div>#${e.sequence} ${esc(e.previous_state || "∅")} → ${esc(e.next_state)} by ${esc(e.actor)} (expected v${e.expected_case_version}) ${esc(e.occurred_at)}</div>`).join("")}
          <strong>Actions</strong>${d.actions.map((a) => `<div>${esc(a.id)} ${esc(a.type)} <span class="tag">${esc(a.status)}</span> ref ${esc(a.client_request_ref)} → provider ${esc(a.provider_reference || "—")}</div>`).join("") || "<div class='muted'>none</div>"}
          <strong>Tool runs</strong>${d.tool_runs.map((t) => `<div>${esc(t.at)} ${esc(t.tool)} [${esc(t.authority)}/${esc(t.source)}] ${t.latency_ms}ms ${esc(t.outcome)}</div>`).join("")}
          ${d.status === "manual_review" ? `<div class="row"><input id="op-resume-state" placeholder="next state" /><input id="op-resume-reason" placeholder="reason" /><button data-resume="${d.id}">Resume</button></div>` : ""}`;
        $$("[data-resume]").forEach((rb) => rb.addEventListener("click", async () => {
          try { await api(`/v1/ops/cases/${rb.dataset.resume}/resume`, { method: "POST", body: JSON.stringify({ next_state: $("#op-resume-state").value, reason: $("#op-resume-reason").value }) }, true); refreshOps(); } catch (e) { alert(e.message); }
        }));
      }));
      $("#op-jobs").innerHTML = `<table><thead><tr><th>Job</th><th>Type</th><th>Status</th><th>Run at</th><th>Att.</th></tr></thead><tbody>${jobs.jobs.slice(0, 30).map((j) => `<tr><td class="small">${esc(j.id)}</td><td>${esc(j.type)}</td><td><span class="tag ${j.status === "dead" ? "bad" : ""}">${esc(j.status)}</span>${j.last_error ? `<div class="small pos">${esc(j.last_error.slice(0, 120))}</div>` : ""}</td><td class="small">${esc(j.run_at)}</td><td>${j.attempts}</td></tr>`).join("")}</tbody></table>`;
      $("#op-adapter").innerHTML = `<p class="small muted">capabilities: ${esc(JSON.stringify(adapter.capabilities))}</p><p class="small">pending callbacks: ${adapter.pending_callbacks.length}; faults armed: ${adapter.faults.length}</p><table><thead><tr><th>At</th><th>Op</th><th>Lender</th><th>Request ref</th><th>Outcome</th></tr></thead><tbody>${adapter.requests.slice().reverse().slice(0, 40).map((r) => `<tr><td class="small">${esc(r.at)}</td><td>${esc(r.operation)}</td><td>${esc(r.lender_id)}</td><td class="small">${esc(r.request_ref)}</td><td>${esc(r.outcome)}</td></tr>`).join("")}</tbody></table>`;
    } catch (e) {
      $("#op-result").textContent = e.message;
    }
  }
  const opAction = async (fn) => { try { const r = await fn(); $("#op-result").textContent = JSON.stringify(r, null, 2).slice(0, 4000); } catch (e) { $("#op-result").textContent = e.message; } await refreshOps(); if (state.caseId) refreshCase(); };
  $("#op-run-once").addEventListener("click", () => opAction(() => api("/v1/ops/worker/run-once", { method: "POST" }, true)));
  $("#op-drain").addEventListener("click", () => opAction(() => api("/v1/ops/worker/drain", { method: "POST" }, true)));
  $("#op-callbacks").addEventListener("click", () => opAction(() => api("/v1/ops/mock/deliver-callbacks", { method: "POST" }, true)));
  $("#op-advance").addEventListener("click", () => opAction(() => api("/v1/ops/clock/advance", { method: "POST", body: JSON.stringify({ days: parseInt($("#op-days").value, 10) || 0 }) }, true)));
  $("#op-inject").addEventListener("click", () => opAction(() => api("/v1/ops/faults", { method: "POST", body: JSON.stringify({ kind: $("#op-fault").value }) }, true)));

  $("#token").addEventListener("change", () => { state.caseId = null; $("#case-panel").innerHTML = "<p class='muted'>Select or create a case.</p>"; loadMe(); });
  loadMe();
})();
