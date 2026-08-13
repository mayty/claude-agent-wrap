// This file has been edited with the assistance of an AI tool.
"use strict";
const $ = (id) => document.getElementById(id);
const chatBody = () => document.querySelector("#chat .chat-body");
const chatHead = () => document.querySelector("#chat .chat-head");
let state = { project: null, session: null, reqs: [], groups: null, tab: "main",
              poll: null, fp: null, gen: 0,
              listPoll: null, projectsFp: null, sessionsFp: null,
              rawReqs: [], pendingHashes: null };

// Stand-in text for a hash:<sha256> pointer whose original string has not been
// fetched yet. replaceLoadingPlaceholders() swaps it for a spinner on an exact
// match, so anything rendering it must keep it alone in its own text node.
const LOADING_PLACEHOLDER = "➳ Loading…";

function hasPendingHashes() {
  return state.pendingHashes !== null && state.pendingHashes.size > 0;
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// Read an NDJSON response stream, returning {meta, records}.
// When *onRecord* is provided it is called for each record as it arrives
// (before the record is appended to the returned array), enabling
// progressive rendering.  When *onMeta* is provided it is called as soon
// as the session_meta line is parsed, so the caller can render the header
// before any records arrive.
async function readNDJSONStream(response, onRecord, onMeta) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let meta = null;
  const records = [];

  function consumeLine(raw) {
    let item;
    try { item = JSON.parse(raw); } catch (e) { return; }
    if (item.__type__ === "session_meta") {
      meta = item;
      if (onMeta) onMeta(item);
    } else {
      records.push(item);
      if (onRecord) onRecord(item);
    }
  }

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      if (buffer.trim()) consumeLine(buffer.trim());
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, nl);
      buffer = buffer.slice(nl + 1);
      if (line.trim()) consumeLine(line.trim());
    }
  }
  return { meta, records };
}

// Parse raw strings.jsonl content into a {hash: original} lookup dict.
// Each line is a JSON object {"hash": "...", "original": "..."}.
// Malformed lines are silently skipped.
function parseStrings(text) {
  const strings = {};
  if (!text) return strings;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      const entry = JSON.parse(line);
      if (entry.hash && entry.original !== undefined) {
        strings[entry.hash] = entry.original;
      }
    } catch (e) { /* skip corrupt lines */ }
  }
  return strings;
}

// Recursively resolve hash:<sha256> references in a record's tree, using the
// strings table from /api/strings.  Returns a new object tree (does not
// mutate the input).  When *strings* is empty, returns the input unchanged.
function resolveRecord(r, strings) {
  if (!strings || Object.keys(strings).length === 0) return r;

  function walk(v) {
    if (typeof v === "string") {
      const resolved = strings[v];
      if (resolved !== undefined) return resolved;
      // Detect unresolved hash references (format: "hash:" + 64 hex chars).
      if (/^hash:[a-f0-9]{64}$/.test(v)) {
        if (state.pendingHashes === null) state.pendingHashes = new Set();
        state.pendingHashes.add(v);
        return LOADING_PLACEHOLDER;
      }
      return v;
    }
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") {
      const out = {};
      for (const [k, val] of Object.entries(v)) {
        out[k] = walk(val);
      }
      return out;
    }
    return v;
  }

  return walk(r);
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

// Positions (within `s`) of every separator matching [.\-:].
function separatorOffsets(s) {
  const offsets = [];
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "." || s[i] === "-" || s[i] === ":") offsets.push(i);
  }
  return offsets;
}

