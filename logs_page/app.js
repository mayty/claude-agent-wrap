// This file has been edited with the assistance of an AI tool.
"use strict";
const $ = (id) => document.getElementById(id);
const chatBody = () => document.querySelector("#chat .chat-body");
const chatHead = () => document.querySelector("#chat .chat-head");
let state = { project: null, session: null, reqs: [], groups: null, tab: "main",
              poll: null, fp: null, gen: 0,
              listPoll: null, projectsFp: null, sessionsFp: null };

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function fmtTs(ts) {
  if (!ts) return "—";
  // Timestamps are Unix epoch *seconds* (floats) in the new record format;
  // Date() expects milliseconds, so scale numbers up. ISO strings (older
  // records / metadata) are still accepted as-is.
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (isNaN(d)) return ts;
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
                  "Jul","Aug","Sep","Oct","Nov","Dec"];
  const day = d.getDate();
  const mon = MONTHS[d.getMonth()];
  const year = d.getFullYear();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const datePart = year === new Date().getFullYear()
    ? `${day} ${mon}`
    : `${day} ${mon} ${year}`;
  return `${datePart}, ${hh}:${mm}:${ss}`;
}

// A record's start timestamp (epoch seconds) from its `timing` object, or null.
function recStart(r) {
  return r && r.timing ? r.timing.start : null;
}

// A record's full timing object ({ start, completionStart, end }), or null.
function recTiming(r) {
  return r && r.timing ? r.timing : null;
}

