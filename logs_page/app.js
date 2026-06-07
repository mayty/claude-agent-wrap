// This file has been created with the assistance of an AI tool.
"use strict";
const $ = (id) => document.getElementById(id);
let state = { project: null, session: null, reqs: [], groups: null, tab: "main",
              poll: null, fp: null, gen: 0 };

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (isNaN(d)) return ts;
  return d.toLocaleString();
}

function fmtCost(c) {
  if (c == null) return "?";
  if (c < 0.01) return "$" + c.toFixed(4);
  return "$" + c.toFixed(2);
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

async function loadProjects() {
  const projects = await getJSON("/api/projects");
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
    list.appendChild(item);
  }
}

async function selectProject(p, item) {
  stopPolling();
  state.gen++; // discard any session load still in flight from the prior project
  state.project = p.id;
  state.session = null;
  document.querySelectorAll("#proj-list .item").forEach(e => e.classList.remove("active"));
  item.classList.add("active");
  $("chat").innerHTML = '<div class="hint">Loading sessions…</div>';
  const list = $("sess-list");
  list.innerHTML = "";
  try {
    const sessions = await getJSON(`/api/sessions?project=${p.id}`);
    if (!sessions.length) {
      list.appendChild(el("div", "empty", "No sessions."));
      $("chat").innerHTML = '<div class="hint">No sessions available.</div>';
      return;
    }
    for (const s of sessions) {
      const sessItem = el("div", "item"); // renamed to avoid shadowing
      const top = el("div", null);
      top.appendChild(el("span", "badge", s.provider.replace(/^litellm-/, "")));
      top.appendChild(document.createTextNode(s.alias || s.session_id.slice(0, 8)));
      sessItem.appendChild(top);
      const sub = s.alias ? `${s.session_id.slice(0, 8)} · ` : "";
      sessItem.appendChild(el("div", "meta",
        `${sub}${s.count} req · ${fmtTs(s.last_ts)}` + (s.models.length ? ` · ${s.models.join(", ")}` : "")));
      sessItem.title = s.session_id;
      sessItem.onclick = () => selectSession(s, sessItem);
      list.appendChild(sessItem);
    }
    $("chat").innerHTML = '<div class="hint">Select a session to view its requests.</div>';
  } catch (e) {
    list.appendChild(el("div", "empty", "Error loading sessions"));
    showError("chat", "Could not load sessions: " + e.message);
  }
}

// Query string identifying one session, shared by the /api/session and
// /api/session-stat calls.
function sessionQuery(s) {
  return `project=${state.project}&provider=${encodeURIComponent(s.provider)}` +
         `&session=${encodeURIComponent(s.session_id)}`;
}

async function selectSession(s, item) {
  stopPolling();
  // Bump the generation so a slower in-flight load for a previously-clicked
  // session can detect it has been superseded and discard its late response.
  const gen = ++state.gen;
  state.session = s.session_id;
  document.querySelectorAll("#sess-list .item").forEach(e => e.classList.remove("active"));
  item.classList.add("active");
  $("chat").innerHTML = '<div class="hint">Loading…</div>';
  try {
    const reqs = await getJSON(`/api/session?${sessionQuery(s)}`);
    if (gen !== state.gen) return; // another session was selected mid-fetch
    renderChat(reqs, s);
    // Seed the fingerprint from the state at fetch time, then poll for changes.
    try { state.fp = fpKey(await getJSON(`/api/session-stat?${sessionQuery(s)}`)); }
    catch (e) { state.fp = null; }
    if (gen !== state.gen) return;
    startPolling(s);
  } catch (e) {
    showError("chat", e.message);
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

// One poll: if the session the user opened is still open and its fingerprint
// changed, re-fetch and re-render in place, preserving scroll (auto-following
// only when the user was already at the bottom). Errors are swallowed so a
// transient failure doesn't kill the interval — the next tick retries.
async function tick(s) {
  if (state.session !== s.session_id) return;
  try {
    const fp = fpKey(await getJSON(`/api/session-stat?${sessionQuery(s)}`));
    if (fp === state.fp) return;
    const reqs = await getJSON(`/api/session?${sessionQuery(s)}`);
    if (state.session !== s.session_id) return; // user moved on during the fetch
    state.reqs = reqs;
    state.groups = groupBySubagent(reqs);
    state.fp = fp;
    const chat = $("chat");
    const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 40;
    const prevTop = chat.scrollTop;
    renderStream();
    if (atBottom) {
      // Glide down to the new content rather than snapping to it.
      chat.scrollTo({ top: chat.scrollHeight, behavior: "smooth" });
    } else {
      chat.scrollTop = prevTop; // hold the user's place instantly (no visible shift)
    }
  } catch (e) { /* transient; retry next tick */ }
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
  cap.appendChild(el("span", "when", fmtTs(r.ts)));
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

  const info = infoLine(r);
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

  if (info) {
    const row = el("div", "exchange-row");
    row.appendChild(info);
    row.appendChild(userBubble);
    turn.appendChild(row);
  } else {
    turn.appendChild(userBubble);
  }

  const respBubble = el("div", "bubble " + (r.error ? "error" : "assistant"));
  if (r.error) {
    respBubble.appendChild(Object.assign(el("pre"), { textContent: asText(r.error) }));
  } else {
    renderResponseInto(r.response, respBubble);
  }
  applySectionHeights(respBubble);
  decorateSections(respBubble);
  turn.appendChild(respBubble);

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
  return g.snippet ? `⌁ ${g.short} · "${g.snippet}…"` : `⌁ ${g.short}`;
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
    bar.appendChild(tab("sub:" + g.id, subLabel(g), g.items.length));
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
    pushMarker(g.firstIdx, marker(g, "started", state.reqs[g.firstIdx].ts));
    pushMarker(g.lastIdx, marker(g, "finished", state.reqs[g.lastIdx].ts));
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
  const chat = $("chat");
  chat.innerHTML = "";
  const s = state.session_meta;
  const label = s.alias ? `${s.alias} · ${s.session_id.slice(0, 8)}` : s.session_id;
  const head = el("h2", null, `${label} · ${state.reqs.length} request(s)`);
  const sticky = el("div", "chat-head");
  sticky.appendChild(head);
  if (!state.reqs.length) {
    chat.appendChild(sticky);
    chat.appendChild(el("div", "hint", "No requests in this session."));
    return;
  }
  const groups = state.groups;
  sticky.appendChild(renderTabs(groups));
  chat.appendChild(sticky);

  if (state.tab === "all") {
    state.reqs.forEach((r, i) => chat.appendChild(renderTurn(r, i + 1)));
  } else if (state.tab.startsWith("sub:")) {
    const id = state.tab.slice(4);
    const g = groups.subs.find(x => x.id === id);
    if (g) g.items.forEach((it, i) => chat.appendChild(renderTurn(it.r, i + 1)));
    else renderMainStream(chat, groups); // stale tab → fall back to main
  } else {
    renderMainStream(chat, groups);
  }
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
  const target = back ? back.querySelector(".modal-body") : $("chat");
  if (!target) return;
  e.preventDefault();
  target.scrollTo({ top: e.key === "Home" ? 0 : target.scrollHeight, behavior: "smooth" });
});

loadProjects().catch(e => {
  $("proj-list").innerHTML = "";
  $("proj-list").appendChild(el("div", "empty", "Error: " + e.message));
});