// Render a session's model list, collapsing to at most 3 shared-prefix groups
// (cut on [.\-:]) when there are more than 3 distinct models. Returns the
// display string; callers should also set a title with the full list when
// `models.length > 3` so the raw ids stay discoverable on hover.
function collapseModels(models) {
  if (models.length <= 3) return models.join(", ");

  const offsets = models.map(separatorOffsets);
  const maxDepth = Math.max(...offsets.map((o) => o.length));

  let groups = null;
  for (let depth = maxDepth; depth >= 0; depth--) {
    const prefixes = models.map((m, i) => {
      const o = offsets[i];
      return depth < o.length ? m.slice(0, o[depth]) : m;
    });
    const order = [];
    const counts = new Map();
    for (const p of prefixes) {
      if (!counts.has(p)) order.push(p);
      counts.set(p, (counts.get(p) || 0) + 1);
    }
    if (order.length <= 3 || depth === 0) {
      groups = order.map((p) => ({ prefix: p, count: counts.get(p) }));
      break;
    }
  }

  return groups
    .map((g) => (g.count > 1 ? `${g.prefix}… (${g.count})` : g.prefix))
    .join(", ");
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

// Replace "➳ Loading…" text nodes in `container` with spinner elements.
// Uses exact-match so JSON-stringified tool inputs (where the placeholder is
// embedded in a larger string) are left alone.
function replaceLoadingPlaceholders(container) {
  const placeholder = LOADING_PLACEHOLDER;
  const walker = document.createTreeWalker(
    container,
    NodeFilter.SHOW_TEXT,
    null,
    false
  );
  const textNodes = [];
  while (walker.nextNode()) {
    if (walker.currentNode.nodeValue.trim() === placeholder) {
      textNodes.push(walker.currentNode);
    }
  }
  for (const node of textNodes) {
    const span = document.createElement("span");
    span.className = "loading-hash";
    span.innerHTML = '<span class="spinner-icon"></span> Loading…';
    node.parentNode.replaceChild(span, node);
  }
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
    const metaEl = el("div", "meta",
      `${sub}${s.count} req · ${fmtTs(s.last_ts)}` + (s.models.length ? ` · ${collapseModels(s.models)}` : ""));
    if (s.models.length > 3) metaEl.title = s.models.join("\n");
    sessItem.appendChild(metaEl);
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
  state.reqs = [];
  state.rawReqs = [];
  state.pendingHashes = null;
  state.groups = null;
  document.querySelectorAll("#sess-list .item").forEach(e => e.classList.remove("active"));
  item.classList.add("active");
  chatHead().innerHTML = "";
  chatBody().innerHTML = '<div class="hint">Loading…</div>';
  try {
    // Step 1: fetch strings first (unchanged)
    const stringsText = await (await fetch(`/api/strings?${sessionQuery(s)}`)).text();
    const strings = parseStrings(stringsText);
    if (gen !== state.gen) return;

    // Step 2: stream session as NDJSON, rendering turns as they arrive
    const response = await fetch(`/api/session?${sessionQuery(s)}`);
    if (!response.ok) throw new Error(await response.text());

    const body = chatBody();
    body.innerHTML = "";
    let displayIdx = 0;
    const reqs = [];
    const rawReqs = [];

    const { meta } = await readNDJSONStream(response, (record) => {
      if (gen !== state.gen) return;
      rawReqs.push(record);
      const resolved = resolveRecord(record, strings);
      reqs.push(resolved);
      state.rawReqs = rawReqs;
      state.reqs = reqs;
      body.appendChild(renderTurn(resolved, ++displayIdx));
      renderChatHead();
    }, (metaItem) => {
      if (gen !== state.gen) return;
      state.session_meta = metaItem;
      renderChatHead();
    });

    if (gen !== state.gen) return;

    // Step 3: finalize — tabs replace spinner, body stays intact
    const session_meta = meta || s;
    state.reqs = reqs;
    state.rawReqs = rawReqs;
    state.session_meta = session_meta;
    state.groups = groupBySubagent(reqs, buildSubagentCallMap(reqs));
    insertMarkers(state.groups);
    applyTabFilter("main");
    renderChatHead();
    ensureScrollButton();

    // Step 4: seed fingerprint and start polling
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
    (meta.models.length ? ' · ' + collapseModels(meta.models) : '');
  metaEl.title = meta.models.length > 3 ? meta.models.join('\n') : '';
}

// One poll: if the session the user opened is still open and its fingerprint
// changed, re-fetch and re-render in place, preserving scroll (auto-following
// only when the user was already at the bottom). Errors are swallowed so a
// transient failure doesn't kill the interval — the next tick retries.
async function tick(s) {
  if (state.session !== s.session_id) return;
  if (state.tickInFlight) return;
  state.tickInFlight = true;
  try {
    const fp = fpKey(await getJSON(`/api/session-stat?${sessionQuery(s)}`));
    if (fp === state.fp && !hasPendingHashes()) return;
    const stringsText = await (await fetch(`/api/strings?${sessionQuery(s)}`)).text();
    const fromIndex = state.reqs.length;
    const response = await fetch(`/api/session?${sessionQuery(s)}&from=${fromIndex}`);
    if (!response.ok) return;
    const { meta, records } = await readNDJSONStream(response);
    if (state.session !== s.session_id) return; // user moved on during the fetch
    const strings = parseStrings(stringsText);

    // fp unchanged + pending hashes: try to resolve without fetching session data.
    if (fp === state.fp && hasPendingHashes()) {
      let anyResolved = false;
      for (const h of state.pendingHashes) {
        if (strings[h] !== undefined) { anyResolved = true; break; }
      }
      if (anyResolved) {
        state.pendingHashes = new Set();  // clear; resolveRecord repopulates
        state.reqs = state.rawReqs.map(r => resolveRecord(r, strings));
        if (!hasPendingHashes()) state.pendingHashes = null;
        const scrollTop = chatBody().scrollTop;
        renderStream();
        requestAnimationFrame(() => { chatBody().scrollTop = scrollTop; });
      }
      state.fp = fp;
      renderChatHead();
      return;
    }

    for (const r of records) state.rawReqs.push(r);
    const resolved = records.map(r => resolveRecord(r, strings));

    // If no new records arrived, just refresh metadata and fingerprint.
    if (resolved.length === 0) {
      state.session_meta = meta || state.session_meta;
      state.fp = fp;
      updateSessionListItem(meta);
      renderChatHead();
      return;
    }

    // If the total count is less than what we already have, records were
    // deleted or the session was rebuilt — do a full re-fetch and rebuild.
    if (meta && meta.count < state.reqs.length) {
      const scrollTop = chatBody().scrollTop;
      const fullResp = await fetch(`/api/session?${sessionQuery(s)}`);
      if (!fullResp.ok) return;
      const full = await readNDJSONStream(fullResp);
      if (state.session !== s.session_id) return;
      const fullResolved = full.records.map(r => resolveRecord(r, strings));
      state.rawReqs = full.records;
      state.reqs = fullResolved;
      state.pendingHashes = null;
      state.groups = groupBySubagent(state.reqs, buildSubagentCallMap(state.reqs));
      state.session_meta = full.meta || state.session_meta;
      state.fp = fp;
      updateSessionListItem(full.meta);
      renderStream();
      requestAnimationFrame(() => { chatBody().scrollTop = scrollTop; });
      return;
    }

    // Normal append path: only new records were returned.
    const body = chatBody();
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    // Insert before the sticky scroll-to-bottom wrapper (rather than
    // appending after it) so it stays the last element in flow — position:
    // sticky only tracks the viewport bottom while it has no later siblings.
    const wrapBot = body.querySelector(".scroll-btn-wrap-bot");
    for (const r of resolved) {
      state.reqs.push(r);
      const turnEl = renderTurn(r, state.reqs.length);
      if (wrapBot) body.insertBefore(turnEl, wrapBot);
      else body.appendChild(turnEl);
    }

    state.groups = groupBySubagent(state.reqs, buildSubagentCallMap(state.reqs));
    state.session_meta = meta || state.session_meta;
    updateSessionListItem(meta);
    state.fp = fp;

    // Rebuild markers and re-apply filter for the current tab.
    removeMarkers();
    insertMarkers(state.groups);
    applyTabFilter(state.tab);
    renderChatHead();

    if (atBottom) {
      scrollToBottom();
    }
  } catch (e) { /* transient; retry next tick */ }
  finally { state.tickInFlight = false; }
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
  if (state.listTickInFlight) return;
  state.listTickInFlight = true;
  try {
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
  } finally { state.listTickInFlight = false; }
}

function asText(v) {
  if (v == null) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v, null, 2);
}

// Pretty-print a tool input value for display. If it's valid JSON that is NOT a
// single-key-string-value object, pretty-print it with "description" sorted first.
// Otherwise return the value as-is (compact single-line for simple shapes, or the
// original text for unparseable strings).
function prettyToolInput(v) {
  let obj;
  if (typeof v === "string") {
    try { obj = JSON.parse(v); } catch (e) { return v; }
  } else if (v != null && typeof v === "object" && !Array.isArray(v)) {
    obj = v;
  } else {
    return asText(v);
  }

  const keys = Object.keys(obj);
  // Single-key string-value objects stay compact (e.g. {"command": "ls -la"})
  if (keys.length === 1 && typeof obj[keys[0]] === "string") {
    return JSON.stringify(obj);
  }

  // Bring "description" to the top
  if ("description" in obj) {
    const reordered = { description: obj.description };
    for (const [k, val] of Object.entries(obj)) {
      if (k !== "description") reordered[k] = val;
    }
    return JSON.stringify(reordered, null, 2);
  }

  return JSON.stringify(obj, null, 2);
}

// Extract plain text from content (string or [{type:"text", text:"..."}, ...] array).
function extractText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter(function(b) { return b && b.type === "text"; })
      .map(function(b) { return b.text || ""; })
      .join("\n");
  }
  return "";
}

// Return the response's thinking blocks, preferring the top-level field but
// falling back to LiteLLM's Bedrock-adapter location for signature-only
// blocks (display: "omitted") that it sometimes drops from the top level.
function getThinkingBlocks(resp) {
  if (!resp) return null;
  if (Array.isArray(resp.thinking_blocks) && resp.thinking_blocks.length) {
    return resp.thinking_blocks;
  }
  const psf = resp.provider_specific_fields;
  if (psf && Array.isArray(psf.thinking_blocks) && psf.thinking_blocks.length) {
    return psf.thinking_blocks;
  }
  return null;
}