// Human-readable duration from a count of seconds: "X.Xs" under a minute, else
// "Xm Ys". Returns null for null/negative input so callers can skip the part.
function fmtDur(seconds) {
  if (seconds == null || seconds < 0) return null;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

function fmtCost(c) {
  if (c == null) return "?";
  if (c < 0.01) return "$" + c.toFixed(4);
  return "$" + c.toFixed(2);
}

function sessionStats(reqs) {
  let totalCost = 0;
  let cacheRead = 0;
  let totalInput = 0;
  let hasCost = false;

  for (const r of reqs) {
    if (r.cost != null) {
      totalCost += r.cost;
      hasCost = true;
    }
    const u = r.usage || {};
    const input = u.input_tokens || u.prompt_tokens || 0;
    const cr = u.cache_read_input_tokens || 0;
    totalInput += input;
    cacheRead += cr;
  }

  return {
    cost: hasCost ? totalCost : null,
    cacheRate: totalInput > 0 ? Math.round(100 * cacheRead / totalInput) : null,
  };
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function showError(containerId, message) {
  const container = $(containerId);
  const box = el("div", "err-box");
  box.appendChild(Object.assign(el("pre"), { textContent: "Error: " + message }));
  container.innerHTML = "";
  container.appendChild(box);
}

function renderProjectsList(projects) {
  const list = $("proj-list");
  list.innerHTML = "";
  if (!projects.length) {
    list.appendChild(el("div", "empty", "No projects with logs."));
    return;
  }
  for (const p of projects) {
    const item = el("div", "item");
    item.appendChild(el("div", null, p.name));
    const meta = el("div", "meta", `${p.sessions} session(s) · ${fmtTs(p.last_ts)}`);
    item.appendChild(meta);
    item.title = p.path;
    item.onclick = () => selectProject(p, item);
    if (state.project === p.id) item.classList.add("active");
    list.appendChild(item);
  }
}

async function loadProjects() {
  const projects = await getJSON("/api/projects");
  renderProjectsList(projects);
}

function renderSessionsList(sessions) {
  const list = $("sess-list");
  list.innerHTML = "";
  if (!sessions.length) {
    list.appendChild(el("div", "empty", "No sessions."));
    return;
  }
  for (const s of sessions) {
    const sessItem = el("div", "item");
    const top = el("div", null);
    for (const p of s.providers) {
      top.appendChild(el("span", "badge", p.replace(/^litellm-/, "")));
    }
    const name = s.alias || s.session_id.slice(0, 8);
    if (s.providers.length > 1) {
      sessItem.appendChild(top);
      sessItem.appendChild(el("div", "name", name));
    } else {
      top.appendChild(document.createTextNode(name));
      sessItem.appendChild(top);
    }
    const sub = s.alias ? `${s.session_id.slice(0, 8)} · ` : "";
    sessItem.appendChild(el("div", "meta",
      `${sub}${s.count} req · ${fmtTs(s.last_ts)}` + (s.models.length ? ` · ${s.models.join(", ")}` : "")));
    sessItem.title = s.session_id;
    sessItem.onclick = () => selectSession(s, sessItem);
    if (state.session === s.session_id) sessItem.classList.add("active");
    list.appendChild(sessItem);
  }
}

async function selectProject(p, item) {
  stopPolling();
  state.gen++; // discard any session load still in flight from the prior project
  state.project = p.id;
  state.session = null;
  state.sessionsFp = null;
  document.querySelectorAll("#proj-list .item").forEach(e => e.classList.remove("active"));
  item.classList.add("active");
  chatHead().innerHTML = "";
  chatBody().innerHTML = '<div class="hint">Loading sessions…</div>';
  try {
    const sessions = await getJSON(`/api/sessions?project=${p.id}`);
    renderSessionsList(sessions);
    if (!sessions.length) {
      chatBody().innerHTML = '<div class="hint">No sessions available.</div>';
      return;
    }
    chatBody().innerHTML = '<div class="hint">Select a session to view its requests.</div>';
  } catch (e) {
    const body = chatBody();
    body.innerHTML = "";
    const box = el("div", "err-box");
    box.appendChild(Object.assign(el("pre"), { textContent: "Error: Could not load sessions: " + e.message }));
    body.appendChild(box);
  }
}

// Query string identifying one session, shared by the /api/session and
// /api/session-stat calls.
function sessionQuery(s) {
  return `project=${state.project}&session=${encodeURIComponent(s.session_id)}`;
}

async function selectSession(s, item) {
  stopPolling();
  // Bump the generation so a slower in-flight load for a previously-clicked
  // session can detect it has been superseded and discard its late response.
  const gen = ++state.gen;
  state.session = s.session_id;
  document.querySelectorAll("#sess-list .item").forEach(e => e.classList.remove("active"));
  item.classList.add("active");
  chatHead().innerHTML = "";
  chatBody().innerHTML = '<div class="hint">Loading…</div>';
  try {
    const data = await getJSON(`/api/session?${sessionQuery(s)}`);
    if (gen !== state.gen) return; // another session was selected mid-fetch
    const reqs = data.reqs;
    const session_meta = data.session_meta || s;
    renderChat(reqs, session_meta);
    // Seed the fingerprint from the state at fetch time, then poll for changes.
    try { state.fp = fpKey(await getJSON(`/api/session-stat?${sessionQuery(s)}`)); }
    catch (e) { state.fp = null; }
    if (gen !== state.gen) return;
    startPolling(s);
  } catch (e) {
    const body = chatBody();
    body.innerHTML = "";
    const box = el("div", "err-box");
    box.appendChild(Object.assign(el("pre"), { textContent: "Error: " + e.message }));
    body.appendChild(box);
  }
}

// ---------------------------------------------------------------------------
// Live polling: refresh the open session as the agent appends new requests.
// ---------------------------------------------------------------------------

function fpKey(o) {
  return `${o && o.mtime}:${o && o.size}`;
}

function stopPolling() {
  if (state.poll) { clearInterval(state.poll); state.poll = null; }
}

function startPolling(s) {
  state.poll = setInterval(() => tick(s), 1000);
}

// Update a session list item's metadata in-place when new data arrives.
// `meta` is the session_meta object from /api/session — same shape as a
// list_sessions() entry — so we format it the same way selectProject() does.
function updateSessionListItem(meta) {
  if (!meta || !state.session) return;
  const item = document.querySelector(
    '#sess-list .item[title="' + CSS.escape(state.session) + '"]');
  if (!item) return;
  const metaEl = item.querySelector('.meta');
  if (!metaEl) return;

  const sub = meta.alias ? state.session.slice(0, 8) + ' · ' : '';
  metaEl.textContent =
    sub + meta.count + ' req · ' + fmtTs(meta.last_ts) +
    (meta.models.length ? ' · ' + meta.models.join(', ') : '');
}

// One poll: if the session the user opened is still open and its fingerprint
// changed, re-fetch and re-render in place, preserving scroll (auto-following
// only when the user was already at the bottom). Errors are swallowed so a
// transient failure doesn't kill the interval — the next tick retries.
async function tick(s) {
  if (state.session !== s.session_id) return;
  try {
    const fp = fpKey(await getJSON(`/api/session-stat?${sessionQuery(s)}`));
    if (fp === state.fp) return;
    const data = await getJSON(`/api/session?${sessionQuery(s)}`);
    if (state.session !== s.session_id) return; // user moved on during the fetch
    state.reqs = data.reqs;
    state.groups = groupBySubagent(data.reqs);
    updateSessionListItem(data.session_meta);
    state.fp = fp;
    const body = chatBody();
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    const prevTop = body.scrollTop;
    renderStream();
    if (atBottom) {
      // Glide down to the new content rather than snapping to it.
      body.scrollTo({ top: body.scrollHeight, behavior: "smooth" });
    } else {
      body.scrollTop = prevTop; // hold the user's place instantly (no visible shift)
    }
  } catch (e) { /* transient; retry next tick */ }
}

// ---------------------------------------------------------------------------
// List-level polling: refresh projects and sessions lists as the agent writes
// new records.  Runs independently from the per-session poll so both lists
// stay live even when no session is open.
// ---------------------------------------------------------------------------

function startListPolling() {
  state.listPoll = setInterval(listTick, 3000);
}

async function listTick() {
  try {
    const fp = fpKey(await getJSON("/api/projects-stat"));
    if (state.projectsFp === null) {
      state.projectsFp = fp; // seed on first tick, no re-fetch needed
    } else if (fp !== state.projectsFp) {
      state.projectsFp = fp;
      const projects = await getJSON("/api/projects");
      renderProjectsList(projects);
    }
  } catch (e) { /* transient */ }

  if (state.project == null) return;

  try {
    const fp = fpKey(await getJSON(`/api/sessions-stat?project=${state.project}`));
    if (state.sessionsFp === null) {
      state.sessionsFp = fp; // seed on first tick for this project
    } else if (fp !== state.sessionsFp) {
      state.sessionsFp = fp;
      const sessions = await getJSON(`/api/sessions?project=${state.project}`);
      renderSessionsList(sessions);
    }
  } catch (e) { /* transient */ }
}

function asText(v) {
  if (v == null) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v, null, 2);
}

function renderContent(content, parent) {
  if (typeof content === "string") {
    parent.appendChild(Object.assign(el("pre"), { textContent: content }));
    return;
  }
  if (!Array.isArray(content)) {
    parent.appendChild(Object.assign(el("pre"), { textContent: asText(content) }));
    return;
  }
  // With more than one block, give each text block its own box so adjacent
  // blocks (e.g. a system-reminder followed by the real message) are visually
  // distinguished — the same way tool_use/tool_result blocks already are.
  const multi = content.length > 1;
  const addText = (text) => {
    if (multi) {
      const box = el("div", "block-text");
      box.appendChild(Object.assign(el("pre"), { textContent: text }));
      parent.appendChild(box);
    } else {
      parent.appendChild(Object.assign(el("pre"), { textContent: text }));
    }
  };
  for (const block of content) {
    if (block == null) continue;
    if (typeof block === "string") {
      addText(block);
      continue;
    }
    const type = block.type || "text";
    if (type === "text") {
      addText(asText(block.text));
    } else if (type === "tool_use") {
      const box = el("div", "block-tool_use");
      box.appendChild(el("div", "block-label", `tool_use · ${block.name || ""}`));
      box.appendChild(Object.assign(el("pre"), { textContent: asText(block.input) }));
      parent.appendChild(box);
    } else if (type === "tool_result") {
      const box = el("div", "block-tool_result");
      box.appendChild(el("div", "block-label", "tool_result"));
      box.appendChild(Object.assign(el("pre"), { textContent: asText(block.content) }));
      parent.appendChild(box);
    } else {
      const box = el("div", "block-tool_use");
      box.appendChild(el("div", "block-label", type));
      box.appendChild(Object.assign(el("pre"), { textContent: asText(block) }));
      parent.appendChild(box);
    }
  }
}

// Copy `text` to the clipboard, flashing the button to confirm. Uses the async
// Clipboard API (available on the localhost secure context) with an
// execCommand fallback for older browsers.
function copyText(text, btn) {
  const done = () => {
    btn.classList.add("copied");
    btn.textContent = "✓";
    setTimeout(() => { btn.classList.remove("copied"); btn.textContent = "⧉"; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => {});
    return;
  }
  const ta = el("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { /* ignore */ }
  ta.remove();
}

// Attach a copy button to the top-right (header level) of `host`, copying the
// text of `pre`. The button stops click propagation so copying from a bubble
// does not also open the turn's modal.
function addCopyButton(host, pre) {
  host.classList.add("section");
  const btn = el("button", "copy-btn", "⧉");
  btn.title = "Copy to clipboard";
  btn.onclick = (e) => { e.stopPropagation(); copyText(pre.textContent, btn); };
  host.appendChild(btn);
}

// Give every section a copy button at its header level. A section is a block box
// (`.block-text`/`.block-tool_use`/`.block-tool_result`, which carry a label
// row) or a bare single-content `<pre>` (wrapped on the fly). Idempotent.
function decorateSections(container) {
  container
    .querySelectorAll(".block-text, .block-tool_use, .block-tool_result")
    .forEach((box) => {
      if (box.querySelector(":scope > .copy-btn")) return;
      const pre = box.querySelector("pre");
      if (pre) addCopyButton(box, pre);
    });
  container.querySelectorAll("pre").forEach((pre) => {
    if (pre.closest(".block-text, .block-tool_use, .block-tool_result, .section")) return;
    const wrap = el("div", "section");
    pre.replaceWith(wrap);
    wrap.appendChild(pre);
    addCopyButton(wrap, pre);
  });
}

function msgEl(role, content) {
  const m = el("div", `msg role-${role}`);
  m.appendChild(el("div", "role", role));
  renderContent(content, m);
  return m;
}

function renderResponse(resp, parent) {
  const m = el("div", "msg role-assistant");
  m.appendChild(el("div", "role", "response"));
  if (resp && resp.content) renderContent(resp.content, m);
  const calls = resp && resp.tool_calls;
  if (Array.isArray(calls)) {
    for (const c of calls) {
      const fn = (c && c.function) || {};
      const box = el("div", "block-tool_use");
      box.appendChild(el("div", "block-label", `tool_call · ${fn.name || ""}`));
      box.appendChild(Object.assign(el("pre"), { textContent: asText(fn.arguments) }));
      m.appendChild(box);
    }
  }
  if (!resp || (!resp.content && !(Array.isArray(calls) && calls.length))) {
    m.appendChild(el("div", "meta", "(empty response)"));
  }
  parent.appendChild(m);
}

function usageLine(u) {
  if (!u) return null;
  const parts = [];
  const add = (label, k) => { if (u[k]) parts.push(`${label}: ${u[k].toLocaleString()}`); };
  add("in", "prompt_tokens"); add("in", "input_tokens");
  add("out", "completion_tokens"); add("out", "output_tokens");
  add("cache write", "cache_creation_input_tokens");
  add("cache read", "cache_read_input_tokens");
  add("total", "total_tokens");
  if (!parts.length) return null;
  const line = el("div", "usage");
  for (const p of parts) line.appendChild(el("span", null, p));
  return line;
}

function infoLine(r) {
  const hasTokens = (r.context_tokens != null) || (r.output_tokens != null);
  const hasCost = r.cost != null;
  if (!hasTokens && !hasCost) return null;

  const parts = [];

  if (r.context_tokens != null && r.context_tokens > 0) {
    let ctx = "Context: " + r.context_tokens.toLocaleString();
    if (r.cache_percent != null) {
      ctx += " (" + r.cache_percent + "% cached)";
    }
    parts.push(ctx);
  }

  if (r.output_tokens != null) {
    parts.push("Output: " + r.output_tokens.toLocaleString());
  }

  if (r.cost != null) {
    parts.push("Cost: " + fmtCost(r.cost));
  } else if (hasTokens) {
    parts.push("Cost: ?");
  }

  if (!parts.length) return null;

  const line = el("div", "info-line");
  line.textContent = parts.join(" · ");
  return line;
}

// Response-side timing line shown above the response bubble: the end timestamp plus
// the time-to-first-token (completionStart − start) and generation time
// (end − completionStart). Each part appears only when its inputs are present, so
// failures / non-streaming calls (completionStart === null) and old records with
// no timing object degrade gracefully. Returns null when nothing is available.
function respTimingLine(r) {
  const t = recTiming(r);
  if (!t) return null;

  const parts = [];
  if (t.end != null) parts.push(fmtTs(t.end));
  if (t.start != null && t.completionStart != null) {
    const ttft = fmtDur(t.completionStart - t.start);
    if (ttft) parts.push("TTFT " + ttft);
  }
  if (t.completionStart != null && t.end != null) {
    const gen = fmtDur(t.end - t.completionStart);
    if (gen) parts.push("gen " + gen);
  }
  if (!parts.length) return null;

  const line = el("div", "resp-timing");
  line.textContent = parts.join(" · ");
  return line;
}

// The full detail body for one record: error box, system prompt, tool
// definitions, the complete message thread, and the response. Shown in the
// modal opened from a turn.
function renderFullDetail(r) {
  const body = el("div", "body");
  if (r.error) {
    const e = el("div", "err-box");
    e.appendChild(Object.assign(el("pre"), { textContent: asText(r.error) }));
    body.appendChild(e);
  }
  if (r.system) body.appendChild(msgEl("system", r.system));
  if (Array.isArray(r.tools) && r.tools.length) {
    const td = el("details", "toolsdef");
    td.appendChild(el("summary", null, `${r.tools.length} tool definition(s)`));
    td.appendChild(Object.assign(el("pre"), { textContent: asText(r.tools) }));
    body.appendChild(td);
  }
  for (const m of (r.messages || [])) {
    body.appendChild(msgEl(m.role || "user", m.content));
  }
  renderResponse(r.response, body);
  return body;
}

// A short "#N · model · status · ts" caption line shared by a turn and its modal.
function captionEl(r, displayIdx) {
  const cap = el("div", "caption");
  cap.appendChild(el("span", "idx", `#${displayIdx}`));
  cap.appendChild(el("span", null, (r.model || "").split("/").pop()));
  if (r.status && r.status !== "success") {
    cap.appendChild(el("span", "fail", `· ${r.status}`));
  }
  cap.appendChild(el("span", "when", fmtTs(recStart(r))));
  return cap;
}

// The last message with role "user" (messages with no role default to user),
// skipping any trailing non-user messages such as system reminders.
function lastUserMessage(msgs) {
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m && (m.role || "user") === "user") return m;
  }
  return null;
}

// Base per-section scroll height (matches `.bubble pre` in styles.css).
const SECTION_BASE = 240;

// Cap each section's height so the bubble's total stays near 1.5×SECTION_BASE:
// split that budget across the sections, with a floor of SECTION_BASE/3 so a
// many-section bubble's sections stay individually usable.
function applySectionHeights(bubble) {
  const pres = bubble.querySelectorAll("pre");
  if (!pres.length) return;
  const per = Math.max(SECTION_BASE / 3, (SECTION_BASE * 1.5) / pres.length);
  pres.forEach((p) => { p.style.maxHeight = `${Math.round(per)}px`; });
}

// One turn: the latest user message as a right-aligned bubble and the response
// (or error) as a left-aligned bubble below it. Clicking opens the full detail.
function renderTurn(r, displayIdx) {
  const turn = el("div", "turn");
  turn.appendChild(captionEl(r, displayIdx));

  const userBubble = el("div", "bubble user");
  // The latest *user* message — not simply the last message, which may be a
  // trailing system-reminder appended after the user's tool_result.
  const last = lastUserMessage(r.messages || []);
  if (last) {
    renderContent(last.content, userBubble);
  } else {
    userBubble.appendChild(el("div", "meta", "(no user message)"));
  }
  applySectionHeights(userBubble);
  decorateSections(userBubble);
  turn.appendChild(userBubble);

  const respBubble = el("div", "bubble " + (r.error ? "error" : "assistant"));
  if (r.error) {
    respBubble.appendChild(Object.assign(el("pre"), { textContent: asText(r.error) }));
  } else {
    renderResponseInto(r.response, respBubble);
  }
  applySectionHeights(respBubble);
  decorateSections(respBubble);
  // The timing line sits above the response bubble; the context/output/cost
  // info line sits below it. Both are left-aligned.
  const rt = respTimingLine(r);
  if (rt) turn.appendChild(rt);
  turn.appendChild(respBubble);
  const info = infoLine(r);
  if (info) turn.appendChild(info);

  turn.onclick = () => openModal(r, displayIdx);
  return turn;
}

// Like renderResponse but appends content/tool_calls straight into `parent`
// without wrapping in a `.msg` block (the bubble is the wrapper here).
function renderResponseInto(resp, parent) {
  if (resp && resp.content) renderContent(resp.content, parent);
  const calls = resp && resp.tool_calls;
  if (Array.isArray(calls)) {
    for (const c of calls) {
      const fn = (c && c.function) || {};
      const box = el("div", "block-tool_use");
      box.appendChild(el("div", "block-label", `tool_call · ${fn.name || ""}`));
      box.appendChild(Object.assign(el("pre"), { textContent: asText(fn.arguments) }));
      parent.appendChild(box);
    }
  }
  if (!resp || (!resp.content && !(Array.isArray(calls) && calls.length))) {
    parent.appendChild(el("div", "meta", "(empty response)"));
  }
}

// ---------------------------------------------------------------------------
// Modal: the full detail for one turn.
// ---------------------------------------------------------------------------

function closeModal() {
  const back = $("modal-backdrop");
  if (back) back.remove();
  document.removeEventListener("keydown", onModalKey);
}

function onModalKey(e) {
  if (e.key === "Escape") closeModal();
}

function openModal(r, displayIdx) {
  closeModal();
  const back = el("div", "modal-backdrop");
  back.id = "modal-backdrop";
  back.onclick = (e) => { if (e.target === back) closeModal(); };

  const modal = el("div", "modal");
  const head = el("div", "modal-head");
  head.appendChild(captionEl(r, displayIdx));
  const close = el("button", "modal-close", "✕");
  close.onclick = closeModal;
  head.appendChild(close);
  modal.appendChild(head);

  // The header stays fixed; only this body scrolls, so its scrollbar starts
  // below the header rather than spanning the whole panel.
  const mbody = el("div", "modal-body");

  const info = infoLine(r);
  if (info) mbody.appendChild(info);

  const detail = renderFullDetail(r);
  decorateSections(detail);
  mbody.appendChild(detail);
  const u = usageLine(r.usage);
  if (u) mbody.appendChild(u);
  modal.appendChild(mbody);

  back.appendChild(modal);
  document.body.appendChild(back);
  document.addEventListener("keydown", onModalKey);
}

// Plain text of a record's first user message, with leading <system-reminder>
// blocks stripped, for use as a subagent's human-readable label.
function firstPromptSnippet(r) {
  const msgs = r.messages || [];
  if (!msgs.length) return "";
  const c = msgs[0].content;
  let text = "";
  if (typeof c === "string") {
    text = c;
  } else if (Array.isArray(c)) {
    text = c.map(b => (typeof b === "string" ? b : (b && b.type === "text" ? b.text : "")))
            .filter(t => typeof t === "string").join("\n");
  }
  text = text.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "").trim();
  text = text.replace(/\s+/g, " ");
  return text.slice(0, 60);
}

