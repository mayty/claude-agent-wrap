// This file has been created with the assistance of an AI tool.
"use strict";
const $ = (id) => document.getElementById(id);
let state = { project: null, session: null, reqs: [], groups: null, tab: "main" };

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

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
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
  state.project = p.id;
  state.session = null;
  document.querySelectorAll("#proj-list .item").forEach(e => e.classList.remove("active"));
  item.classList.add("active");
  $("chat").innerHTML = '<div class="hint">Select a session to view its requests.</div>';
  const sessions = await getJSON(`/api/sessions?project=${p.id}`);
  const list = $("sess-list");
  list.innerHTML = "";
  if (!sessions.length) {
    list.appendChild(el("div", "empty", "No sessions."));
    return;
  }
  for (const s of sessions) {
    const item = el("div", "item");
    const top = el("div", null);
    top.appendChild(el("span", "badge", s.provider.replace(/^litellm-/, "")));
    top.appendChild(document.createTextNode(s.alias || s.session_id.slice(0, 8)));
    item.appendChild(top);
    const sub = s.alias ? `${s.session_id.slice(0, 8)} · ` : "";
    item.appendChild(el("div", "meta",
      `${sub}${s.count} req · ${fmtTs(s.last_ts)}` + (s.models.length ? ` · ${s.models.join(", ")}` : "")));
    item.title = s.session_id;
    item.onclick = () => selectSession(s, item);
    list.appendChild(item);
  }
}

async function selectSession(s, item) {
  state.session = s.session_id;
  document.querySelectorAll("#sess-list .item").forEach(e => e.classList.remove("active"));
  item.classList.add("active");
  $("chat").innerHTML = '<div class="hint">Loading…</div>';
  const reqs = await getJSON(
    `/api/session?project=${state.project}&provider=${encodeURIComponent(s.provider)}&session=${encodeURIComponent(s.session_id)}`);
  renderChat(reqs, s);
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
  for (const block of content) {
    if (block == null) continue;
    if (typeof block === "string") {
      parent.appendChild(Object.assign(el("pre"), { textContent: block }));
      continue;
    }
    const type = block.type || "text";
    if (type === "text") {
      parent.appendChild(Object.assign(el("pre"), { textContent: asText(block.text) }));
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

function renderRequest(r, displayIdx) {
  const det = el("details", "req");
  const sum = el("summary");
  sum.appendChild(el("span", "idx", `#${displayIdx}`));
  sum.appendChild(el("span", null, (r.model || "").split("/").pop()));
  if (r.status && r.status !== "success") {
    sum.appendChild(el("span", "fail", `· ${r.status}`));
  }
  sum.appendChild(el("span", "when", fmtTs(r.ts)));
  det.appendChild(sum);

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
  det.appendChild(body);

  const u = usageLine(r.usage);
  if (u) det.appendChild(u);
  return det;
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
    if (!r.agent_id) chat.appendChild(renderRequest(r, ++shown));
  });
}

function renderStream() {
  const chat = $("chat");
  chat.innerHTML = "";
  const s = state.session_meta;
  const label = s.alias ? `${s.alias} · ${s.session_id.slice(0, 8)}` : s.session_id;
  const head = el("h2", null, `${label} · ${state.reqs.length} request(s)`);
  head.style.position = "static"; head.style.padding = "0 0 10px";
  chat.appendChild(head);
  if (!state.reqs.length) {
    chat.appendChild(el("div", "hint", "No requests in this session."));
    return;
  }
  const groups = state.groups;
  chat.appendChild(renderTabs(groups));

  if (state.tab === "all") {
    state.reqs.forEach((r, i) => chat.appendChild(renderRequest(r, i + 1)));
  } else if (state.tab.startsWith("sub:")) {
    const id = state.tab.slice(4);
    const g = groups.subs.find(x => x.id === id);
    if (g) g.items.forEach((it, i) => chat.appendChild(renderRequest(it.r, i + 1)));
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

loadProjects().catch(e => {
  $("proj-list").innerHTML = "";
  $("proj-list").appendChild(el("div", "empty", "Error: " + e.message));
});