// Render one thinking block. Anthropic's display: "omitted" mode returns a
// signature only (no visible reasoning text) for many turns — show that
// thinking occurred instead of leaving a blank box.
function appendThinkingBlock(tb, parent) {
  const box = el("div", "block-thinking");
  box.appendChild(el("div", "block-label", "thinking"));
  if (tb && tb.thinking) {
    box.appendChild(Object.assign(el("pre"), { textContent: asText(tb.thinking) }));
  } else {
    box.appendChild(el("div", "meta", "(thinking occurred; not shown by the model)"));
  }
  parent.appendChild(box);
  replaceLoadingPlaceholders(box);
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
      box.appendChild(Object.assign(el("pre"), { textContent: prettyToolInput(block.input) }));
      parent.appendChild(box);
    } else if (type === "tool_result") {
      const box = el("div", "block-tool_result");
      box.appendChild(el("div", "block-label", "tool_result"));
      box.appendChild(Object.assign(el("pre"), { textContent: asText(block.content) }));
      parent.appendChild(box);
    } else if (type === "thinking") {
      appendThinkingBlock(block, parent);
    } else if (type === "image") {
      const src = block.source;
      if (src && src.data) {
        const box = el("div", "block-image");
        const img = el("img", "content-image");
        img.src = "data:" + (src.media_type || "image/png") + ";base64," + src.data;
        img.alt = "Image";
        img.title = "Click to view full size";
        img.onclick = function(e) { e.stopPropagation(); showImageOverlay(img.src); };
        box.appendChild(img);
        parent.appendChild(box);
      }
    } else {
      const box = el("div", "block-tool_use");
      box.appendChild(el("div", "block-label", type));
      box.appendChild(Object.assign(el("pre"), { textContent: asText(block) }));
      parent.appendChild(box);
    }
  }
  replaceLoadingPlaceholders(parent);
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
    .querySelectorAll(".block-text, .block-tool_use, .block-tool_result, .block-thinking")
    .forEach((box) => {
      if (box.querySelector(":scope > .copy-btn")) return;
      const pre = box.querySelector("pre");
      if (pre) addCopyButton(box, pre);
    });
  container.querySelectorAll("pre").forEach((pre) => {
    if (pre.closest(".block-text, .block-tool_use, .block-tool_result, .block-thinking, .section")) return;
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
  const thoughts = getThinkingBlocks(resp);
  if (thoughts) {
    for (const tb of thoughts) {
      appendThinkingBlock(tb, m);
    }
  }
  if (resp && resp.content) renderContent(resp.content, m);
  const calls = resp && resp.tool_calls;
  if (Array.isArray(calls)) {
    for (const c of calls) {
      const fn = (c && c.function) || {};
      const box = el("div", "block-tool_use");
      box.appendChild(el("div", "block-label", `tool_call · ${fn.name || ""}`));
      box.appendChild(Object.assign(el("pre"), { textContent: prettyToolInput(fn.arguments) }));
      m.appendChild(box);
    }
  }
  if (!resp || (!resp.content && !(Array.isArray(calls) && calls.length) && !thoughts)) {
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

// ---------------------------------------------------------------------------
// Tool definitions
//
// A request's `tools` array has a predictable shape, so it gets a structured
// view rather than a JSON dump: one collapsed row per tool (name, one-line
// description, param count) expanding to the full description and a parameter
// table. There is no raw-JSON fallback, so every field the renderer does not
// model is surfaced as "other fields" instead of being dropped.
// ---------------------------------------------------------------------------

const isPlainObj = (v) => v != null && typeof v === "object" && !Array.isArray(v);

// How many tool-name pills fit in the section's summary before the rest are
// counted off as "+N more".
const TOOL_PILL_LIMIT = 12;
// Longest one-line description snippet shown on a collapsed row.
const SNIPPET_MAX = 140;
// Deepest nesting of object-valued parameters that still gets its own table.
const PARAM_DEPTH_CAP = 3;
// MCP tool names arrive namespaced as mcp__<server>__<tool>.
const MCP_NAME_RE = /^mcp__(.+?)__(.+)$/;
// JSON Schema validation keywords rendered as a compact constraint line.
const CONSTRAINT_KEYS = ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                         "minLength", "maxLength", "minItems", "maxItems",
                         "pattern", "format"];
// Schema keys the structured view accounts for. Everything else (e.g. $defs)
// surfaces as "other schema keys". `additionalProperties: false` and `$schema`
// are deliberately silent: they are boilerplate on every Claude Code tool and
// say nothing a log reader needs — an *unusual* additionalProperties still
// shows up, via schemaConstraints().
const SCHEMA_CONSUMED = ["type", "properties", "required", "$schema", "title",
                         "additionalProperties", "propertyNames", ...CONSTRAINT_KEYS];
// The same, per property. `properties`/`required` are re-added per property when
// its own nested table is rendered, so hitting PARAM_DEPTH_CAP reports a
// sub-schema rather than hiding it.
const PROP_CONSUMED = ["description", "enum", "default", "items", "anyOf", "oneOf",
                       "const", "$ref",
                       ...SCHEMA_CONSUMED.filter((k) => k !== "properties" && k !== "required")];

// Reduce one entry of a `tools` array to {name, type, description, deferred,
// schema, extras}, accepting the encodings that reach the logs: Anthropic's
// inline `input_schema`, OpenAI's nested `function`, Bedrock Converse's
// `toolSpec`, and server tools that carry a `type` and no schema at all.
function toolSpec(tool) {
  if (!isPlainObj(tool)) {
    return { name: null, type: null, description: null, deferred: false,
             schema: null, extras: tool };
  }

  // Unwrap to the object that actually carries name/description, noting where
  // its schema lives.
  let src = tool;
  let schemaKeys = ["input_schema"];
  if (isPlainObj(tool.function)) {
    src = tool.function;
    schemaKeys = ["parameters", "input_schema"];
  } else if (isPlainObj(tool.toolSpec)) {
    src = tool.toolSpec;
    schemaKeys = ["inputSchema", "input_schema"];
  }

  let schema = null;
  for (const k of schemaKeys) {
    if (isPlainObj(src[k])) { schema = src[k]; break; }
  }
  // Bedrock wraps the JSON Schema one level deeper, in {json: {...}}.
  if (schema && isPlainObj(schema.json) && !isPlainObj(schema.properties)) {
    schema = schema.json;
  }

  const consumed = new Set(["name", "description", "type", "defer_loading", ...schemaKeys]);
  const extras = {};
  for (const [k, v] of Object.entries(src)) {
    if (!consumed.has(k)) extras[k] = v;
  }
  // A nested encoding can also carry keys on the wrapper (e.g. a cache_control
  // sitting beside .function).
  if (src !== tool) {
    for (const [k, v] of Object.entries(tool)) {
      if (!["type", "function", "toolSpec", "defer_loading"].includes(k)) extras[k] = v;
    }
  }

  return {
    name: typeof src.name === "string" ? src.name : null,
    type: typeof tool.type === "string" ? tool.type : null,
    description: typeof src.description === "string" ? src.description : null,
    // Claude Code marks tools whose schema the model loads on demand (ToolSearch).
    deferred: tool.defer_loading === true || src.defer_loading === true,
    schema,
    extras,
  };
}

// The sub-schema whose `properties` describe a parameter's inner shape, as
// {schema, key} where `key` is the property key that carried it: the property
// itself ("self") when it is an object, an array's item schema, a map's value
// schema, or the first object-shaped member of an anyOf/oneOf union. Null when
// there is none.
function childSchema(prop) {
  if (isPlainObj(prop.properties)) return { schema: prop, key: "self" };
  if (isPlainObj(prop.items) && isPlainObj(prop.items.properties)) {
    return { schema: prop.items, key: "items" };
  }
  const ap = prop.additionalProperties;
  if (isPlainObj(ap) && isPlainObj(ap.properties)) {
    return { schema: ap, key: "additionalProperties" };
  }
  for (const key of ["anyOf", "oneOf"]) {
    if (!Array.isArray(prop[key])) continue;
    for (const alt of prop[key]) {
      if (isPlainObj(alt) && isPlainObj(alt.properties)) return { schema: alt, key };
    }
  }
  return null;
}

// A short type label for one property: "string", "string[]", "string | null",
// "enum", a $ref's basename, and so on. "any" when the schema says nothing.
function schemaTypeLabel(prop) {
  if (!isPlainObj(prop)) return "any";
  if (typeof prop.$ref === "string") return prop.$ref.split("/").pop() || "ref";
  if ("const" in prop) return "const " + compactJSON(prop.const);
  const t = prop.type;
  if (Array.isArray(t) && t.length) return t.join(" | ");
  if (typeof t === "string") {
    if (t !== "array") return t;
    return isPlainObj(prop.items) ? schemaTypeLabel(prop.items) + "[]" : "array";
  }
  const union = Array.isArray(prop.anyOf) ? prop.anyOf
    : (Array.isArray(prop.oneOf) ? prop.oneOf : null);
  if (union && union.length) {
    const parts = union.map(schemaTypeLabel).filter((s) => s !== "any");
    if (parts.length) return [...new Set(parts)].join(" | ");
  }
  if (Array.isArray(prop.enum)) return "enum";
  if (isPlainObj(prop.properties)) return "object";
  return "any";
}

// Flatten one schema's `properties` into display rows, recursing into
// object-valued properties up to PARAM_DEPTH_CAP.
function schemaParams(schema, depth) {
  if (!isPlainObj(schema) || !isPlainObj(schema.properties)) return [];
  const required = Array.isArray(schema.required) ? schema.required : [];
  const rows = [];
  for (const [name, raw] of Object.entries(schema.properties)) {
    const prop = isPlainObj(raw) ? raw : {};
    const child = childSchema(prop);
    const recurse = child !== null && depth < PARAM_DEPTH_CAP;
    const consumed = new Set(PROP_CONSUMED);
    if (recurse) {
      consumed.add("properties");
      consumed.add("required");
    } else if (child !== null && child.key !== "self") {
      // Past PARAM_DEPTH_CAP: report the sub-schema rather than hide it.
      consumed.delete(child.key);
    }
    const extras = {};
    for (const [k, v] of Object.entries(prop)) {
      if (!consumed.has(k)) extras[k] = v;
    }
    rows.push({
      name,
      required: required.includes(name),
      type: schemaTypeLabel(prop),
      description: typeof prop.description === "string" ? prop.description : null,
      enumValues: Array.isArray(prop.enum) ? prop.enum : null,
      def: "default" in prop ? prop.default : undefined,
      constraints: schemaConstraints(prop),
      extras,
      children: recurse ? schemaParams(child.schema, depth + 1) : [],
    });
  }
  return rows;
}

// Schema keys outside SCHEMA_CONSUMED, as an object (empty when there are none).
function schemaExtras(schema) {
  if (!isPlainObj(schema)) return {};
  const out = {};
  for (const [k, v] of Object.entries(schema)) {
    if (!SCHEMA_CONSUMED.includes(k)) out[k] = v;
  }
  return out;
}

// One bound pair as a single phrase: "1–4 items", "length ≥1", "≤10". Bounds at
// the JS safe-integer limit are dropped: schema generators pin every integer
// param to that range, so they say nothing about the tool.
function boundPhrase(min, max, prefix, suffix) {
  const real = (v) => v !== undefined && Math.abs(v) !== Number.MAX_SAFE_INTEGER;
  const lo = real(min) ? compactJSON(min) : null;
  const hi = real(max) ? compactJSON(max) : null;
  if (lo !== null && hi !== null) return prefix + lo + "–" + hi + suffix;
  if (lo !== null) return prefix + "≥" + lo + suffix;
  if (hi !== null) return prefix + "≤" + hi + suffix;
  return null;
}

// The validation keywords of one schema node (a whole tool schema or a single
// property) as short display strings, e.g. ["1–4 items", "matches /^a$/"].
function schemaConstraints(node) {
  if (!isPlainObj(node)) return [];
  const out = [];
  const bounds = [
    boundPhrase(node.minimum, node.maximum, "", ""),
    boundPhrase(node.exclusiveMinimum, node.exclusiveMaximum, "", ""),
    boundPhrase(node.minLength, node.maxLength, "length ", ""),
    boundPhrase(node.minItems, node.maxItems, "", " items"),
  ];
  // An exclusive bound is strict, so its ≥/≤ has to become >/<.
  if (bounds[1]) bounds[1] = bounds[1].replace("≥", ">").replace("≤", "<");
  for (const b of bounds) {
    if (b) out.push(b);
  }
  if (typeof node.pattern === "string") out.push("matches /" + node.pattern + "/");
  if (typeof node.format === "string") out.push("format " + node.format);
  if (isPlainObj(node.propertyNames) && node.propertyNames.type) {
    out.push("keys " + schemaTypeLabel(node.propertyNames));
  }
  // Only the unusual cases are worth saying out loud; see SCHEMA_CONSUMED. A map
  // whose values are objects says it with its own nested table (childSchema), so
  // nothing is added here for that case.
  const ap = node.additionalProperties;
  if (ap === true) {
    out.push("additionalProperties: true");
  } else if (isPlainObj(ap) && !isPlainObj(ap.properties)) {
    out.push("values " + schemaTypeLabel(ap));
  }
  return out;
}

function compactJSON(v) {
  return typeof v === "string" ? v : JSON.stringify(v);
}

// A one-line gist of `text` for a collapsed row. Returns the loading
// placeholder untouched so it still becomes a spinner.
function descSnippet(text) {
  if (typeof text !== "string") return "";
  const trimmed = text.trim();
  if (trimmed === LOADING_PLACEHOLDER) return trimmed;
  const line = (trimmed.split("\n").find((l) => l.trim()) || "").trim();
  if (line.length <= SNIPPET_MAX) return line;
  return line.slice(0, SNIPPET_MAX - 1).trimEnd() + "…";
}

function countLines(text) {
  return typeof text === "string" ? text.trim().split("\n").length : 0;
}

// "mcp__jira__jira_search" → "jira_search"; any other name unchanged.
function shortToolName(name) {
  if (typeof name !== "string" || !name) return "(unnamed)";
  const m = MCP_NAME_RE.exec(name);
  return m ? m[2] : name;
}

// Show keys the structured view does not model, so an unfamiliar shape is
// visible rather than silently dropped.
function appendExtras(extras, parent, label) {
  if (extras == null) return;
  if (isPlainObj(extras) && !Object.keys(extras).length) return;
  parent.appendChild(el("div", "block-label", label));
  parent.appendChild(Object.assign(el("pre"), { textContent: asText(extras) }));
}

// The full description, behind a nested toggle because tool descriptions run to
// hundreds of lines. Adds nothing when the row's snippet already showed the
// whole text — which includes an unresolved description's placeholder.
function appendToolDescription(desc, parent) {
  if (typeof desc !== "string") return;
  const text = desc.trim();
  if (!text || text === descSnippet(text)) return;
  const lines = countLines(text);
  const d = el("details", "tool-desc");
  d.appendChild(el("summary", null,
    lines > 1 ? `full description (${lines} lines)` : "full description"));
  d.appendChild(Object.assign(el("pre"), { textContent: text }));
  parent.appendChild(d);
}

// A parameter table: name (with a * when required), type, then description,
// enum values, default, any nested table, and unmodelled schema keys. Each
// description sits alone in its own element so the placeholder rule holds.
function renderParams(params) {
  const grid = el("div", "params");
  for (const p of params) {
    const name = el("div", "p-name");
    name.appendChild(el("span", null, p.name));
    if (p.required) name.appendChild(el("span", "req", "*"));
    grid.appendChild(name);
    grid.appendChild(el("div", "p-type", p.type));

    const cell = el("div", "p-cell");
    if (p.description) cell.appendChild(el("div", "p-desc", p.description));
    if (p.enumValues && p.enumValues.length) {
      const chips = el("div", "p-enum");
      for (const v of p.enumValues) chips.appendChild(el("span", "badge", compactJSON(v)));
      cell.appendChild(chips);
    }
    const meta = el("div", "p-meta");
    if (p.def !== undefined) {
      meta.appendChild(el("span", null, "default: "));
      // The value gets its own text node: a hashed default resolves to the
      // loading placeholder, which only becomes a spinner when it stands alone.
      meta.appendChild(el("span", null, compactJSON(p.def)));
    }
    if (p.constraints.length) {
      meta.appendChild(el("span", null,
        (p.def !== undefined ? " · " : "") + p.constraints.join(" · ")));
    }
    if (meta.children.length) cell.appendChild(meta);
    if (p.children.length) cell.appendChild(renderParams(p.children));
    appendExtras(p.extras, cell, "other schema keys");
    grid.appendChild(cell);
  }
  return grid;
}

// One collapsed tool row plus its expanded body.
function renderToolRow(spec) {
  const row = el("details", "tool");
  const sum = el("summary");

  const nameEl = el("span", "tool-name");
  const mcp = typeof spec.name === "string" ? MCP_NAME_RE.exec(spec.name) : null;
  if (mcp) {
    nameEl.appendChild(el("span", "tool-ns", mcp[1]));
    nameEl.appendChild(el("span", null, mcp[2]));
  } else {
    nameEl.textContent = spec.name || "(unnamed)";
  }
  sum.appendChild(nameEl);
  if (spec.deferred) sum.appendChild(el("span", "badge tool-flag", "deferred"));
  if (spec.description) {
    sum.appendChild(el("span", "tool-snip", descSnippet(spec.description)));
  }

  const params = schemaParams(spec.schema, 0);
  if (params.length) {
    sum.appendChild(el("span", "tool-nparams",
      `${params.length} param${params.length === 1 ? "" : "s"}`));
  } else if (spec.type) {
    // A server-side tool has no schema of its own; its type is the useful fact.
    sum.appendChild(el("span", "badge tool-type", spec.type));
  }
  row.appendChild(sum);

  const body = el("div", "tool-body");
  appendToolDescription(spec.description, body);
  if (params.length) {
    body.appendChild(el("div", "block-label", "parameters"));
    body.appendChild(renderParams(params));
  } else {
    body.appendChild(el("div", "meta", "(no parameters)"));
  }
  const notes = schemaConstraints(spec.schema);
  if (notes.length) body.appendChild(el("div", "meta", notes.join(" · ")));
  appendExtras(schemaExtras(spec.schema), body, "other schema keys");
  appendExtras(spec.extras, body, "other fields");
  row.appendChild(body);
  return row;
}

// The whole "N tools" section: a summary naming the first few tools, then one
// row per tool in the array's own (meaningful) order.
function renderToolsSection(tools) {
  const specs = tools.map(toolSpec);
  const section = el("details", "toolsdef");

  const sum = el("summary");
  sum.appendChild(el("span", "tools-count",
    `${specs.length} tool${specs.length === 1 ? "" : "s"}`));
  const pills = el("span", "tool-pills");
  for (const spec of specs.slice(0, TOOL_PILL_LIMIT)) {
    pills.appendChild(el("span", "tool-pill", shortToolName(spec.name)));
  }
  if (specs.length > TOOL_PILL_LIMIT) {
    pills.appendChild(el("span", "meta", `+${specs.length - TOOL_PILL_LIMIT} more`));
  }
  sum.appendChild(pills);
  section.appendChild(sum);

  const list = el("div", "tool-list");
  for (const spec of specs) list.appendChild(renderToolRow(spec));
  section.appendChild(list);
  return section;
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
    body.appendChild(renderToolsSection(r.tools));
  }
  for (const m of (r.messages || [])) {
    body.appendChild(msgEl(m.role || "user", m.content));
  }
  renderResponse(r.response, body);
  const notice = finishReasonNotice(r);
  if (notice) body.appendChild(notice);
  replaceLoadingPlaceholders(body);
  return body;
}

// Abnormal `finish_reason` values, mapped to the notice shown under the reply. A
// reply that ends this way is cut short mid-sentence — or missing entirely — but
// still arrives as status "success", so nothing else in the view distinguishes it
// from a complete one. The normal terminations ("stop", "tool_calls") are absent
// on purpose: they cover ~98% of traffic and saying so would be noise.
const FINISH_REASON_NOTICES = {
  content_filter: "The response was terminated due to content filtering",
  length: "The response was truncated: it reached the max_tokens limit",
};

// Claude Code's probe calls ("quota", "count") ask for max_tokens: 1, so "length"
// is the only reason they can possibly report — the model was never given room to
// stop on its own. Flagging those would bury the handful of real truncations under
// one notice per session start. A reply is only meaningfully truncated when its cap
// allowed more than the token it produced.
function isMeaningfulFinishReason(r) {
  if (r.finish_reason !== "length") return true;
  return r.max_tokens == null || r.max_tokens > 1;
}

// A red notice explaining why a reply stopped early, or null when it ended normally.
// Appended under the response wherever one is rendered, since the reply itself is
// where the truncation is visible and the explanation belongs next to it.
function finishReasonNotice(r) {
  const text = FINISH_REASON_NOTICES[r.finish_reason];
  if (!text || !isMeaningfulFinishReason(r)) return null;
  // The cap is the actionable part of a truncation — it says whose limit was hit.
  const cap = r.finish_reason === "length" && r.max_tokens != null
    ? ` (${r.max_tokens.toLocaleString()})`
    : "";
  return el("div", "finish-notice", `⚠ ${text}${cap}`);
}

// A short "#N · model · status · ts" caption line shared by a turn and its modal.
function captionEl(r, displayIdx) {
  const cap = el("div", "caption");
  cap.appendChild(el("span", "idx", `#${displayIdx}`));
  cap.appendChild(el("span", null, (r.model || "").split("/").pop()));
  if (r.status && r.status !== "success") {
    cap.appendChild(el("span", "fail", `· ${r.status}`));
  }
  if (isClassifierRequest(r)) {
    cap.appendChild(el("span", "auto-badge", "auto"));
  }
  if (isStatusSummaryRequest(r)) {
    cap.appendChild(el("span", "status-badge", "status"));
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

// Compact rendering for auto classifier requests — shows the evaluated
// command and result instead of the usual user/assistant chat bubbles.
// Clicking opens the full detail modal.
function renderClassifierTurn(r, displayIdx) {
  const turn = el("div", "turn");
  turn.dataset.role = "main";

  // Caption line with auto badge (captionEl adds the badge for classifiers)
  turn.appendChild(captionEl(r, displayIdx));

  const block = el("div", "classifier-block");

  // Evaluated command
  const cmd = extractEvaluatedCommand(r);
  if (cmd) {
    const cmdBox = el("div", "block-tool_use");
    cmdBox.appendChild(el("div", "block-label", "eval · " + cmd.tool));
    const pre = Object.assign(el("pre"), { textContent: cmd.body });
    cmdBox.appendChild(pre);
    addCopyButton(cmdBox, pre);
    block.appendChild(cmdBox);
  }

  // Result: the verdict, trailed by the 0-100 severity score and the matched
  // rule when the classifier reported them (records predating the score show
  // the verdict alone).
  const parsed = parseClassifierResult(r);
  const resultEl = el("div", "classifier-result");
  if (parsed.unparseable) {
    resultEl.textContent = "? Unparseable";
    resultEl.className = "classifier-result blocked";
  } else {
    resultEl.textContent = parsed.allowed ? "✓ Allowed" : "✗ Blocked";
    resultEl.className = "classifier-result " + (parsed.allowed ? "allowed" : "blocked");
    const trailing = [];
    if (parsed.severity != null) trailing.push(String(parsed.severity));
    if (parsed.category) trailing.push(parsed.category);
    if (trailing.length) {
      resultEl.appendChild(el("span", "sev", " · " + trailing.join(" · ")));
    }
  }
  block.appendChild(resultEl);

  if (parsed.reason) {
    block.appendChild(el("div", "classifier-reason", parsed.reason));
  }

  // A cut-off verdict is why parseClassifierResult would report "? Unparseable",
  // so the cause belongs next to it rather than being left to guess at.
  const notice = finishReasonNotice(r);
  if (notice) block.appendChild(notice);

  turn.appendChild(block);
  turn.onclick = () => openModal(r, displayIdx);
  return turn;
}

// Compact rendering for status-summary requests — the produced caption instead
// of the whole conversation the call re-sends. Clicking opens the full detail.
function renderStatusSummaryTurn(r, displayIdx) {
  const turn = el("div", "turn");
  // These calls come from subagents, so they need the same role/sub dataset
  // attributes renderTurn sets or the tab filter would drop them.
  if (r.agent_id) {
    turn.dataset.role = "sub";
    turn.dataset.sub = r.agent_id;
  } else {
    turn.dataset.role = "main";
  }
  turn.appendChild(captionEl(r, displayIdx));

  const parsed = parseStatusSummary(r);
  const block = el("div", "status-block");
  if (parsed.summary) {
    block.appendChild(el("div", "status-summary", parsed.summary));
  } else {
    block.appendChild(el("div", "status-summary empty", r.error ? "(failed)" : "(no summary)"));
  }
  if (parsed.previous) {
    block.appendChild(el("div", "status-prev", "prev: " + parsed.previous));
  }
  // These captions carry a small max_tokens, so they are where a real `length`
  // truncation actually shows up.
  const notice = finishReasonNotice(r);
  if (notice) block.appendChild(notice);
  turn.appendChild(block);

  // Unlike the classifier turn, keep the cost/context line: these calls are pure
  // display overhead, so what they cost is the interesting part.
  const info = infoLine(r);
  if (info) turn.appendChild(info);

  turn.onclick = () => openModal(r, displayIdx);
  return turn;
}

// One turn: the latest user message as a right-aligned bubble and the response
// (or error) as a left-aligned bubble below it. Clicking opens the full detail.
function renderTurn(r, displayIdx) {
  if (isClassifierRequest(r)) {
    return renderClassifierTurn(r, displayIdx);
  }
  if (isStatusSummaryRequest(r)) {
    return renderStatusSummaryTurn(r, displayIdx);
  }

  const turn = el("div", "turn");
  if (r.agent_id) {
    turn.dataset.role = "sub";
    turn.dataset.sub = r.agent_id;
  } else {
    turn.dataset.role = "main";
  }
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
  // Appended after the two calls above, which wrap loose `pre`s in copyable
  // sections — the notice is commentary on the reply, not part of it.
  const notice = finishReasonNotice(r);
  if (notice) respBubble.appendChild(notice);
  // The timing line sits above the response bubble; the context/output/cost
  // info line sits below it. Both are left-aligned.
  const rt = respTimingLine(r);
  if (rt) turn.appendChild(rt);
  turn.appendChild(respBubble);
  const info = infoLine(r);
  if (info) turn.appendChild(info);

  turn.onclick = () => openModal(r, displayIdx);
  replaceLoadingPlaceholders(turn);
  return turn;
}

// Like renderResponse but appends content/tool_calls straight into `parent`
// without wrapping in a `.msg` block (the bubble is the wrapper here).
function renderResponseInto(resp, parent) {
  const thoughts = getThinkingBlocks(resp);
  if (thoughts) {
    for (const tb of thoughts) {
      appendThinkingBlock(tb, parent);
    }
  }
  if (resp && resp.content) renderContent(resp.content, parent);
  const calls = resp && resp.tool_calls;
  if (Array.isArray(calls)) {
    for (const c of calls) {
      const fn = (c && c.function) || {};
      const box = el("div", "block-tool_use");
      box.appendChild(el("div", "block-label", `tool_call · ${fn.name || ""}`));
      box.appendChild(Object.assign(el("pre"), { textContent: prettyToolInput(fn.arguments) }));
      parent.appendChild(box);
    }
  }
  if (!resp || (!resp.content && !(Array.isArray(calls) && calls.length) && !thoughts)) {
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

function closeImageOverlay() {
  const back = $("image-overlay");
  if (back) back.remove();
  document.removeEventListener("keydown", onImageOverlayKey);
}

function onImageOverlayKey(e) {
  if (e.key === "Escape") closeImageOverlay();
}

function showImageOverlay(src) {
  closeImageOverlay();
  const back = el("div", "modal-backdrop");
  back.id = "image-overlay";
  back.onclick = function(e) { if (e.target === back) closeImageOverlay(); };

  const img = el("img");
  img.src = src;
  img.style.cssText = "max-width:90vw;max-height:90vh;object-fit:contain;border-radius:6px;";
  back.appendChild(img);

  document.body.appendChild(back);
  document.addEventListener("keydown", onImageOverlayKey);
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

// Normalize prompt text for comparison: extract from content (string or content
// array), strip <system-reminder> blocks, collapse whitespace, and trim.
// Does NOT truncate — callers needing a short snippet should slice the result.
function normalizePrompt(content) {
  let text = "";
  if (typeof content === "string") {
    text = content;
  } else if (Array.isArray(content)) {
    text = content
      .map(b => (typeof b === "string" ? b : (b && b.type === "text" ? b.text : "")))
      .filter(t => typeof t === "string")
      .join("\n");
  }
  text = text.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "").trim();
  text = text.replace(/\s+/g, " ");
  return text;
}

// Plain text of a record's first user message, with leading <system-reminder>
// blocks stripped, for use as a subagent's human-readable label.
function firstPromptSnippet(r) {
  const msgs = r.messages || [];
  if (!msgs.length) return "";
  return normalizePrompt(msgs[0].content).slice(0, 60);
}

// Extract tool calls that spawn subagents from a main-agent record's response.
// Returns an array of { name, subagent_type, description, prompt } objects.
// Handles both tool_use blocks (response.content) and tool_calls arrays.
function extractSubagentToolCalls(r) {
  const calls = [];
  const resp = r.response || {};

  // Check response.content for tool_use blocks with subagent_type
  if (Array.isArray(resp.content)) {
    for (const block of resp.content) {
      if (block && block.type === "tool_use" && block.input && block.input.subagent_type) {
        calls.push({
          name: block.name,
          subagent_type: block.input.subagent_type,
          description: block.input.description,
          prompt: block.input.prompt,
        });
      }
    }
  }

  // Check response.tool_calls for function calls with subagent_type
  if (Array.isArray(resp.tool_calls)) {
    for (const tc of resp.tool_calls) {
      const fn = tc && tc.function;
      if (!fn || !fn.arguments) continue;
      let args;
      try { args = JSON.parse(fn.arguments); } catch (e) { continue; }
      if (args && args.subagent_type) {
        calls.push({
          name: fn.name,
          subagent_type: args.subagent_type,
          description: args.description,
          prompt: args.prompt,
        });
      }
    }
  }

  return calls;
}

// Build a lookup map from normalized prompt text to { subagent_type, description }.
// Scans all main-agent records for tool calls that spawn subagents.
// First-match-wins when two tool calls normalize to the same prompt string.
function buildSubagentCallMap(reqs) {
  const map = {};
  for (const r of reqs) {
    if (r.agent_id) continue;
    for (const call of extractSubagentToolCalls(r)) {
      const key = normalizePrompt(call.prompt);
      if (key && !map[key]) {
        map[key] = { subagent_type: call.subagent_type, description: call.description };
      }
    }
  }
  return map;
}

// Does a record look like a subagent's final turn (a text answer, no tool call)?
function looksTerminal(r) {
  const resp = r.response || {};
  const calls = resp.tool_calls;
  if (Array.isArray(calls) && calls.length) return false;
  return !!resp.content;
}

// Locate the transcript a classifier request wraps the reviewed action in.
// Claude Code emits the two markers as content blocks of their own, so this
// looks for text blocks trimming to exactly "<transcript>" and "</transcript>"
// within one message. Both markers are shorter than the log's string-hashing
// threshold, so they are always stored literally — this works before
// strings.jsonl has resolved. Returns { content, open, close } or null.
function transcriptBlocks(r) {
  const msgs = r.messages || [];
  for (let i = 0; i < msgs.length; i++) {
    const content = msgs[i].content;
    if (!Array.isArray(content)) continue;
    let open = -1;
    let close = -1;
    for (let j = 0; j < content.length; j++) {
      const block = content[j];
      if (!block || block.type !== "text" || typeof block.text !== "string") continue;
      const marker = block.text.trim();
      if (marker === "<transcript>" && open === -1) {
        open = j;
      } else if (marker === "</transcript>") {
        close = j;
        break;
      }
    }
    if (open !== -1 && close > open) return { content, open, close };
  }
  return null;
}

// Does a record look like an auto classifier request?
//
// Keyed on request structure rather than prompt wording, so the special display
// survives Claude Code's periodic rewrites of the classifier prompt: a
// classifier call frames the reviewed action in <transcript> markers and never
// carries tool definitions, while every main-agent and subagent call does. The
// tools check is what keeps an ordinary turn that merely quotes "</transcript>"
// in its text from being mistaken for one.
function isClassifierRequest(r) {
  if (Array.isArray(r.tools) && r.tools.length) return false;
  return transcriptBlocks(r) !== null;
}

// Extract the action under review from a classifier request's transcript, as
// { tool, body }, or null.
//
// The action is always the transcript's last entry. The block before
// </transcript> is the newest prompt-cache chunk and may hold several entries,
// so the newer JSONL format ({"Bash":"…"}, {"Edit":{…}}) reads the last line;
// the retired plain-text format ("Bash <cmd>") reads the whole block, since its
// commands can span lines. Which one applies is decided by whether that last
// line parses as a single-key JSON object.
function extractEvaluatedCommand(r) {
  const found = transcriptBlocks(r);
  if (!found) return null;
  const block = found.content[found.close - 1];
  if (!block || block.type !== "text" || typeof block.text !== "string") return null;
  const text = block.text;

  // Walk back over harness-inserted {"meta": …} lines: they annotate the entry
  // below them and never count as the action.
  const lines = text.split("\n").filter((line) => line.trim());
  for (let i = lines.length - 1; i >= 0; i--) {
    const entry = parseTranscriptEntry(lines[i]);
    if (!entry) break;
    if (entry.tool !== "meta") return entry;
  }

  // Legacy plain-text format: first word is the tool, the rest is the body.
  const trimmed = text.trim();
  const firstSpace = trimmed.indexOf(" ");
  if (firstSpace === -1) return { tool: "", body: trimmed };
  return { tool: trimmed.slice(0, firstSpace).trim(), body: trimmed.slice(firstSpace + 1).trim() };
}

// Parse one JSONL transcript entry — {"<Tool>": <input>} — into { tool, body },
// or null when the line is not a single-key JSON object.
function parseTranscriptEntry(line) {
  let obj;
  try { obj = JSON.parse(line); } catch (e) { return null; }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
  const keys = Object.keys(obj);
  if (keys.length !== 1) return null;
  const value = obj[keys[0]];
  return { tool: keys[0], body: typeof value === "string" ? value : prettyToolInput(value) };
}

// The classifier grades an action 0-100; its prompt puts the allow/block
// boundary at exactly 50 ("below 50 means allow, above 50 means block").
const SEVERITY_BLOCK_THRESHOLD = 50;

// Parse the classifier result from a record's response.  Combines thinking
// blocks and content text because the verdict can appear in either (DeepSeek
// puts it inside thinking, Anthropic puts it in content).
//
// Current classifier calls answer <severity>N</severity>, optionally followed
// by <category>Rule Name</category> when a block rule matched.  The first
// stage stops on "</severity>", so the closing tag is often missing, and the
// second stage prefixes a <thinking> section that can quote tags of its own —
// hence the verdict is read from outside that section.  Records predating the
// switch answered <block>yes|no</block> plus <reason>; that path is kept so
// older sessions still render.
function parseClassifierResult(r) {
  const resp = r.response || {};
  const parts = [];
  const thoughts = getThinkingBlocks(resp);
  if (thoughts) {
    for (let i = 0; i < thoughts.length; i++) {
      if (thoughts[i] && thoughts[i].thinking) {
        parts.push(thoughts[i].thinking);
      }
    }
  }
  const contentText = extractText(resp.content);
  if (contentText) parts.push(contentText);
  const fullText = parts.join("\n");
  const verdictText = fullText.replace(/<thinking>[\s\S]*?<\/thinking>/gi, "");

  const sevRe = /<severity>\s*(\d{1,3})/gi;
  const sev = lastMatch(sevRe, verdictText) || lastMatch(sevRe, fullText);
  if (sev) {
    const severity = parseInt(sev[1], 10);
    const cat = /<category>([\s\S]*?)<\/category>/i.exec(verdictText);
    return {
      allowed: severity < SEVERITY_BLOCK_THRESHOLD,
      severity: severity,
      category: cat ? cat[1].trim() : null,
      reason: null,
      unparseable: false,
    };
  }

  // Retired format: match <block>yes or <block>no
  const blockRe = /<block>(yes|no)\b(<\/block>)?/gi;
  const matches = [];
  let m;
  while ((m = blockRe.exec(fullText)) !== null) {
    matches.push(m);
  }
  if (matches.length === 0) {
    return { allowed: false, severity: null, category: null, reason: null, unparseable: true };
  }
  const isYes = matches[0][1].toLowerCase() === "yes";
  // Extract reason (only for blocked)
  let reason = null;
  if (isYes) {
    const reasonRe = /<reason>([\s\S]*?)<\/reason>/g;
    const rm = reasonRe.exec(fullText);
    if (rm) {
      reason = rm[1].trim();
    }
  }
  return { allowed: !isYes, severity: null, category: null, reason: reason, unparseable: false };
}

// Claude Code periodically asks for a 3-5 word gerund describing the agent's last
// action, purely to caption the CLI spinner; the answer never re-enters the
// session. Unlike the classifier there is no structural tell — the call carries
// the full conversation, the full tool list and an ordinary max_tokens — so this
// keys on the prompt's opening line, with the word count matched loosely so a
// future "4-6 words" rewrite still hits.
//
// The prompt is far longer than the log's string-hashing threshold, so it only
// matches once /api/strings has resolved; tick() re-renders the stream when a
// pending hash arrives, so a live session corrects itself within a poll.
const STATUS_SUMMARY_RE = /^Describe your most recent action in \d+-\d+ words/;

// The prompt is appended as the trailing text of the last user message: the whole
// content when no tool results are pending, otherwise one text block after them.
// Returns the prompt text, or null.
function statusSummaryPrompt(r) {
  const last = lastUserMessage(r.messages || []);
  if (!last) return null;
  const content = last.content;
  let text = null;
  if (typeof content === "string") {
    text = content;
  } else if (Array.isArray(content) && content.length) {
    const tail = content[content.length - 1];
    if (tail && tail.type === "text" && typeof tail.text === "string") text = tail.text;
  }
  if (typeof text !== "string") return null;
  const trimmed = text.trim();
  return STATUS_SUMMARY_RE.test(trimmed) ? trimmed : null;
}

// Does a record look like a status-summary request?
function isStatusSummaryRequest(r) {
  return statusSummaryPrompt(r) !== null;
}

// The caption the model produced, plus the previous one the prompt told it not to
// repeat ('Previous: "…" — say something NEW.'). Either may be null.
function parseStatusSummary(r) {
  const prev = /^Previous:\s*"([\s\S]*?)"/m.exec(statusSummaryPrompt(r) || "");
  const summary = extractText((r.response || {}).content);
  return { summary: summary ? summary.trim() : null, previous: prev ? prev[1] : null };
}

// Last match of a global regex in text, or null. Resets lastIndex so the same
// regex object can be reused across calls.
function lastMatch(re, text) {
  re.lastIndex = 0;
  let found = null;
  let m;
  while ((m = re.exec(text)) !== null) found = m;
  return found;
}

// Partition the time-ordered records into the main stream and per-agent-id
// subagent streams (ordered by first appearance).
function groupBySubagent(reqs, subagentCallMap) {
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
      // Match subagent to its parent tool call
      if (subagentCallMap) {
        const msgs = r.messages || [];
        if (msgs.length) {
          const key = normalizePrompt(msgs[0].content);
          const match = subagentCallMap[key];
          if (match) {
            g.subagent_type = match.subagent_type;
            g.description = match.description;
          }
        }
      }
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
  if (g.subagent_type && g.description) {
    return `${g.subagent_type}: ${g.description} ${cnt}`;
  }
  return g.snippet ? `⌁ ${g.short} ${cnt} · "${g.snippet}…"` : `⌁ ${g.short} ${cnt}`;
}

function renderTabs(groups) {
  const total = state.reqs.length;
  const bar = el("div", "tabs");
  const tab = (key, label, n) => {
    const t = el("div", "tab" + (state.tab === key ? " active" : ""));
    t.appendChild(el("span", null, label));
    if (n != null) t.appendChild(el("span", "n", `(${n})`));
    t.onclick = () => { applyTabFilter(key); renderChatHead(); };
    return t;
  };

  bar.appendChild(tab("main", "Main agent", groups.main.length));

  if (groups.subs.length === 0) {
    return bar;
  }

  if (groups.subs.length === 1) {
    const g = groups.subs[0];
    bar.appendChild(tab("all", "All", total));
    bar.appendChild(tab("sub:" + g.id, subLabel(g)));
    return bar;
  }

  // 2+ subagents: dropdown selector
  bar.appendChild(tab("all", "All", total));
  bar.appendChild(renderSubagentDropdown(groups));
  return bar;
}

function renderSubagentDropdown(groups) {
  const wrap = el("div", "tab-dropdown");

  // Trigger tab — same .tab class as other tabs
  const activeSub = groups.subs.find(function(g) { return state.tab === "sub:" + g.id; });
  const trigger = el("div", "tab");
  trigger.textContent = activeSub ? subLabel(activeSub) : "Select subagent (" + groups.subs.length + ")";
  trigger.appendChild(el("span", "tab-arrow", " ▾"));

  // Menu
  const menu = el("div", "tab-dropdown-menu");
  for (let i = 0; i < groups.subs.length; i++) {
    const g = groups.subs[i];
    const item = el("div", "tab-dropdown-item");
    item.textContent = subLabel(g);
    if (state.tab === "sub:" + g.id) item.classList.add("active");
    item.onclick = (function(id) {
      return function(e) {
        e.stopPropagation();
        applyTabFilter("sub:" + id);
        renderChatHead();
        menu.style.display = "none";
      };
    })(g.id);
    menu.appendChild(item);
  }

  trigger.onclick = function(e) {
    e.stopPropagation();
    if (menu.style.display === "block") {
      menu.style.display = "none";
    } else {
      menu.style.display = "block";
      setTimeout(function() {
        document.addEventListener("click", function close(e) {
          if (!wrap.contains(e.target)) {
            menu.style.display = "none";
            document.removeEventListener("click", close);
          }
        });
      }, 0);
    }
  };

  wrap.appendChild(trigger);
  wrap.appendChild(menu);
  return wrap;
}

// Render the main stream with clickable "started"/"finished" markers spliced
// --- Marker management -----------------------------------------------------------

function createMarker(g, kind, ts) {
  const done = kind !== "started";
  const verb = kind === "started" ? "started"
             : (g.lastTerminal ? "finished" : "last seen");
  const m = el("div", "marker" + (done ? " done" : ""));
  m.dataset.role = "marker";
  m.dataset.sub = g.id;
  if (g.subagent_type && g.description) {
    m.appendChild(el("span", null, `${g.subagent_type}: ${g.description} ${verb}`));
  } else {
    m.appendChild(el("span", null, `⌁ Subagent ${g.ordinal} (${g.short}) ${verb}`));
  }
  m.appendChild(el("span", "when", fmtTs(ts)));
  m.onclick = () => { applyTabFilter("sub:" + g.id); renderChatHead(); };
  return m;
}

function removeMarkers() {
  chatBody().querySelectorAll(':scope > [data-role="marker"]').forEach(m => m.remove());
}

function insertMarkers(groups) {
  if (!groups || !groups.subs.length) return;
  const body = chatBody();
  const turns = body.querySelectorAll(':scope > .turn');
  const placements = [];
  for (const g of groups.subs) {
    placements.push({ idx: g.firstIdx, marker: createMarker(g, "started", recStart(state.reqs[g.firstIdx])) });
    placements.push({ idx: g.lastIdx, marker: createMarker(g, "finished", recStart(state.reqs[g.lastIdx])) });
  }
  // Insert descending so earlier insertBefore calls don't shift later reference positions.
  placements.sort((a, b) => b.idx - a.idx);
  for (const p of placements) {
    const ref = turns[p.idx];
    if (ref && ref.parentNode === body) body.insertBefore(p.marker, ref);
    else body.appendChild(p.marker);
  }
}

// --- Tab visibility filter -------------------------------------------------------

function applyTabFilter(tab) {
  // Capture whether user was at the bottom before changing the view.
  const body = chatBody();
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;

  // Remove previous sub-tab visibility classes (only matches elements that
  // have it — typically few, from the previous sub-tab view).
  body.querySelectorAll('.tab-visible').forEach(el => el.classList.remove('tab-visible'));

  // Set data-tab on #chat — CSS handles everything for "all" and "main"
  document.getElementById("chat").dataset.tab = tab;

  // For sub tabs (few elements), mark matching elements as visible
  if (tab.startsWith("sub:")) {
    const id = tab.slice(4);
    body.querySelectorAll(`:scope > [data-role="sub"][data-sub="${id}"]`).forEach(el => {
      el.classList.add('tab-visible');
    });
  }

  state.tab = tab;
  ensureScrollButton();

  // If user was at the bottom of the previous view, scroll to the new bottom.
  if (atBottom) {
    scrollToBottom();
  }
}

function renderChatHead() {
  const head = chatHead();
  head.innerHTML = "";
  const s = state.session_meta;
  if (!s) return;

  const hash = (s.session_id || "").slice(0, 8);

  // Line 1: title — prefer the parsed title, fall back to alias or hash
  const title = s.title || s.alias || hash;
  head.appendChild(el("h2", "chat-title", title));

  // Line 2: alias · hash · reqs · cache · cost (partial during streaming)
  {
    const parts = [];
    if (s.alias) parts.push(s.alias);
    parts.push(hash);
    if (state.reqs.length) {
      const stats = sessionStats(state.reqs);
      parts.push(state.reqs.length + " req");
      if (stats.cacheRate != null) parts.push(stats.cacheRate + "% cached");
      parts.push("Cost: " + fmtCost(stats.cost));
    } else if (s.count != null) {
      parts.push(s.count + " req");
    }
    if (s.providers && s.providers.length) {
      parts.push(s.providers.join(", "));
    }
    const statsEl = el("div", "chat-stats");
    statsEl.textContent = parts.join(" · ");
    head.appendChild(statsEl);
  }

  // Line 3: tabs (only once groups are known; spinner while streaming)
  if (state.groups && state.reqs.length) {
    head.appendChild(renderTabs(state.groups));
  } else if (state.reqs.length > 0 || (s.count && s.count > 0)) {
    const bar = el("div", "tabs");
    const spinner = el("div", "tab loading");
    spinner.textContent = "Loading…";
    bar.appendChild(spinner);
    head.appendChild(bar);
  }
}

function renderStream() {
  const body = chatBody();
  body.innerHTML = "";
  renderChatHead();

  if (!state.reqs.length) {
    body.appendChild(el("div", "hint", "No requests in this session."));
    ensureScrollButton();
    return;
  }

  state.reqs.forEach((r, i) => body.appendChild(renderTurn(r, i + 1)));
  if (state.groups) insertMarkers(state.groups);
  applyTabFilter(state.tab);
  ensureScrollButton();
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
  if (e.key === "Home") target.scrollTo({ top: 0, behavior: "smooth" });
  else scrollToBottom();
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

function scrollToBottom() {
  chatBody().scrollTo({ top: chatBody().scrollHeight, behavior: "smooth" });
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
  btnBot.onclick = () => { scrollToBottom(); };
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