// Does a record look like a subagent's final turn (a text answer, no tool call)?
function looksTerminal(r) {
  const resp = r.response || {};
  const calls = resp.tool_calls;
  if (Array.isArray(calls) && calls.length) return false;
  return !!resp.content;
}

// Partition the time-ordered records into the main stream and per-agent-id
// subagent streams (ordered by first appearance).
function groupBySubagent(reqs) {
  const main = [];
  const subById = new Map();
  reqs.forEach((r, i) => {
    const id = r.agent_id;
    if (!id) { main.push({ r, i }); return; }
    let g = subById.get(id);
    if (!g) {
      g = { id, ordinal: subById.size + 1, short: id.slice(0, 7),
            snippet: firstPromptSnippet(r), firstIdx: i, lastIdx: i,
            lastTerminal: false, items: [] };
      subById.set(id, g);
    }
    g.items.push({ r, i });
    g.lastIdx = i;
    g.lastTerminal = looksTerminal(r);
  });
  return { main, subs: [...subById.values()] };
}

function subLabel(g) {
  const cnt = `(${g.items.length})`;
  return g.snippet ? `⌁ ${g.short} ${cnt} · "${g.snippet}…"` : `⌁ ${g.short} ${cnt}`;
}

function renderTabs(groups) {
  const total = state.reqs.length;
  const bar = el("div", "tabs");
  const tab = (key, label, n) => {
    const t = el("div", "tab" + (state.tab === key ? " active" : ""));
    t.appendChild(el("span", null, label));
    if (n != null) t.appendChild(el("span", "n", `(${n})`));
    t.onclick = () => { state.tab = key; renderStream(); };
    return t;
  };
  bar.appendChild(tab("main", "Main agent", groups.main.length));
  for (const g of groups.subs) {
    bar.appendChild(tab("sub:" + g.id, subLabel(g)));
  }
  bar.appendChild(tab("all", "All", total));
  return bar;
}

// Render the main stream with clickable "started"/"finished" markers spliced
// into chronological position for each subagent.
function renderMainStream(chat, groups) {
  const markers = new Map(); // original record index -> [marker elements]
  const pushMarker = (idx, m) => {
    if (!markers.has(idx)) markers.set(idx, []);
    markers.get(idx).push(m);
  };
  const marker = (g, kind, ts) => {
    const done = kind !== "started";
    const verb = kind === "started" ? "started"
               : (g.lastTerminal ? "finished" : "last seen");
    const m = el("div", "marker" + (done ? " done" : ""));
    m.appendChild(el("span", null, `⌁ Subagent ${g.ordinal} (${g.short}) ${verb}`));
    m.appendChild(el("span", "when", fmtTs(ts)));
    m.onclick = () => { state.tab = "sub:" + g.id; renderStream(); };
    return m;
  };
  for (const g of groups.subs) {
    pushMarker(g.firstIdx, marker(g, "started", recStart(state.reqs[g.firstIdx])));
    pushMarker(g.lastIdx, marker(g, "finished", recStart(state.reqs[g.lastIdx])));
  }
  // Walk the global record order so markers land relative to main records.
  let shown = 0;
  state.reqs.forEach((r, i) => {
    const ms = markers.get(i);
    if (ms) for (const m of ms) chat.appendChild(m);
    if (!r.agent_id) chat.appendChild(renderTurn(r, ++shown));
  });
}

function renderStream() {
  const head = document.querySelector("#chat .chat-head");
  const body = chatBody();
  head.innerHTML = "";
  body.innerHTML = "";
  const s = state.session_meta;
  const hash = s.session_id.slice(0, 8);
  const stats = sessionStats(state.reqs);

  // Line 1: title — prefer the parsed title, fall back to alias or hash
  const title = s.title || s.alias || hash;
  head.appendChild(el("h2", "chat-title", title));

  // Line 2: alias · hash · reqs · cache · cost
  {
    const parts = [];
    if (s.alias) parts.push(s.alias);
    parts.push(hash);
    parts.push(state.reqs.length + " req");
    if (stats.cacheRate != null) parts.push(stats.cacheRate + "% cached");
    parts.push("Cost: " + fmtCost(stats.cost));
    const statsEl = el("div", "chat-stats");
    statsEl.textContent = parts.join(" · ");
    head.appendChild(statsEl);
  }

  if (!state.reqs.length) {
    body.appendChild(el("div", "hint", "No requests in this session."));
    ensureScrollButton();
    return;
  }

  // Line 3: agents bar
  const groups = state.groups;
  head.appendChild(renderTabs(groups));

  if (state.tab === "all") {
    state.reqs.forEach((r, i) => body.appendChild(renderTurn(r, i + 1)));
  } else if (state.tab.startsWith("sub:")) {
    const id = state.tab.slice(4);
    const g = groups.subs.find(x => x.id === id);
    if (g) g.items.forEach((it, i) => body.appendChild(renderTurn(it.r, i + 1)));
    else renderMainStream(body, groups); // stale tab → fall back to main
  } else {
    renderMainStream(body, groups);
  }
  ensureScrollButton();
}

function renderChat(reqs, s) {
  state.reqs = reqs;
  state.session_meta = s;
  state.groups = groupBySubagent(reqs);
  state.tab = "main";
  renderStream();
}

// Home/End scroll the request pop-up when it is open, otherwise the session
// chat. Plain keypress only — leave modified combos (e.g. Ctrl+Home) alone.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Home" && e.key !== "End") return;
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
  const back = $("modal-backdrop");
  const target = back ? back.querySelector(".modal-body") : chatBody();
  if (!target) return;
  e.preventDefault();
  target.scrollTo({ top: e.key === "Home" ? 0 : target.scrollHeight, behavior: "smooth" });
});

// ---------------------------------------------------------------------------
// Scroll-to-bottom button — appears when the chat column isn't scrolled to the
// very bottom, giving the user a one-click way to jump back to the newest turn.
// ---------------------------------------------------------------------------

function updateScrollButtons() {
  const body = chatBody();
  const atTop = body.scrollTop < 40;
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  const toTop = body.querySelector(".scroll-to-top-btn");
  const toBot = body.querySelector(".scroll-to-bottom-btn");
  if (toTop) toTop.classList.toggle("visible", !atTop);
  if (toBot) toBot.classList.toggle("visible", !atBottom);
}

function ensureScrollButton() {
  const body = chatBody();
  if (body.querySelector(".scroll-btn-wrap-bot")) return;

  const wrapTop = el("div", "scroll-btn-wrap-top");
  const btnTop = el("button", "scroll-btn scroll-to-top-btn");
  btnTop.title = "Scroll to top";
  btnTop.onclick = () => {
    body.scrollTo({ top: 0, behavior: "smooth" });
  };
  wrapTop.appendChild(btnTop);
  body.insertBefore(wrapTop, body.firstChild);

  const wrapBot = el("div", "scroll-btn-wrap-bot");
  const btnBot = el("button", "scroll-btn scroll-to-bottom-btn");
  btnBot.title = "Scroll to bottom";
  btnBot.onclick = () => {
    body.scrollTo({ top: body.scrollHeight, behavior: "smooth" });
  };
  wrapBot.appendChild(btnBot);
  body.appendChild(wrapBot);

  updateScrollButtons();
}

chatBody().addEventListener("scroll", updateScrollButtons);

loadProjects().catch(e => {
  $("proj-list").innerHTML = "";
  $("proj-list").appendChild(el("div", "empty", "Error: " + e.message));
}).finally(() => {
  startListPolling();
});
