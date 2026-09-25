// The room, as a plain chat: what was said, in the order it was said, and
// everything the assistant did between the lines folded into one small line each,
// sitting where it happened. Two tabs -- the chat, and a developer view of
// everything underneath it.

const $ = (id) => document.getElementById(id);
const MAIN = $("main");

let state = null;      // /api/state -- rows, events, its prompt, the contract
let progress = null;   // /api/progress -- the turn as it happens, and who is out
let plans = null;      // /api/projects -- their projects, notes and tasks
// What has just been said and the room has not handed back yet. A list, not
// one line: a second thing can be said while the assistant is still thinking
// about the first, and both should be on screen. Each is {text, at}.
let echoes = [];
let failed = false;    // the send itself broke
let watching = false;  // a turn is running and we are following it
let watchedTurn = null; // which turn the preview on screen belongs to
let turnStart = 0;
let poller = null;
let devTimer = null;

// Everything the reader has opened, by key, so a re-render never folds it back up.
// The two sections of the Developer tab that answer the everyday questions
// start open; the rest wait to be asked.
const OPEN = new Set(["dev:memory"]);

// --- who is writing --------------------------------------------------------
// The door says who came IN -- loopback is the owner, a key is a paired
// phone. This is who is WRITING: people who share one screen switch between
// themselves freely -- attribution, not a lock. The choice lives on the
// device; until a hand touches it, the device follows whoever the door said.
// Ids stay short, people are called by their names.
// Whose room this is: the room writes it into the page when it serves it.
const ASSISTANT = window.ASSISTANT || { name: "Assistant", self: "assistant",
  owner: "owner", people: { owner: { called: "Owner" } } };
const ME = ASSISTANT.self;             // the kind my own lines are stored under
const ME_INTERIM = ME + "_interim";
const MY_NAME = ASSISTANT.name;
const OWNER = ASSISTANT.owner;
const PEOPLE = Object.fromEntries(Object.entries(ASSISTANT.people)
  .map(([id, p]) => [id, p.called || id]));
// The door a labelled line came through, said after the name. Only the
// doors the page knows, so nothing in a row can write itself into the
// label; the desk says nothing, because the desk is the ordinary case.
const VIA_SAID = Object.fromEntries(Object.entries(ASSISTANT.people)
  .map(([id, p]) => [id + "'s phone", "from " + (p.possessive || "their") + " phone"]));
let person = null;
try { person = localStorage.getItem("assistant:person"); } catch (e) {}
if (person && !PEOPLE[person]) person = null;

function whoNow() {
  return person || (state && state.who) || OWNER;
}

// The class a labelled row wears. Only ids the page knows, so nothing that
// arrives in a row can write itself into a class attribute. `not-owner` is
// what the stylesheet colours by: it cannot know a home's ids.
function whoClass(w) {
  return w && w !== OWNER && PEOPLE[w] ? " " + w + " not-owner" : "";
}

// --- plain, the workings, and the gauges -----------------------------------
// Plain is the default and, for a person who has never chosen, the answer.
// It is just their own room -- the way a chat looks. Detailed is the same
// page with the assistant's workings drawn back in; the gauges are their own
// switch, because a desk wants the instruments on and hidden is the right
// phone default. Both choices are remembered PER PERSON on this device: when
// another person picks their name, the page turns to how they like it, and
// the last person's choice stays theirs.
let PLAIN = true;
let GAUGES = false;

function loadPrefs() {
  let view = null, gauges = null;
  try {
    view = localStorage.getItem("assistant:view:" + whoNow())
      // The key from before choices were per person, read as the owner's:
      // it was written when there was only one person's choice to keep.
      || (whoNow() === OWNER ? localStorage.getItem("assistant:view") : null);
    gauges = localStorage.getItem("assistant:gauges:" + whoNow());
  } catch (e) {}
  PLAIN = view !== "detailed";
  GAUGES = gauges === "on";
}

function applyView() {
  document.body.classList.toggle("plain", PLAIN);
  document.body.classList.toggle("detailed", !PLAIN);
  document.body.classList.toggle("gauges", GAUGES);
  const b = $("workings");
  if (b) {
    b.textContent = PLAIN ? "show workings" : "hide workings";
    b.title = PLAIN ? "what " + MY_NAME + " looked at, the essences, the small lines"
                    : "back to just the messages";
  }
  const g = $("gauges");
  if (g) {
    g.textContent = GAUGES ? "hide gauges" : "show gauges";
    g.title = GAUGES ? "put the instruments away"
                     : "the model, the plan's windows, the automatic memory";
  }
}
loadPrefs();
applyView();

// --- small helpers ---------------------------------------------------------

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// The assistant's replies carry a little markdown: bold, italic and code. Escaped first,
// so the marks can never be markup.
//
// And the sounds its voice makes where one is written, "(laugh)", drawn the way
// the mood under a line is: part of what was said, set apart from the words.
function md(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/\((?:laugh|sigh|cough|clears throat)\)/gi, '<span class="sfx">$&</span>');
}

const MONTH_SAID = {
  en: ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
  bg: ["ян.", "фев.", "март", "апр.",
       "май", "юни", "юли", "авг.",
       "септ.", "окт.", "ноем.",
       "дек."],
};
// A date is furniture, not somebody's words, so it answers to the reader's
// language like the rest of the frame does. `langNow` is defined further
// down; this is only ever called from a render, long after everything is in
// place.
const MONTH = new Proxy({}, {
  get: (_, i) => (MONTH_SAID[typeof langNow === "function" ? langNow() : "en"]
                  || MONTH_SAID.en)[i],
});
const DAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const p2 = (n) => String(n).padStart(2, "0");

function clockOnly(d) {
  d = d instanceof Date ? d : new Date(d);
  return isNaN(d) ? "" : `${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

function shortWhen(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return String(iso || "");
  return `${d.getDate()} ${MONTH[d.getMonth()]} ${clockOnly(d)}`;
}

// Today's rows get a clock; older ones get the day as well.
function when(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? clockOnly(d) : shortWhen(d);
}

function k(n) {
  n = Number(n) || 0;
  return n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
}

function ago(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (isNaN(s)) return "";
  if (s < 60) return Math.round(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + p2(Math.round(s % 60)) + "s";
  return Math.floor(s / 3600) + "h " + p2(Math.floor((s % 3600) / 60)) + "m";
}

function firstLine(text, n = 90) {
  const one = String(text || "").replace(/\s+/g, " ").trim();
  return one.length > n ? one.slice(0, n) + "…" : one;
}

function stripFrontmatter(t) {
  const s = (t || "").replace(/^\s+/, "");
  if (!s.startsWith("---")) return s;
  const end = s.indexOf("---", 3);
  return end === -1 ? s : s.slice(end + 3).trim();
}

// A small block-level markdown renderer, for the Spark and the instructions.
// `frontmatter` is on for those, because they open with a real one. It comes
// off for anything a person typed into a box: a note that happens to start
// with a line of dashes must not have its words eaten as a header block.
function mdBlock(text, frontmatter = true) {
  const inline = (t) => t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
  const src = esc(frontmatter ? stripFrontmatter(text || "") : String(text ?? ""));
  const out = [];
  let list = false, para = [];
  const flushPara = () => {
    if (para.length) { out.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; }
  };
  const closeList = () => { if (list) { out.push("</ul>"); list = false; } };
  for (const raw of src.split("\n")) {
    const line = raw.trim();
    let m;
    if (!line) { flushPara(); closeList(); continue; }
    if (/^-{3,}$/.test(line)) { flushPara(); closeList(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara(); closeList();
      out.push("<h4>" + inline(m[2]) + "</h4>");
    } else if ((m = line.match(/^[-*]\s+(.*)$/))) {
      flushPara();
      if (!list) { out.push("<ul>"); list = true; }
      out.push("<li>" + inline(m[1]) + "</li>");
    } else if ((m = line.match(/^(\d+)\.\s+(.*)$/))) {
      flushPara();
      if (!list) { out.push("<ul>"); list = true; }
      out.push("<li><span class=\"num\">" + m[1] + "</span> " + inline(m[2]) + "</li>");
    } else if (list && /^\s/.test(raw)) {
      out[out.length - 1] = out[out.length - 1].replace(/<\/li>$/, " " + inline(line) + "</li>");
    } else {
      closeList();
      para.push(line);
    }
  }
  flushPara(); closeList();
  return `<div class="prose">${out.join("")}</div>`;
}

async function getJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  return r.json();
}

// --- what the room is holding in memory -----------------------------------
//
// The server carries the working set whole every time and the newest stretch
// of the past behind it; everything older is asked for a page at a time and
// kept here. So `state.rows` is what this window has *ever* been given rather
// than what the last answer happened to carry -- otherwise every turn would
// throw away the scrolling back the reader had already done.

const HELD = { rows: new Map(), events: new Map(), trails: {} };
let oldest = null;     // the floor of the past we hold; where the next page starts
let older = 0;         // rows out of memory below it, per the server

function shelve(page) {
  for (const r of page.rows || []) HELD.rows.set(r.id, r);
  for (const e of page.events || []) HELD.events.set(e.id, e);
  Object.assign(HELD.trails, page.trails || {});
}

// The keys the chat draws from, rebuilt off everything held. Sorting by id is
// what puts a page fetched second in the place it was said. The two bounds are
// stamped on as well, so nothing can read a stale floor off the last answer
// and believe it over what the room actually holds.
function laidOut(into) {
  into.rows = [...HELD.rows.values()].sort((a, b) => a.id - b.id);
  into.events = [...HELD.events.values()].sort((a, b) => a.id - b.id);
  into.trails = HELD.trails;
  into.oldest = oldest;
  into.older = older;
  return into;
}

// Whose hands are on the device. The pick at the top wins when it has been
// touched; otherwise the door's word stands. The body carries the answer,
// so the whole chat's accent is one CSS rule -- and the pick says it in
// letters as well as colour. When the person changes, their own
// remembered view comes with them.
let prefWho = null;
function applyWho() {
  const w = whoNow();
  document.body.dataset.who = w;
  document.body.classList.toggle("not-owner", w !== OWNER);
  const sel = $("person");
  if (sel) sel.value = w;
  if (w !== prefWho) {
    prefWho = w;
    loadPrefs();
    applyView();
  }
}

// Which plain views a said line belongs to -- the page's mirror of the
// server's rooms_of, one rule in two places that must never disagree. A
// user line is its speaker's; a line of the assistant's wears the room it
// answered; absence is the history from before the labels, which is the
// owner's. Anyone else's room is a conversation and nothing else: the house
// lines, the night, everything with machinery in it draws in the owner's
// view alone. Essences are never filtered -- their label sorts the
// shelf and is not a permission.
function roomsOf(r) {
  const m = r.meta || {};
  if (r.kind === "user") return [m.who || OWNER];
  if (r.kind === ME || r.kind === ME_INTERIM || r.kind === "lost") {
    const room = m.room;
    if (Array.isArray(room)) return room.length ? room : [OWNER];
    return [room && room !== "house" ? room : OWNER];
  }
  return [OWNER];
}

function absorbState(fresh) {
  // Every row the assistant is holding comes with every state, so one held here as
  // loaded that is missing from a fresh one has been put out of memory since
  // -- there is no other way for it to be absent. Marking it says so at once
  // rather than leaving it sitting in memory on screen until a reload.
  // An echo is a person's line drawn before the room had written it down. The row
  // that comes back carries exactly the text that was posted -- the server
  // strips it, and so does send() before echoing -- so the match is on the
  // text, oldest echo first. Two identical lines make two echoes and two
  // rows, and each row clears one of them, in order. This runs on EVERY
  // state that arrives, not just on send()'s own answer, because the 2s
  // poller can bring the row back before the POST has returned -- which is
  // how the same line ended up drawn twice.
  for (const r of fresh.rows || []) {
    if (r.kind !== "user") continue;
    const i = echoes.findIndex((e) => e.text === r.text);
    if (i >= 0) echoes.splice(i, 1);
  }
  const here = new Set((fresh.rows || []).map((r) => r.id));
  for (const r of HELD.rows.values()) {
    if (r.loaded && !here.has(r.id)) r.loaded = false;
  }
  shelve(fresh);
  // A page fetched earlier reaches further back than a fresh state does, and
  // it is still held, so the floor only ever moves down.
  if (oldest === null || (fresh.oldest !== null && fresh.oldest < oldest)) {
    oldest = fresh.oldest;
    older = fresh.older || 0;
  }
  state = laidOut(fresh);
  applyWallpaper(state.wallpaper);
  return state;
}

// One page further back. Returns how many rows it actually brought.
async function pageBack(limit) {
  if (oldest === null || older <= 0) return 0;
  const page = await getJson(`api/rows?before=${oldest}&limit=${limit || 120}`);
  shelve(page);
  oldest = page.oldest;
  older = page.older || 0;
  if (state) laidOut(state);
  return (page.rows || []).length;
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "something broke");
  return data;
}

// --- what a step or an event found, as plain text --------------------------

function spanText(c) {
  if (!c) return "no said rows underneath";
  const a = shortWhen(c.from), b = shortWhen(c.to);
  return `${c.rows} row${c.rows === 1 ? "" : "s"}, ${a === b ? a : a + " → " + b}`;
}

function trailHtml(t) {
  if (!t) return "";
  if (!t.ids || !t.ids.length) {
    return `<div class="trail">written from the live conversation, over no rows</div>`;
  }
  const bits = ["stands for " + t.ids.map((i) => "#" + i).join(", "), spanText(t.covers)];
  if (t.through_essences && t.through_essences.length) {
    bits.push("through " + t.through_essences.map((i) => "#" + i).join(", "));
  }
  const rows = t.rows || t.ids || [];
  const reach = rows.length
    ? ` <button class="small-btn reload" data-id="${rows.join(",")}">bring its rows back into memory</button>`
    : "";
  const broken = t.missing && t.missing.length
    ? ` <span class="broken">cannot be found: ${t.missing.map((i) => "#" + i).join(", ")}</span>`
    : "";
  return `<div class="trail">${esc(bits.join("  ·  "))}${reach}${broken}</div>`;
}

function searchText(s) {
  const a = s.asked || {};
  const lines = ["looked for: " + (a.restatement || "(nothing said in words)")];
  if ((a.keywords || []).length) lines.push("keywords: " + a.keywords.join(", "));
  if (a.from || a.to) {
    lines.push("written between " + (a.from || "the beginning") + " and " + (a.to || "now"));
  }
  lines.push(s.summary);
  for (const h of s.hits || []) {
    lines.push("   " + (h.score === null ? "  —  " : h.score.toFixed(3)) +
               "  #" + h.id + "  " + (h.title || "(untitled)") + "  [" + h.matched + "]");
  }
  return lines.concat(s.notes || [], s.problems || []).join("\n");
}

function fetchText(d) {
  const line = (label, ids) =>
    ids && ids.length ? label + ": " + ids.map((i) => "#" + i).join(", ") + "\n" : "";
  return "asked for: " + (d.asked || []).join("; ") + "\n" +
    line("brought back", d.brought_back) +
    line("already in hand", d.already_here) +
    line("not in the store", d.not_found) +
    line("held back by the bound", d.held_back_by_the_bound) +
    line("followed through", d.through_essences) +
    (d.notes || []).concat(d.problems || []).map((s) => "\n" + s).join("");
}

// How a run went: the steps it took, what it cost, what went wrong.
function runText(d) {
  if (!d) return "";
  const head = [];
  if (d.ended && d.ended !== "completed") head.push("Ended: " + d.ended);
  if (d.woke_us === false && d.spent) head.push("It never knocked.");
  const steps = (d.steps || []).map((s) =>
    "  " + String(s.at).padStart(5) + "s  " + s.tool + "  " + (s.input || ""));
  const cost = [];
  if (d.cost_usd) cost.push("$" + d.cost_usd);
  if (d.turns) cost.push(d.turns + " turns");
  if (d.seconds) cost.push(d.seconds + "s");
  if (d.model) cost.push(d.model);
  return [head.join("\n"), steps.join("\n"), cost.join("  ·  "),
          (d.problems || []).join("\n\n")].filter(Boolean).join("\n\n");
}

function workerText(d) {
  if (!d) return "";
  return [d.brief ? "Asked: " + d.brief : "", runText(d)].filter(Boolean).join("\n\n");
}

function shelfText(d) {
  const flags = (l) => [l.retired ? "retired" : "", l.in_hand ? "in hand" : "",
                        l.vector ? l.vector.toUpperCase() : ""].filter(Boolean).join(", ");
  return (d.listed || []).map((l) =>
    "#" + l.id + "  " + (l.title || "(untitled)") + "   " + shortWhen(l.date) +
    "  ~" + l.tokens_est + (flags(l) ? "   [" + flags(l) + "]" : "")).join("\n") +
    (d.notes || []).concat(d.problems || []).map((x) => "\n\n" + x).join("");
}

function fileOpText(g) {
  if (!g) return "";
  if (g.refused) return "✗ " + g.refused;
  const notes = (g.notes || []).map((n) => "\n  — " + n).join("");
  if (g.op === "list") {
    return g.summary + "\n" + (g.entries || []).map((e) =>
      "  " + e.name.padEnd(32) + (e.bytes === null ? "" : String(e.bytes).padStart(9)) +
      "  " + e.changed).join("\n") + notes;
  }
  if (g.op === "read") {
    const lines = (g.text || "").split("\n").map((t, i) =>
      String(g.from_line + i).padStart(5) + "  " + t).join("\n");
    return g.summary + "\n" + lines + notes;
  }
  if (g.op === "find") {
    return g.summary + "\n" + (g.hits || []).map((h) =>
      "  " + h.path + ":" + h.line + ":  " + h.text).join("\n") + notes;
  }
  return (g.summary || "") + notes;
}

function filesText(d) {
  return (d.ran || [d]).map(fileOpText).join("\n\n")
    + (d.roots ? "\n\nshe may look in: " + d.roots.join("; ") : "");
}

function webOpText(g) {
  if (!g) return "";
  if (g.refused) return "✗ " + g.refused;
  const notes = (g.notes || []).map((n) => "\n  — " + n).join("");
  if (g.op === "search") {
    const hits = (g.results || []).map((r) =>
      "  " + (r.title || "(no title)") + "\n    " + (r.url || "") +
      (r.quoted ? "\n" + r.quoted : "")).join("\n\n");
    return g.summary + "\n\n" + (g.quoted_answer || "") + "\n\n" + hits + notes;
  }
  return g.summary + "\n" + (g.url || "") + "\n\n" + (g.quoted || "") + notes;
}

function webText(d) {
  return (d.ran || [d]).map(webOpText).join("\n\n") + (d.trust ? "\n\n" + d.trust : "");
}

function nativeText(d) {
  const lines = [];
  if (d.cwd) lines.push("Working directory: " + d.cwd);
  if (d.command) lines.push("Command:\n" + ((d.commandActions || []).map(a => a.command).filter(Boolean).join("\n") || d.command));
  if (d.status) lines.push("Status: " + d.status);
  if (d.exitCode != null) lines.push("Exit code: " + d.exitCode);
  if (d.aggregatedOutput != null) lines.push("Output:\n" + d.aggregatedOutput);
  if (d.stdout != null) lines.push("Standard output:\n" + d.stdout);
  if (d.stderr != null) lines.push("Standard error:\n" + d.stderr);
  if (d.delta) lines.push(d.delta);
  for (const change of d.changes || []) lines.push(change.path + "\n" + (change.diff || ""));
  if (d.diff) lines.push("Diff:\n" + d.diff);
  if (d.text) {
    let text = d.text;
    try { const parsed = JSON.parse(text); if (typeof parsed.reply === "string") text = parsed.reply; } catch (_) {}
    lines.push(text);
  }
  if (d.log) lines.push("Saved log: " + d.log);
  if (d.error) lines.push("Error: " + (typeof d.error === "string" ? d.error : JSON.stringify(d.error)));
  if (d.note) lines.push(d.note);
  return lines.join("\n\n") || JSON.stringify(d, null, 2);
}

function eventDetail(e) {
  const d = e.detail;
  if (!d) return "";
  if (e.kind === "native") return nativeText(d);
  if (e.kind === "essence" || e.kind === "essence-remove") return d.text || "";
  if (e.kind === "essence-rename") {
    return (d.was ? "was: " + d.was + "\n" : "had no name\n") + "now: " + d.now;
  }
  if (e.kind === "essence-edit") return "Was:\n\n" + d.was + "\n\nNow:\n\n" + d.now;
  if (e.kind === "drop") {
    return (d.rows || []).map((r) => "#" + r.id + "  " + (r.text || "")).join("\n\n");
  }
  if (e.kind === "search") {
    return (d.searches || []).map(searchText).concat(d.problems || []).join("\n\n");
  }
  if (e.kind === "shelf") return shelfText(d);
  if (e.kind === "files") return filesText(d);
  if (e.kind === "web") return webText(d);
  if (e.kind === "fetch") return fetchText(d);
  if (e.kind === "worker") return workerText(d);
  if (e.kind === "recall") return recallText(d);
  if (e.kind === "digest") return digestText(d);
  if (e.kind === "sent") {
    return [d.why ? "why: " + d.why : "", d.brief || ""].filter(Boolean).join("\n\n");
  }
  if (e.kind === "spark") return JSON.stringify(d, null, 2);
  if (e.kind === "notebook") {
    return ((d.lines && d.lines.length) ? d.lines : (d.problems || [])).join("\n");
  }
  if (e.kind === "dream") {
    const bits = [];
    if (d.report) bits.push(MY_NAME.toUpperCase() + "'S ACCOUNT OF THE NIGHT\n\n" + d.report);
    if (d.sentence) bits.push("for the room: " + d.sentence);
    if ((d.refused || []).length) {
      bits.push("THE DOOR REFUSED\n" + d.refused.map(
        (r) => "· " + r.op + ": " + r.why).join("\n"));
    }
    if (d.read && d.read.length) bits.push("read whole: " + d.read.join(", "));
    if (d.spent) bits.push("cost: " + d.rounds + " read round(s), ~"
      + (d.spent.input || 0) + " in / ~" + (d.spent.output || 0) + " out, $"
      + Number(d.spent.cost || 0).toFixed(2));
    return bits.join("\n\n") || JSON.stringify(d, null, 2);
  }
  return JSON.stringify(d, null, 2);
}

// What a live step found, in the same words the kept event uses.
function stepText(s) {
  const d = s.detail || {};
  if (s.kind === "native") return nativeText(d);
  if (s.kind === "search") return searchText(d);
  if (s.kind === "shelf") return shelfText(d);
  if (s.kind === "reach") return fetchText(d);
  if (s.kind === "worker") return workerText(d);
  if (s.kind === "files") return fileOpText(d);
  if (s.kind === "web") return webOpText(d);
  if (s.kind === "recall") return recallText(d);
  if (s.kind === "digest") return digestText(d);
  return JSON.stringify(d, null, 2);
}

// The automatic memory, drawn from the very block the assistant was handed:
// what the small model asked for, what came back with its likeness score, and
// the reviewer's note and want on each. The text form and the html form carry
// the same content; the raw block is one fold further down.
const RECALL_MORE = new Map();    // fold key -> {skip, total, results, error}
const RECALL_ASKED = new Map();   // fold key -> {asked, shown: [ids]}

function recallTook(d) {
  if (d.took_s == null) return "";
  const parts = [];
  if (d.write_s != null) parts.push("write " + Number(d.write_s).toFixed(1));
  if (d.search_s != null) parts.push("search " + Number(d.search_s).toFixed(1));
  if (d.review_s != null) parts.push("review " + Number(d.review_s).toFixed(1));
  return Number(d.took_s).toFixed(1) + " s" + (parts.length ? " (" + parts.join(" \u00b7 ") + ")" : "");
}

function recallHead(d) {
  const n = (k) => k + " line" + (k === 1 ? "" : "s");
  return [d.model, d.read_lines ? "writer read the last " + n(d.read_lines) : "",
          d.review_lines ? "reviewer read the last " + n(d.review_lines) : "",
          d.reviewed_against ? "reviewed against " + d.reviewed_against
            : d.typed && (d.results || []).length ? "not reviewed (the reviewer box was unticked)" : "",
          recallTook(d)].filter(Boolean).join(" \u00b7 ");
}

// Which suggestions the assistant then pulled in, from the card's `after`,
// written once its turn was over.
let LOADED_ESS = new Set();

function useOf(d, r) {
  const how = pulledOf(d, r);
  if (how) return MY_NAME + " pulled it \u00b7 " + how;
  if (LOADED_ESS.has(r.id)) return "in " + MY_NAME + "'s memory now";
  return null;
}

function recallBadge(d) {
  if (!d || !d.asked || !Array.isArray(d.results)) return "";
  const used = d.results.map((r) => [r, useOf(d, r)]).filter(([, u]) => u);
  const title = used.length
    ? used.map(([r, u]) => "#" + r.id + " \u2014 " + u).join("\n")
    : "none of these suggestions was pulled, and none is in " + MY_NAME + "'s memory now";
  return `<span class="rc-count${used.length ? " hot" : ""}" title="${esc(title)}">${used.length}</span>`;
}

function pulledOf(d, r) {
  const p = d.after && d.after.pulled;
  return p ? p[String(r.id)] || null : null;
}

function recallKeywords(d) {
  const a = d.asked || {}, counts = d.keyword_counts || {};
  return (a.keywords || []).map((k) => {
    const c = counts[k];
    if (!c) return k;
    return k + " (in " + c.in + (c.too_common ? ", too common, not used" : "") + ")";
  });
}

function recallDates(a) {
  if (!a.from && !a.to) return "any";
  return (a.from || "the beginning") + " \u2192 " + (a.to || "now");
}

function recallFacts(r) {
  return [r.date, r.score != null ? "likeness " + Number(r.score).toFixed(3) : "",
    (r.keywords_in_it || []).length ? "has: " + r.keywords_in_it.join(", ") : "",
    r.in_hand ? "already in hand" : ""].filter(Boolean).join(" \u00b7 ");
}

function recallText(d) {
  const out = [recallHead(d)];
  const a = d.asked;
  if (a) {
    out.push("", "It searched for:",
      "  essence: " + (a.essence || "(none)"),
      "  keywords: " + (recallKeywords(d).join(", ") || "(none)"),
      "  dates: " + recallDates(a));
  }
  const res = d.results || [];
  if (res.length) {
    out.push("", "What came back" + (d.searched != null ? " (of " + d.searched + " essences)" : "") + ":");
    for (const r of res) {
      if (r.found_by === "keyword" && r === res.find((x) => x.found_by === "keyword")) {
        out.push("", "Found by keyword, not already above:");
      }
      const how = pulledOf(d, r);
      out.push("  " + (r.want != null ? r.want + "%  " : "") + "#" + r.id + "  " + r.name +
               (how ? "   [" + MY_NAME + " pulled it, " + how + "]" : ""),
               "      " + recallFacts(r));
      if (r.note) out.push("      " + r.note);
    }
  } else if (a) out.push("", "Nothing came back.");
  if (d.after) {
    const others = d.after.pulled_not_suggested || [];
    if (!Object.keys(d.after.pulled || {}).length) out.push("", MY_NAME + " pulled none of these.");
    if (others.length) out.push("", MY_NAME + " also pulled, not suggested: " +
      others.map((o) => "#" + o.id + " " + o.name + " (" + o.how + ")").join("; "));
  }
  for (const n of d.notes || []) out.push("note: " + n);
  if (d.missed) out.push("missed: " + d.missed);
  return out.join("\n");
}

function wantClass(w) { return w == null ? "none" : w >= 70 ? "hi" : w >= 30 ? "mid" : "lo"; }

function recallRowHtml(r, reviewed, pulled) {
  const want = reviewed && r.want !== undefined
    ? `<span class="rc-want ${wantClass(r.want)}" title="how much the reviewer thinks ${esc(MY_NAME)} would want to read it">${r.want == null ? "\u2013" : r.want + "%"}</span>`
    : `<span class="rc-want none"></span>`;
  return `<div class="rc-row">${want}<div class="rc-main">` +
    `<div class="rc-name"><span class="rc-id">#${esc(r.id)}</span> ${esc(r.name)}` +
    (pulled ? ` <span class="rc-pulled">${esc(pulled)}</span>` : "") + `</div>` +
    `<div class="rc-facts">${esc(recallFacts(r))}</div>` +
    (r.note ? `<div class="rc-note">${esc(r.note)}</div>` : "") + `</div></div>`;
}

function recallHtml(d, key) {
  if (!d.asked && d.query !== undefined) {
    // A run of the earlier, more complicated automatic memory, kept as it was.
    return `<div class="act-body rc"><div class="rc-facts">an earlier version of the automatic memory</div>` +
      (d.query ? `<div class="rc-asked"><div><b>searched for</b> “${esc(d.query)}”</div></div>` : "") +
      (d.missed ? `<div class="bad">missed: ${esc(d.missed)}</div>` : "") +
      `<pre>${esc(JSON.stringify(d, null, 2))}</pre></div>`;
  }
  const a = d.asked;
  const res = d.results || [];
  let h = `<div class="act-body rc"><div class="rc-facts">${esc(recallHead(d))}</div>`;
  if (a) {
    h += `<div class="cap">It searched for</div><div class="rc-asked">` +
      `<div><b>essence</b> \u201c${esc(a.essence || "")}\u201d</div>` +
      `<div><b>keywords</b> ${esc(recallKeywords(d).join(", ") || "none")}</div>` +
      `<div><b>dates</b> ${esc(recallDates(a))}</div></div>`;
    h += `<div class="cap">What came back${d.searched != null ? " \u2014 of " + d.searched + " essences" : ""}</div>`;
  }
  const byKw = res.filter((r) => r.found_by === "keyword");
  h += res.filter((r) => r.found_by !== "keyword").map((r) => recallRowHtml(r, true, useOf(d, r))).join("");
  if (byKw.length) {
    h += `<div class="cap">Found by keyword, not already above</div>` +
      byKw.map((r) => recallRowHtml(r, true, useOf(d, r))).join("");
  }
  if (d.after) {
    const others = d.after.pulled_not_suggested || [];
    h += `<div class="cap">What ${esc(MY_NAME)} did with it</div>`;
    if (!Object.keys(d.after.pulled || {}).length) {
      h += `<div class="rc-facts">${esc(MY_NAME)} pulled none of these</div>`;
    }
    if (others.length) {
      h += `<div class="rc-facts">${esc(MY_NAME)} also pulled, not suggested:</div>` +
        others.map((o) => recallRowHtml({ id: o.id, name: o.name, date: o.date, keywords_in_it: [] },
                                        false, MY_NAME + " pulled it \u00b7 " + o.how)).join("");
    } else if (Object.keys(d.after.pulled || {}).length) {
      h += `<div class="rc-facts">nothing else pulled</div>`;
    }
  }
  if (a && !res.length) h += `<div class="rc-facts">nothing came back</div>`;
  for (const n of d.notes || []) h += `<div class="rc-facts">note: ${esc(n)}</div>`;
  if (d.missed) h += `<div class="bad">missed: ${esc(d.missed)}</div>`;
  const more = RECALL_MORE.get(key);
  if (more && more.results.length) {
    h += `<div class="cap">Further down, by likeness only \u2014 not reviewed</div>` +
      more.results.map((r) => recallRowHtml(r, false)).join("");
  }
  if (more && more.error) h += `<div class="bad">${esc(more.error)}</div>`;
  if (a && a.essence) {
    RECALL_ASKED.set(key, { asked: a, shown: res.map((r) => r.id),
                            skip: res.filter((r) => r.found_by === "likeness").length });
    const done = more && more.total != null && more.skip >= more.total;
    if (!done) h += `<button class="small-btn recall-more" data-rk="${esc(key)}">show 10 more</button>`;
  }
  const raw = Object.assign({}, d);
  delete raw.before_reply;
  delete raw.late;
  delete raw.after;
  h += `<details class="rc-raw" data-k="${esc(key)}:raw"${OPEN.has(key + ":raw") ? " open" : ""}>` +
    `<summary>exactly what ${esc(MY_NAME)} was handed</summary>` +
    `<pre>${esc(JSON.stringify(raw, null, 2))}</pre></details>`;
  return h + `</div>`;
}

async function recallMore(key, btn) {
  const got = RECALL_ASKED.get(key);
  if (!got) return;
  const prev = RECALL_MORE.get(key) || { skip: got.skip, total: null, results: [] };
  btn.disabled = true;
  btn.textContent = "looking\u2026";
  try {
    const out = await post("api/recall/more", { asked: got.asked, skip: prev.skip });
    const seen = new Set(got.shown.concat(prev.results.map((r) => r.id)));
    RECALL_MORE.set(key, { skip: prev.skip + out.results.length, total: out.total,
      results: prev.results.concat(out.results.filter((r) => !seen.has(r.id))) });
  } catch (err) {
    RECALL_MORE.set(key, Object.assign({}, prev, { error: String(err.message || err) }));
  }
  render();
  if (key === "try") paintTry();
}

// What the digest organ did to one report: stood in, or missed out loud.
function digestText(d) {
  const out = [];
  const who = d.name ? "the report from " + d.name + (d.page ? ", page " + d.page : "")
    : "a report";
  if (d.missed) {
    out.push("missed on " + who + ": " + d.missed + " — it went to " + MY_NAME + " whole");
  } else {
    out.push("digested " + who + ": " + (d.full_chars != null ? d.full_chars : "?") +
      " chars → a paragraph" + (d.by ? " · by " + d.by : "") +
      (d.took_s != null ? " · " + Number(d.took_s).toFixed(1) + " s" : ""));
  }
  if (d.paragraph) out.push("", d.paragraph);
  if (d.note) out.push("note: " + d.note);
  if (d.path) out.push("the whole report: " + d.path);
  return out.join("\n");
}
// --- the pieces of the chat ------------------------------------------------

function kindClass(k) {
  if (k === ME) return "self";
  if (k === ME_INTERIM) return "self_interim self";
  return k;
}

const WHO = { user: PEOPLE[OWNER], [ME]: MY_NAME, [ME_INTERIM]: MY_NAME + " · while looking",
              angel: "angel " + MY_NAME, lost: MY_NAME + " · turn broke",
              dream: MY_NAME + " · from a dream" };

// One line each for what the assistant did, in plain words. The second word is the
// colour it gets.
const ACT = {
  native: ["Native Codex activity", "files"],
  files: ["Files", "files"],
  web: ["Web", "web"],
  search: ["Searched essences", "search"],
  shelf: ["Listed essences", "shelf"],
  fetch: ["Reached into memory", "fetch"],
  essence: ["Wrote an essence", "essence"],
  "essence-edit": ["Rewrote an essence", "essence"],
  "essence-rename": ["Renamed an essence", "essence"],
  "essence-remove": ["Let go of an essence", "essence"],
  drop: ["Put out of memory", "drop"],
  spark: ["Rewrote the Spark", "spark"],
  notebook: ["Notebook", "notebook"],
  snag: ["Snag", "snag"],
  sent: ["Sent an errand", "sent"],
  worker: ["Came back", "worker"],
  recall: ["Automatic memory", "recall"],
  digest: ["Digest", "recall"],
  dream: ["The night, whole", "dream"],
};

const ENDED = {
  completed: "came back",
  budget_exhausted: "ran out of money",
  max_turns: "ran out of turns",
  killed_timeout: "ran out of time",
  stalled: "got stuck",
  mismatch: "came up wrong and was stopped",
  vanished: "never came home",
  "came home late": "came home late",
  refused: "was refused",
  error: "broke",
  running: "is out",
};

function tidySummary(s) {
  return String(s || "").replace(/^while looking, round \d+:\s*/, "").replace(/^automatic memory:\s*/, "");
}

// One small line. With a body it opens; without, it is just the line.
function actHtml(key, cls, label, text, bodyHtml, grey, badge) {
  const line = `<span class="lbl">${esc(label)}</span>${badge || ""}<span class="txt">${esc(text)}</span>`;
  const g = grey ? " grey" : "";
  if (!bodyHtml) return `<div class="act ${cls}${g}"><div class="line">${line}</div></div>`;
  const open = OPEN.has(key) ? " open" : "";
  return `<details class="act ${cls}${g}" data-k="${esc(key)}"${open}>` +
         `<summary>${line}</summary>${bodyHtml}</details>`;
}

function body(text, mono) {
  return text ? `<div class="act-body${mono ? " mono" : ""}">${esc(text)}</div>` : "";
}

// Transport notifications are pieces of an action, not separate actions.
// This is a display projection only: the original audit events stay untouched.
function compactWorkings(events, scope = "") {
  const requestRow = (events || []).find(e => Number.isInteger(e.detail?.request_row))?.detail.request_row;
  if (requestRow != null) scope = "row:" + requestRow;
  const out = [], actions = new Map(), notes = [];
  let first = null;
  for (const e of events || []) {
    if (e.kind !== "native") { out.push(e); continue; }
    first ||= e;
    const d = e.detail || {};
    const startup = /native command startup/i.test(e.summary || e.text || "");
    const id = d.id ?? d.itemId;
    if (!startup && (id != null || d.type === "commandExecution" || d.type === "fileChange" || d.command)) {
      const key = String(id ?? ("event:" + (e.id ?? out.length)));
      let action = actions.get(key);
      if (!action) {
        action = {...e, detail: {_nativeGroup: true, _key: scope + ":" + key, _chunks: ""}};
        actions.set(key, action);
        out.push(action);
      }
      const chunks = action.detail._chunks + (d.delta || "");
      Object.assign(action.detail, d, {_chunks: chunks});
      delete action.detail.delta;
    } else notes.push(e);
  }
  for (const action of actions.values()) {
    const d = action.detail;
    if (d.aggregatedOutput == null && d._chunks) d.aggregatedOutput = d._chunks;
  }
  if (first) {
    let holder = actions.values().next().value;
    if (!holder) {
      holder = {...first, detail: {_nativeGroup: true, _key: scope + ":notes"}};
      out.push(holder);
    }
    holder.detail._notes = notes;
    holder.detail.request_row = requestRow;
    holder.detail._runFailed = [...notes, ...actions.values()].some(e => {
      const d = e.detail || {};
      return d.error || (d.exitCode != null && d.exitCode !== 0) || /failed|denied|declined|cancel|interrupt|unknown|error/.test(d.status || "");
    });
  }
  return out;
}

function nativeActionHtml(e, grey) {
  const d = e.detail;
  const command = (d.commandActions || []).map(a => a.command).filter(Boolean).join("; ") || d.command || "";
  const failed = d.error || (d.exitCode != null && d.exitCode !== 0) || /failed|denied|declined|cancel|interrupt|unknown|error/.test(d.status || "");
  const isAction = command || d.type || d.itemId || d.aggregatedOutput != null;
  const hasDiffs = d._notes?.some(n => n.detail?.diff);
  // Routine startup/completion records stay in the audit, not on the chat.
  if (!isAction && !hasDiffs && !d._runFailed) return "";
  const status = d.exitCode != null ? `exit ${d.exitCode}` : ({inProgress: "running", completed: "done", unknown: "result unknown"}[d.status] || d.status || "");
  const label = d.type === "fileChange" ? "File edit" : command ? "Command" : d.itemId ? "Tool output" : hasDiffs && !d._runFailed ? "Changes" : "Run notes";
  const description = d.type === "fileChange" ? (d.changes || []).map(c => c.path).join(", ") : command;
  const short = description.replace(/\s+/g, " ").trim();
  const summary = [short.length > 120 ? short.slice(0, 117) + "…" : short, status,
    d._runFailed ? "turn has a failure — see run notes" : ""].filter(Boolean).join(" · ");
  let inner = (command || d.type || d.aggregatedOutput != null) ? body(nativeText(d), true) : "";
  if (d._notes?.length && (failed || d._runFailed)) {
    const notesBody = body(d._notes.map(n => (n.summary || n.text || "") + "\n" + nativeText(n.detail || {})).join("\n\n"), true);
    inner += isAction ? actHtml("native-notes:" + d._key, "", "Run notes", "setup, commentary and turn result", notesBody) : notesBody;
  } else if (hasDiffs) {
    const diffsBody = body(d._notes.filter(n => n.detail?.diff).map(n => n.detail.diff).join("\n\n"), true);
    inner += isAction ? actHtml("native-diffs:" + d._key, "", "Changes", "recorded turn diffs", diffsBody) : diffsBody;
  }
  if (Number.isInteger(d.request_row)) inner += `<a href="api/native-log?row=${d.request_row}" download="native-turn-${d.request_row}.json">Download run log</a>`;
  return actHtml("native:" + d._key, failed || d._runFailed ? "files snag" : "files", label, summary, inner, grey);
}

function actFromEvent(e, grey) {
  if (e.detail?._nativeGroup) return nativeActionHtml(e, grey);
  const [label, cls] = ACT[e.kind] || [e.kind, e.kind];
  const text = tidySummary(e.summary);
  const mono = e.kind === "files" || e.kind === "shelf" || e.kind === "native";
  const download = e.kind === "native" && e.detail && Number.isInteger(e.detail.request_row)
    ? `<a href="api/native-log?row=${e.detail.request_row}" download="native-turn-${e.detail.request_row}.json">Download run log</a>` : "";
  if (e.kind === "recall" && e.detail) {
    return actHtml("ev:" + e.id, cls, label, text, recallHtml(e.detail, "ev:" + e.id), grey,
                   recallBadge(e.detail));
  }
  return actHtml("ev:" + e.id, cls, label, text, body(eventDetail(e), mono) + download, grey);
}

function stepsHtml(steps) {
  return (steps || []).map((s) =>
    `<div class="step ${esc(s.kind)}"><span class="at">${Number(s.at).toFixed(1)}s</span>` +
    `<span>${esc(s.text)}</span></div>`).join("");
}

// The pictures on a line, drawn where they were said. The chat is the
// reader's history, not a picture of the inside of the assistant's head --
// it already shows rows that have left memory, and these stay for the same
// reason. No badge in the plain view: only the workings say the pixels have
// left memory, because there it is one more fact about memory and here it
// would be furniture.
function picsHtml(pics, grey) {
  if (!pics || !pics.length) return "";
  const gone = (grey && !PLAIN)
    ? `<div class="note">out of memory — the picture is still on disk, and`
      + ` comes back with the row</div>` : "";
  return `<div class="pics">` + pics.map((p) => {
    const size = p.w && p.h ? `${p.w}×${p.h}` : "";
    const cap = [p.name, size, p.tokens ? "~" + p.tokens + " tokens" : ""]
      .filter(Boolean).join(" · ");
    // The stored name is a sha and a suffix, and nothing else is ever served
    // from that folder -- so an `src` built from it cannot point elsewhere.
    return `<a class="pic" href="pictures/${esc(p.file)}" target="_blank"`
      + ` title="${esc(cap)}"><img src="pictures/${esc(p.file)}"`
      + ` alt="${esc(p.name || "picture")}" loading="lazy"></a>`;
  }).join("") + `</div>${gone}`;
}

function msgHtml(r, turn, grey) {
  const m = r.meta || {};
  // The tool handoff is an internal queue entry, never words from the person.
  // Keep its row identity and surrounding activity without repeating its payload.
  if (r.kind === "user" && m.voice_task) {
    return `<div class="act voice-handoff${grey ? " grey" : ""}" data-k="row:${r.id}" data-id="${r.id}">` +
      `<div class="line"><span class="lbl">Voice request</span>` +
      `<span class="txt">Passed to ${esc(MY_NAME)}'s tools</span>` +
      `<span class="when">${when(r.dt)}</span></div></div>`;
  }
  // The full backend answer remains stored with its tool events and is the
  // fallback if voice delivery fails. Once a native output transcript exists,
  // that transcript owns the visible wording and this copy folds to activity.
  if (r.kind === ME && m.voice_backend?.delivered) {
    return `<div class="act voice-result${grey ? " grey" : ""}" data-k="row:${r.id}" data-id="${r.id}">` +
      `<div class="line"><span class="lbl">Voice result</span>` +
      `<span class="txt">Spoken through ${esc(MY_NAME)}'s voice</span>` +
      `<span class="when">${when(r.dt)}</span></div></div>`;
  }
  // A labelled user row wears its speaker: the name on the front, and a
  // class so another person's lines read in their own colour. No label
  // means the owner, the store's rule.
  const sig = r.kind === "user" && m.who && PEOPLE[m.who] ? m.who : "";
  const cls = `msg ${kindClass(r.kind)}${whoClass(sig)}${grey ? " grey" : ""}`;
  const back = grey
    ? ` <button class="small-btn reload" data-id="${r.id}">bring back into memory</button>` : "";
  // A memory note is bookkeeping, and how long the turn took is bookkeeping.
  // Why an answer never arrived is not: that one stays in both views.
  let note = (m.note && !PLAIN) ? `<div class="note">memory note: ${esc(m.note)}</div>` : "";
  // How the assistant meant it to land, in its own words, under the words.
  // Kept on the row only when an engine that performs it was speaking, so
  // this is what was actually heard rather than what was hoped for. Drawn in
  // plain too: it is part of what was said, not an instrument reading.
  const sound = m.sound ? `<div class="sound">*${esc(m.sound)}*</div>` : "";
  if (r.kind === "lost") {
    note += `<div class="note">${esc(m.reason || "the turn did not come back")}</div>`;
  }
  let tlog = "", log = "";
  if (turn && !PLAIN) {
    const d = turn.detail || {};
    const steps = d.steps || [];
    const snags = steps.filter((s) => s.kind === "snag").length;
    const key = "turn:" + r.id;
    tlog = `<button class="tlog${snags ? " snags" : ""}" data-k="${key}" ` +
      `title="how the turn went, step by step">${Number(d.seconds || 0).toFixed(1)}s` +
      `${snags ? " · " + snags + " snag" + (snags === 1 ? "" : "s") : ""}</button>`;
    log = `<div class="steps${OPEN.has(key) ? "" : " hidden"}" data-for="${key}">` +
      stepsHtml(steps) + `</div>`;
  }
  const text = r.text || (r.kind === "lost" ? "(nothing of the answer arrived)" : "");
  const name = sig ? PEOPLE[sig] : (WHO[r.kind] || r.kind);
  // The door, when it was a phone: the assistant is told it on the row, so
  // the reader sees it on the row. A line from the desk carries no door and says nothing.
  const via = (r.kind === "user" && m.via && VIA_SAID[m.via])
    ? `<span class="via">${esc(VIA_SAID[m.via])}</span>` : "";
  return `<div class="${cls}" data-id="${r.id}">
    <div class="who"><b>${esc(name)}</b>${via}` +
    `<span class="when" title="row #${r.id} · ~${r.tokens_est} tokens">${when(r.dt)}</span>` +
    `${tlog}${back}</div>${log}<div class="body">${md(text)}</div>${sound}` +
    `${picsHtml(m.pictures, grey)}${note}</div>`;
}

// Their line, on screen the instant send is pressed and before the room has
// answered. The same markup a real user row is drawn in, so the bubble does
// not change shape when the real one replaces it -- only a shade lighter,
// which is the whole of how they can tell it has not landed yet. The echo
// keeps the name it was sent under: switching while it is in flight must
// not repaint a line already said.
function echoHtml(e) {
  const name = (e.who && PEOPLE[e.who]) || WHO.user;
  // The pictures are drawn from what is still in the browser's own hands, so
  // they are on screen the instant send is pressed -- before the room has
  // written them down and before there is anything to fetch back.
  const pics = (e.pics || []).length
    ? `<div class="pics">` + e.pics.map((p) =>
        `<span class="pic"><img src="${esc(p.url)}" alt=""></span>`).join("")
      + `</div>` : "";
  return `<div class="msg user echo${whoClass(e.who)}"><div class="who"><b>${esc(name)}</b>` +
    `<span class="when">${clockOnly(new Date(e.at))}</span></div>` +
    `<div class="body">${md(e.text)}</div>${pics}</div>`;
}

function essHtml(r, grey, keyPrefix) {
  const key = (keyPrefix || "") + "row:" + r.id;
  const title = r.title ? `<span class="title">${esc(r.title)}</span>`
                        : `<span class="title none">(untitled)</span>`;
  const back = grey
    ? ` <button class="small-btn reload" data-id="${r.id}">bring back into memory</button>` : "";
  const open = OPEN.has(key) ? " open" : "";
  return `<details class="ess${grey ? " grey" : ""}" data-k="${esc(key)}"${open}>` +
    `<summary>${title}<span class="meta">${r.meta && r.meta.room ? esc(r.meta.room) + " · " : ""}#${r.id} · ${when(r.dt)} · ~${r.tokens_est}</span></summary>` +
    `<div class="body">${md(r.text)}</div>${trailHtml((state.trails || {})[r.id])}${back}</details>`;
}

// A line of the assistant's to a hand, or to the angel at the door.
function tellHtml(r, grey) {
  const m = r.meta || {};
  const to = m.to || "?";
  const label = to === "angel" ? MY_NAME + " → angel" : `Sent ${to}`;
  const text = (m.page && to !== "angel" ? `page ${m.page} · ` : "") + firstLine(r.text);
  const detail = (m.why ? "why: " + m.why + "\n\n" : "") + (r.text || "");
  return actHtml("row:" + r.id, "tell", label, text, body(detail), grey);
}

// The assistant and the angel talking -- the angel session at the door, and
// the answers to it -- is not the people's conversation. One small line each
// way, opened only if somebody wants it.
function angelHtml(r, grey) {
  const m = r.meta || {};
  const re = m.reply_to ? "answering " + MY_NAME + "'s line #" + m.reply_to + "\n\n" : "";
  // A line from the angel that showed the assistant something: the picture opens with
  // the line rather than being lost inside a fold that says nothing about it.
  const pics = (m.pictures || []).length
    ? picsHtml(m.pictures, grey) : "";
  const said = firstLine(r.text)
    || (m.pictures || []).map((p) => "[" + (p.name || "picture") + "]").join(" ");
  return actHtml("row:" + r.id, "tell", "angel → " + MY_NAME, said,
                 body(re + (r.text || "")) + pics, grey);
}

function selfToAngelHtml(r, turn, grey) {
  const steps = ((turn && turn.detail) || {}).steps || [];
  const log = steps.length ? `<div class="act-body"><div class="cap">the turn</div>` +
    esc(steps.map((s) => Number(s.at).toFixed(1) + "s  " + s.text).join("\n")) + `</div>` : "";
  return actHtml("row:" + r.id, "tell", MY_NAME + " → angel", firstLine(r.text),
                 body(r.text || "") + log, grey);
}

// Replies that answered only the angel, so they draw as the assistant and
// the angel talking rather than as something said to the people.
let TO_ANGEL = new Set();

function findAngelTurns(rows) {
  const out = new Set();
  let angel = false, user = false;
  for (const r of rows) {
    if (r.kind === "angel") angel = true;
    else if (r.kind === "user") user = true;
    else if (r.kind === ME || r.kind === "lost") {
      // A reply says whom it is for now. Rows from before it could are
      // drawn by the old guess: the angel spoke last and no person did.
      const said = r.kind === ME && r.meta && r.meta.to;
      if (said ? said === "angel" : (angel && !user && r.kind === ME)) out.add(r.id);
      angel = false; user = false;
    }
  }
  return out;
}

// A hand's page, home. The report is what the assistant keeps; the run is the log of it.
function workerRowHtml(r, ev, grey) {
  const m = r.meta || {};
  const d = (ev && ev.detail) || {};
  const ended = m.ended || d.ended || "completed";
  const name = m.name || d.name;
  const label = name ? `${name} ${ENDED[ended] || ended}` : `An errand ${ENDED[ended] || ended}`;
  const bits = [m.page ? "page " + m.page : "", r.title || m.title || d.title || "",
                m.cost_usd ? "$" + Number(m.cost_usd).toFixed(2) : ""].filter(Boolean);
  const inner = `<div class="act-body"><div class="cap">what it said</div>` +
    `<div class="prose">${md(r.text)}</div>` +
    (ev ? `<div class="cap">how the run went</div>${esc(runText(d))}` : "") + `</div>`;
  const cls = ended === "completed" ? "worker" : "worker snag";
  return actHtml("row:" + r.id, cls, label, bits.join(" · "), inner, grey);
}

// A turn of the assistant's: what it looked at before answering, then the
// answer, then what it did to its memory afterwards -- in that order, because that
// is the order it happened in.
function turnHtml(r, events, grey) {
  const before = [], after = [];
  let turn = null;
  for (const e of compactWorkings(events || [], "row:" + r.id)) {
    if (e.kind === "turn") { turn = e; continue; }
    if (e.detail && (e.detail.while_looking || e.detail.before_reply)) before.push(e); else after.push(e);
  }
  // In plain the message is the whole of it: what the assistant read before
  // answering and what it filed afterwards are the workings, and they stay
  // out of sight until somebody goes looking.
  if (PLAIN) return msgHtml(r, turn, false);
  let html = "";
  let round = null;
  for (const e of before) {
    const d = e.detail || {};
    if (d.why && d.while_looking !== round) {
      round = d.while_looking;
      html += actHtml("", "looking", "Looking first", d.why, "", grey);
    }
    html += actFromEvent(e, grey);
  }
  html += (r.kind === ME && TO_ANGEL.has(r.id))
    ? selfToAngelHtml(r, turn, grey) : msgHtml(r, turn, grey);
  html += after.map((e) => actFromEvent(e, grey)).join("");
  return html;
}

// A night's dream. The line it wakes to draws folded, with what it did to
// the shelf hung under it in time order; the sentence for the room -- the one
// thing worth reading over breakfast -- draws as a plain line of its own.
function dreamHtml(r, events, grey) {
  const m = r.meta || {};
  if (m.to === "room") return msgHtml(r, null, grey);
  const label = m.why === "missed" ? "A night not dreamt"
              : m.broke ? "A dream broke"
              : m.free ? MY_NAME + " dreamed (a free night)" : MY_NAME + " dreamed";
  let turn = null, acts = [];
  for (const e of events || []) {
    if (e.kind === "turn") turn = e;
    else acts.push(e);
  }
  const steps = ((turn && turn.detail) || {}).steps || [];
  const log = steps.length ? `<div class="cap">the night, step by step</div>` +
    esc(steps.map((s) => Number(s.at).toFixed(1) + "s  " + s.text).join("\n")) : "";
  const head = "the night of " + (m.night || "?") + (m.why ? " · " + m.why : "");
  let html = actHtml("row:" + r.id, m.broke || m.why === "missed" ? "snag" : "dream",
                     label, firstLine(r.text),
                     `<div class="act-body">${esc(head)}\n\n${esc(r.text || "")}\n\n${log}</div>`, grey);
  html += acts.map((e) => actFromEvent(e, grey)).join("");
  return html;
}

// The room noticing, with nobody speaking. A world row is born already out
// of reach -- it passed through the assistant, which is the point -- so it always
// draws grey, one small line where it happened.
function worldHtml(r, grey) {
  const src = r.meta && r.meta.source ? " · " + r.meta.source : "";
  return actHtml("row:" + r.id, "world", "The room noticed" + src,
                 firstLine(r.text), body(r.text || ""), grey);
}

// A job opened, decided on, or closed -- the trail row, one small line.
function jobHtml(r, grey) {
  const op = (r.meta && r.meta.op) || "";
  const label = op === "open" ? "A job opened"
              : op === "close" ? "A job closed"
              : op === "decide" ? "A decision kept" : "A job";
  return actHtml("row:" + r.id, "job", label, firstLine(r.text),
                 body(r.text || ""), grey);
}

function rowHtml(r, byRow, byWorkerRow, grey) {
  if (r.kind === ME_INTERIM) {
    if (r.meta?.to === "angel") return PLAIN ? "" : selfToAngelHtml(r, null, grey);
    return msgHtml(r, null, grey);
  }
  if (PLAIN) {
    // The conversation, and nothing beside it. The assistant's lines to the
    // angel, its hands coming home, its essences, the jobs and the room's own
    // noticing are all real -- they are simply not what a chat looks like.
    // The one exception is the sentence a night leaves for the room: it was
    // written to be read at breakfast, so it draws as an ordinary line.
    if (r.kind === "dream") {
      return ((r.meta || {}).to === "room") ? msgHtml(r, null, false) : "";
    }
    if (r.kind === "user" || r.kind === "lost") return turnHtml(r, byRow[r.id], false);
    if (r.kind === ME) return TO_ANGEL.has(r.id) ? "" : turnHtml(r, byRow[r.id], false);
    return "";
  }
  if (r.kind === "angel") return angelHtml(r, grey);
  if (r.kind === "world") return worldHtml(r, grey);
  if (r.kind === "job") return jobHtml(r, grey);
  if (r.kind === "dream") return dreamHtml(r, byRow[r.id], grey);
  if (r.kind === ME || r.kind === "lost" || r.kind === "user") {
    return turnHtml(r, byRow[r.id], grey);
  }
  if (r.kind === "worker") return workerRowHtml(r, byWorkerRow[r.id], grey);
  if (r.kind === "tell") return tellHtml(r, grey);
  if (r.kind === "essence") return essHtml(r, grey);
  return msgHtml(r, null, grey);
}

function prepare() {
  const byRow = {}, byWorkerRow = {};
  for (const e of state.events || []) {
    if (e.kind === "worker" && e.detail && e.detail.row) { byWorkerRow[e.detail.row] = e; continue; }
    (byRow[e.reply_row] = byRow[e.reply_row] || []).push(e);
  }
  return { rows: state.rows || [], byRow, byWorkerRow };
}

// Re-render without the page jumping: a reader at the bottom stays at the
// bottom, otherwise they stay where they were.
function patchHtml(root, html, outer = false) {
  // Keep the actual scrolling elements and text nodes alive across poll ticks.
  // Replacing them and restoring scroll offsets cannot preserve a scrollbar
  // drag, pointer capture, or the browser's text selection.
  const template = document.createElement("template");
  template.innerHTML = html;
  const key = node => node.nodeType === 1
    ? node.getAttribute("data-k") || node.id || (node.matches(".msg[data-id]") ? "msg:" + node.dataset.id : "") : "";
  const same = (a, b) => a.nodeType === b.nodeType && a.nodeName === b.nodeName && key(a) === key(b);
  const selection = document.getSelection();
  let selected = null;
  if (selection && !selection.isCollapsed && selection.rangeCount &&
      root.contains(selection.anchorNode) && root.contains(selection.focusNode)) {
    const range = selection.getRangeAt(0);
    const prefix = range.cloneRange();
    prefix.selectNodeContents(root);
    prefix.setEnd(range.startContainer, range.startOffset);
    selected = {text: selection.toString(), at: prefix.toString().length,
      backwards: selection.anchorNode === range.endContainer && selection.anchorOffset === range.endOffset};
  }
  const sync = (node, next) => {
    if (node.isEqualNode(next) && !(node.nodeType === 1 &&
        (node.matches("input,textarea,select") || node.querySelector("input,textarea,select")))) return;
    if (node.nodeType === 3 || node.nodeType === 8) {
      const before = node.data, after = next.data;
      let start = 0, end = 0;
      while (start < before.length && start < after.length && before[start] === after[start]) start++;
      while (end < before.length - start && end < after.length - start && before[before.length - end - 1] === after[after.length - end - 1]) end++;
      node.replaceData(start, before.length - start - end, after.slice(start, after.length - end));
      return;
    }
    for (const attr of [...node.attributes]) if (!next.hasAttribute(attr.name)) node.removeAttribute(attr.name);
    for (const attr of next.attributes) if (node.getAttribute(attr.name) !== attr.value) node.setAttribute(attr.name, attr.value);
    children(node, next);
    if (node !== document.activeElement && node.matches("input,textarea,select")) {
      if (node.type !== "file" && node.value !== next.value) node.value = next.value;
      if (node.matches("input") && node.checked !== next.checked) node.checked = next.checked;
    }
  };
  const children = (parent, next) => {
    const keyed = new Map([...parent.childNodes].filter(n => key(n)).map(n => [key(n), n]));
    let cursor = parent.firstChild;
    for (const wanted of next.childNodes) {
      let node = key(wanted) ? keyed.get(key(wanted)) : cursor;
      if (!node || !same(node, wanted)) node = wanted.cloneNode(true);
      if (node !== cursor) parent.insertBefore(node, cursor);
      sync(node, wanted);
      cursor = node.nextSibling;
    }
    while (cursor) { const old = cursor; cursor = cursor.nextSibling; old.remove(); }
  };
  if (outer) sync(root, template.content.firstChild);
  else children(root, template.content);
  // A status change and new output can edit both sides of selected text in a
  // single text node. Retain the same selected passage when it still exists.
  if (selected?.text && selection.toString() !== selected.text) {
    const text = root.textContent;
    let at = text.indexOf(selected.text), nearest = -1;
    while (at >= 0) {
      if (nearest < 0 || Math.abs(at - selected.at) < Math.abs(nearest - selected.at)) nearest = at;
      at = text.indexOf(selected.text, at + 1);
    }
    if (nearest >= 0) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node, offset = 0, start, end;
      while ((node = walker.nextNode())) {
        if (!start && offset + node.length >= nearest) start = [node, nearest - offset];
        if (offset + node.length >= nearest + selected.text.length) { end = [node, nearest + selected.text.length - offset]; break; }
        offset += node.length;
      }
      if (start && end) selection.setBaseAndExtent(...(selected.backwards ? [...end, ...start] : [...start, ...end]));
    }
  }
}

function keepScroll(fn) {
  // A reader moving even a few pixels upward has chosen their place. Polling
  // may keep following only when the view was genuinely resting at its end.
  const atBottom = (typeof shown === "undefined" || shown === "chat") &&
    MAIN.scrollHeight - MAIN.scrollTop - MAIN.clientHeight <= 4;
  const top = MAIN.scrollTop;
  const left = MAIN.scrollLeft;
  const saved = new Map();
  // Key descendants by their nearest stable panel/action, not by their index
  // in the whole page: inserting another action must not move these handles.
  const positions = () => {
    const result = new Map();
    const walk = (node, parentKey) => {
      Array.from(node.children || []).forEach((child, i) => {
        const key = child.dataset?.k ? "key:" + child.dataset.k : child.id ? "id:" + child.id :
          child.matches?.(".msg[data-id]") ? "message:" + child.dataset.id : parentKey + "/" + child.tagName + ":" + i;
        result.set(key, child);
        walk(child, key);
      });
    };
    walk(MAIN, "main");
    return result;
  };
  let anchor = null;
  const viewport = MAIN.getBoundingClientRect?.();
  for (const [key, el] of positions()) {
    if (el.scrollTop || el.scrollLeft) saved.set(key, [el.scrollTop, el.scrollLeft, el]);
    if (!atBottom && viewport && (el.dataset?.k || el.matches?.(".msg[data-id]"))) {
      const rect = el.getBoundingClientRect();
      if (rect.height && rect.bottom > viewport.top && rect.top < viewport.bottom &&
          (!anchor || (rect.top <= viewport.top && rect.top > anchor.y))) anchor = {key, y: rect.top};
    }
  }
  fn();
  const current = positions();
  for (const [key, el] of current) {
    const pos = saved.get(key);
    if (pos && pos[2] !== el) { el.scrollTop = pos[0]; el.scrollLeft = pos[1]; }
  }
  MAIN.scrollLeft = left;
  MAIN.scrollTop = atBottom ? MAIN.scrollHeight : top;
  const standing = anchor && current.get(anchor.key);
  if (standing) MAIN.scrollTop += standing.getBoundingClientRect().top - anchor.y;
}

function renderChat() {
  if (!state) return;
  const { rows, byRow, byWorkerRow } = prepare();
  TO_ANGEL = findAngelTurns(rows);
  const past = [], mem = [], essMem = [];
  for (const r of rows) {
    if (!r.loaded) past.push(r);
    else if (r.kind === "essence") essMem.push(r);
    else mem.push(r);
  }
  LOADED_ESS = new Set(essMem.map((r) => r.id));
  // What is drawn is what the room holds; what is hidden is still on the
  // server, and the button goes and gets it.
  let html = "";
  if (older > 0) {
    html += `<div class="earlier"><button class="small-btn" id="more-past">` +
      (PLAIN ? `show earlier messages`
             : `show earlier · ${older} more rows out of memory`) + `</button></div>`;
  }
  if (PLAIN) {
    // One run of messages, oldest first, with no line drawn through it --
    // and only the picked person's room. The other room's lines are not
    // gone: each stretch of them stands as one quiet counted line, because
    // a filter that says nothing makes a broken conversation read as a
    // whole one. The whole store is one click away
    // under the workings: filtered, never walled.
    const w = whoNow();
    const other = Object.keys(PEOPLE).find((p) => p !== w) || w;
    let hidden = 0;
    const stand = () => {
      if (!hidden) return "";
      const t = hidden + " line" + (hidden === 1 ? "" : "s") + " in "
        + (PEOPLE[other] || other) + "'s room";
      hidden = 0;
      return `<div class="elsewhere">${t}</div>`;
    };
    for (const r of rows) {
      const piece = rowHtml(r, byRow, byWorkerRow, false);
      if (!piece) continue;
      if (roomsOf(r).includes(w)) html += stand() + piece;
      else hidden += 1;
    }
    html += stand();
  } else {
  html += past.map((r) => rowHtml(r, byRow, byWorkerRow, true)).join("");

  const me = (state.prompt || {}).self || {};
  const tokens = me.prompt_tokens_est ?? me.measured_input_tokens_last_turn ?? 0;
  html += `<div class="divider">in memory now · ${mem.length} message${mem.length === 1 ? "" : "s"}` +
    ` · ${essMem.length} essence${essMem.length === 1 ? "" : "s"} · ~${k(tokens)} tokens</div>`;
  if (essMem.length) {
    html += `<div class="block-title">essences held</div>` +
      essMem.map((r) => essHtml(r, false)).join("");
    html += `<div class="block-title">the conversation</div>`;
  }
  html += mem.map((r) => rowHtml(r, byRow, byWorkerRow, false)).join("");
  }
  // An echo in plain draws only in its sender's own room, same rule as the
  // row that will replace it.
  html += echoes.filter((e) => !PLAIN || (e.who || OWNER) === whoNow())
    .map(echoHtml).join("");
  html += `<div id="live">${$("live")?.innerHTML || ""}</div>`;
  keepScroll(() => { patchHtml($("view-chat"), html); renderLive(); });
}

// --- the turn as it happens ------------------------------------------------
// Drawn in the place it will stay: what the assistant looked at goes above
// its answer, the answer forms where it will land, and its housekeeping goes
// below it.

const LIVE_LABEL = { reach: ["Reached into memory", "fetch"], search: ["Searched essences", "search"],
                     shelf: ["Listed essences", "shelf"], files: ["Files", "files"],
                     web: ["Web", "web"], worker: ["Hands", "worker"],
                     recall: ["Automatic memory", "recall"], native: ["Native Codex activity", "files"] };

function liveStep(s, key, afterReply) {
  if (s.kind === "interim") {
    const r = s.detail?.row;
    if (!r || (state?.rows || []).some((saved) => saved.id === r.id)) return "";
    if (PLAIN && !roomsOf(r).includes(whoNow())) return "";
    return rowHtml(r, {}, {}, false);
  }
  if (s.detail?._nativeGroup) return nativeActionHtml(s, false);
  if (s.kind === "snag") return actHtml(key, "snag", "Snag", s.text, "");
  if (s.kind === "looking") return actHtml(key, "looking", "Looking first", s.text, "");
  if (s.detail && LIVE_LABEL[s.kind]) {
    const [label, cls] = LIVE_LABEL[s.kind];
    const mono = s.kind === "files" || s.kind === "shelf" || s.kind === "native";
    if (s.kind === "recall") {
      return actHtml(key, cls, label, tidySummary(s.text), recallHtml(s.detail, key), false,
                     recallBadge(s.detail));
    }
    return actHtml(key, cls, label, tidySummary(s.text),
                   body(stepText(s), mono));
  }
  if (s.kind === "worker") return actHtml(key, "sent", "Hands", s.text, "");
  if (afterReply && s.kind === "plain") {
    const t = s.text;
    if (/^(kept a new essence|rewrote essence|named essence|let go of essence)/.test(t)) {
      return actHtml(key, "essence", "Essence", t, "");
    }
    if (/^put \d+ rows? down/.test(t)) return actHtml(key, "drop", "Put out of memory", t, "");
    if (/^.+ rewrote \w+ Spark/.test(t)) return actHtml(key, "spark", "Spark", t, "");
  }
  return "";
}

// What the assistant is doing, said the way a person would say it. The step
// texts underneath are its own working notes -- "reach: 3 rows", "shelf 14"
// -- which is what detailed shows. Plain gets a sentence instead, because a
// reader wants to be told what is happening, not shown the instrument.
const PLAIN_WHAT = {
  reach: "checking memory…",
  search: "searching…",
  shelf: "searching…",
  files: "reading a file…",
  web: "using the web…",
  worker: "working with a helper…",
  recall: "checking memory…",
  snag: "something failed — carrying on…",
};

function nativeWorkingLabel(steps) {
  const latest = [...steps].reverse().find((s) => s.kind === "native" && s.detail &&
    (s.detail.type === "commandExecution" || s.detail.type === "fileChange" || s.detail.command));
  if (!latest) return "getting tools ready…";
  const d = latest.detail || {};
  if (d.type === "fileChange") return "editing a file…";
  const command = String((d.commandActions || []).map((a) => a.command).filter(Boolean).join(" ") || d.command || "").toLowerCase();
  if (/\bcurl(?:\.exe)?\b|invoke-webrequest|invoke-restmethod/.test(command)) return "using the web…";
  if (/\brg(?:\.exe)?\b|select-string|get-childitem|\bdir\b/.test(command)) return "searching…";
  if (/\bcmd(?:\.exe)?\b/.test(command)) return "using CMD…";
  if (/powershell(?:\.exe)?/.test(command)) return "using PowerShell…";
  return "using tools…";
}

function plainWhat(last, kept, w, steps) {
  if (kept >= 0 && last && last.kind !== "snag") return "finishing…";
  if (w && w.mode === "thinking") return "thinking…";
  if (w && w.mode === "writing" && kept < 0) return "writing…";
  if (!last) return "waking up…";
  if (last.kind === "looking") return "thinking…";
  if (last.kind === "native") return nativeWorkingLabel(steps);
  return PLAIN_WHAT[last.kind] || "thinking";
}

function renderNativeStop() {
  const stop = $("native-stop-live");
  if (!stop) return;
  stop.classList.toggle("hidden", !(progress && progress.busy && progress.native_active));
  stop.onclick = cancelNativeTools;
}

function renderLive() {
  renderNativeStop();
  const el = $("live");
  if (!el) return;
  const busy = progress && progress.busy;
  const broke = !busy && ((progress && progress.error) || failed);
  if (!busy && !broke) { keepScroll(() => { patchHtml(el, ""); }); return; }

  const steps = (progress && progress.steps) || [];
  const kept = steps.findIndex((s) => /^kept \w+ answer as row #/.test(s.text));
  const before = compactWorkings(kept < 0 ? steps : steps.slice(0, kept), "live:" + progress.turn);
  const after = compactWorkings(kept < 0 ? [] : steps.slice(kept + 1), "live:" + progress.turn + ":after");
  const secs = busy ? (Date.now() - turnStart) / 1000 : (progress && progress.elapsed) || 0;
  const w = progress && progress.writing;
  let html = before.filter((s) => !PLAIN || s.kind === "interim")
    .map((s, i) => liveStep(s, "live:" + i, false)).join("");

  if (busy) {
    const forming = w && w.reply ? md(w.reply)
      : w && w.mode === "thinking" ? ""
      : "";
    if (forming || kept >= 0) {
      html += `<div class="msg self"><div class="who"><b>${esc(MY_NAME)}</b>` +
        `<span class="when">${clockOnly(new Date())}</span></div>` +
        `<div class="body${kept < 0 ? " forming" : ""}">${forming}</div></div>`;
    }
  }
  if (!PLAIN) html += after.map((s, i) => liveStep(s, "live:a" + i, true)).join("");

  const last = steps[steps.length - 1];
  if (broke) {
    const why = (progress && progress.error) || (state && state.last_turn && state.last_turn.error) || "the turn broke";
    html += `<div class="status broke"><span class="clock">${secs.toFixed(1)}s</span>` +
      `<span class="what">stopped: ${esc(why)}\nsaying anything tries again · the whole of it is under Developer → last turn</span></div>`;
  } else {
    let what = plainWhat(last, kept, w, steps);
    if (!PLAIN) {
      what = last?.kind === "native" ? nativeWorkingLabel(steps) : last ? last.text : "waking up";
      if (kept >= 0 && last && last.kind !== "snag") {
        what = "finishing the turn — housekeeping";
      } else if (w && w.mode === "thinking" && w.thought_chars) {
        what = "thinking it through — about " + Math.round(w.thought_chars / 5) + " words of it so far";
      } else if (w && w.mode === "writing" && kept < 0) {
        what = "writing the answer";
      }
    }
    html += `<div class="status"><span class="clock">${secs.toFixed(1)}s</span>` +
      `<span class="what">${esc(what)}</span></div>`;
  }
  keepScroll(() => { patchHtml(el, html); });
}

// --- the header --------------------------------------------------------------

function gb(bytes) {
  return bytes ? (bytes / 1e9).toFixed(1) + " GB" : "";
}

// The automatic memory in one line: which model, and where it stands.
function recallLine(rc) {
  if (!rc || !rc.model) return { text: "automatic memory: off", cls: "off" };
  const name = rc.label || rc.model;
  const size = gb(rc.size_bytes) || (rc.about_gb ? "about " + rc.about_gb + " GB" : "");
  const ph = rc.phase;
  let state = ph, cls = "";
  if (ph === "loaded") { state = "loaded"; cls = "on"; }
  else if (ph === "working") { state = "working"; cls = "busy"; }
  else if (ph === "loading") { state = "loading" + (rc.loading_for_s ? " " + Math.round(rc.loading_for_s) + "s" : "\u2026"); cls = "busy"; }
  else if (ph === "checking") { state = "checking\u2026"; cls = "busy"; }
  else if (ph === "downloading") {
    const d = rc.download || {};
    const pct = d.total ? Math.round((d.downloaded || 0) / d.total * 100) + "%" : "";
    state = "downloading " + pct; cls = "busy";
  }
  else if (ph === "not downloaded") { state = "not downloaded"; cls = "bad"; }
  else if (ph === "no server") { state = "LM Studio not running"; cls = "bad"; }
  else if (ph === "on disk") { state = "on disk, not loaded"; cls = "bad"; }
  else if (ph === "failed") { state = "failed"; cls = "bad"; }
  return { text: "automatic memory: " + name + " \u00b7 " + state + (size ? " \u00b7 " + size : ""), cls };
}

function renderRecall() {
  const el = $("recall");
  if (!el) return;
  const rc = progress && progress.recall;
  const line = recallLine(rc);
  el.className = "recall " + line.cls;
  el.textContent = line.text;
  el.title = (rc && rc.detail) ? rc.detail : "the automatic memory \u2014 details under Menu";
}

const WIN_NAME = {
  five_hour: "5-hour limit",
  seven_day: "Weekly · all models",
  seven_day_opus: "Weekly · Opus",
  seven_day_sonnet: "Weekly · Sonnet",
  seven_day_oauth_apps: "Weekly · apps",
};

const NO_NUMBER = "The room has not managed to read the plan yet -- no login on this " +
  "machine, or the account did not answer. The Developer tab says which.";

function resetsText(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const diff = d.getTime() - Date.now();
  if (diff <= 0) return "resets soon";
  if (diff < 24 * 3600 * 1000) {
    const h = Math.floor(diff / 3600000), m = Math.round((diff % 3600000) / 60000);
    return `resets in ${h ? h + " hr " : ""}${m} min`;
  }
  return `resets ${DAY[d.getDay()]} ${clockOnly(d)}`;
}

function winHtml(l) {
  const name = l.label || WIN_NAME[l.window] ||
    String(l.window || "?").replace(/_/g, " ");
  const f = l.used_fraction;
  const pct = f === null || f === undefined ? null : Math.round(f * 100);
  const tone = pct === null ? "" : pct >= 90 ? "hot" : pct >= 75 ? "warm" : "";
  const over = l.using_overage ? " · in overage" : "";
  // No number is a state, not a broken gauge: the plan said the window is
  // open (or near its limit, or shut) and nothing more.
  const STATE = { allowed: "open", allowed_warning: "near limit", rejected: "shut" };
  const pctHtml = pct === null
    ? `<span class="pct none" title="${esc(NO_NUMBER)}">${esc(STATE[l.status] || l.status || "—")}</span>`
    : `<span class="pct">${pct}%</span>`;
  return `<div class="win ${tone}"><span class="name">${esc(name)}</span>` +
    `<span class="when">${esc(resetsText(l.resets_at))}${esc(over)}</span>${pctHtml}` +
    `<div class="bar"><i style="width:${pct || 0}%"></i></div></div>`;
}

function creditsHtml(credits) {
  if (!credits) return "";
  if (credits.unlimited) {
    return `<div class="win credits" title="Codex reports an unlimited credit balance">` +
      `<span class="name">Credits</span><span class="when">remaining</span>` +
      `<span class="pct">unlimited</span></div>`;
  }
  const exact = Number(credits.balance);
  if (!Number.isFinite(exact) || exact < 0) return "";
  const whole = Math.floor(exact);
  const how = `${credits.balance} credits remaining, read live from Codex`;
  return `<div class="win credits" title="${esc(how)}">` +
    `<span class="name">Credits</span><span class="when">remaining</span>` +
    `<span class="pct">${whole}</span></div>`;
}

function renderUsage() {
  if (!state || !state.prompt) return;
  const me = state.prompt.self || {};
  const real = me.measured_input_tokens_last_turn;
  const used = me.prompt_tokens_est ?? real ?? 0;
  // The ceiling belongs to whichever model the assistant is NOW.
  // `self.context_max_tokens` is only as fresh as its last turn, so right
  // after a switch it is the model it just stopped being -- and the percentage under it would be measured
  // against the wrong window. The live catalogue's figure wins when there is
  // one; `pv` is read just below and hoisted here for it.
  const pvNow = (progress && progress.provider) || (state.prompt.paused_capabilities ? {
    codex_only: true, service: (me.model || "").split("/")[0],
    label: me.model_name, service_label: me.thinking_through,
    limits: (state.prompt.plan || {}).limits,
    limits_error: (state.prompt.plan || {}).error,
  } : null);
  const cap = (pvNow && pvNow.price && pvNow.price.context)
    || me.context_max_tokens;
  const pct = cap ? Math.round(used / cap * 100) : null;
  const plan = pvNow && pvNow.codex_only
    ? (pvNow.service === "codex" ? "ChatGPT subscription" : "Codex-only mode")
    : (state.prompt.plan || {}).plan || "";
  const how = "estimated current room prompt; target " + k(me.budget_target_tokens || 0) +
    (real != null ? "; previous initial prompt measured " + k(real) : "; no separate prompt measurement yet");
  // Which model it is comes off the poll, not off its last turn: a model
  // picked a moment ago must show now, and `self.model` is only as fresh as
  // the last thing it said. The model's display name -- "Opus 5", not the
  // room's key.
  const pv = pvNow;
  const mine = pv ? pv.label : (me.model_name || me.model || "?");
  const road = pv && pv.service !== "claude_code" ? " · " + pv.service_label : "";
  let html = `<div class="model"><span title="${esc(how)}">${esc(mine)}${esc(road)} · ` +
    `${k(used)}${cap ? " of " + roomy(cap) : ""}${pct !== null ? " · " + pct + "%" : ""}</span>` +
    `<span class="plan">${esc(plan)}</span></div>`;
  // The live reading off the plan, refreshed by the poll while nobody is
  // typing. What came home with the last turn is the fallback: it is only
  // as fresh as the turn, and the stream it came from carries no figure.
  const lim = pv && pv.service === "codex"
    ? (pv.limits || [])
    : pv && pv.codex_only ? []
    : (progress && progress.limits) || (state.prompt.plan || {}).limits || [];
  // The plan's windows measure the plan. On a paid road they are somebody
  // else's gauge -- still true, still worth seeing, but not what the
  // assistant is spending -- so what is left on THIS road goes above them, in the same
  // shape as the windows: a figure on the right, and a bar that fills as it
  // goes. That is the gauge on a road billed in money.
  if (pv && pv.service !== "claude_code") {
    const L = pv.left;
    const has = (v) => v !== null && v !== undefined;
    const pct = L && has(L.used_fraction) ? Math.round(L.used_fraction * 100) : null;
    const tone = pct === null ? "" : pct >= 90 ? "hot" : pct >= 75 ? "warm" : "";
    if (L && L.reads === "spent" && has(L.spent_usd)) {
      // Real dollars, read off OpenAI's own counter and counted UP towards
      // their own ceiling. The other road counts down from a balance, which
      // is why the row says which it is doing rather than leaving the reader
      // to guess from the size of the number.
      const each = has(L.today_usd) && Math.abs(L.today_usd - L.spent_usd) > 0.01
        ? " · today " + money(L.today_usd, 2) : "";
      const how = "read from OpenAI a moment ago — " + money(L.spent_usd, 2) +
        " spent this " + (L.interval || "month") +
        (has(L.of_usd) ? " against their " + money(L.of_usd) +
          " organisation spend limit" : "") +
        (L.enforced ? ", which does stop spending at it."
                    : ". Written down as a ceiling, not enforced as a stop.");
      html += `<div class="win paid ${tone}" title="${esc(how)}">` +
        `<span class="name">${esc(pv.service_label)}</span>` +
        `<span class="when">${esc((has(L.of_usd) ? "spent of " + money(L.of_usd)
                                                 : "spent") + each)}</span>` +
        `<span class="pct">${esc(money(L.spent_usd))}</span>` +
        `<div class="bar"><i style="width:${pct || 0}%"></i></div></div>`;
    } else if (L && has(L.balance_usd)) {
      // A balance, counted down. Live from OpenRouter, or — when nothing
      // could be measured — the old reckoning, which says so on its face.
      const how = L.real === false
        ? "an ESTIMATE, not a reading: what you noted, less what the assistant's own " +
          "turns were priced at since. Anything else spending the same key " +
          "is not in it." + (L.why ? " The real figure is missing because " +
            L.why : "")
        : L.from === "OpenRouter, live"
          ? "read from OpenRouter a moment ago"
          : "what you noted, less what has been spent since";
      html += `<div class="win paid ${tone}${L.real === false ? " muted" : ""}"` +
        ` title="${esc(how)}">` +
        `<span class="name">${esc(pv.service_label)}</span>` +
        `<span class="when">${L.real === false ? "estimate · " : ""}` +
        `${esc(L.of_usd ? "of " + money(L.of_usd) : "left")}` +
        `${L.today_usd ? " · today " + esc(money(L.today_usd, 2)) : ""}</span>` +
        `<span class="pct">${esc(money(L.balance_usd))}</span>` +
        `<div class="bar"><i style="width:${pct || 0}%"></i></div></div>`;
    } else if (L && L.why) {
      // Nothing measured and nothing noted. The reason goes up in place of a
      // figure — never the estimate wearing the real one's clothes.
      html += `<div class="win paid muted" title="${esc(L.why)}">` +
        `<span class="name">${esc(pv.service_label)}</span>` +
        `<span class="when">${pv.service === "codex" ? "check limits in Codex" : "spend not read"}</span>` +
        `<span class="pct none">—</span>` +
        `<div class="bar"><i></i></div></div>`;
    } else if (pv.price && pv.price.known) {
      // No balance to show yet — either nothing noted for this service, or
      // the first read has not come back. The price is the honest stand-in.
      html += `<div class="win paid"><span class="name">${esc(pv.service_label)}</span>` +
        `<span class="when">billed per token</span>` +
        `<span class="pct none">${esc(money(pv.price.out_per_m))}/M</span>` +
        `<div class="bar"><i></i></div></div>`;
    }
  }
  html += lim.map(winHtml).join("");
  if (pv && pv.service === "codex") html += creditsHtml(pv.credits);
  if (!lim.length && !(pv && pv.codex_only && pv.service !== "codex")) {
    for (const name of ["5-hour limit", "Weekly"]) {
      const absent = pv && pv.service === "codex"
        ? (pv.limits_error || "Codex has not returned the subscription limits yet.")
        : NO_NUMBER;
      html += `<div class="win muted" title="${esc(absent)}">` +
        `<span class="name">${esc(name)}</span>` +
        `<span class="when">not read yet</span><span class="pct none">—</span>` +
        `<div class="bar"><i></i></div></div>`;
    }
  }
  keepScroll(() => { patchHtml($("usage"), html); });
}

// --- the developer tab -------------------------------------------------------

function dev(key, title, sub, inner) {
  const open = OPEN.has(key) ? " open" : "";
  return `<details class="dev" data-k="${esc(key)}"${open}><summary>${esc(title)}` +
    `${sub ? `<span class="sub">${esc(sub)}</span>` : ""}</summary>` +
    `<div class="inner">${inner}</div></details>`;
}

function fold(key, cls, label, text, meta, inner) {
  const open = OPEN.has(key) ? " open" : "";
  return `<details class="fold ${cls}" data-k="${esc(key)}"${open}><summary>` +
    `<span class="lbl">${label}</span><span class="txt">${esc(text)}</span>` +
    `${meta ? `<span class="meta">${esc(meta)}</span>` : ""}</summary>${inner}</details>`;
}

function facts(pairs) {
  return `<dl class="facts">` + pairs
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([kk, v]) => `<dt>${esc(kk)}</dt><dd>${esc(String(v))}</dd>`).join("") + `</dl>`;
}

function devMemory() {
  const p = state.prompt || {}, me = p.self || {};
  const idx = p.out_of_reach || {};
  let html = facts([
    ["messages in memory", me.messages_loaded],
    ["essences in memory", me.essences_loaded],
    ["costs, estimated", "~" + (me.prompt_tokens_est || 0).toLocaleString() + " tokens a turn"],
    ["previous initial prompt, measured", me.measured_input_tokens_last_turn
      ? me.measured_input_tokens_last_turn.toLocaleString() + " tokens in" : ""],
    ["aims to sit at", (me.budget_target_tokens || 0).toLocaleString() + " tokens"],
    ["out of memory", (idx.total_dropped || 0) + " rows, all still on disk"],
    ["rows on disk in all", me.rows_kept_on_disk],
  ]);
  html += fold("dev:spark", "tool", MY_NAME + "'s Spark", "version " + (me.spark_version ?? "?") +
    " · read first every turn · ~" + (((state.contract || {}).tokens_est || {}).spark || 0) + " tokens",
    "", `<div class="body">${mdBlock((state.contract || {}).spark || p.spark || "")}</div>`);
  // Their free notes, exactly as the assistant reads them, right under the Spark --
  // because that is where they ride. The box that writes them is its own
  // section below; this is the mirror of what it was sent.
  // Built here from the saved notes rather than read off the contract, so a
  // save shows in the mirror at once instead of after the next state.
  const free = notesBlock();
  html += fold("dev:notes-mirror", "tool", "Their free notes",
    free ? "ride under the Spark every turn · ~" + Math.max(1, Math.round(free.length / 4)) + " tokens"
         : "none written · nothing rides",
    "", `<div class="body">${free ? `<pre>${esc(free)}</pre>` : `<p class="quiet">nothing yet — the box is in the notes section below.</p>`}</div>`);

  html += `<div class="block-title">essences in memory · ${(p.essences || []).length}</div>`;
  html += (p.essences || []).map((e) => essHtml({ ...e, loaded: true }, false, "dev:")).join("")
    || `<p class="quiet">none</p>`;

  html += `<div class="block-title">messages in memory · ${(p.messages || []).length}</div>`;
  html += (p.messages || []).map((m) => {
    let who = WHO[m.kind] || m.kind, cls = m.kind;
    if (m.kind === "tell") who = `${MY_NAME} → ${m.to || "?"}`;
    if (m.kind === "worker") who = `${m.from || "an errand"} → ${MY_NAME}`;
    // The prompt says who spoke on labelled lines; the mirror of it here
    // says the same, so what the reader sees is what the assistant was sent.
    if (m.kind === "user" && m.who) who = (PEOPLE[m.who] || m.who) + (m.via ? " · " + m.via : "");
    const meta = `#${m.id} · ${when(m.dt)} · ~${m.tokens_est}`;
    return fold("dev:msg:" + m.id, cls, esc(who), firstLine(m.text, 110), meta,
                `<div class="body">${md(m.text)}</div>`);
  }).join("") || `<p class="quiet">none</p>`;
  return html;
}

// The harness, cut at its headings, so each tool is one folded item.
function harnessSections(text) {
  const out = [];
  let cur = null;
  for (const line of (text || "").split("\n")) {
    const m = line.match(/^(#{2,3})\s+(.*)$/);
    if (m) {
      cur = { level: m[1].length, title: m[2].trim(), lines: [] };
      out.push(cur);
    } else if (cur) {
      cur.lines.push(line);
    }
  }
  return out.map((s) => ({ ...s, body: s.lines.join("\n").trim() }));
}

function oneLiner(body) {
  const t = body.replace(/\s+/g, " ").trim();
  const m = t.match(/^(.{20,160}?[.!?])(\s|$)/);
  return (m ? m[1] : t.slice(0, 140)) || "";
}

function devTools() {
  const c = state.contract || {};
  const sections = harnessSections(c.harness || "");
  const answerAt = sections.findIndex((s) => /What I answer with/i.test(s.title));
  const answer = answerAt >= 0 ? sections[answerAt] : null;
  // The one-line description of each field, from the list in that section.
  const brief = {};
  for (const line of (answer ? answer.body : "").split("\n")) {
    const m = line.match(/^-\s+`([a-z_]+)`\s+—\s+(.*)$/);
    if (m) brief[m[1]] = m[2];
  }
  const props = ((c.answers_with || {}).properties) || {};
  const schemaOf = (key) => {
    const s = props[key];
    if (!s) return "";
    return `<div class="cap">the shape ${esc(MY_NAME)} sends</div><pre>${esc(JSON.stringify(s.items || s, null, 2))}</pre>`;
  };
  const toolOf = (title) => {
    const m = title.match(/`([a-z_]+)`/);
    return m ? m[1] : null;
  };
  const tools = answerAt >= 0 ? sections.slice(answerAt + 1).filter((s) => s.level === 3) : [];
  const handed = answerAt >= 0 ? sections.slice(0, answerAt) : sections;
  const rest = answerAt >= 0 ? sections.slice(answerAt + 1).filter((s) => s.level === 2) : [];

  let html = `<p class="quiet">Everything here is read off ${esc(MY_NAME)}'s actual instructions, so it cannot go stale.</p>`;
  html += `<div class="block-title">what ${esc(MY_NAME)} can do · the tools it answers with</div>`;
  const lines = Object.keys(brief).filter((kk) => !tools.some((s) => toolOf(s.title) === kk));
  html += tools.map((s) => {
    const key = toolOf(s.title);
    const text = (key && brief[key]) || oneLiner(s.body);
    const label = key ? `<code>${esc(key)}</code>` : md(s.title);
    return fold("tool:" + s.title, "tool", label, text, "",
                `<div class="body"><h4 style="margin-top:4px">${md(s.title)}</h4>${mdBlock(s.body)}${key ? schemaOf(key) : ""}</div>`);
  }).join("");
  // Fields with a one-liner but no section of their own.
  html += lines.map((kk) => fold("tool:" + kk, "tool", `<code>${esc(kk)}</code>`, brief[kk], "",
    `<div class="body">${mdBlock(brief[kk])}${schemaOf(kk)}</div>`)).join("");

  html += `<div class="block-title">what ${esc(MY_NAME)} is handed every turn</div>`;
  html += handed.map((s) => fold("tool:" + s.title, "tool", md(s.title), oneLiner(s.body), "",
    `<div class="body">${mdBlock(s.body)}</div>`)).join("");
  if (rest.length) {
    html += `<div class="block-title">how ${esc(MY_NAME)} is asked to behave</div>`;
    html += rest.map((s) => fold("tool:" + s.title, "tool", md(s.title), oneLiner(s.body), "",
      `<div class="body">${mdBlock(s.body)}</div>`)).join("");
  }
  html += fold("tool:all", "tool", "the whole of the instructions", "as sent, ~" +
    ((c.tokens_est || {}).harness || 0) + " tokens every turn", "",
    `<div class="body">${mdBlock(c.harness)}</div>`);
  return html;
}

function devBackup() {
  const b = state.backup || {};
  const last = b.last
    ? `last copy ${shortWhen(b.last.taken)} · ${b.last.file} · ${b.last.size_kb} KB · ${b.count} on the shelf`
    : "no copy taken yet";
  const tracked = b.tracked
    ? `the tracked copy is from ${shortWhen(b.tracked.taken)}, ${b.tracked.size_kb} KB`
    : "there is no tracked copy yet";
  return `<p><button class="btn" id="backup">take a backup now</button> ` +
    `<span class="quiet" id="backup-note">${esc(last)}</span></p>` +
    `<p class="quiet">A checked copy of ${esc(MY_NAME)}'s store, safe to take mid-sentence — ` +
    `the one thing here that cannot be rebuilt. Every press stacks a new zip in ` +
    `${esc(b.where || "the home's all_backups folder")}, outside the repo, and rewrites ` +
    `data_backups/assistant-backup.zip, which is tracked and leaves with the next push — ` +
    `${esc(tracked)}.</p>`;
}

function devLastTurn() {
  const t = state.last_turn || {};
  const steps = (progress && progress.steps) || [];
  let html = "";
  if (t.error) html += `<p class="bad">${esc(t.error)}</p>`;
  html += fold("dev:steps", "tool", "the turn, step by step", steps.length ? steps.length + " steps" : "nothing yet", "",
    `<div class="steps" style="margin-top:8px">${stepsHtml(steps)}</div>`);
  html += fold("dev:sent", "tool", "what " + MY_NAME + " was sent", "the whole object, as bytes", "",
    `<pre>${esc(JSON.stringify(state.prompt, null, 2))}</pre>`);
  html += fold("dev:answer", "tool", "what " + MY_NAME + " answered", t.answer ? "the reply and every operation" : "nothing yet", "",
    `<pre>${esc(JSON.stringify({ answer: t.answer, applied: t.applied }, null, 2))}</pre>`);
  return html;
}

function devPlan() {
  const p = state.prompt || {};
  const pv = (progress && progress.provider) || {};
  if (pv.codex_only) {
    return facts([
      ["mode", "Codex-only mode"],
      ["subscription", pv.service === "codex" ? "ChatGPT subscription" : "none selected"],
      ["subscription windows", (pv.limits || []).map(l =>
        `${l.label || l.window}: ${l.used_fraction == null ? "no number" : Math.round(l.used_fraction * 100) + "%"}`).join(" · ") || pv.limits_error || "not read yet"],
      ["paused", (pv.paused || []).map(x => x.capability + ": " + x.reason).join(" · ")],
    ]);
  }
  const five = (p.spend && p.spend.last_5h) || {};
  const lim = (progress && progress.limits) || (p.plan && p.plan.limits) || [];
  return facts([
    ["plan", p.plan && p.plan.plan],
    ["the plan's windows", lim.length
      ? lim.map((l) => (l.label || WIN_NAME[l.window] || l.window) + ": " +
          (l.used_fraction == null ? "no number given" : Math.round(l.used_fraction * 100) + "%") +
          (l.resets_at ? ", resets " + shortWhen(l.resets_at) : "")).join("   ·   ")
      : "not read yet"],
    // Why the gauge is blank, when it is. Reading it needs the owner's login
    // on this machine; without one there is no number to have.
    ["reading the plan", (progress && progress.limits_read) || ""],
    ["our own count, last 5h", `${five.turns || 0} turns · ${(five.input_tokens || 0).toLocaleString()} in · ` +
      `${(five.output_tokens || 0).toLocaleString()} out` +
      (five.cost_all_usd ? ` · ~$${five.cost_all_usd.toFixed(2)}` : "")],
    ["talking since", p.spend && p.spend.talking_since ? shortWhen(p.spend.talking_since) : ""],
  ]);
}

// The automatic memory's knobs: the name the room keeps it by, its label,
// the keys a phone offers for it, and what it does.
const RECALL_KNOBS = [
  ["timeout_s", "hard limit, s", "decimal", "a turn never waits longer than this for it; when it misses, the turn goes ahead without it"],
  ["lines", "writer reads, lines", "numeric", "how many of the last lines of the room the writer reads to write its search"],
  ["review_lines", "reviewer reads, lines", "numeric", "how many of the last lines of the room the reviewer reads to judge each result against"],
  ["top", "top by likeness", "numeric", "how many of the closest essences are shown and reviewed"],
  ["keyword_top", "keyword finds", "numeric", "how many essences found by keyword are added, when they are not already among the top"],
  ["read_chars", "reviewer reads, chars", "numeric", "how much of each result the reviewer reads, from the top"],
];
// What the knobs last said -- saved, or why not -- and the saves, each one
// behind the last, so Enter, a click away and a button never cross.
let KNOB_SAYS = null;
let KNOB_SAVE = Promise.resolve();

// The automatic memory's own section: the dropdown, its state with the
// download and load buttons, the hard limit, and a try-it-now.
function devRecall() {
  const rc = (progress && progress.recall) || {};
  const models = rc.models || [];
  const opts = [`<option value=""${!rc.model ? " selected" : ""}>none</option>`].concat(
    models.map((m) => `<option value="${esc(m.key)}"${rc.model === m.key ? " selected" : ""}>${esc(m.label)}</option>`));
  const line = recallLine(rc);
  let action = "";
  if (rc.model && rc.phase === "not downloaded") {
    action = `<button class="btn" id="recall-download">download` +
      (rc.about_gb ? ", about " + rc.about_gb + " GB" : "") + `</button>`;
  } else if (rc.model && rc.phase === "on disk") {
    action = `<button class="btn" id="recall-load">load now</button>`;
  } else if (rc.model && (rc.phase === "failed" || rc.phase === "no server")) {
    action = `<button class="btn" id="recall-load">try again</button>`;
  }
  let bar = "";
  if (rc.phase === "downloading" && rc.download) {
    const d = rc.download;
    const pct = d.total ? Math.round((d.downloaded || 0) / d.total * 100) : 0;
    bar = `<div class="dl"><div class="bar"><i style="width:${pct}%"></i></div>` +
      `<span class="quiet">${gb(d.downloaded)} of ${gb(d.total)}` +
      (d.bps ? " \u00b7 " + (d.bps / 1e6).toFixed(1) + " MB/s" : "") + `</span></div>`;
  }
  let html = `<p class="recall-row"><label>model <select id="recall-model">${opts.join("")}</select></label>` +
    `<span class="state ${line.cls}">${esc(line.text.replace(/^automatic memory: /, ""))}</span> ${action}</p>` + bar;
  if (rc.detail) html += `<p class="quiet">${esc(rc.detail)}</p>`;
  const kn = rc.knobs || {}, kb = rc.knob_bounds || {};
  // A number typed and not yet saved is kept through every redraw -- each
  // turn redraws this section -- and wears ember until it is saved, so it
  // can never pass for the one the room is using. Text boxes, not number
  // boxes, so a mouse wheel passing over one cannot move it.
  const drafts = knobDrafts();
  const open = Object.keys(drafts).length > 0;
  const says = KNOB_SAYS && (KNOB_SAYS.bad ? open : Date.now() < KNOB_SAYS.until) ? KNOB_SAYS : null;
  html += `<p class="recall-row knobs">` + RECALL_KNOBS.map(([name, label, keys, title]) => {
    const b = kb[name] || {};
    const saved = kn[name] != null ? String(kn[name]) : "";
    const draft = drafts[name];
    const range = b.min != null ? ` (${b.min}\u2013${b.max})` : "";
    return `<label title="${esc(title + range)}; Enter or a click away saves it, Esc puts it back">${label} ` +
      `<input class="knob${draft != null ? " draft" : ""}" data-knob="${name}" type="text" inputmode="${keys}" ` +
      `autocomplete="off" spellcheck="false" data-saved="${esc(saved)}" value="${esc(draft != null ? draft : saved)}"></label>`;
  }).join("") +
    `<button class="small-btn knob-save" id="recall-knobs-set" type="button"${open ? "" : " hidden"}>save</button>` +
    `<button class="small-btn" id="recall-knobs-reset" type="button" title="back to the defaults">defaults</button>` +
    (says ? `<span class="knob-says ${says.bad ? "bad" : "good"}">${esc(says.text)}</span>` : "") + `</p>`;
  html += `<p class="recall-row"><button class="btn" id="recall-try"${rc.model && rc.phase === "loaded" ? "" : " disabled"}>ask it now</button>` +
    `<span class="quiet" id="recall-try-note">runs it on the room as it stands \u2014 shown here, kept nowhere</span></p>`;
  const last = rc.last;
  if (last) {
    html += `<div class="quiet" style="margin-top:8px">last run${last.late ? " \u00b7 late \u2014 finished after the limit, so " + esc(MY_NAME) + " did not get it" : ""}</div>` +
      recallHtml(last, "dev:recall-last");
    const sent = rc.last_sent || {};
    const calls = [["write", sent.write], ["review", sent.review]].filter(([, m]) => m && m.length);
    if (calls.length) {
      const text = calls.map(([name, msgs]) => "=== " + name + " ===\n\n" +
        msgs.map((m) => "[" + m.role + "]\n" + m.content).join("\n\n")).join("\n\n\n");
      html += fold("dev:recall-sent", "tool", "what the model was sent", "both calls, whole", "",
        `<pre>${esc(text)}</pre>`);
    }
  }
  html += facts([
    ["how it works", `before each turn: the model looks at a few of ${MY_NAME}'s essences for their style and the last ${kn.lines || 5} lines of the room, and writes an essence-shaped search, keywords and maybe dates. The search returns the top ${kn.top || 3} by likeness and up to ${kn.keyword_top != null ? kn.keyword_top : 2} keyword finds. The model reviews each against the last ${kn.review_lines || 5} lines: a note and how much ${MY_NAME} would want to read it. ${MY_NAME} gets exactly what the fold in the room shows.`],
    ["hosted by", rc.server ? "LM Studio at " + rc.server + " \u2014 the room loads and frees the model, LM Studio runs it" : ""],
    ["model key", rc.model || ""],
    ["embedder", rc.embedder === "warm" ? "warm" : rc.embedder === "warming" ? "warming up (about 13 s the first time)"
      : rc.embedder === "unavailable" ? "unavailable \u2014 the search cannot run" : "cold \u2014 warms when a model is chosen"],
  ]);
  return html;
}

// The knobs whose box holds a number the room is not using, {name: typed}.
// Read off the boxes themselves, so a redraw can put them back.
function knobDrafts() {
  const out = {};
  for (const el of MAIN.querySelectorAll("input.knob")) {
    const typed = String(el.value).trim();
    if (typed !== String(el.dataset.saved || "")) out[el.dataset.knob] = typed;
  }
  return out;
}

// The ember and the save button, straight on the boxes: the row is not
// redrawn while somebody is in it.
function markKnobs() {
  const drafts = knobDrafts();
  for (const el of MAIN.querySelectorAll("input.knob")) el.classList.toggle("draft", el.dataset.knob in drafts);
  const save = $("recall-knobs-set");
  if (save) save.hidden = !Object.keys(drafts).length;
}

// Saved the moment it is meant: Enter, a click anywhere else, or the save
// button. A number typed and walked away from used to sit in its box looking
// saved, until the next turn redrew the row with the old one and the run
// went on using it. `reset` puts every knob back to its default instead.
function saveKnobs(reset = false) {
  const run = KNOB_SAVE.then(() => saveKnobsNow(reset));
  KNOB_SAVE = run.catch(() => {});
  return run;
}

async function saveKnobsNow(reset) {
  const label = (name) => (RECALL_KNOBS.find((k) => k[0] === name) || [, name])[1];
  const body = reset ? { reset: true } : {}, sent = new Map(), bad = [];
  if (!reset) {
    for (const el of MAIN.querySelectorAll("input.knob")) {
      const typed = String(el.value).trim();
      // Unchanged, or still being typed in: that one waits for its own
      // Enter or click away.
      if (typed === String(el.dataset.saved || "") || el === document.activeElement) continue;
      if (typed === "") el.value = el.dataset.saved;    // emptied and left: as it was
      else if (!Number.isFinite(Number(typed))) bad.push(label(el.dataset.knob) + ": \u201c" + typed + "\u201d is not a number");
      else { body[el.dataset.knob] = Number(typed); sent.set(el, typed); }
    }
  }
  if (bad.length) KNOB_SAYS = { text: bad.join(" \u00b7 "), bad: true };
  if (reset || sent.size) {
    try {
      const got = await post("api/recall/settings", body);
      if (progress) progress.recall = got;
      const kept = got.knobs || {}, kb = got.knob_bounds || {}, held = [];
      for (const el of MAIN.querySelectorAll("input.knob")) {
        const name = el.dataset.knob, now = String(kept[name]);
        if (!reset && !sent.has(el)) continue;
        // A box typed in again while this was on its way keeps the new number.
        if (reset || String(el.value).trim() === sent.get(el)) el.value = now;
        el.dataset.saved = now;
        // The room holds each knob to its bounds and a count to a whole one.
        if (!reset && Number(now) !== body[name]) {
          const b = kb[name] || {};
          held.push(label(name) + ": " + now + (Number(now) === b.max ? ", the most it takes"
            : Number(now) === b.min ? ", the least it takes" : ""));
        }
      }
      if (!bad.length) {
        KNOB_SAYS = { text: reset ? "back to the defaults" : "saved" + (held.length ? " \u2014 " + held.join(" \u00b7 ") : ""),
                      until: Date.now() + 4000 };
        setTimeout(paintRecall, 4100);
      }
    } catch (e) {
      KNOB_SAYS = { text: String(e.message || e), bad: true };
    }
  }
  markKnobs();
  paintRecall();
}

// The prompt file, raw. Its own piece, redrawn only when the file changes:
// redrawing it with the rest every two seconds threw away the reader's
// scroll and selection mid-read.
function devRecallConfig() {
  const rc = (progress && progress.recall) || {};
  return fold("dev:recall-config", "tool", "what it is told", (rc.config_path || "server/recall_prompts.py") +
    " \u2014 edit the file; it is read fresh on every run", "",
    (rc.config_error ? `<p class="bad">the file could not be read, so the built-in words are used: ${esc(rc.config_error)}</p>` : "") +
    `<pre>${esc(rc.config_text || "")}</pre>`);
}

// Try a search by hand: the automatic memory's own search on what is typed
// here, drawn with the same card a turn gets. Kept in memory so a redraw of
// the tab brings back what was typed and what came out.
const TRY = { essence: "", keywords: "", from: "", to: "", review: true, result: null, busy: false };

function devRecallSearch() {
  const field = (name, label, ph, wide) =>
    `<label class="try-field${wide ? " wide" : ""}">${label}` +
    `<input data-try="${name}" type="text" spellcheck="false" placeholder="${esc(ph)}" value="${esc(TRY[name])}"></label>`;
  return `<div class="try-search"><div class="cap">Try a search</div>` +
    `<label class="try-field wide">essence<textarea data-try="essence" rows="2" spellcheck="false" ` +
    `placeholder="what an essence about it would say">${esc(TRY.essence)}</textarea></label>` +
    `<div class="try-line">` + field("keywords", "keywords", "comma, separated") +
    field("from", "from", "YYYY-MM-DD") + field("to", "to", "YYYY-MM-DD") + `</div>` +
    `<div class="try-line"><button class="btn" id="try-go"${TRY.busy ? " disabled" : ""}>${TRY.busy ? "searching\u2026" : "search"}</button>` +
    `<label class="quiet"><input type="checkbox" id="try-review"${TRY.review ? " checked" : ""}> ` +
    `ask the reviewer whether each one fits what you typed (a few seconds; untick for the search alone)</label></div>` +
    `<div id="try-out">${TRY.result ? recallHtml(TRY.result, "try") : ""}</div></div>`;
}

function paintTry() {
  const out = $("try-out");
  if (out) out.innerHTML = TRY.result ? recallHtml(TRY.result, "try") : "";
  const go = $("try-go");
  if (go) { go.disabled = TRY.busy; go.textContent = TRY.busy ? "searching\u2026" : "search"; }
}

async function runTry() {
  if (TRY.busy) return;
  TRY.busy = true;
  RECALL_MORE.delete("try");
  paintTry();
  try {
    TRY.result = await post("api/recall/search", {
      asked: { essence: TRY.essence, keywords: TRY.keywords, from: TRY.from, to: TRY.to },
      review: TRY.review });
  } catch (e) {
    TRY.result = { missed: String(e.message || e), asked: null };
  }
  TRY.busy = false;
  paintTry();
}

function devRecallSection() {
  return `<div id="recall-live">${devRecall()}</div><div id="recall-search">${devRecallSearch()}</div>` +
    `<div id="recall-config">${devRecallConfig()}</div>`;
}

// Redraw only what changed, so a download's progress moves without the whole
// tab being redrawn under the reader -- and nothing is touched while someone
// is in its controls, or while what would be drawn is what is already there.
let painted = { live: null, config: null };

function paintRecall() {
  const live = $("recall-live"), conf = $("recall-config");
  if (!live || !conf) return;
  const a = document.activeElement;
  if (a && live.contains(a) && /^(SELECT|INPUT)$/.test(a.tagName)) return;
  const liveHtml = devRecall(), confHtml = devRecallConfig();
  keepScroll(() => {
    if (liveHtml !== painted.live) { patchHtml(live, liveHtml); painted.live = liveHtml; wireRecall(); }
    if (confHtml !== painted.config) { patchHtml(conf, confHtml); painted.config = confHtml; }
  });
}

function wireRecall() {
  const sel = $("recall-model");
  if (sel) sel.onchange = async () => {
    try {
      const got = await post("api/recall/choose", { model: sel.value || null });
      if (progress) progress.recall = got;
    } catch (e) { alert(e.message); }
    sel.blur();
    renderRecall(); paintRecall();
  };
  for (const el of MAIN.querySelectorAll("input.knob")) {
    el.oninput = markKnobs;
    el.onchange = () => saveKnobs();
    el.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); el.blur(); saveKnobs(); }
      if (e.key === "Escape") { el.value = el.dataset.saved; KNOB_SAYS = null; markKnobs(); el.blur(); paintRecall(); }
    };
  }
  const set = $("recall-knobs-set");
  if (set) set.onclick = () => saveKnobs();
  const reset = $("recall-knobs-reset");
  if (reset) reset.onclick = () => saveKnobs(true);
  for (const [id, url] of [["recall-download", "api/recall/download"], ["recall-load", "api/recall/load"]]) {
    const b = $(id);
    if (b) b.onclick = async () => {
      b.disabled = true;
      try {
        const got = await post(url);
        if (progress) progress.recall = got;
      } catch (e) { alert(e.message); b.disabled = false; return; }
      renderRecall(); paintRecall();
    };
  }
  const t = $("recall-try");
  if (t) t.onclick = async () => {
    t.disabled = true;
    $("recall-try-note").textContent = "asking\u2026";
    try {
      // A number typed just before is the one it runs with.
      await saveKnobs();
      await post("api/recall/try");
      await pollProgress();
    } catch (e) { $("recall-try-note").textContent = String(e.message || e); t.disabled = false; return; }
    renderRecall(); paintRecall();
  };
}

function devVoice() {
  const v = (state.contract || {}).voice || {};
  return facts([
    ["voice", v.on ? "on, speaking as " + (v.voice || "?") : "off — " + (v.reason || "no reason given")],
  ]);
}

// Their free notes: the box. Whoever is picked at the top writes their own,
// and what is saved rides next to the Spark on the next turn. The section
// stands still while the box is being written in -- a redraw every five
// seconds would otherwise throw a half-written note away -- and moves again
// the moment the text matches what the room has.
// Where the models we fetch ourselves are kept -- the embedder that reads
// the essences, and whatever the night trains from the shelf later. One
// folder, the owner's to choose; LM Studio's own is shown beside it and never
// touched. A change moves what is already downloaded, and the bar shows it.
function modelsSub() {
  const m = (state && state.models) || null;
  if (!m) return "";
  return (m.folder ? m.folder : "the cache on " + (m.drive || "C:")) + " · " + (gb(m.bytes) || "nothing there");
}

let modelsSeenFinish = null;

// The move ran in the background; when the poll says it finished, the box
// asks once for the folder as it now stands rather than drawing the stale
// leftovers from the last state.
function maybeRefreshModels(mv) {
  if (!mv || mv.active || !mv.finished || modelsSeenFinish === mv.finished) return;
  modelsSeenFinish = mv.finished;
  fetch("api/models").then((r) => r.json()).then((got) => {
    if (state) { state.models = got; renderDev(); }
  }).catch(() => {});
}

function devModels() {
  const m = state.models || {};
  const mv = (progress && progress.models_move) || m.move || null;
  maybeRefreshModels(progress && progress.models_move);
  const n = (m.items || []).length;
  let html = `<p class="recall-row"><label>folder <input class="path" id="models-folder" type="text" spellcheck="false" ` +
    `value="${esc(m.folder || "")}" placeholder="${esc(m.default || "")}" data-saved="${esc(m.folder || "")}"></label>` +
    `<button class="small-btn" id="models-set">keep them here</button>` +
    `<span class="quiet" id="models-note"></span></p>`;
  html += `<p class="quiet">read from ${esc(m.here || "?")} · ${gb(m.bytes) || "nothing"} in ${n} model${n === 1 ? "" : "s"}` +
    (m.free_bytes != null ? ` · ${gb(m.free_bytes)} free on ${esc(m.drive || "")}` : "") + `</p>`;
  if (n) html += `<ul class="models">` + (m.items || []).map((it) =>
    `<li>${esc(it.name)} <span class="quiet">${gb(it.bytes)}</span></li>`).join("") + `</ul>`;
  if (mv && (mv.active || mv.error || (mv.finished && Date.now() / 1000 - mv.finished < 300))) {
    const pct = mv.total ? Math.round((mv.done || 0) / mv.total * 100) : 0;
    let said;
    if (mv.active) said = `moving ${gb(mv.done)} of ${gb(mv.total)} from ${esc(mv.from)} — do not restart the room until it is done`;
    else if (mv.error) said = `the move stopped: ${esc(mv.error)}`;
    else said = `moved ${esc((mv.moved || []).join(", ") || "nothing")}` +
      (mv.skipped && mv.skipped.length ? ` · left alone: ${esc(mv.skipped.join(", "))}` : "") +
      (mv.left && mv.left.length ? ` · could not remove ${mv.left.length} old file${mv.left.length === 1 ? "" : "s"}` : "");
    html += `<div class="dl"><div class="bar"><i style="width:${mv.active ? pct : 100}%"></i></div><span class="quiet">${said}</span></div>`;
  }
  if (m.leftover) {
    html += `<p class="recall-row"><span class="quiet">still in ${esc(m.leftover.folder)}: ` +
      m.leftover.items.map((it) => esc(it.name) + " " + gb(it.bytes)).join(", ") + `</span>` +
      `<button class="small-btn" id="models-move" data-from="${esc(m.leftover.folder)}">move them here</button></p>`;
  }
  html += `<p class="quiet">LM Studio keeps its own in ${esc(m.lm_studio || "a folder it did not tell us")} — its setting, changed in LM Studio under My Models.</p>`;
  html += `<p class="quiet">The embedder that reads the essences downloads here. ` +
    `A new folder is read from on the next start; what is already downloaded is moved into it on its own.</p>`;
  return html;
}

function modelsBusy() {
  const el = $("models-folder");
  return !!el && el.value !== (el.dataset.saved || "");
}

function wireModels() {
  const set = $("models-set"), inp = $("models-folder"), note = $("models-note");
  if (set && inp) set.onclick = async () => {
    set.disabled = true;
    if (note) note.textContent = "…";
    try {
      const got = await post("api/models", { folder: inp.value });
      if (state) state.models = got;
      if (note) note.textContent = "";
    } catch (e) {
      if (note) note.textContent = String(e.message || e);
      set.disabled = false;
      return;
    }
    inp.blur();
    renderDev();
  };
  if (inp) inp.onkeydown = (e) => { if (e.key === "Enter" && set) set.click(); };
  const mv = $("models-move");
  if (mv) mv.onclick = async () => {
    mv.disabled = true;
    try {
      const got = await post("api/models/move", { from: mv.dataset.from });
      if (state) state.models = got;
    } catch (e) { alert(e.message); mv.disabled = false; return; }
    renderDev();
  };
}

function devNotes() {
  const who = whoNow();
  const all = state.notes || {};
  const n = all[who] || {};
  const cap = all.max_chars || 8000;
  const said = n.updated ? "saved " + shortWhen(n.updated) + " · " + (n.chars || 0) + " of " + cap.toLocaleString() + " characters"
    : "nothing written yet";
  return `<div class="my-notes" data-who="${esc(who)}">` +
    `<textarea id="my-notes" rows="6" maxlength="${cap}" spellcheck="false" ` +
    `placeholder="whatever you want ${esc(MY_NAME)} to always have in view — short, it is paid for every turn">${esc(n.text || "")}</textarea>` +
    `<p class="row"><button class="btn" id="my-notes-save" disabled>save</button>` +
    `<span class="quiet" id="my-notes-note">${esc(said)}</span></p>` +
    `<p class="quiet">Rides right under the Spark, every turn, as “${esc(((all.label || {})[who]) || (PEOPLE[who] || who) + "'s free notes")}: …”. ` +
    `${esc(MY_NAME)}'s to read, never to write; yours to change here whenever you like. It leaves in every backup beside the Spark.</p></div>`;
}

function notesTitle() {
  return (PEOPLE[whoNow()] || whoNow()) + "'s notes";
}

// The block as the assistant reads it, the same shape server/notes.py
// writes: each person's label, then the text -- on the same line when it is
// one line, under it when it is more -- in household order, only the ones
// written.
function notesBlock() {
  const all = (state && state.notes) || {};
  const labels = all.label || {};
  const parts = [];
  for (const who of Object.keys(PEOPLE)) {
    const text = ((all[who] || {}).text || "").trim();
    if (!text) continue;
    const label = labels[who] || (PEOPLE[who] || who) + "'s free notes";
    parts.push(label + (text.includes("\n") ? ":\n" : ": ") + text);
  }
  return parts.join("\n\n");
}

function notesSub() {
  const n = ((state && state.notes) || {})[whoNow()] || {};
  return n.text ? "~" + (n.tokens_est || 0) + " tokens, next to the Spark" : "nothing written yet";
}

// --- room background --------------------------------------------------------
// The page receives only a server-made artwork URL.  This still quotes it for
// CSS: Settings must not turn a malformed response into a CSS declaration.
function applyWallpaper(wallpaper) {
  const current = wallpaper && wallpaper.current;
  if (!current || !current.url) return;
  const url = String(current.url).replace(/["\\\n\r]/g, "\\$&");
  document.body.style.setProperty("--room-wallpaper", `url("${url}")`);
}

const WALLPAPER_MAX_BYTES = 10 * 1024 * 1024;
let WALLPAPER_NOTICE = null;
let wallpaperUploading = false;

function wallpaperFileProblem(file, maxBytes) {
  if (!file) return "choose an image first";
  const type = String(file.type || "").toLowerCase();
  if (!/^(image\/(png|jpeg|gif|webp))$/.test(type))
    return "PNG, JPEG, GIF and WEBP are supported";
  const cap = Number(maxBytes) || WALLPAPER_MAX_BYTES;
  if (file.size > cap)
    return (file.name || "that file") + " is larger than " + Math.round(cap / 1024 / 1024) + " MB";
  return "";
}

function wallpaperBusy() {
  const input = $("wallpaper-file");
  return wallpaperUploading || !!(input && input.files && input.files.length);
}

function devWallpaper(wp = (state && state.wallpaper) || {}) {
  const current = wp.current || null;
  const items = Array.isArray(wp.items) ? wp.items : [];
  const maxBytes = Number(wp.max_bytes) || WALLPAPER_MAX_BYTES;
  const note = WALLPAPER_NOTICE || {};
  let html = `<p class="quiet">Choose the scenery behind ${esc(MY_NAME)}'s room. New images are kept in ${esc(MY_NAME)}'s artwork collection; the original scenery stays available as the default.</p>`;
  if (current) {
    html += `<div class="wallpaper-current"><img src="${esc(current.url)}" alt="Current room background: ${esc(current.name)}">` +
      `<div><b>${esc(current.name)}</b><p class="quiet">${esc(current.width + "×" + current.height)} · ${esc(current.built_in ? "original room background" : "chosen wallpaper")}</p></div></div>`;
  } else {
    html += `<p class="bad">No usable room background was found. Add a supported image below.</p>`;
  }
  html += `<div class="wallpaper-upload"><input id="wallpaper-file" type="file" accept="image/png,image/jpeg,image/gif,image/webp">` +
    `<button id="wallpaper-upload" type="button" disabled>upload and use it</button></div>` +
    `<p id="wallpaper-note" class="wallpaper-note${note.bad ? " bad" : ""}" role="status">${esc(note.text || "PNG, JPEG, GIF or WEBP · up to " + Math.round(maxBytes / 1024 / 1024) + " MB")}</p>`;
  html += items.length
    ? `<div class="wallpaper-grid" role="radiogroup" aria-label="Room backgrounds">` + items.map((item) =>
      `<button class="wallpaper-option" type="button" role="radio" data-wallpaper="${esc(item.id)}" ` +
      `aria-checked="${item.id === wp.selected ? "true" : "false"}" title="Use ${esc(item.name)}">` +
      `<img src="${esc(item.url)}" alt=""><span class="wallpaper-name">${esc(item.name)}</span></button>`).join("") + `</div>`
    : `<p class="quiet">No supported images are in the collection yet.</p>`;
  return html;
}

function wireWallpapers(root = MAIN, rerender = renderDev) {
  const input = root.querySelector("#wallpaper-file"), upload = root.querySelector("#wallpaper-upload"),
    note = root.querySelector("#wallpaper-note");
  if (!input || !upload || !note) return;
  const maxBytes = ((state || {}).wallpaper || {}).max_bytes || WALLPAPER_MAX_BYTES;
  const say = (text, bad) => {
    note.textContent = text;
    note.classList.toggle("bad", !!bad);
    WALLPAPER_NOTICE = {text, bad: !!bad};
  };
  const updateUpload = () => {
    const problem = wallpaperFileProblem(input.files && input.files[0], maxBytes);
    upload.disabled = !!problem || wallpaperUploading;
    if (input.files && input.files[0]) say(problem || (input.files[0].name + " is ready to upload"), !!problem);
  };
  input.onchange = updateUpload;
  upload.onclick = async () => {
    const file = input.files && input.files[0];
    const problem = wallpaperFileProblem(file, maxBytes);
    if (problem) { say(problem, true); return; }
    wallpaperUploading = true;
    upload.disabled = true;
    say("uploading " + (file.name || "wallpaper") + "…", false);
    try {
      const data = await readAsDataURL(file);
      const got = await post("api/wallpapers/upload", {name: file.name, data});
      if (state) state.wallpaper = got;
      applyWallpaper(got);
      WALLPAPER_NOTICE = {text: "Uploaded and now using " + ((got.current || {}).name || file.name), bad: false};
      input.value = "";
      wallpaperUploading = false;
      rerender();
    } catch (e) {
      wallpaperUploading = false;
      upload.disabled = false;
      say(String(e.message || e), true);
    }
  };
  for (const button of root.querySelectorAll(".wallpaper-option")) button.onclick = async () => {
    if (wallpaperUploading || button.getAttribute("aria-checked") === "true") return;
    wallpaperUploading = true;
    for (const b of root.querySelectorAll(".wallpaper-option")) b.disabled = true;
    say("changing the room background…", false);
    try {
      const got = await post("api/wallpapers/choose", {id: button.dataset.wallpaper});
      if (state) state.wallpaper = got;
      applyWallpaper(got);
      WALLPAPER_NOTICE = {text: "Room background changed to " + ((got.current || {}).name || "the selected image"), bad: false};
      wallpaperUploading = false;
      rerender();
    } catch (e) {
      wallpaperUploading = false;
      for (const b of root.querySelectorAll(".wallpaper-option")) b.disabled = false;
      say(String(e.message || e), true);
    }
  };
}

// True while the box holds something the room does not have yet, or the
// cursor is in it -- compared against the notes of the person the box was
// drawn for, so switching the pick at the top still redraws it.
function notesBusy() {
  const box = $("my-notes");
  if (!box) return false;
  if (document.activeElement === box) return true;
  const who = (box.closest(".my-notes") || {}).dataset ? box.closest(".my-notes").dataset.who : whoNow();
  const saved = ((((state && state.notes) || {})[who]) || {}).text || "";
  return box.value !== saved;
}

function wireNotes() {
  const box = $("my-notes"), btn = $("my-notes-save"), note = $("my-notes-note");
  if (!box || !btn || !note) return;
  const who = box.closest(".my-notes").dataset.who;
  const saved = () => ((((state && state.notes) || {})[who]) || {}).text || "";
  box.oninput = () => {
    const dirty = box.value.trim() !== saved();
    btn.disabled = !dirty;
    if (dirty) note.textContent = "not saved yet";
  };
  btn.onclick = async () => {
    btn.disabled = true;
    note.textContent = "saving…";
    try {
      const got = await post("api/notes", { who, text: box.value });
      if (state) state.notes = got;
      note.textContent = "saved";
    } catch (e) {
      note.textContent = String(e.message || e);
      btn.disabled = false;
      return;
    }
    // The box now matches the room, so the section may move again; the
    // mirror under the Spark redraws with it.
    box.blur();
    renderDev();
  };
}

let backingUp = false;

// --- which model it thinks with -----------------------------------------------
//
// Several services can host the assistant, so it is a setting -- and a
// setting about money, which means the page has to be honest about which
// figures are read live and which are not there to read.
//
// The catalogue is fetched, not polled. It touches the network (OpenRouter's
// price list, the credit balance) and the two-second poll must never do that.
// The header's own line comes off the poll instead, which reads a cached
// table and is cheap.

let PROV = null;            // the catalogue, as last fetched
let PROV_ASKED = false;

async function loadProviders(force) {
  if (PROV && !force) return PROV;
  if (PROV_ASKED && !force) return PROV;
  PROV_ASKED = true;
  try {
    PROV = await getJson("api/providers");
  } catch (e) {
    PROV = { error: e.message };
  }
  // The server waits for a cold price list, but a fetch that is slow or
  // refused leaves the table blank -- and a page of dashes reads as broken
  // rather than as early. One quiet retry, once, and then it stands.
  if (PROV && !PROV.error && !PROV.prices_read_at && !force) {
    setTimeout(async () => {
      try {
        const again = await getJson("api/providers");
        if (again && again.prices_read_at) { PROV = again; paintProviders(); }
      } catch (e) { /* it stands as it is */ }
    }, 3000);
  }
  return PROV;
}

// A context window, said the way the model makers say it. k() gives "1000k"
// for a million, which is arithmetically fine and reads like a typo.
function roomy(n) {
  if (!n) return "";
  return n >= 1e6 ? (n / 1e6).toFixed(n % 1e6 ? 2 : 1).replace(/\.0$/, "") + "M"
                  : Math.round(n / 1000) + "k";
}

function money(n, dp) {
  if (n === null || n === undefined || isNaN(n)) return "";
  return "$" + Number(n).toFixed(dp === undefined ? 2 : dp);
}

// A price, said the way a person reads it: per million tokens, in then out.
function priceLine(p) {
  if (!p || !p.known) return "no price read";
  return money(p.in_per_m) + " in / " + money(p.out_per_m) + " out per million"
    + (p.context ? " · " + roomy(p.context) + " of room" : "");
}

// What one turn actually costs on this model -- the number the owner will
// care about, and not one anybody publishes. The prompt is measured; the
// reply is small beside it.
//
// Given as a range, because the true figure depends on how much of the prompt
// came out of the cache, and that is not knowable in advance. The two ends
// are exact: nothing cached, and all of it cached. A single number here would
// have to guess the fraction, and a guess can be wrong by half.
function perTurn(p, inTok, outTok) {
  if (!p || !p.known || !inTok) return "";
  const out = (outTok || 700) / 1e6 * p.out_per_m;
  const cold = inTok / 1e6 * p.in_per_m + out;
  const say = (c) => (c < 0.01 ? "$" + c.toFixed(4) : money(c));
  if (!p.cache_read_per_m || p.cache_read_per_m >= p.in_per_m)
    return "about " + say(cold) + " a turn at the current prompt size";
  const warm = inTok / 1e6 * p.cache_read_per_m + out;
  return say(warm) + "–" + say(cold) + " a turn at the current prompt size, " +
    "warm cache to cold";
}

function provMoneyLine(m, label, service) {
  if (!m) return "";
  const has = (v) => v !== null && v !== undefined;
  const bits = [];
  // The measured figure first and in bold, because on this page it is the one
  // number that is not a reckoning.
  if (has(m.spent_usd)) {
    bits.push("<b>" + esc(money(m.spent_usd, 2)) + " spent" +
      (has(m.limit_usd) ? " of " + esc(money(m.limit_usd)) : "") +
      (m.interval ? " this " + esc(m.interval) : "") + "</b>" +
      (has(m.used_fraction)
        ? " (" + Math.round(m.used_fraction * 100) + "%)" : ""));
  }
  if (has(m.balance_usd)) bits.push("<b>" + esc(money(m.balance_usd)) + " left</b>");
  if (has(m.today_usd)) bits.push("today " + esc(money(m.today_usd, 4)));
  if (has(m.month_usd) && !has(m.spent_usd))
    bits.push("this month " + esc(money(m.month_usd, 4)));
  if (has(m.limit_left_usd)) bits.push("key limit " + esc(money(m.limit_left_usd)) + " left");

  let html = bits.length
    ? `<p>${esc(label)}: ${bits.join(" · ")}</p>`
    : `<p class="quiet">${esc(label)}: ${esc(m.why || "nothing to read")}</p>`;

  // Where the number came from, always -- "read live" and "you wrote it down
  // and we have been subtracting since" are different claims and must not
  // look alike.
  if (has(m.spent_usd)) {
    html += `<p class="quiet">Read from OpenAI's own counter, so everything ` +
      `spending this account is in it — the assistant's turns and anything else on the key. ` +
      (has(m.limit_usd)
        ? (m.enforced
            ? `The ${esc(money(m.limit_usd))} ceiling does stop spending at it.`
            : `The ${esc(money(m.limit_usd))} ceiling is written down, not ` +
              `enforced as a stop.`)
        : "") + `</p>`;
  }
  if (has(m.entered_usd)) {
    html += `<p class="quiet">` +
      (has(m.spent_usd) ? "Also noted by hand: " : "<b>Estimate.</b> ") +
      `${esc(money(m.entered_usd))} you noted on ` +
      `${esc(shortWhen(m.entered_at))}, less ${esc(money(m.spent_since_usd, 4))} ` +
      `the assistant's own turns were priced at since` +
      `${has(m.spent_since_turns) ? " (" + m.spent_since_turns + ")" : ""} — ` +
      `<b>a reckoning, not a reading</b>, and blind to whatever else ` +
      `spends on the same key.` +
      (has(m.spent_usd)
        ? " It is the credit balance, which no endpoint will give us."
        : " It stands in only until the real counter can be read.") + `</p>`;
  } else if (m.balance_why) {
    html += `<p class="quiet">${esc(m.balance_why)}</p>`;
  }
  if (bits.length && m.why) html += `<p class="quiet">${esc(m.why)}</p>`;

  // The box for writing it down. Desk only, like the keys.
  if (service && PROV.can_edit_keys && m.balance_from !== "OpenRouter, live") {
    const now = (PROV.credits || {})[service];
    html += `<p><input class="prov-credit" data-service="${esc(service)}" ` +
      `inputmode="decimal" placeholder="balance off their dashboard, e.g. 7.88" ` +
      `value="${now ? esc(String(now.usd)) : ""}">` +
      ` <button class="prov-credit-save" data-service="${esc(service)}">note it</button>` +
      (now ? ` <button class="prov-credit-clear" data-service="${esc(service)}">forget</button>` : "") +
      `</p>`;
  }
  return html;
}

function modelOptions(models, chosen, allowNone) {
  const groups = {};
  for (const m of models) (groups[m.service] = groups[m.service] || []).push(m);
  const name = (s) => ((PROV.services || {})[s] || {}).label || s;
  let html = PROV.codex_only && chosen && !models.some(m => m.key === chosen)
    ? `<option value="" selected disabled>Saved selection paused by Codex-only mode</option>` : "";
  html += allowNone && !PROV.codex_only
    ? `<option value=""${!chosen ? " selected" : ""}>the same mind the assistant has always dreamt as</option>` : "";
  for (const s of ["claude_code", "codex", "openai", "openrouter"]) {
    if (!groups[s]) continue;
    html += `<optgroup label="${esc(name(s))}">`;
    for (const m of groups[s]) {
      const p = m.price;
      const tail = p && p.known ? "  —  " + money(p.in_per_m) + " / " + money(p.out_per_m) + " per M" : "";
      html += `<option value="${esc(m.key)}"${m.key === chosen ? " selected" : ""}` +
        `${m.ready ? "" : " disabled"}>${esc(m.label)}${esc(tail)}` +
        `${m.ready ? "" : "  (no key)"}</option>`;
    }
    html += `</optgroup>`;
  }
  return html;
}

function devProviders() {
  if (!PROV) return `<p class="quiet">reading the catalogue…</p>`;
  if (PROV.error) return `<p class="bad">the catalogue did not come: ${esc(PROV.error)}</p>`;
  const me = (state && state.prompt && state.prompt.self) || {};
  const inTok = me.prompt_tokens_est ?? me.measured_input_tokens_last_turn ?? 0;
  const now = PROV.models.find((m) => m.key === PROV.chosen);
  const svc = (PROV.services || {})[(now || {}).service] || {};

  let html = `<p class="prov-now">Thinking with <b>${esc((now || {}).label || PROV.chosen)}</b>, ` +
    `through ${esc(svc.label || "?")}.</p>`;
  if (PROV.chat_paused) html = `<p>${esc(PROV.chat_paused)} Choose a chat model below.</p>`;
  html = `<p><label><input type="checkbox" id="prov-codex-only"${PROV.codex_only ? " checked" : ""}> Codex-only mode</label></p>` +
    `<p class="quiet">Blocks the room's own Claude/Anthropic calls. Saved selections and history stay on disk. Local models remain available.</p>` +
    (PROV.paused || []).map(x => `<p class="quiet"><b>${esc(x.capability)} — paused.</b> ${esc(x.reason)}</p>`).join("") + html;
  if (now && now.price && now.price.known) {
    html += `<p class="quiet">${esc(priceLine(now.price))}` +
      (perTurn(now.price, inTok) ? " · " + esc(perTurn(now.price, inTok)) : "") +
      (now.price.borrowed ? " · price borrowed from OpenRouter" : "") + `</p>`;
  }
  html += `<p><select id="prov-pick">${modelOptions(PROV.models, PROV.chosen, false)}</select>` +
    ` <span class="quiet">takes effect on the next turn</span></p>`;
  html += `<p class="quiet">Codex subscription uses your ChatGPT plan. Sign in on this ` +
    `computer with <code>codex login</code>. An OpenAI API key has separate billing. ` +
    `Subscription limits can be checked in Codex.</p>`;

  // The night, apart from the chat, and said as such: the note is here so a
  // person moving it knows it is a different question.
  html += `<h4>native Codex tools</h4><p><label><input type="checkbox" id="prov-native-tools"${PROV.native_tools ? " checked" : ""}> Enable tools in ordinary chat</label> <button id="native-stop-settings">Stop current tools</button></p>`;
  html += `<p class="quiet">Applies to your next Codex conversation. Background and angel turns have no native tools. Commands, output, exit codes and native diffs stay in the room and in logs/native/turn-ROW.jsonl. Expand Native Codex activity to inspect or download a run.</p>`;
  html += PROV.native_tools_profile === "assistant-chat"
    ? `<p class="quiet">On by default for Codex chat. Read/write access covers the room account's home folder, including Desktop, Downloads, Documents and Pictures, with networking enabled and Windows denials still enforced. Authentication locations and the running adapter are protected.</p>`
    : `<p class="quiet">The running adapter still uses the earlier Documents write profile. The permanent broad profile has not been loaded. Existing native activity and controls remain available.</p>`;
  html += `<h4>at night</h4>`;
  html += `<p class="quiet">A dream is the assistant in full, whole Spark — the ` +
    `reason the night does not follow the chat's picker. Moving it is a thing to do ` +
    `on purpose.</p>`;
  html += `<p><select id="prov-dream">` +
    modelOptions(PROV.models, PROV.dream_pinned && !PROV.codex_only ? "" : PROV.dream_model, true) +
    `</select>` + (PROV.dream_paused ? ` <span class="quiet">paused — saved night model requires Claude</span>`
      : PROV.dream_pinned ? ` <span class="quiet">on its pin</span>` : "") + `</p>`;

  // Money, per service, only where there is a number to have.
  html += `<h4>what is left</h4>`;
  html += provMoneyLine((PROV.money || {}).openrouter, "OpenRouter", "openrouter");
  html += provMoneyLine((PROV.money || {}).openai, "OpenAI", "openai");
  if (!PROV.codex_only) html += `<p class="quiet">Claude Code has no bill: the plan's five-hour and weekly ` +
    `windows are the gauge, and they are in the header.</p>`;

  // The keys.
  html += `<h4>keys</h4>`;
  if (!PROV.can_edit_keys) {
    html += `<p class="quiet">A key is only ever set at the desk. This is the room ` +
      `reached from another device, so the boxes are not drawn — a key typed ` +
      `here would cross the network to get in.</p>`;
  }
  html += `<div class="prov-keys">`;
  for (const [slot, info] of Object.entries(PROV.keys || {})) {
    html += `<div class="prov-key"><label>${esc(info.about)}</label>` +
      `<p class="quiet">${info.set ? "set, ending " + esc(info.tail) : "not set"}</p>`;
    if (PROV.can_edit_keys) {
      html += `<p><input type="password" class="prov-key-box" data-slot="${esc(slot)}" ` +
        `placeholder="${info.set ? "paste a new one to replace it" : "paste a key"}" ` +
        `autocomplete="off" spellcheck="false">` +
        ` <button class="prov-key-save" data-slot="${esc(slot)}">save</button>` +
        (info.set ? ` <button class="prov-key-clear" data-slot="${esc(slot)}">clear</button>` : "") +
        `</p>`;
    }
    html += `</div>`;
  }
  html += `</div>`;

  // Every model, what it is, and what it costs -- read live, said so.
  html += `<h4>the minds, and what they cost</h4>`;
  html += `<table class="prov-table"><tbody>`;
  for (const m of PROV.models) {
    const p = m.price || {};
    html += `<tr${m.key === PROV.chosen ? ' class="on"' : ""}>` +
      `<td class="nm">${esc(m.label)}<span class="svc">${esc(((PROV.services || {})[m.service] || {}).label || m.service)}</span></td>` +
      `<td class="ab">${esc(m.about || "")}</td>` +
      `<td class="pr">${esc(p.known ? money(p.in_per_m) + " / " + money(p.out_per_m) : "—")}` +
      `<span class="svc">${esc(p.source === "subscription" ? "ChatGPT subscription" : p.known ? "per M in / out" + (p.borrowed ? ", borrowed" : "") : "no price read")}</span></td>` +
      `<td class="cx">${esc(roomy(p.context))}</td></tr>`;
  }
  html += `</tbody></table>`;
  const when = PROV.prices_read_at ? new Date(PROV.prices_read_at * 1000) : null;
  html += `<p class="quiet">Every price on this page is read from OpenRouter's live ` +
    `catalogue${when ? ", last at " + esc(clockOnly(when)) : ""} — nothing here is typed in, ` +
    `so nothing here goes stale after a price cut. OpenAI publish no price endpoint, so ` +
    `Terra and Sol borrow the figure for the same model.` +
    (PROV.prices_error ? ` The last read failed: ${esc(PROV.prices_error)}` : "") + `</p>`;
  html += `<p><button id="prov-refresh">read the prices again</button></p>`;
  return html;
}

async function cancelNativeTools() {
  try { await post("api/native-tools/cancel", {}); }
  catch (e) { alert(e.message); }
}

function wireProviders() {
  const native = $("prov-native-tools");
  if (native) native.onchange = async () => {
    native.disabled = true;
    try { PROV = await post("api/providers/native-tools", {enabled: native.checked}); }
    catch (e) { alert(e.message); }
    finally { native.checked = !!PROV.native_tools; native.disabled = false; paintProviders(); }
  };
  const stopNative = $("native-stop-settings");
  if (stopNative) stopNative.onclick = cancelNativeTools;

  const mode = $("prov-codex-only");
  if (mode) mode.onchange = async () => {
    mode.disabled = true;
    try { PROV = await post("api/providers/codex-only", { enabled: mode.checked }); }
    catch (e) { alert(e.message); }
    paintProviders();
    await pollProgress();
    renderUsage();
  };
  const pick = $("prov-pick");
  if (pick) pick.onchange = async () => {
    try { PROV = await post("api/providers/choose", { model: pick.value }); }
    catch (e) { alert(e.message); }
    pick.blur(); paintProviders(); renderUsage();
  };
  const dream = $("prov-dream");
  if (dream) dream.onchange = async () => {
    try { PROV = await post("api/providers/dream", { model: dream.value }); }
    catch (e) { alert(e.message); }
    dream.blur(); paintProviders();
  };
  for (const b of MAIN.querySelectorAll(".prov-key-save")) b.onclick = async () => {
    const box = MAIN.querySelector(`.prov-key-box[data-slot="${b.dataset.slot}"]`);
    if (!box || !box.value.trim()) return;
    try { PROV = await post("api/providers/key", { service: b.dataset.slot, key: box.value }); }
    catch (e) { alert(e.message); return; }
    box.value = ""; paintProviders();
  };
  for (const b of MAIN.querySelectorAll(".prov-key-clear")) b.onclick = async () => {
    if (!confirm("Clear this key? Any model that needs it stops being offered.")) return;
    try { PROV = await post("api/providers/key", { service: b.dataset.slot, key: "" }); }
    catch (e) { alert(e.message); return; }
    paintProviders();
  };
  for (const b of MAIN.querySelectorAll(".prov-credit-save")) b.onclick = async () => {
    const box = MAIN.querySelector(`.prov-credit[data-service="${b.dataset.service}"]`);
    try { PROV = await post("api/providers/credits", { service: b.dataset.service, usd: box ? box.value : "" }); }
    catch (e) { alert(e.message); return; }
    paintProviders();
  };
  for (const b of MAIN.querySelectorAll(".prov-credit-clear")) b.onclick = async () => {
    try { PROV = await post("api/providers/credits", { service: b.dataset.service, usd: "" }); }
    catch (e) { alert(e.message); return; }
    paintProviders();
  };
  const again = $("prov-refresh");
  if (again) again.onclick = async () => { await loadProviders(true); paintProviders(); };
}

let paintedProv = null;

function paintProviders() {
  const box = $("prov-body");
  if (!box) return;
  const a = document.activeElement;
  if (a && box.contains(a) && /^(SELECT|INPUT)$/.test(a.tagName)) return;
  const html = devProviders();
  if (html === paintedProv) return;
  paintedProv = html;
  keepScroll(() => { patchHtml(box, html); });
  wireProviders();
}

// --- its notebook ------------------------------------------------------------
// The assistant's own short notes, as a table to sort and read: its votes,
// how many turns each note has been kept and what it weighs, and only the
// first words until a note is opened. The notes are its alone -- nothing on
// this page writes one. The cap is the owner's, and the meter says how close
// the book is to it.
let NB_SORT = { key: "id", dir: 1 };
const NB_OPEN = new Set();
let NB_GONE = false;
// What the cap box last said -- "saved", or why not -- and the one save in
// flight, so Enter, a click away and the button never send it twice.
let NB_SAYS = null;
let NB_SAVING = false;

// Key, heading, and which way a first click sorts: the numbers most-first,
// the id and the words in their own order.
const NB_COLS = [
  ["id", "#", 1, "the room numbers each note; a number is never given out twice"],
  ["text", "Note", 1, ""],
  ["up", "Up", -1, "votes for it"],
  ["down", "Down", -1, "votes against it"],
  ["turns", "Turns", -1, "how many of its turns the note has been kept"],
  ["tokens", "Tokens", -1, "estimated, about four characters a token"],
];

function nbState() {
  return (state && state.notebook) || null;
}

function notebookSub() {
  const nb = nbState();
  if (!nb) return "";
  const n = nb.count || 0;
  return (n ? n + " note" + (n === 1 ? "" : "s") : "empty") +
    " · ~" + (nb.used || 0).toLocaleString() + " of " + (nb.cap || 0).toLocaleString() +
    " tokens" + (nb.locked ? " · full" : "");
}

function nbSorted(notes) {
  const { key, dir } = NB_SORT;
  const val = (n) => key === "text" ? String(n.text || "").toLowerCase() : Number(n[key]) || 0;
  return notes.slice().sort((a, b) => {
    const x = val(a), y = val(b);
    return ((x < y ? -1 : x > y ? 1 : 0) * dir) || (a.id - b.id);
  });
}

function nbRow(n) {
  const open = NB_OPEN.has(n.id);
  const one = String(n.text || "").replace(/\s+/g, " ").trim();
  const said = "written " + shortWhen(n.dt) + (n.gone ? " · removed " + shortWhen(n.gone_dt) : "");
  const tag = n.gone ? `<span class="nb-tag">removed</span>` : "";
  const words = open
    ? `<div class="nb-whole">${esc(n.text)}</div><div class="nb-meta">${esc(said)}</div>`
    : `<span class="nb-first">${tag}${esc(one)}</span>`;
  return `<tr class="nb-row${open ? " open" : ""}${n.gone ? " gone" : ""}" data-k="nb:${n.id}">` +
    `<td class="num id">${n.id}</td>` +
    `<td class="note" data-nb-note="${n.id}" tabindex="0" role="button" aria-expanded="${open}"` +
    ` title="${open ? "fold it back" : "read it whole"}">${words}</td>` +
    `<td class="num up${n.up ? " has" : ""}">${n.up}</td>` +
    `<td class="num down${n.down ? " has" : ""}">${n.down}</td>` +
    `<td class="num turns">${(n.turns || 0).toLocaleString()}</td>` +
    `<td class="num tokens">${(n.tokens || 0).toLocaleString()}</td></tr>`;
}

function devNotebook() {
  const nb = nbState();
  if (!nb) return `<p class="quiet">looking…</p>`;
  const pct = nb.pct || 0;
  const tone = nb.locked ? " over" : pct >= 80 ? " near" : "";
  const mine = (state.who || OWNER) === OWNER;
  // A number typed and not yet saved is kept through every redraw -- the
  // table and the meter go on answering the room -- and wears ember until it
  // is saved, so it can never pass for the cap the room is using.
  const draft = mine ? nbDraft() : null;
  const says = NB_SAYS && (NB_SAYS.bad ? draft !== null : Date.now() < NB_SAYS.until) ? NB_SAYS : null;
  let html = `<div class="nb-top"><div class="nb-usage${tone}">` +
    `<div class="nb-figs"><span><b>~${(nb.used || 0).toLocaleString()}</b> of ${(nb.cap || 0).toLocaleString()} tokens</span>` +
    `<span class="nb-pct">${nb.locked ? `<span class="pill bad">full — adding locked</span> ` : ""}${pct}%</span></div>` +
    `<div class="nb-bar" role="meter" aria-label="how full the notebook is" aria-valuemin="0"` +
    ` aria-valuemax="${nb.cap}" aria-valuenow="${nb.used}"><i style="width:${Math.min(100, pct)}%"></i></div></div>` +
    `<div class="nb-cap"><label for="nb-cap">cap</label>` +
    `<input id="nb-cap" type="text" inputmode="numeric" autocomplete="off" spellcheck="false"` +
    `${draft !== null ? ` class="draft"` : ""} value="${esc(draft !== null ? draft : String(nb.cap))}"` +
    ` data-saved="${nb.cap}"${mine ? ` title="${nb.min_cap.toLocaleString()}–${nb.max_cap.toLocaleString()} tokens; Enter saves, Esc puts it back"`
      : ` disabled title="${esc(PEOPLE[OWNER] || OWNER)}'s to move"`}>` +
    `<span class="quiet">tokens</span>` +
    (mine ? `<button class="small-btn nb-save" id="nb-cap-save" type="button"${draft !== null ? "" : " hidden"}>save</button>` : "") +
    `</div></div>`;
  html += `<p class="nb-rule${says ? (says.bad ? " bad" : " good") : ""}" id="nb-cap-note">` + (says ? esc(says.text)
    : `A note may go ${nb.grace_pct}% past the cap; past it, adding locks until ${esc(MY_NAME)} removes notes or the cap is raised.`) +
    `</p>`;

  const notes = (nb.notes || []).filter((n) => NB_GONE || !n.gone);
  if (!notes.length) {
    html += `<div class="nb-empty">${nb.count || nb.removed
      ? "Nothing in it right now."
      : `No notes yet — when ${esc(MY_NAME)} writes one, it shows up here.`}</div>`;
  } else {
    html += `<table class="nb-table"><colgroup><col class="c-id"><col><col class="c-up">` +
      `<col class="c-down"><col class="c-turns"><col class="c-tokens"></colgroup><thead><tr>` +
      NB_COLS.map(([key, label, , help]) => {
        const on = NB_SORT.key === key;
        const sort = on ? ` aria-sort="${NB_SORT.dir > 0 ? "ascending" : "descending"}"` : "";
        const arrow = `<span class="arrow" aria-hidden="true">${on && NB_SORT.dir < 0 ? "▾" : "▴"}</span>`;
        return `<th class="${key === "text" ? "note" : key === "id" ? "id" : "num"}"${sort}>` +
          `<button type="button" data-nb-sort="${key}"${help ? ` title="${esc(help)}"` : ""}>${label}${arrow}</button></th>`;
      }).join("") +
      `</tr></thead><tbody>` + nbSorted(notes).map(nbRow).join("") + `</tbody></table>`;
  }
  if (nb.removed) {
    html += `<p class="nb-foot"><label><input type="checkbox" id="nb-gone"${NB_GONE ? " checked" : ""}>` +
      ` show removed (${nb.removed})</label></p>`;
  }
  return html;
}

// The number in the cap box when it is not the cap the room is using, else
// null. Read off the box itself, so a redraw can put it back.
function nbDraft() {
  const el = $("nb-cap");
  if (!el) return null;
  const typed = String(el.value).trim();
  return typed !== String(el.dataset.saved || "") ? typed : null;
}

// Only this section, off its own clicks: the rest of Settings keeps still,
// and a focused checkbox does not stop the table from answering it.
function paintNotebook() {
  const el = $("view-dev") && $("view-dev").querySelector('.dev[data-k="dev:notebook"]');
  if (!el) return;
  keepScroll(() => {
    patchHtml(el, dev("dev:notebook", MY_NAME + "'s notebook", notebookSub(), devNotebook()), true);
  });
  wireNotebook();
}

function wireNotebook() {
  const box = $("view-dev") && $("view-dev").querySelector('.dev[data-k="dev:notebook"]');
  if (!box) return;
  for (const b of box.querySelectorAll("[data-nb-sort]")) {
    b.onclick = () => {
      const key = b.dataset.nbSort;
      const first = (NB_COLS.find((c) => c[0] === key) || [])[2] || 1;
      NB_SORT = NB_SORT.key === key ? { key, dir: -NB_SORT.dir } : { key, dir: first };
      paintNotebook();
    };
  }
  for (const cell of box.querySelectorAll("[data-nb-note]")) {
    const flip = () => {
      const id = Number(cell.dataset.nbNote);
      if (NB_OPEN.has(id)) NB_OPEN.delete(id); else NB_OPEN.add(id);
      paintNotebook();
    };
    // A reader selecting words to copy is not asking for the note to fold.
    cell.onclick = () => { if (!String(document.getSelection() || "")) flip(); };
    cell.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); }
    };
  }
  const gone = $("nb-gone");
  if (gone) gone.onchange = () => { NB_GONE = gone.checked; paintNotebook(); };
  const inp = $("nb-cap"), save = $("nb-cap-save");
  if (!inp || !save) return;
  // Saved the moment it is meant: Enter, a click anywhere else, or the
  // button. A number typed and walked away from used to sit in the box
  // looking saved while the room went on using the old cap.
  const commit = async () => {
    const typed = nbDraft();
    if (NB_SAVING || typed === null || (NB_SAYS && NB_SAYS.bad && NB_SAYS.tried === typed)) return;
    NB_SAVING = true;
    try {
      const got = await post("api/notebook", { cap: typed });
      if (state) state.notebook = got;
      inp.dataset.saved = inp.value = String(got.cap);
      NB_SAYS = { text: "Saved — the cap is now " + got.cap.toLocaleString() + " tokens.",
                  until: Date.now() + 4000 };
      setTimeout(paintNotebook, 4100);
    } catch (e) {
      NB_SAYS = { text: String(e.message || e), bad: true, tried: typed };
    } finally {
      NB_SAVING = false;
    }
    paintNotebook();
  };
  inp.oninput = () => {
    const open = nbDraft() !== null;
    inp.classList.toggle("draft", open);
    save.hidden = !open;
  };
  inp.onchange = commit;
  inp.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    if (e.key === "Escape") { inp.value = inp.dataset.saved; NB_SAYS = null; inp.oninput(); inp.blur(); paintNotebook(); }
  };
  // Pressed without taking the focus off the box, so the click is the one
  // save and not a second one behind the box's own.
  save.onmousedown = (e) => e.preventDefault();
  save.onclick = commit;
}

function renderDev() {
  if (!state || backingUp) return;
  const a = document.activeElement;
  if (a && /^(SELECT|INPUT|TEXTAREA)$/.test(a.tagName) && $("view-dev").contains(a)) return;
  const me = (state.prompt || {}).self || {};
  const rc = (progress && progress.recall) || null;
  const recallSub = recallLine(rc).text.replace(/^automatic memory: /, "");
  const pv = (progress && progress.provider) || null;
  const provSub = pv ? pv.label + " \u00b7 " + pv.service_label : "";
  const parts = [
    ["dev:provider", () => dev("dev:provider", "which model " + MY_NAME + " thinks with", provSub,
        `<div id="prov-body">${devProviders()}</div>`)],
    ["dev:recall", () => dev("dev:recall", "automatic memory", recallSub, devRecallSection())],
    ["dev:models", () => dev("dev:models", "models on disk", modelsSub(), devModels())],
    ["dev:wallpaper", () => dev("dev:wallpaper", "room background", "", devWallpaper())],
    ["dev:memory", () => dev("dev:memory", MY_NAME + "'s memory right now",
        `${me.messages_loaded ?? "?"} messages \u00b7 ${me.essences_loaded ?? "?"} essences \u00b7 ~${k(me.prompt_tokens_est)} tokens`,
        devMemory())],
    ["dev:notebook", () => dev("dev:notebook", MY_NAME + "'s notebook", notebookSub(), devNotebook())],
    ["dev:notes", () => dev("dev:notes", notesTitle(), notesSub(), devNotes())],
    ["dev:tools", () => dev("dev:tools", MY_NAME + "'s tools and instructions", "", devTools())],
    ["dev:backup", () => dev("dev:backup", "backup", state.backup && state.backup.last ? "last " + shortWhen(state.backup.last.taken) : "none yet", devBackup())],
    ["dev:last", () => dev("dev:last", "last turn", "what was sent, what came back, how it went", devLastTurn())],
    ["dev:plan", () => dev("dev:plan", "plan and spend", "", devPlan())],
    ["dev:voice", () => dev("dev:voice", "voice", "", devVoice())],
  ];
  const view = $("view-dev");
  const have = view.querySelector('.dev[data-k="dev:recall"]');
  if (!have) {
    // The switches live up here now, off the header -- the top of the room
    // is names and places, and a phone has no width to spare there. Two of
    // them, each its own thing: the workings (the small lines, the
    // essences), and the gauges (the model, the plan's windows, the
    // automatic memory). Both remembered per person on this device, so the
    // page turns to whoever picked their name. Labels are written by
    // applyView, same as always.
    keepScroll(() => {
      patchHtml(view, `<p class="workings-row">`
        + `<button id="workings" class="tab switch"></button>`
        + `<button id="gauges" class="tab switch"></button>`
        + `<button id="native-stop-live" class="tab hidden">Stop tools</button></p>`
        + `<p id="pair" class="pair${PAIR_LINE ? "" : " hidden"}">${PAIR_LINE}</p>`
        + parts.map(([, f]) => f()).join(""));
    });
    applyView();
    renderNativeStop();
    painted = { live: devRecall(), config: devRecallConfig() };
    // Fetched, never polled: this one touches the network.
    loadProviders().then(paintProviders);
  } else {
    // Section by section, and the automatic memory's left standing: redrawing
    // it with the rest every five seconds threw away the reader's scroll and
    // selection while they were reading the prompt. It repaints itself, only
    // when it changes.
    keepScroll(() => {
      for (const [key, f] of parts) {
        const el = view.querySelector(`.dev[data-k="${key}"]`);
        if (key === "dev:recall") {
          const sub = el.querySelector("summary .sub");
          if (sub && sub.textContent !== recallSub) sub.textContent = recallSub;
          continue;
        }
        // The notes box stands still while it holds something unsaved.
        if (key === "dev:notes" && el && notesBusy()) continue;
        // And the folder box, while it holds a path not yet kept.
        if (key === "dev:models" && el && modelsBusy()) continue;
        // A selected file cannot be restored into a file input after a redraw;
        // leave this section intact while somebody has chosen or is uploading one.
        if (key === "dev:wallpaper" && el && wallpaperBusy()) continue;
        // The provider section repaints itself, off its own fetch. Redrawing
        // it here every five seconds would throw away a half-pasted key.
        if (key === "dev:provider" && el) {
          const sub = el.querySelector("summary .sub");
          if (sub && provSub && sub.textContent !== provSub) sub.textContent = provSub;
          continue;
        }
        const html = f();
        if (el) patchHtml(el, html, true);
        else view.insertAdjacentHTML("beforeend", html);
      }
    });
    paintRecall();
  }
  const b = $("backup");
  if (b) b.onclick = takeBackup;
  wireRecall();
  wireProviders();
  wireNotes();
  wireNotebook();
  wireModels();
  wireWallpapers();
}

async function takeBackup() {
  const btn = $("backup"), note = $("backup-note");
  if (!btn || btn.disabled) return;
  backingUp = true;
  btn.disabled = true;
  btn.classList.remove("broken");
  btn.textContent = "taking a copy…";
  try {
    const got = await post("api/backup");
    const counts = Object.entries(got.counts || {}).map(([t, n]) => t + " " + n).join(", ");
    btn.textContent = "take a backup now";
    note.textContent = `backed up just now · ${got.file.split(/[\\/]/).pop()} · ${got.size_kb} KB · checked: ${got.integrity} · ${counts}` +
      (got.tracked ? "" : ` · the tracked copy was not rewritten: ${got.tracked_error || "?"}`);
    const was = state.backup || {};
    state.backup = { count: (was.count || 0) + 1, where: was.where,
                     last: { file: got.file.split(/[\\/]/).pop(), taken: got.taken, size_kb: got.size_kb },
                     // The tracked copy is the same zip under its one name, unless
                     // the rewrite is what failed -- then the old one is still there.
                     tracked: got.tracked
                       ? { file: got.tracked.split(/[\\/]/).pop(), taken: got.taken, size_kb: got.size_kb }
                       : was.tracked };
  } catch (e) {
    btn.classList.add("broken");
    btn.textContent = "backup failed";
    note.textContent = String(e.message || e);
  } finally {
    btn.disabled = false;
    backingUp = false;
  }
}

// --- projects ------------------------------------------------------------------
// The people's work, not the assistant's: a project is one person's or
// everyone's, with a folder maybe, a Notes box they all write in and tasks
// they want doing. Plain by default and plain all the way down -- plain for
// everyone, detail opt-in -- so this is a list of things with their words
// under them, and the only fold on the page is the one that adds something
// new.
//
// Two rules are drawn, not just stored. Every note line wears its author,
// because a line whose writer is a guess is the failure this whole tab is
// shaped around. And a repeating task says *no clock yet* on its own face:
// nothing in this build schedules anything, and a task that looked scheduled
// would be a promise the room cannot keep.

// --- the frame, in the reader's own language -------------------------------
//
// Only the furniture: what the page says on its own behalf. Every word a
// person wrote -- a task's description, a note, a project's name, a resource
// -- stays exactly as they wrote it, in whatever language that was. Putting
// somebody's words through a translator is putting words in their mouth, and
// this tab is shaped around never doing that.
//
// The one borrowed sentence is the no-clock line. It belongs to the server
// and the server's wording is the truth of it; this is its translated face,
// and if the English ever changes this has to change with it.
const SAID = {
  en: {
    and: "and",
    open: "not started", doing: "in progress", done: "done",
    dropped: "dropped",
    waitingOn: "Waiting on", waitingFor: "waiting on", inProject: "in",
    project: "project", projects: "projects",
    openTask: "open task", openTasks: "open tasks",
    sensesOn: "senses on", of: "of", lastWaking: "last waking",
    writingAs: "writing as",
    everyNote: "every note line keeps whoever wrote it",
    newProject: "new project", title: "title", whose: "whose it is",
    folderIfAny: "folder on disk, if it has one", addProject: "add project",
    onHerDesk: "on " + MY_NAME + "'s desk", closedP: "closed",
    waitingOnYou: "waiting on you", openN: "open", moved: "moved",
    noticeWaiting: "thing waiting on you", noticesWaiting: "things waiting on you",
    waitingHere: "Waiting on you", checked: "checked", checkedOff: "checked off",
    checkedBy: "checked by", notCheckedAfterAll: "not checked after all",
    openPage: "open the page",
    noteForIt: "a note to leave with it — say, answered on the page; or: I won't " +
               "answer this one, let it sit. Empty is fine",
    noFolder: "no folder", notHere: "not found on this machine",
    tasks: "Tasks", standing: "Standing", todo: "To do",
    nothingOpen: "nothing open", finished: "finished and dropped",
    resources: "Resources", notes: "Notes", lastBy: "last by",
    nothingWritten: "nothing written here yet", writeAs: "write a line as",
    addNote: "add note", addTask: "add a task",
    editProject: "edit this project", editTask: "edit this task",
    folder: "folder", none: "none", putDown: "put down",
    folderOnDisk: "folder on disk", saveFolder: "save folder",
    where: "where", addAs: "add as", whatItIs: "what it is",
    whatItWants: "what it wants", whatItWantsLines: "what it wants, a line each",
    itRepeats: "it repeats",
    whenRepeat: "when it should repeat, in your words",
    herJob: MY_NAME + "'s job, if it has one", noJob: "no job",
    whoWaits: "who it waits on", nobody: "nobody",
    whichSense: "which sense watches it", nothingWatches: "nothing watches it",
    whichItem: "and which thing it watches for this task",
    orADate: "or a date it should happen on — this one really fires",
    saveTask: "save this task", askedBy: "asked by",
    byHand: "by hand", inWords: "in words", onDate: "on",
    activity: "Activity", neverLooked: "nothing has looked at this yet",
    nothingNotable: "nothing notable yet — quiet looks are not written " +
                    "down one by one, only the last one is kept",
    herJobIs: MY_NAME + "'s job:", lastDecided: "last decided:", closedJob: "closed:",
    noProjects: "no projects yet. The first one is " + PEOPLE[OWNER] + "’s to add.",
    noneYours: "nothing here is yours or shared yet",
    seesOwnAnd: "sees their own projects and the ones marked",
    standing_: "standing", noClock:
      "no clock yet — nothing schedules this; it is stored, not running",
  },
  bg: {
    and: "и",
    open: "незапочнато",
    doing: "в процес",
    done: "готово",
    dropped: "отказано",
    waitingOn: "Чака се от",
    waitingFor: "чака се от",
    inProject: "в",
    project: "проект",
    projects: "проекта",
    openTask: "отворена задача",
    openTasks: "отворени задачи",
    sensesOn: "активни сетива",
    of: "от",
    lastWaking: "последно събуждане",
    writingAs: "пишеш като",
    everyNote: "всеки ред запазва кой го е написал",
    newProject: "нов проект",
    title: "заглавие",
    whose: "чий е",
    folderIfAny: "папка на диска, ако има",
    addProject: "добави проект",
    onHerDesk: "на бюрото на " + MY_NAME,
    closedP: "затворен",
    waitingOnYou: "чакат теб",
    noticeWaiting: "нещо чака теб",
    noticesWaiting: "неща чакат теб",
    waitingHere: "Чакат теб",
    checked: "видяно",
    checkedOff: "отметнати",
    checkedBy: "отметнато от",
    notCheckedAfterAll: "всъщност не е видяно",
    openPage: "отвори страницата",
    noteForIt: "бележка към него — напр. отговорих на страницата, или: няма да отговарям, нека си стои. Може и празно",
    openN: "отворени",
    moved: "променено",
    noFolder: "без папка",
    notHere: "не е намерена на тази машина",
    tasks: "Задачи",
    standing: "Постоянни",
    todo: "За правене",
    nothingOpen: "няма отворени",
    finished: "завършени и отказани",
    resources: "Ресурси",
    notes: "Бележки",
    lastBy: "последно от",
    nothingWritten: "тук още нищо не е писано",
    writeAs: "напиши ред като",
    addNote: "добави бележка",
    addTask: "добави задача",
    editProject: "редактирай проекта",
    editTask: "редактирай задачата",
    folder: "папка", none: "няма",
    putDown: "остави",
    folderOnDisk: "папка на диска",
    saveFolder: "запази папката",
    where: "къде", addAs: "добави като",
    whatItIs: "какво е",
    whatItWants: "какво иска",
    whatItWantsLines: "какво иска, по ред на всеки",
    itRepeats: "повтаря се",
    whenRepeat: "кога да се повтаря, с твои думи",
    herJob: "задачата на " + MY_NAME + ", ако има",
    noJob: "без задача",
    whoWaits: "кого чака",
    nobody: "никого",
    whichSense: "кое сетиво я наблюдава",
    nothingWatches: "нищо не я наблюдава",
    whichItem: "и кое нещо да следи за тази задача",
    orADate: "или дата, на която да се случи — тази наистина се задейства",
    saveTask: "запази задачата",
    askedBy: "поискана от",
    byHand: "на ръка",
    inWords: "с думи", onDate: "на",
    activity: "Активност",
    neverLooked: "още нищо не е поглеждало тук",
    nothingNotable: "още няма нищо забележително — тихите проверки не се записват една по една, пази се само последната",
    herJobIs: "задачата на " + MY_NAME + ":",
    lastDecided: "последно решено:",
    closedJob: "затворена:",
    noProjects: "още няма проекти.",
    noneYours: "тук още няма нищо твое или общо",
    seesOwnAnd: "вижда своите проекти и тези, отбелязани като",
    standing_: "състояние",
    noClock: "все още няма часовник — нищо не го планира; записано е, не работи",
  },
};

// Which language this reader gets. It follows the person, because who you
// are is what decides which language you read -- and it can be overridden on
// the tab, because a default is a guess and anyone may want the other
// language. Remembered per person on this device.
function langNow() {
  const who = whoNow();
  try {
    const set = localStorage.getItem("assistant:lang:" + who);
    if (set === "en" || set === "bg") return set;
  } catch (e) { /* a device that cannot remember takes the default */ }
  return (ASSISTANT.people[who] || {}).lang || "en";
}

function setLang(code) {
  try { localStorage.setItem("assistant:lang:" + whoNow(), code); } catch (e) {}
  renderProjects();
}

// One frame word. An unknown key falls back to English rather than to
// nothing: a missing translation should read oddly, never blankly.
function T(key) {
  const lang = SAID[langNow()] || SAID.en;
  return lang[key] !== undefined ? lang[key] : (SAID.en[key] || key);
}

// A person's name in the reader's language, when the home gives one.
function nameIn(w) {
  const p = ASSISTANT.people[w];
  if (!p) return "";
  return (p.names || {})[langNow()] || p.called || w;
}
function everyone() {
  const names = Object.keys(PEOPLE).map(nameIn);
  return names.length < 2 ? names.join("")
    : names.slice(0, -1).join(", ") + " " + T("and") + " " + names[names.length - 1];
}
const OWNER_SAID_EN = Object.assign({}, PEOPLE, { both: "everyone" });
// Kept as getters so a language change needs no reload: every read is fresh.
function ownerSaid(o) { return o === "both" ? everyone() : (nameIn(o) || o); }
function stateSaid(s) { return T(s) || s; }
const OWNER_SAID = OWNER_SAID_EN;
const STATE_SAID = {
  open: "not started", doing: "in progress", done: "done", dropped: "dropped",
};

function whoSaid(w) {
  // The assistant's own name is the same in any language; people's names
  // come per language from the home, because a name and its spelling in
  // another alphabet are the same person.
  if (w === ME) return MY_NAME;
  return nameIn(w) || String(w || "");
}

// Which projects the reader has opened out of the shelf, by id, remembered
// on this device. Shut is the answer for a device that has never been asked
// -- nothing remembered means nothing open, which is what a shelf is.
const PROJ_OPEN = new Set();
try {
  for (const id of JSON.parse(localStorage.getItem("assistant:projects:open") || "[]")) {
    PROJ_OPEN.add(String(id));
  }
} catch (e) { /* a device that cannot remember simply starts shut every time */ }

function toggleProject(id) {
  id = String(id);
  if (PROJ_OPEN.has(id)) PROJ_OPEN.delete(id); else PROJ_OPEN.add(id);
  try {
    localStorage.setItem("assistant:projects:open", JSON.stringify([...PROJ_OPEN]));
  } catch (e) {}
  keepForms();
  renderProjects();
}

// What is typed into a form survives a redraw -- a note half written and a
// re-render arriving from a poll should not throw the words away.
const FORM = {};

function keepForms() {
  for (const el of MAIN.querySelectorAll("#view-projects [data-keep]")) {
    // A tick's `value` is the word "on" whether or not it is ticked, so
    // asking for it loses the one thing a tick knows.
    FORM[el.dataset.keep] = el.type === "checkbox" ? el.checked : el.value;
  }
}

function held(name) {
  return esc(FORM[name] || "");
}

// A field that already holds something: what the person has typed wins, and
// what is stored stands until they type. Editing an existing thing needs this; an
// empty add form does not.
// A stored instant, as a date box wants it: the local wall clock, to the
// minute, with no offset on it. The server reads a bare one back as local
// time, which is the same clock it was typed on -- so the round trip means
// what the person meant.
function forDateBox(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function heldOr(name, stored) {
  return FORM[name] === undefined ? esc(stored || "") : esc(FORM[name] || "");
}

function tickedOr(name, stored) {
  return (FORM[name] === undefined ? !!stored : !!FORM[name]) ? " checked" : "";
}

// Who a task may wait on: the room's own list, so the page cannot offer a
// person the store will refuse. Nobody is a real answer and comes first.
function needsOpts(view, current) {
  return [`<option value="">${esc(T("nobody"))}</option>`].concat(
    (view.needs || []).map((n) =>
      `<option value="${esc(n)}"${n === current ? " selected" : ""}>` +
      `${esc(whoSaid(n))}</option>`)).join("");
}

// The senses the room actually has, named by the watcher.
function senseOpts(view, current) {
  const senses = ((view.watch || {}).senses) || {};
  return [`<option value="">${esc(T("nothingWatches"))}</option>`].concat(
    Object.keys(senses).map((s) =>
      `<option value="${esc(s)}"${s === current ? " selected" : ""}>` +
      `${esc(s)}${senses[s].said ? " (" + esc(senses[s].said) + ")" : ""}` +
      `</option>`)).join("");
}

function jobOpts(view, current) {
  return [`<option value="">${esc(T("noJob"))}</option>`].concat(
    (view.jobs || []).map((j) =>
      `<option value="${esc(j)}"${j === current ? " selected" : ""}>${esc(j)}</option>`)
  ).join("");
}

// Which tasks are open, this session only and never remembered. Shut is the
// right answer for a task nobody has asked about: its description answers
// none of the three questions until somebody wants it, and a device that
// remembered would come back to a page already unfolded.
const TASK_OPEN = new Set();

function toggleTask(id) {
  id = String(id);
  if (TASK_OPEN.has(id)) TASK_OPEN.delete(id); else TASK_OPEN.add(id);
  keepForms();
  renderProjects();
}

// When a project last moved: the newest thing that happened inside it. A
// shut row has to answer that without being opened, and a project with no
// tasks and no notes has honestly never moved.
function lastMoved(p) {
  const all = (p.tasks || []).map((t) => t.moved)
    .concat((p.notes || []).map((n) => n.dt))
    .filter(Boolean).sort();
  return all.length ? all[all.length - 1] : null;
}

// Whose shelf a project stands on: its owner's, and everyone's when it is
// shared. One page for everyone, showing each person their own and the
// shared ones.
function mine(p, who) {
  return p.owner === who || p.owner === "both";
}

function liveTasks(p) {
  return (p.tasks || []).filter((t) => t.state === "open" || t.state === "doing");
}

// Whose turn it is to do something. `needs` is a person label rather than
// free text, so this match cannot silently stop working
// against a string somebody spelled differently one day.
function waitsOn(t, who) {
  return !!t.needs && (t.needs === who || t.needs === "both");
}

// Both the note lines and a task's description are written the way people
// actually write: several lines, usually dashes. They were going through the
// inline `md()`, which carries bold and code and no idea what a line is, so
// four bites arrived as one run-on sentence. `mdBlock()` is the same block
// renderer the Spark is drawn with; it keeps the lines as written.
// One resource: what it is called, what it says, and who wrote it. A value
// that is plainly a link is a link -- only http, https or a bare www., so a
// value can never smuggle a javascript: href onto the page.
function resourceRowHtml(p, r) {
  const v = String(r.value || "");
  const link = /^(https?:\/\/|www\.)/i.test(v);
  const href = v.toLowerCase().startsWith("www.") ? "https://" + v : v;
  return `<div class="res-row">` +
    `<span class="res-key">${esc(r.key)}</span>` +
    (link
      ? `<a class="res-val" href="${esc(href)}" target="_blank" ` +
        `rel="noopener noreferrer">${esc(v)}</a>`
      : `<span class="res-val">${esc(v)}</span>`) +
    `<span class="res-who">${esc(whoSaid(r.who))} \u00b7 ${esc(when(r.dt))}</span>` +
    `<button class="mini" data-do="res_clear" data-project="${p.id}" ` +
    `data-key="${esc(r.key)}">put down</button>` +
  `</div>`;
}

// What a sense has actually been doing on one task. Two halves kept apart on
// purpose: the last look is overwritten every time and is usually quiet --
// that is the half answering "is this even running" -- and the lines are the
// notable ones the ledger kept. Every sentence here was written by the
// server; the page has no business inventing what a watcher did.
function activityHtml(t) {
  const a = t.activity;
  if (!a) return "";
  const key = "task:act:" + t.id;
  const head = a.last_result
    ? esc(a.last_result) + `<span class="act-when">${esc(when(a.last_look))}</span>`
    : `<span class="act-never">${esc(T("neverLooked"))}</span>`;
  const lines = (a.lines || []).length
    ? (a.lines || []).map((ln) =>
        `<div class="act-line"><span class="act-at">${esc(when(ln.at))}</span>` +
        `<span class="act-said">${esc(ln.said)}</span></div>`).join("")
    : `<div class="empty">${esc(T("nothingNotable"))}</div>`;
  return `<details class="dev" data-k="${esc(key)}"` +
    `${OPEN.has(key) ? " open" : ""}><summary>${esc(T("activity"))}` +
    `<span class="sub">${head}</span></summary>` +
    `<div class="inner">${lines}</div></details>`;
}

// How many things have arrived on a task that nobody has checked off yet.
function noticeCount(t) {
  return (((t || {}).notices) || []).length;
}

// What has arrived on one task and waits for somebody to say it has been
// seen to: a comment a sense detected, a date that came, a line somebody
// put there. Each row is when, what, and one button -- "checked" -- with a
// note if the reader wants to leave one ("I won't answer this one, let it
// sit"). The checked ones fold under with who said so, when, and the note,
// and a way back. Nothing here reads the world to decide; the check is the
// word. Every sentence is the server's; the page only draws the list.
//
// A notification pops up when something arrives, and a person or the
// assistant can mark it checked -- for any kind of task, not comments only.
// So it sits here, and on the tab's own button, until somebody has said so.
function noticesHtml(p, t) {
  const open = t.notices || [];
  const done = t.checked || [];
  if (!open.length && !done.length) return "";
  const a = t.activity || {};
  const row = (n, back) =>
    `<div class="wait-row">` +
      `<span class="wait-when">${esc(when(n.at))}</span>` +
      `<button class="mini" data-do="${back ? "notice_back" : "notice_done"}" ` +
        `data-project="${p.id}" data-notice="${n.id}">` +
        `${esc(T(back ? "notCheckedAfterAll" : "checked"))}</button>` +
      `<span class="wait-text">${esc(n.said || "")}</span>` +
      (back
        ? `<span class="wait-aside">${esc(T("checkedBy"))} ` +
          `${esc(whoSaid(n.done_by))} · ${esc(when(n.done_at))}` +
          `${n.note ? " — " + esc(n.note) : ""}</span>`
        : "") +
    `</div>`;
  const link = a.page
    ? `<a class="wait-page" href="${esc(a.page)}" target="_blank" ` +
      `rel="noopener noreferrer">${esc(T("openPage"))}</a>`
    : "";
  const key = "task:checked:" + t.id;
  return `<div class="wait-list">` +
    (open.length
      ? `<div class="wait-head">${esc(T("waitingHere"))} (${open.length})` +
        `${link}</div>` + open.map((n) => row(n, false)).join("")
      : "") +
    (done.length
      ? `<details class="dev" data-k="${esc(key)}"${OPEN.has(key) ? " open" : ""}>` +
        `<summary>${esc(T("checkedOff"))} (${done.length})</summary>` +
        `<div class="inner">${done.map((n) => row(n, true)).join("")}</div>` +
        `</details>`
      : "") +
  `</div>`;
}

// The count on the Projects tab's own button, so a thing waiting on the
// reader is seen from the chat too. The tab was the one place the answer
// lived, and a person who never opened it never saw it. Notices only: a
// task somebody set to wait on a person was put there by hand and is
// already known; the count is for the thing that arrives by itself.
function markProjectsTab() {
  const btn = $("tab-projects");
  if (!btn || !plans) return;
  const who = whoNow();
  let n = 0;
  for (const p of (plans.projects || []).filter((q) => mine(q, who))) {
    for (const t of liveTasks(p)) n += noticeCount(t);
  }
  let mark = btn.querySelector(".tab-mark");
  if (!n) { if (mark) mark.remove(); return; }
  if (!mark) {
    mark = document.createElement("span");
    mark.className = "tab-mark";
    btn.appendChild(mark);
  }
  mark.textContent = String(n);
}

// The first thing on the tab, and the reason it was redone: what a person
// with five minutes between meetings can read standing up. The waiting line
// sits above the counts because it is the only one that asks anything of the
// reader. The counts are the quiet half, and quiet has to be legible as
// quiet, so the zeros are drawn rather than hidden.
function glanceHtml(view, list) {
  const who = whoNow();
  const live = list.reduce((n, p) => n + liveTasks(p).length, 0);
  const w = view.watch || {};
  const senses = w.senses || {};
  const names = Object.keys(senses);
  const on = names.filter((kk) => senses[kk].on).length;
  const quiet = [
    list.length + " " + T(list.length === 1 ? "project" : "projects"),
    live + " " + T(live === 1 ? "openTask" : "openTasks"),
    names.length
      ? on + " " + T("of") + " " + names.length + " " + T("sensesOn") : "",
    (w.last && w.last.at) ? T("lastWaking") + " " + when(w.last.at) : "",
  ].filter(Boolean);

  // Drawn only when there is something to draw it from. Until a task can say
  // who it waits on, a line reading "waiting on you: nothing" would be a
  // promise the store cannot keep -- the same lie NO_CLOCK exists to refuse.
  // It appears by itself the day `needs` lands.
  const mine = [];
  for (const p of list) {
    for (const t of liveTasks(p)) if (waitsOn(t, who)) mine.push({ p, t });
  }
  // And the things that arrive by themselves: notices -- a comment a sense
  // detected, a date that came, a line somebody put on a task -- that
  // nobody has checked off yet. The list is the server's, newest first;
  // this only counts it and names the newest. `list` is already the
  // reader's own projects and the shared ones, so one person's project
  // never asks another person.
  const asks = [];
  for (const p of list) {
    for (const t of liveTasks(p)) {
      const w = t.notices || [];
      if (w.length) asks.push({ p, t, w });
    }
  }
  const waiting = (mine.length || asks.length)
    ? `<div class="glance-wait">` +
      `<span class="glance-lbl">${esc(T("waitingOn"))} ` +
      `${esc(whoSaid(who))}</span>` +
      asks.map(({ p, t, w }) =>
        `<span class="wait-one">${w.length} ` +
        `${esc(T(w.length === 1 ? "noticeWaiting" : "noticesWaiting"))}` +
        ` — ${esc(when(w[0].at))}: ${esc(w[0].said)}` +
        `<span class="wait-in">${esc(T("inProject"))} ` +
        `${esc(p.title)} · ${esc(t.title)}</span></span>`).join("") +
      mine.map(({ p, t }) =>
        `<span class="wait-one">${esc(t.title)}` +
        `<span class="wait-in">${esc(T("inProject"))} ` +
        `${esc(p.title)}</span></span>`).join("") +
      `</div>`
    : "";

  return `<div class="glance">${waiting}` +
    `<div class="glance-quiet">${esc(quiet.join(" · "))}</div></div>`;
}

function noteLineHtml(p, n) {
  const mine = n.who === whoNow();
  return `<div class="note-line${whoClass(n.who)}">` +
    `<span class="note-who">${esc(whoSaid(n.who))}</span>` +
    `<span class="note-when">${esc(when(n.dt))}</span>` +
    (n.edited ? `<span class="note-when">edited</span>` : "") +
    `<div class="note-text">${mdBlock(n.text, false)}</div>` +
    (mine ? `<div class="note-own">` +
      `<button class="mini" data-do="note_edit" data-note="${n.id}" ` +
      `data-project="${p.id}" data-raw="${esc(n.text)}">edit</button>` +
      `<button class="mini" data-do="note_hide" data-note="${n.id}">put down</button>` +
      `</div>` : "") +
    `</div>`;
}

// The assistant's job under a task, one line: is it running, and what has it
// cost. The card used to print the whole diary unfolded under every linked
// task, which read as a sheet of text -- so the diary went behind a caret, where the rest of the page keeps its detail. A broken link stays
// unfolded and loud, because that one is not detail, it is something wrong.
function jobCardHtml(c, taskId) {
  if (c.missing) {
    return `<div class="job-card missing">the job “${esc(c.title)}” — ` +
      `${esc(MY_NAME)} has no job by that name. The link is broken, not the task.</div>`;
  }
  const figs = [
    c.status === "open" ? c.state : "closed",
    (c.spent_usd != null && c.ceiling_usd != null)
      ? "$" + c.spent_usd.toFixed(2) + " of $" + Number(c.ceiling_usd).toFixed(2) : "",
  ].filter(Boolean);
  const line = `<span class="job-title">${esc(T("herJobIs"))} ` +
    `${esc(c.title)}</span>` +
    (figs.length ? `<span class="job-figs">${esc(figs.join(" · "))}</span>` : "");
  // What the line leaves out, and only what it leaves out.
  const inner = [
    c.goal ? `<div class="job-goal">${esc(c.goal)}</div>` : "",
    (c.links_left != null)
      ? `<div class="job-figs">${esc(c.links_left + " of " + c.links + " links left")}</div>` : "",
    c.decided ? `<div class="job-said">${esc(T("lastDecided"))} ` +
      `${esc(c.decided)}</div>` : "",
    c.outcome ? `<div class="job-said">${esc(T("closedJob"))} ` +
      `${esc(c.outcome)}</div>` : "",
  ].filter(Boolean).join("");
  // A job with nothing behind the line gets no caret to open onto nothing.
  if (!inner) return `<div class="job-card flat">${line}</div>`;
  const key = "job:" + taskId;
  return `<details class="job-card" data-k="${esc(key)}"` +
    `${OPEN.has(key) ? " open" : ""}><summary>${line}</summary>` +
    `<div class="job-inner">${inner}</div></details>`;
}

// The chip a task wears on its own face. The words are the server's: what
// fires a task is the store's fact and a sense's cadence is the watcher's,
// and a "15 min" typed in here is a figure that would go stale in silence.
function triggerChip(t) {
  const tr = t.trigger || { kind: "hand", said: "by hand" };
  // The three the page can say for itself; a sense's cadence stays the
  // server's words, because that figure is the watcher's and not ours.
  const said =
    (tr.kind === "date" && tr.at) ? T("onDate") + " " + shortWhen(tr.at)
    : tr.kind === "hand" ? T("byHand")
    : tr.kind === "words"
      ? T("inWords") + String(tr.said || "").replace(/^in words/, "")
      : tr.said;
  return `<span class="chip trig-${esc(tr.kind)}">${esc(said)}</span>` +
    (tr.item ? `<span class="chip chip-item">${esc(tr.item)}</span>` : "");
}

// One line, shut: what it is, what fires it, where it stands, when it last
// moved. Everything else lives behind the caret, because everything else
// answers none of the three questions until somebody asks it to.
function taskHtml(p, t, view) {
  const open = TASK_OPEN.has(String(t.id));
  const opts = Object.keys(STATE_SAID).map((k) =>
    `<option value="${k}"${k === t.state ? " selected" : ""}>` +
    `${esc(stateSaid(k))}</option>`).join("");
  const key = "te:" + t.id;
  // `unfolded`, not `open`: the task already wears its state as a class and
  // one of the states is called open, so the two would be the same word.
  const head = `<div class="task ${esc(t.state)}${open ? " unfolded" : ""}">` +
    `<div class="t-row" data-task-row="${t.id}" title="${open ? "shut" : "open"} this task">` +
      `<span class="t-caret">${open ? "▾" : "▸"}</span>` +
      `<span class="task-title">${esc(t.title)}</span>` +
      triggerChip(t) +
      (t.needs ? `<span class="chip chip-needs">${esc(T("waitingFor"))} ` +
        `${esc(whoSaid(t.needs))}</span>` : "") +
      // The shut row answers the third question too: how many things have
      // arrived on this task that nobody has checked off.
      (noticeCount(t) ? `<span class="chip chip-wait">${noticeCount(t)} ` +
        `${esc(T(noticeCount(t) === 1 ? "noticeWaiting" : "noticesWaiting"))}` +
        `</span>` : "") +
      `<select class="task-state" data-task="${t.id}">${opts}</select>` +
      (t.moved ? `<span class="t-moved">${esc(T("moved"))} ` +
        `${esc(when(t.moved))}</span>` : "") +
    `</div>`;
  if (!open) return head + `</div>`;

  // Content before forms, everywhere: what the task says, then who asked for
  // it, then the assistant's job, and only then the controls that change it.
  return head + `<div class="t-body">` +
    (t.wants ? `<div class="task-wants">${mdBlock(t.wants, false)}</div>` : "") +
    `<div class="task-meta">${esc(T("askedBy"))} ` +
      `${esc(whoSaid(t.who))}</div>` +
    // The honesty line stays exactly where it was: on a task whose only
    // trigger is the words somebody wrote, and nowhere else. A dated task
    // will genuinely fire, and must never carry a line saying it cannot.
    ((t.trigger || {}).kind === "words"
      ? `<div class="no-clock">` +
        (t.schedule ? `<b>${esc(t.schedule)}</b> — ` : "") +
        // The server's wording is the truth of this line; T carries its
        // translated face, and English falls back to the server's own words.
        `${esc(langNow() === "en" ? view.no_clock : T("noClock"))}</div>`
      : "") +
    (t.job_card ? jobCardHtml(t.job_card, t.id) : "") +
    noticesHtml(p, t) +
    activityHtml(t) +
    `<details class="dev" data-k="task:edit:${t.id}"` +
      `${OPEN.has("task:edit:" + t.id) ? " open" : ""}>` +
      `<summary>${esc(T("editTask"))}</summary>` +
      `<div class="inner"><div class="form">` +
        `<label>${esc(T("whatItIs"))}<input data-keep="${key}:title" ` +
          `value="${heldOr(key + ":title", t.title)}"></label>` +
        `<label>${esc(T("whatItWantsLines"))}` +
          `<textarea data-keep="${key}:wants" rows="5">` +
          `${heldOr(key + ":wants", t.wants)}</textarea></label>` +
        `<label class="tick"><input type="checkbox" data-keep="${key}:rep" ` +
          `class="rep"${tickedOr(key + ":rep", t.repeats)}> ` +
          `${esc(T("itRepeats"))}</label>` +
        `<label>${esc(T("whenRepeat"))}` +
          `<input data-keep="${key}:sched" ` +
          `value="${heldOr(key + ":sched", t.schedule)}" ` +
          `placeholder="every first Sunday"></label>` +
        `<div class="no-clock">` +
          `${esc(langNow() === "en" ? view.no_clock : T("noClock"))}</div>` +
        `<label>${esc(T("herJob"))}<select data-keep="${key}:job">` +
          `${jobOpts(view, t.job)}</select></label>` +
        `<label>${esc(T("whoWaits"))}<select data-keep="${key}:needs">` +
          `${needsOpts(view, t.needs)}</select></label>` +
        // Binding a sense is what makes a task actually run by itself. The
        // list is the room's own, so a sense that is not there cannot be
        // picked -- and the server checks it again anyway.
        `<label>${esc(T("whichSense"))}<select data-keep="${key}:sense">` +
          `${senseOpts(view, t.sense)}</select></label>` +
        `<label>${esc(T("whichItem"))}` +
          `<input data-keep="${key}:item" ` +
          `value="${heldOr(key + ":item", t.sense_item)}" ` +
          `placeholder="my-project"></label>` +
        // The one field that makes something happen at a time. Schedule
        // words above are still words; this is a date and it really fires.
        `<label>${esc(T("orADate"))}` +
          `<input type="datetime-local" data-keep="${key}:at" ` +
          `value="${heldOr(key + ":at", forDateBox(t.at))}"></label>` +
        `<button data-do="task_edit" data-project="${p.id}" ` +
          `data-task="${t.id}">${esc(T("saveTask"))}</button>` +
      `</div></div></details>` +
    `</div></div>`;
}

function projectHtml(p, view) {
  const tasks = p.tasks || [];
  const live = liveTasks(p);
  const over = tasks.filter((t) => t.state !== "open" && t.state !== "doing");
  // Two groups, because to a reader they are two different things. A
  // standing task is something the house keeps doing; a to-do is something
  // somebody wants done once and then never again.
  const standing = live.filter((t) => (t.trigger || {}).kind !== "hand");
  const todo = live.filter((t) => (t.trigger || {}).kind === "hand");
  const moved = lastMoved(p);
  // Tasks somebody set to wait on the reader, plus the notices on this
  // project's tasks nobody has checked off -- both are the third question.
  const waiting = live.filter((t) => waitsOn(t, whoNow())).length
    + live.reduce((n, t) => n + noticeCount(t), 0);
  // Shut by default, and one row each until the reader opens one: a shelf,
  // and the few things actually on the desk. What is open is remembered per
  // project on this device; a fresh load with nothing remembered is all shut.
  const open = PROJ_OPEN.has(String(p.id));
  const head = `<section class="project${p.status === "closed" ? " closed" : ""}` +
    `${open ? " open" : ""}" data-project="${p.id}">` +
    `<div class="p-row" data-open="${p.id}" title="${open ? "shut" : "open"} this project">` +
      `<span class="p-caret">${open ? "▾" : "▸"}</span>` +
      `<span class="p-name">${esc(p.title)}</span>` +
      `<span class="p-owner">${esc(ownerSaid(p.owner))}</span>` +
      // Whether the assistant is carrying this one. Its shelf is one line per
      // project every turn; a project on its desk is the one whose tasks are
      // in front of it. The assistant's to open and close, so it is shown and
      // not switched here -- this is a window on its head, not a knob on it.
      (p.desk ? `<span class="p-owner desk">${esc(T("onHerDesk"))}</span>` : "") +
      (p.status === "closed"
        ? `<span class="p-owner">${esc(T("closedP"))}</span>` : "") +
      // The third question, bubbled up so a shut shelf can answer it too.
      (waiting ? `<span class="p-wait">${waiting} ` +
        `${esc(T("waitingOnYou"))}</span>` : "") +
      `<span class="p-count">${live.length} ${esc(T("openN"))}</span>` +
      (moved ? `<span class="p-moved">${esc(T("moved"))} ` +
        `${esc(when(moved))}</span>` : "") +
      // The folder on the shut row too, because "where is it on disk" is half
      // of what a shelf is for. Long paths wrap rather than widening the page.
      `<div class="p-folder${p.folder ? "" : " none"}">` +
        `${esc(p.folder || T("noFolder"))}` +
        (p.folder_here === false
          ? `<span class="p-miss">${esc(T("notHere"))}</span>` : "") +
      `</div>` +
    `</div>`;
  if (!open) return head + `</section>`;
  const lastNote = p.notes.length ? p.notes[p.notes.length - 1] : null;
  const group = (label, rows) => rows.length
    ? `<div class="t-group"><div class="t-group-name">${esc(label)}` +
      `<span class="t-group-n">${rows.length}</span></div>` +
      rows.map((t) => taskHtml(p, t, view)).join("") + `</div>`
    : "";
  return head + `<div class="p-body">` +

    // Work first, forms after. The folder input and the note box used to be
    // the first two things a person met on opening a project, which put the
    // furniture in front of the thing the furniture is for.
    `<h3>${esc(T("tasks"))}</h3>` +
    (live.length
      ? group(T("standing"), standing) + group(T("todo"), todo)
      : `<div class="empty">${esc(T("nothingOpen"))}</div>`) +
    (over.length
      ? `<details class="dev" data-k="proj:done:${p.id}">` +
        `<summary>${esc(T("finished"))}` +
        `<span class="sub">${over.length}</span></summary><div class="inner">` +
        over.map((t) => taskHtml(p, t, view)).join("") + `</div></details>`
      : "") +

    // Where the project lives. The folder is the first row and stays
    // exactly what it was -- kept as typed, never checked, never refused,
    // empty clears it -- and the key:value rows keep it company. Every one
    // of those carries who wrote it, and putting one down leaves it on disk.
    `<details class="dev" data-k="proj:res:${p.id}"` +
      `${OPEN.has("proj:res:" + p.id) ? " open" : ""}>` +
      `<summary>${esc(T("resources"))}<span class="sub">` +
        `${(p.resources || []).length + (p.folder ? 1 : 0)}</span></summary>` +
      `<div class="inner">` +
      `<div class="res-row"><span class="res-key">${esc(T("folder"))}</span>` +
        `<span class="res-val${p.folder ? "" : " none"}">` +
        `${esc(p.folder || T("none"))}</span>` +
        (p.folder && p.folder_here === false
          ? `<span class="p-miss">${esc(T("notHere"))}</span>` : "") +
      `</div>` +
      (p.resources || []).map((r) => resourceRowHtml(p, r)).join("") +
      `<div class="form folder-form">` +
        `<label>${esc(T("folderOnDisk"))}` +
          `<input data-keep="p:folder:${p.id}" ` +
          `value="${heldOr("p:folder:" + p.id, p.folder)}" ` +
          `placeholder="C:\\Users\\you\\Documents\\my-project"></label>` +
        `<button data-do="folder" data-project="${p.id}">` +
          `${esc(T("saveFolder"))}</button>` +
      `</div>` +
      `<div class="form res-form">` +
        `<label>${esc(T("whatItIs"))}<input data-keep="r:key:${p.id}" ` +
          `value="${held("r:key:" + p.id)}" placeholder="github"></label>` +
        `<label>${esc(T("where"))}<input data-keep="r:val:${p.id}" ` +
          `value="${held("r:val:" + p.id)}" ` +
          `placeholder="github.com/you/my-project"></label>` +
        `<button data-do="res_set" data-project="${p.id}">` +
          `${esc(T("addAs"))} ${esc(whoSaid(whoNow()))}</button>` +
      `</div>` +
      `</div></details>` +

    // Notes fold with their count and their last author on the shut line,
    // because "who wrote last, and when" is the whole of what a shut Notes
    // box has to say. Attribution stays on every line inside it.
    `<details class="dev" data-k="proj:notes:${p.id}"` +
      `${OPEN.has("proj:notes:" + p.id) ? " open" : ""}>` +
      `<summary>${esc(T("notes"))}<span class="sub">` +
        `${p.notes.length}${lastNote ? " — " + esc(T("lastBy")) + " " +
          esc(whoSaid(lastNote.who)) + ", " + esc(when(lastNote.dt)) : ""}` +
        `</span></summary>` +
      `<div class="inner">` +
      (p.notes.length
        ? `<div class="notes">${p.notes.map((n) => noteLineHtml(p, n)).join("")}</div>`
        : `<div class="empty">${esc(T("nothingWritten"))}</div>`) +
      `<div class="note-add">` +
        `<textarea data-keep="note:${p.id}" placeholder="${esc(T("writeAs"))} ` +
        `${esc(whoSaid(whoNow()))}...">${held("note:" + p.id)}</textarea>` +
        `<button data-do="note" data-project="${p.id}">` +
          `${esc(T("addNote"))}</button>` +
      `</div></div></details>` +
    `<details class="dev" data-k="proj:newtask:${p.id}">` +
      `<summary>${esc(T("addTask"))}</summary><div class="inner">` +
      `<div class="form">` +
        `<label>${esc(T("whatItIs"))}<input data-keep="t:title:${p.id}" value="${held("t:title:" + p.id)}"></label>` +
        `<label>${esc(T("whatItWants"))}<textarea data-keep="t:wants:${p.id}">${held("t:wants:" + p.id)}</textarea></label>` +
        `<label class="tick"><input type="checkbox" data-keep="t:rep:${p.id}" class="rep"> ${esc(T("itRepeats"))}</label>` +
        `<label>${esc(T("whenRepeat"))}<input data-keep="t:sched:${p.id}" value="${held("t:sched:" + p.id)}" placeholder="every first Sunday"></label>` +
        `<div class="no-clock">${esc(view.no_clock)}</div>` +
        `<label>${esc(MY_NAME)}'s job, if it has one<select data-keep="t:job:${p.id}">` +
          `${jobOpts(view)}</select></label>` +
        `<button data-do="task" data-project="${p.id}">add task</button>` +
      `</div>` +
    `</div></details>` +

    `<details class="dev" data-k="proj:edit:${p.id}">` +
      `<summary>${esc(T("editProject"))}</summary><div class="inner">` +
      `<div class="form">` +
        `<label>${esc(T("title"))}<input data-keep="p:title:${p.id}" value="${esc(p.title)}"></label>` +
        `<label>${esc(T("whose"))}<select data-keep="p:owner:${p.id}">` +
          Object.keys(OWNER_SAID).map((k) =>
            `<option value="${k}"${k === p.owner ? " selected" : ""}>${esc(ownerSaid(k))}</option>`).join("") +
        `</select></label>` +
        `<label>${esc(T("standing_"))}<select data-keep="p:status:${p.id}">` +
          ["open", "closed"].map((k) =>
            `<option value="${k}"${k === p.status ? " selected" : ""}>` +
            `${esc(k === "open" ? T("openN") : T("closedP"))}</option>`).join("") +
        `</select></label>` +
        `<button data-do="edit" data-project="${p.id}">${esc(T("saveTask"))}</button>` +
      `</div>` +
    `</div></details>` +
    `</div></section>`;
}

function renderProjects() {
  const view = $("view-projects");
  const a = document.activeElement;
  if (a && /^(SELECT|INPUT|TEXTAREA)$/.test(a.tagName) && view.contains(a)) return;
  if (!plans) {
    view.innerHTML = `<div class="empty">reading their projects…</div>`;
    return;
  }
  // Theirs and the shared ones. The rest are somebody else's shelf, and
  // they are not drawn, counted, or waited on.
  const all = plans.projects || [];
  const list = all.filter((p) => mine(p, whoNow()));
  const other = langNow() === "bg" ? "en" : "bg";
  let html = glanceHtml(plans, list) + `<div class="proj-top">` +
    `<div class="who-writing">${esc(T("writingAs"))} ` +
    `<b>${esc(whoSaid(whoNow()))}</b> — ${esc(T("everyNote"))}` +
    // A default is a guess, and anyone may want the other language, so the
    // guess wears a way out of itself.
    `<button class="mini lang" data-lang="${other}">` +
    `${other === "bg" ? "български" : "English"}</button></div>` +
    `<details class="dev" data-k="proj:new">` +
      `<summary>${esc(T("newProject"))}</summary><div class="inner">` +
      `<div class="form">` +
        `<label>${esc(T("title"))}<input data-keep="n:title" value="${held("n:title")}"></label>` +
        // Whose it is starts as whoever is adding it. The list always begins
        // with the owner, so anyone else opening this form was being offered
        // the owner's name for their own project -- a default that has to be
        // undone every time is a default pointed the wrong way.
        //
        // Kept per person, because `keepForms` remembers every field on
        // every redraw: one shared key meant the first render's owner was
        // stored as though the next person had chosen it, and then shadowed
        // their own default for ever. What they actually pick still wins.
        `<label>${esc(T("whose"))}` +
        `<select data-keep="n:owner:${esc(whoNow())}">` +
          Object.keys(OWNER_SAID).map((k) =>
            `<option value="${k}"${k === (FORM["n:owner:" + whoNow()] || whoNow())
              ? " selected" : ""}>${esc(ownerSaid(k))}</option>`).join("") +
        `</select></label>` +
        `<label>${esc(T("folderIfAny"))}<input data-keep="n:folder" ` +
          `value="${held("n:folder")}" ` +
          `placeholder="C:\\Users\\you\\Documents\\my-project"></label>` +
        `<button data-do="add">${esc(T("addProject"))}</button>` +
      `</div>` +
    `</div></details></div>`;
  html += list.length
    ? list.map((p) => projectHtml(p, plans)).join("")
    // Two different emptinesses, and saying the wrong one would be a small
    // lie: a shelf with nothing on it anywhere, and a shelf with nothing of
    // yours on it.
    : all.length
      ? `<div class="empty">${esc(T("noneYours"))} — ` +
        `${esc(whoSaid(whoNow()))} ${esc(T("seesOwnAnd"))} ` +
        `${esc(ownerSaid("both"))}.</div>`
      : `<div class="empty">${esc(T("noProjects"))}</div>`;
  keepScroll(() => { patchHtml(view, html); });
}

async function refreshProjects() {
  try { plans = await getJson("api/projects"); } catch (e) { /* ask again later */ }
}

function val(name) {
  const el = MAIN.querySelector(`#view-projects [data-keep="${name}"]`);
  if (!el) return "";
  return el.type === "checkbox" ? el.checked : el.value;
}

async function projectDo(t) {
  const id = Number(t.dataset.project);
  const body = { who: whoNow(), project: id, op: t.dataset.do };
  if (t.dataset.do === "add") {
    body.title = val("n:title");
    body.owner = val("n:owner:" + whoNow());
    body.folder = val("n:folder");
  } else if (t.dataset.do === "edit") {
    body.title = val("p:title:" + id);
    body.owner = val("p:owner:" + id);
    body.folder = val("p:folder:" + id);
    body.status = val("p:status:" + id);
  } else if (t.dataset.do === "folder") {
    // The path alone, and nothing else about the project touched. Stored
    // exactly as typed -- never checked, never refused; empty clears it.
    body.op = "edit";
    body.folder = val("p:folder:" + id);
  } else if (t.dataset.do === "note") {
    body.text = val("note:" + id);
    if (!String(body.text).trim()) return;
  } else if (t.dataset.do === "note_edit") {
    const now = prompt("your own line, rewritten. What it said before is kept.",
                       t.dataset.raw || "");
    if (now === null) return;
    body.note = Number(t.dataset.note);
    body.text = now;
  } else if (t.dataset.do === "note_hide") {
    body.note = Number(t.dataset.note);
  } else if (t.dataset.do === "task") {
    body.title = val("t:title:" + id);
    body.wants = val("t:wants:" + id);
    body.repeats = !!val("t:rep:" + id);
    body.schedule = val("t:sched:" + id);
    body.job = val("t:job:" + id) || null;
    if (!String(body.title).trim()) return;
  } else if (t.dataset.do === "res_set") {
    // The author is never sent -- the server stamps it from whoever the
    // device says is writing, exactly as a note line does.
    body.op = "resource_set";
    body.key = val("r:key:" + id);
    body.value = val("r:val:" + id);
    if (!String(body.key).trim() || !String(body.value).trim()) return;
  } else if (t.dataset.do === "res_clear") {
    body.op = "resource_clear";
    body.key = t.dataset.key;
  } else if (t.dataset.do === "notice_done" || t.dataset.do === "notice_back") {
    // A person's word that a thing has been seen to. The note is asked for
    // once and plainly; empty is fine, cancel is a change of mind.
    body.op = "notice_done";
    body.notice = Number(t.dataset.notice);
    body.back = t.dataset.do === "notice_back";
    if (!body.back) {
      const note = prompt(T("noteForIt"), "");
      if (note === null) return;
      body.note = note;
    }
  } else if (t.dataset.do === "task_edit") {
    // What a task wants had no way in from the page at all until now -- it
    // could be written once and never corrected without going through the
    // assistant.
    // State is not sent: that one moves on its own pick.
    //
    // Every field goes as itself, never as null: the server reads null as
    // "leave this alone", so an empty box has to arrive empty to clear, and
    // picking "no job" has to arrive as "" to actually unlink.
    const tk = "te:" + t.dataset.task;
    body.task = Number(t.dataset.task);
    body.title = val(tk + ":title");
    body.wants = val(tk + ":wants");
    body.repeats = !!val(tk + ":rep");
    body.schedule = val(tk + ":sched");
    body.job = val(tk + ":job");
    body.needs = val(tk + ":needs");
    body.sense = val(tk + ":sense");
    body.sense_item = val(tk + ":item");
    body.at = val(tk + ":at");
    if (!String(body.title).trim()) return;
  }
  try {
    const out = await post("api/project", body);
    plans = out.view;
    for (const k of Object.keys(FORM)) delete FORM[k];
    renderProjects();
    markProjectsTab();
  } catch (err) {
    alert(err.message);
  }
}

// --- putting it together -------------------------------------------------------

function render() {
  if (!state) return;
  applyWho();
  renderUsage();
  renderRecall();
  renderChat();
  if (!$("view-dev").classList.contains("hidden")) renderDev();
}

// Each tab keeps its own place on the page.
let shown = "chat";
const SCROLL = { chat: null, dev: 0, projects: 0, sessions: 0 };

function show(which) {
  SCROLL[shown] = MAIN.scrollTop;
  for (const v of ["chat", "projects", "sessions", "dev"]) {
    $("view-" + v).classList.toggle("hidden", which !== v);
    const tab = $("tab-" + v);
    if (tab) tab.classList.toggle("active", which === v);
  }
  shown = which;
  clearInterval(devTimer);
  if (window.showSessions) window.showSessions(which === "sessions");
  if (which === "projects") {
    renderProjects();
    refreshProjects().then(renderProjects);
  }
  if (which === "dev") {
    renderDev();
    devTimer = setInterval(renderDev, 5000);
  }
  MAIN.scrollTop = SCROLL[which] === null ? MAIN.scrollHeight : SCROLL[which];
}

// The plan gauge is read on the poll, so it changes between renders. Redraw
// the header only when the reading is a different one -- and which model it
// is, is part of that reading now. Without it, switching models left the old name
// in the header until a window happened to move, which on a quiet afternoon
// is a long time to be looking at the wrong answer.
let lastUsage = "";

function usageIfMoved() {
  const sig = JSON.stringify([(progress && progress.limits) || null,
                              (progress && progress.provider) || null]);
  // `provider` carries `left`, so a balance that moves redraws the header on
  // its own — which is the whole point of putting it up there.
  if (sig === lastUsage) return;
  lastUsage = sig;
  renderUsage();
}

// `look` says a page is open on the room: only then does it re-ask LM Studio
// whether the automatic memory's model is still there. The manager's own
// asking, every two seconds, leaves it out.
async function pollProgress() {
  try { progress = await getJson("api/progress?look=1"); } catch (e) { /* the next poll asks again */ }
}

// A turn already running -- one a person started, one a session woke it for, or
// one this window did not see begin. Follow it until it ends, then redraw.
async function watchIfBusy() {
  await pollProgress();
  renderRecall();
  if (shown === "dev") paintRecall();
  usageIfMoved();
  if (!progress || !progress.busy || watching) return;
  clearInterval(poller);
  turnStart = Date.now() - (progress.elapsed || 0) * 1000;
  watchedTurn = progress.turn;
  failed = false;
  watching = true;
  renderLive();
  poller = setInterval(async () => {
    await pollProgress();
    renderRecall();
    if (progress && progress.busy) {
      // A different turn than the one being watched: the room woke the
      // assistant again inside half a second, so the last turn ended and this
      // one began without a single poll ever seeing it at rest. Its answer to
      // it was only ever the preview, and the preview belongs to the new turn
      // now -- which is how a finished answer vanished off the page and did
      // not come back until the next turn had finished. The row is in the
      // store the moment a turn ends, so it is fetched and laid down FIRST,
      // and what was written stays there to be read while the next turn works.
      if (progress.turn !== watchedTurn) {
        let ended;
        // A failed fetch leaves the preview standing -- the words, still on
        // screen -- and the next tick tries again.
        try { ended = await getJson("api/state"); } catch (e) { return; }
        watchedTurn = progress.turn;
        turnStart = Date.now() - (progress.elapsed || 0) * 1000;
        absorbState(ended);
        render();
        return;
      }
      renderLive();
      return;
    }
    // The turn has ended, and the real row is already in the store -- so the
    // fresh state is fetched BEFORE the forming preview comes down. Clearing
    // first left a gap where the answer vanished for as long as /api/state
    // took to arrive. On a failed fetch the preview stands and the next tick
    // tries again, rather than leaving the chat blank.
    let fresh;
    try { fresh = await getJson("api/state"); } catch (e) { return; }
    clearInterval(poller);
    watching = false;
    watchedTurn = null;
    absorbState(fresh);
    render();
    renderLive();
  }, 500);
}

// --- pictures ---------------------------------------------------------------
// Ctrl+V a snip, drag a file in, or press +. Three ways in because devices
// each tend to have only one of them: a desk pastes, a laptop drags, a phone
// has a button and nothing else.
//
// The shrinking happens HERE, before anything is sent. The model scales
// anything with a long edge over 1568 before it reads it, so sending more is
// paying to move pixels nobody looks at -- and the server needs no image
// library at all, which is the difference between this working today and
// waiting on an install. A screenshot stays a PNG: text put through JPEG comes
// out with rings around every letter, and reading a screenshot of a page is
// what this is for.
const PICS_MAX = 4;
const PIC_EDGE = 1568;
const PIC_AREA = 1150000;
const PIC_PER_TOKEN = 750;
const PIC_PASS = 2.5 * 1024 * 1024;   // small enough already: send as it lies

// The size the model will actually read, by its own two rules: a long edge of
// 1568, and about 1.15 megapixels of area. Both apply, and for anything large
// the area is the one that bites -- which is what puts a ceiling of about
// 1533 tokens on any single picture.
//
// This mirrors `shown_size` in server/pictures.py on purpose, and the shrink
// below aims at it, so the picture that is sent is exactly the picture that is
// charged for. It said 1967 tokens for a 4K screenshot before this existed,
// against the 1533 it would really cost -- and the number on the chip is the
// one thing about a picture worth knowing before pressing send.
function shownSize(w, h) {
  w = Math.max(1, w); h = Math.max(1, h);
  if (Math.max(w, h) > PIC_EDGE) {
    const s = PIC_EDGE / Math.max(w, h);
    w = Math.max(1, Math.round(w * s)); h = Math.max(1, Math.round(h * s));
  }
  if (w * h > PIC_AREA) {
    const s = Math.sqrt(PIC_AREA / (w * h));
    w = Math.max(1, Math.round(w * s)); h = Math.max(1, Math.round(h * s));
  }
  return [w, h];
}

function picTokens(w, h) {
  const [sw, sh] = shownSize(w, h);
  return Math.max(1, Math.round((sw * sh) / PIC_PER_TOKEN));
}

// What is waiting to go with the next line. Each {name, type, data, w, h,
// bytes, url} -- `url` only for the preview, and revoked when it goes.
let waiting = [];

function picNote(msg) {
  // A refusal has to be visible where the hands are, and the room has no
  // other place to say something small about the composer.
  const tray = $("tray");
  if (!tray) return;
  const line = document.createElement("div");
  line.className = "pic-note";
  line.textContent = msg;
  tray.appendChild(line);
  setTimeout(() => line.remove(), 6000);
}

function readAsDataURL(blob) {
  return new Promise((ok, no) => {
    const fr = new FileReader();
    fr.onload = () => ok(String(fr.result));
    fr.onerror = () => no(new Error("could not read the file"));
    fr.readAsDataURL(blob);
  });
}

function loadImage(url) {
  return new Promise((ok, no) => {
    const img = new Image();
    img.onload = () => ok(img);
    img.onerror = () => no(new Error("that file is not a picture"));
    img.src = url;
  });
}

async function takePicture(file) {
  const type = (file.type || "").toLowerCase();
  if (!/^image\/(png|jpeg|gif|webp)$/.test(type)) {
    throw new Error((file.name || "that file") + " is not a picture " + MY_NAME + " can"
      + " read — PNG, JPEG, GIF and WEBP are what the model takes");
  }
  const url = await readAsDataURL(file);
  // An animated GIF has more than one frame and a canvas has one, so it goes
  // whole or not at all rather than quietly losing its animation.
  if (type === "image/gif" || file.size <= PIC_PASS) {
    const img = await loadImage(url).catch(() => null);
    const big = img && (img.width * img.height > PIC_AREA
                        || Math.max(img.width, img.height) > PIC_EDGE);
    if (type === "image/gif" || !img || !big) {
      return { name: file.name || "picture", type, url,
               data: url.split(",")[1], bytes: file.size,
               w: img ? img.width : 0, h: img ? img.height : 0 };
    }
  }
  const img = await loadImage(url);
  const [w, h] = shownSize(img.width, img.height);
  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(img, 0, 0, w, h);
  const out = type === "image/jpeg"
    ? canvas.toDataURL("image/jpeg", 0.86)
    : canvas.toDataURL("image/png");
  return { name: file.name || "picture", type: type === "image/jpeg" ? type : "image/png",
           url: out, data: out.split(",")[1], w, h,
           bytes: Math.round((out.length - out.indexOf(",") - 1) * 3 / 4) };
}

async function addPictures(files) {
  for (const file of files) {
    if (waiting.length >= PICS_MAX) {
      picNote("four pictures is the most on one line");
      break;
    }
    try {
      waiting.push(await takePicture(file));
    } catch (err) {
      picNote(err.message || "that picture did not go in");
    }
  }
  renderTray();
}

function renderTray() {
  const tray = $("tray");
  if (!tray) return;
  tray.classList.toggle("hidden", waiting.length === 0);
  const notes = [...tray.querySelectorAll(".pic-note")];
  tray.innerHTML = waiting.map((p, i) =>
    `<div class="chip"><img src="${esc(p.url)}" alt="">` +
    `<span class="chip-name">${esc(p.name)}</span>` +
    `<span class="chip-size">${p.w && p.h ? p.w + "×" + p.h + " · " : ""}` +
    `~${p.w && p.h ? picTokens(p.w, p.h) : "?"} tokens</span>` +
    `<button class="chip-x" data-i="${i}" title="take it off">×</button></div>`).join("");
  notes.forEach((n) => tray.appendChild(n));
}

$("tray").addEventListener("click", (e) => {
  const x = e.target.closest(".chip-x");
  if (!x) return;
  waiting.splice(Number(x.dataset.i), 1);
  renderTray();
});

$("attach").onclick = () => $("pick").click();
$("pick").onchange = async () => {
  await addPictures([...$("pick").files]);
  $("pick").value = "";
};

// A snip on the clipboard, wherever the focus happens to be. Only pictures
// are taken -- pasted text goes on being pasted text.
window.addEventListener("paste", (e) => {
  const items = [...((e.clipboardData && e.clipboardData.files) || [])];
  const pics = items.filter((f) => (f.type || "").startsWith("image/"));
  if (!pics.length) return;
  e.preventDefault();
  addPictures(pics);
});

// Dragging one in. The whole window is the target, because aiming at a strip
// at the bottom of the page with a file in hand is a small unkindness.
let dragDepth = 0;
window.addEventListener("dragover", (e) => {
  if (![...(e.dataTransfer.types || [])].includes("Files")) return;
  e.preventDefault();
});
window.addEventListener("dragenter", (e) => {
  if (![...(e.dataTransfer.types || [])].includes("Files")) return;
  dragDepth += 1;
  document.body.classList.add("dropping");
});
window.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) document.body.classList.remove("dropping");
});
window.addEventListener("drop", (e) => {
  const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
  dragDepth = 0;
  document.body.classList.remove("dropping");
  const pics = files.filter((f) => (f.type || "").startsWith("image/"));
  if (!pics.length) return;
  e.preventDefault();
  addPictures(pics);
});

let voiceSending = false;
async function send() {
  if (voiceSending) return;
  const text = BOX.value.trim();
  if (window.AssistantVoice?.active()) {
    if (waiting.length) { window.AssistantVoice.message('End voice before sending a picture.'); return; }
    if (!text) return;
    const draft = BOX.value;
    voiceSending = true;
    try {
      await window.AssistantVoice.sendText(text);
      // A later draft belongs to the person, not this completed send.
      if (BOX.value === draft) { BOX.value = ''; resetBox(); }
    } catch (error) { window.AssistantVoice.message(error.message, true); }
    finally { voiceSending = false; }
    return;
  }
  // A picture on its own is a whole thing to say: pointing at something is
  // what a screenshot is for.
  if (!text && !waiting.length) return;
  const pics = waiting;
  waiting = [];
  renderTray();
  BOX.value = "";
  resetBox();
  // The line goes out signed by whoever the device says is writing, and the
  // echo wears the same name, so what is on screen is what the store gets.
  const who = whoNow();
  echoes.push({ text, at: Date.now(), who, pics });
  failed = false;
  renderChat();
  MAIN.scrollTop = MAIN.scrollHeight;
  try {
    const data = await post("api/send", {
      text, who,
      pictures: pics.map((p) => ({ name: p.name, data: p.data })),
    });
    // absorbState clears the echo by matching the row. This second drop is
    // for the case where the answer somehow comes back without the new row
    // in it: the server has said it wrote the line, so the echo has done its
    // job either way and must not be left standing.
    absorbState(data);
    const i = echoes.findIndex((e) => e.text === text);
    if (i >= 0) echoes.splice(i, 1);
    render();
  } catch (err) {
    // The send broke, so there is no row coming and the echo would sit there
    // for ever looking like a line the assistant had been given. It goes, and
    // the words go back in the box rather than being thrown away -- unless
    // something else has been typed in the meantime, which is the writer's to
    // keep.
    failed = true;
    state.last_turn = { error: String(err.message || err) };
    const i = echoes.findIndex((e) => e.text === text);
    if (i >= 0) echoes.splice(i, 1);
    if (!BOX.value.trim()) { BOX.value = text; growBox(); }
    renderChat();
  } finally {
    BOX.focus();
  }
  watchIfBusy();
}

// --- clicks and folds ------------------------------------------------------------

// What the reader opens stays open across re-renders. `toggle` does not bubble, so it
// is caught on the way down.
// A task's state moves on a pick, not a button: it is the one thing on this
// page that is changed often, and a save button on each of them is furniture.
MAIN.addEventListener("change", async (e) => {
  const sel = e.target.closest(".task-state");
  if (!sel) return;
  sel.blur();
  try {
    const out = await post("api/project", {
      who: whoNow(), op: "task_edit",
      task: Number(sel.dataset.task), state: sel.value });
    plans = out.view;
    renderProjects();
  } catch (err) { alert(err.message); }
});

MAIN.addEventListener("toggle", (e) => {
  const kk = e.target.dataset && e.target.dataset.k;
  if (!kk) return;
  if (e.target.open) OPEN.add(kk); else OPEN.delete(kk);
}, true);

MAIN.addEventListener("click", (e) => {
  const b = e.target.closest(".recall-more");
  if (b) { e.preventDefault(); recallMore(b.dataset.rk, b); }
  if (e.target.closest("#try-go")) { e.preventDefault(); runTry(); }
});

MAIN.addEventListener("input", (e) => {
  const name = e.target.dataset && e.target.dataset.try;
  if (name) TRY[name] = e.target.value;
  if (e.target.id === "try-review") TRY.review = e.target.checked;
});

MAIN.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !(e.target.dataset && e.target.dataset.try)) return;
  if (e.target.tagName === "TEXTAREA" && e.shiftKey) return;
  e.preventDefault();
  runTry();
});

MAIN.addEventListener("click", async (e) => {
  // A project row opens and shuts on the row itself, not on a button: the
  // whole line is the target, which is what a thumb needs.
  const row = e.target.closest("#view-projects .p-row");
  if (row && !e.target.closest("button")) return toggleProject(row.dataset.open);
  // A task opens on its row too, but never on the one control that lives
  // there: picking a state is the thing done most often on this page, and it
  // must not also fold the row away under the hand doing it.
  const trow = e.target.closest("#view-projects .t-row");
  if (trow && !e.target.closest("button, select, input, textarea, a")) {
    return toggleTask(trow.dataset.taskRow);
  }
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset && t.dataset.lang) {
    keepForms();
    return setLang(t.dataset.lang);
  }
  if (t.dataset && t.dataset.do) {
    keepForms();
    return projectDo(t);
  }
  if (t.classList.contains("reload")) {
    try {
      absorbState(await post("api/reload",
        { ids: String(t.dataset.id).split(",").map(Number) }));
      render();
    } catch (err) { alert(err.message); }
  } else if (t.classList.contains("tlog")) {
    const kk = t.dataset.k;
    const box = MAIN.querySelector(`[data-for="${kk}"]`);
    if (!box) return;
    box.classList.toggle("hidden");
    if (box.classList.contains("hidden")) OPEN.delete(kk); else OPEN.add(kk);
  } else if (t.id === "workings") {
    PLAIN = !PLAIN;
    try {
      localStorage.setItem("assistant:view:" + whoNow(),
                           PLAIN ? "plain" : "detailed");
    } catch (err) {}
    applyView();
    renderChat();
    renderUsage();
  } else if (t.id === "gauges") {
    GAUGES = !GAUGES;
    try {
      localStorage.setItem("assistant:gauges:" + whoNow(), GAUGES ? "on" : "off");
    } catch (err) {}
    applyView();
    renderUsage();
  } else if (t.id === "more-past") {
    // Pin the row that was at the top of the view, so the earlier rows
    // appear above it and nothing being read moves. Measured before the
    // fetch, since the button changing its own words moves the page a little.
    const top = MAIN.scrollTop;
    const anchor = [...MAIN.querySelectorAll("#view-chat .msg[data-id]")]
      .find((el) => el.offsetTop >= top);
    const gap = anchor ? anchor.offsetTop - top : 0;
    t.disabled = true;
    t.textContent = "fetching earlier rows…";
    try {
      await pageBack(150);
    } catch (err) {
      t.disabled = false;
      t.textContent = "that did not arrive · try again";
      return;
    }
    $("view-chat").innerHTML = "";
    renderChat();
    const again = anchor && MAIN.querySelector(`#view-chat .msg[data-id="${anchor.dataset.id}"]`);
    MAIN.scrollTop = again ? again.offsetTop - gap : top;
  }
});

// --- the text box ----------------------------------------------------------------

const BOX = $("input");

function growBox() {
  if (BOX.dataset.manual) return;
  const cap = Math.round(window.innerHeight * 0.45);
  BOX.style.height = "auto";
  BOX.style.height = Math.min(BOX.scrollHeight + 2, cap) + "px";
}

function resetBox() {
  delete BOX.dataset.manual;
  BOX.style.height = "";
  growBox();
}

$("send").onclick = send;

// The switch itself: a pick names who is writing, the device remembers, and
// the whole chat changes with it -- the colour, the filtered room, and the
// person's own remembered view of the page.
$("person").onchange = () => {
  person = $("person").value;
  try { localStorage.setItem("assistant:person", person); } catch (e) {}
  applyWho();
  renderChat();
  renderUsage();
  // The pick signs note lines as well as chat lines, so the Projects tab
  // says the new name too rather than offering to write as the last person.
  if (shown === "projects") { keepForms(); renderProjects(); }
  // And the notes box under Settings is the picked person's own, so it
  // turns to them too -- unless it holds something unsaved, which stays.
  if (shown === "dev") renderDev();
  MAIN.scrollTop = MAIN.scrollHeight;
  BOX.focus();
};
applyWho();

BOX.addEventListener("input", growBox);
BOX.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
window.addEventListener("resize", growBox);

let dragging = null;
$("grip").addEventListener("pointerdown", (e) => {
  dragging = { y: e.clientY, height: BOX.offsetHeight };
  $("grip").classList.add("dragging");
  $("grip").setPointerCapture(e.pointerId);
  e.preventDefault();
});
$("grip").addEventListener("pointermove", (e) => {
  if (!dragging) return;
  const cap = Math.round(window.innerHeight * 0.78);
  BOX.dataset.manual = "1";
  BOX.style.height = Math.max(46, Math.min(dragging.height + dragging.y - e.clientY, cap)) + "px";
});
const letGo = () => { dragging = null; $("grip").classList.remove("dragging"); };
$("grip").addEventListener("pointerup", letGo);
$("grip").addEventListener("pointercancel", letGo);
$("grip").addEventListener("dblclick", resetBox);
growBox();

$("tab-chat").onclick = () => show("chat");
$("tab-projects").onclick = () => show("projects");
$("tab-sessions").onclick = () => show("sessions");
$("tab-dev").onclick = () => show("dev");
$("recall").onclick = () => { OPEN.add("dev:recall"); show("dev"); };

// The keyboard. A phone shrinks the visible window when it opens one and does
// not tell the layout, so the composer ends up underneath it. visualViewport
// does know, and --vh hands the real height to the CSS. Above 700px this
// changes nothing: the desktop rules never read --vh.
function fitViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  document.documentElement.style.setProperty("--vh", Math.round(vv.height) + "px");
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", fitViewport);
  window.visualViewport.addEventListener("scroll", fitViewport);
  fitViewport();
}
// Keyboard resizing may follow the chat only if the reader was already at
// its end and has not scrolled away during the focus delay.
BOX.addEventListener("focus", () => {
  const top = MAIN.scrollTop;
  const following = shown === "chat" && MAIN.scrollHeight - top - MAIN.clientHeight <= 4;
  setTimeout(() => {
    if (following && shown === "chat" && MAIN.scrollTop === top) MAIN.scrollTop = MAIN.scrollHeight;
  }, 250);
});

// --- go --------------------------------------------------------------------------

// A link to a row older than the first page. Nothing is ever deleted, so ids
// are dense: the gap between the floor and the row asked for is how many
// rows stand between them, and the walk back is short and finite.
async function reachRow(id) {
  for (let page = 0; page < 12; page++) {
    if (oldest === null || oldest <= id || older <= 0) return;
    if (!(await pageBack(Math.min(400, oldest - id + 20)))) return;
  }
}

getJson("api/state").then(async (s) => {
  absorbState(s);
  render();
  MAIN.scrollTop = MAIN.scrollHeight;
  // #developer opens that tab; #row-123 lands on that row.
  const hash = location.hash || "";
  if (hash === "#developer") show("dev");
  const at = hash.match(/^#row-(\d+)$/);
  if (at) {
    await reachRow(Number(at[1]));
    renderChat();
    const el = MAIN.querySelector(`#view-chat .msg[data-id="${at[1]}"]`);
    if (el) MAIN.scrollTop = el.offsetTop - MAIN.offsetTop - 24;
  }
  watchIfBusy();
});

// The pairing line, at the top of Settings now rather than in the header --
// it is read once when a phone is paired and then it is furniture. Asked
// once, at load, and the server answers it only to a request that came from
// this machine -- so on a phone this fetch 403s and the line simply never
// appears. The address is selectable because pairing is copying it, once,
// to the thing in a pocket.
let PAIR_LINE = "";
fetch("api/pair", { cache: "no-store" }).then((r) => {
  // 403 is the ordinary answer on a phone, not a fault: the line is for the
  // machine itself. Checked rather than left to r.json() throwing on an empty
  // body, so a real breakage is not filed under "not this machine".
  if (!r.ok) return null;
  return r.json();
}).then((p) => {
  if (!p) return;
  PAIR_LINE = p.url
    ? 'your phone: <span class="key">' + esc(p.url) + "</span>"
    // Double quotes, because the sentence has an apostrophe in it. Written
    // with single ones this closed the string early and took the whole script
    // down with it -- app.js did not parse at all, so the page drew its shell
    // and nothing else. Found on this page, not made on it.
    : ("your phone: Tailscale is not up here, so I cannot read this machine's "
       + 'address. It is <span class="key">' + esc(p.template) + "</span>");
  const el = $("pair");
  if (el) { el.classList.remove("hidden"); el.innerHTML = PAIR_LINE; }
}).catch(() => { /* not this machine, or no key: no line, and no hint of one */ });

// A turn can start with nobody typing -- a hand coming home starts one -- so
// the room keeps half an eye open even when nobody is typing.
setInterval(() => { if (!watching) watchIfBusy(); }, 2000);

// The clock on a running turn.
setInterval(() => {
  const c = MAIN.querySelector("#live .status .clock");
  if (c && watching) c.textContent = ((Date.now() - turnStart) / 1000).toFixed(1) + "s";
}, 500);

// "resets in" drifts by the minute.
setInterval(renderUsage, 30000);

// A notice waiting on the reader is counted on the Projects tab's button
// from any view, and re-read every minute -- the senses look every two hours,
// so a minute is plenty, and it is one small read. If the tab is the one
// being looked at, it is redrawn with the fresh list as well.
refreshProjects().then(markProjectsTab);
setInterval(() => refreshProjects().then(() => {
  markProjectsTab();
  if (shown === "projects") renderProjects();
}), 60000);

// Voice transcript events keep their native input/output speaker attribution.
window.assistantVoiceRows = rows => {
  if (!state) return;
  const bottom = MAIN.scrollHeight - MAIN.scrollTop - MAIN.clientHeight < 100;
  shelve({rows}); laidOut(state); renderChat();
  if (bottom) MAIN.scrollTop = MAIN.scrollHeight;
};
window.assistantVoiceRefresh = async () => { absorbState(await getJson('api/state')); render(); };

// --- the face, and the other assistants on this machine --------------------
// The portrait opens a dialog for the face and the room's background; the
// name opens the list of assistants this install knows, with a way to make
// another. Making and waking a room is done at the desk; from anywhere else
// the room says so in words rather than hiding the button.

const FACE_DIALOG = $("face-dialog");
const NEW_DIALOG = $("new-assistant-dialog");
const PICK = $("assistant-pick");
const MENU = $("assistant-menu");
let portraitState = null;
let faceWallpaper = null;    // the backgrounds, if the dialog opened before the room's state had
let portraitBusy = false;
let PORTRAIT_NOTICE = null;
let homesList = null;

function wearPortrait(url) {
  if (url) $("portrait").src = url;
  const icon = document.querySelector('link[rel="icon"]');
  if (icon) icon.href = "artwork/icon.ico?v=" + Date.now();
}

function faceHtml() {
  const ps = portraitState || {};
  const items = Array.isArray(ps.items) ? ps.items : [];
  const note = PORTRAIT_NOTICE || {};
  const maxBytes = Number(ps.max_bytes) || WALLPAPER_MAX_BYTES;
  let html = `<h2 id="face-title">Portrait and background</h2>`;
  html += `<section class="face-part"><h3>Portrait</h3>` +
    `<p class="quiet">The face ${esc(MY_NAME)} wears here, in the manager's list, and on a phone's home screen. ` +
    `New pictures are kept in ${esc(MY_NAME)}'s artwork folder; the original face stays available.</p>` +
    `<div class="wallpaper-upload"><input id="portrait-file" type="file" accept="image/png,image/jpeg,image/gif,image/webp">` +
    `<button id="portrait-upload" type="button" disabled>upload and use it</button></div>` +
    `<p id="portrait-note" class="wallpaper-note${note.bad ? " bad" : ""}" role="status">` +
    esc(note.text || "PNG, JPEG, GIF or WEBP · up to " + Math.round(maxBytes / 1024 / 1024) +
        " MB") + `</p>`;
  html += items.length
    ? `<div class="portrait-grid" role="radiogroup" aria-label="Portraits">` + items.map((item) =>
      `<button class="wallpaper-option portrait-option" type="button" role="radio" data-portrait="${esc(item.id)}" ` +
      `aria-checked="${item.id === ps.selected ? "true" : "false"}" title="Wear ${esc(item.name)}">` +
      `<img src="${esc(item.url)}" alt=""><span class="wallpaper-name">${esc(item.name)}</span></button>`).join("") + `</div>`
    : `<p class="bad">The portraits could not be read just now.</p>`;
  const wp = (state && state.wallpaper) || faceWallpaper || {};
  html += `</section><section class="face-part"><h3>Background</h3><div id="face-wallpaper">${devWallpaper(wp)}</div></section>`;
  html += `<div class="dialog-actions"><button type="button" class="dialog-close">close</button></div>`;
  return html;
}

function renderFace() {
  FACE_DIALOG.innerHTML = faceHtml();
  FACE_DIALOG.querySelector(".dialog-close").onclick = () => FACE_DIALOG.close();
  wireWallpapers(FACE_DIALOG.querySelector("#face-wallpaper"), renderFace);
  wirePortraits();
}

function wirePortraits() {
  const root = FACE_DIALOG;
  const input = root.querySelector("#portrait-file"), upload = root.querySelector("#portrait-upload"),
    note = root.querySelector("#portrait-note");
  if (!input || !upload || !note) return;
  const maxBytes = (portraitState || {}).max_bytes || WALLPAPER_MAX_BYTES;
  const say = (text, bad) => {
    note.textContent = text;
    note.classList.toggle("bad", !!bad);
    PORTRAIT_NOTICE = {text, bad: !!bad};
  };
  const wore = (got, text) => {
    portraitState = got;
    wearPortrait((got.current || {}).url);
    PORTRAIT_NOTICE = {text, bad: false};
    portraitBusy = false;
    renderFace();
  };
  input.onchange = () => {
    const problem = wallpaperFileProblem(input.files && input.files[0], maxBytes);
    upload.disabled = !!problem || portraitBusy;
    if (input.files && input.files[0]) say(problem || (input.files[0].name + " is ready to upload"), !!problem);
  };
  upload.onclick = async () => {
    const file = input.files && input.files[0];
    const problem = wallpaperFileProblem(file, maxBytes);
    if (problem) { say(problem, true); return; }
    portraitBusy = true;
    upload.disabled = true;
    say("drawing the new face…", false);
    try {
      const data = await readAsDataURL(file);
      wore(await post("api/portraits/upload", {name: file.name, data}), "Uploaded, and wearing " + (file.name || "it") + " now");
    } catch (e) {
      portraitBusy = false;
      upload.disabled = false;
      say(String(e.message || e), true);
    }
  };
  for (const button of root.querySelectorAll(".portrait-option")) button.onclick = async () => {
    if (portraitBusy || button.getAttribute("aria-checked") === "true") return;
    portraitBusy = true;
    for (const b of root.querySelectorAll(".portrait-option")) b.disabled = true;
    say("changing the portrait…", false);
    try {
      const name = button.querySelector(".wallpaper-name").textContent;
      wore(await post("api/portraits/choose", {id: button.dataset.portrait}), "Wearing " + name + " now");
    } catch (e) {
      portraitBusy = false;
      for (const b of root.querySelectorAll(".portrait-option")) b.disabled = false;
      say(String(e.message || e), true);
    }
  };
}

async function openFace() {
  PORTRAIT_NOTICE = null;
  try { portraitState = await getJson("api/portraits"); } catch (e) { portraitState = null; }
  if (!state || !state.wallpaper) {
    try { faceWallpaper = await getJson("api/wallpapers"); } catch (e) { faceWallpaper = null; }
  }
  renderFace();
  if (!FACE_DIALOG.open) FACE_DIALOG.showModal();
}

// --- the list under the name ---
// Behind the manager (the usual way), the list is the manager's: every
// assistant at one address, /<slug>/, so switching is an ordinary link and
// works the same on a phone. Reached directly on its own port, the room
// knows only itself and says where the others are.

function closeAssistantMenu() {
  MENU.hidden = true;
  PICK.setAttribute("aria-expanded", "false");
}

const MANAGED = !!ASSISTANT.managed;

function managerAddress() {
  return location.protocol + "//" + location.hostname + ":" + (ASSISTANT.manager_port || 8787) + "/";
}

function assistantMenuHtml(list, problem) {
  if (!MANAGED) {
    return `<p class="assistant-note">This room was opened directly, on its own port. ` +
      `The other assistants are in the <a href="${esc(managerAddress())}">Digital Assistant Manager</a>.</p>`;
  }
  if (problem) return `<p class="assistant-note">${esc(problem)}</p>`;
  const all = (list && list.assistants) || [];
  return all.map((a) => {
    const here = a.slug === ASSISTANT.slug;
    const where = here ? "this room" : a.word;
    return `<a role="menuitemradio" aria-checked="${here ? "true" : "false"}" ` +
      `class="assistant-item${here ? " current" : ""}" href="/${esc(a.slug)}/">` +
      `<b>${esc(a.title || a.name)}</b><span class="quiet">${esc(where)}</span></a>`;
  }).join("") +
    `<a role="menuitem" class="assistant-item" href="/">Manage assistants…</a>` +
    (list && list.owner ? `<button type="button" role="menuitem" class="assistant-item assistant-new" id="assistant-new">+ New assistant…</button>` : "");
}

async function openAssistantMenu() {
  MENU.innerHTML = `<p class="assistant-note">looking…</p>`;
  MENU.hidden = false;
  PICK.setAttribute("aria-expanded", "true");
  let list = null, problem = "";
  if (MANAGED) {
    try {
      list = await getJson("/_/api/list");
    } catch (e) {
      problem = "the manager's list is not available just now";
    }
  }
  homesList = list;
  MENU.innerHTML = assistantMenuHtml(list, problem);
  const here = MENU.querySelector(".assistant-item.current");
  if (here) here.onclick = (e) => { e.preventDefault(); closeAssistantMenu(); };
  const add = MENU.querySelector("#assistant-new");
  if (add) add.onclick = () => { closeAssistantMenu(); openNewAssistant(); };
}

// --- a new one ---

// A name as a folder name: what Windows will not take in a path is dropped.
function folderFor(parent, name) {
  const safe = name.replace(/[<>:"\/\\|?*\x00-\x1f]/g, "").trim().replace(/[. ]+$/, "");
  if (!parent || !safe) return parent || "";
  return parent.replace(/[\\\/]+$/, "") + "\\" + safe;
}

function newAssistantHtml(atDesk) {
  return `<h2 id="new-assistant-title">A new assistant</h2>` +
    `<p class="quiet">An empty folder becomes its home: its memory, its Spark, its pictures and its backups, ` +
    `kept apart from ${esc(MY_NAME)}'s. It gets its own place in the manager, and thinks through the same model ` +
    `as the first assistant here until you change that in its Settings.</p>` +
    `<label class="field">Name<input id="na-name" type="text" maxlength="40" autocomplete="off" spellcheck="false"></label>` +
    `<label class="field">Folder<span class="field-row"><input id="na-folder" type="text" autocomplete="off" spellcheck="false">` +
    (atDesk ? `<button id="na-browse" type="button">Browse…</button>` : "") + `</span></label>` +
    `<p id="na-note" class="wallpaper-note" role="status">Before talking to it much, write its Spark: ` +
    `data\\spark.md in its folder, the text it reads as itself.</p>` +
    `<div class="dialog-actions"><button id="na-create" type="button" disabled>Create and open</button>` +
    `<button type="button" class="dialog-close">cancel</button></div>`;
}

async function openNewAssistant() {
  if (!homesList) {
    try { homesList = await getJson("/_/api/list"); } catch (e) { /* the folder box starts empty */ }
  }
  NEW_DIALOG.innerHTML = newAssistantHtml(!!(homesList && homesList.desk));
  const nameEl = NEW_DIALOG.querySelector("#na-name"), folderEl = NEW_DIALOG.querySelector("#na-folder");
  const create = NEW_DIALOG.querySelector("#na-create"), browse = NEW_DIALOG.querySelector("#na-browse");
  const note = NEW_DIALOG.querySelector("#na-note");
  let parent = (homesList && homesList.default_parent) || "", typed = false, busy = false;
  const say = (text, bad) => { note.textContent = text; note.classList.toggle("bad", !!bad); };
  const ready = () => { create.disabled = busy || !nameEl.value.trim() || !folderEl.value.trim(); };
  nameEl.oninput = () => {
    if (!typed) folderEl.value = folderFor(parent, nameEl.value);
    ready();
  };
  folderEl.oninput = () => { typed = true; ready(); };
  if (browse) browse.onclick = async () => {
    say("A folder dialog is open on this screen. Choose where it should live.", false);
    browse.disabled = true;
    try {
      const got = await post("/_/api/browse", {from: parent});
      if (got.folder) {
        const leaf = got.folder.split(/[\\\/]/).pop() || "";
        const name = nameEl.value.trim();
        // A folder already named for it is the folder; anywhere else is the
        // place it goes into.
        if (name && leaf.toLowerCase() === folderFor("", name).toLowerCase()) {
          folderEl.value = got.folder;
          typed = true;
        } else {
          parent = got.folder;
          typed = false;
          folderEl.value = folderFor(parent, name) || parent;
        }
      }
      say("It will live in the folder shown. An empty or new folder, please.", false);
    } catch (e) {
      say(String(e.message || e), true);
    }
    browse.disabled = false;
    ready();
  };
  create.onclick = async () => {
    const name = nameEl.value.trim(), folder = folderEl.value.trim();
    if (!name || !folder || busy) return;
    busy = true;
    ready();
    say("Making " + name + " and waking it. The first start can take half a minute.", false);
    try {
      const got = await post("/_/api/new", {name, folder});
      homesList = null;
      location.href = got.url;
    } catch (e) {
      busy = false;
      ready();
      say(String(e.message || e), true);
    }
  };
  NEW_DIALOG.querySelector(".dialog-close").onclick = () => NEW_DIALOG.close();
  if (!NEW_DIALOG.open) NEW_DIALOG.showModal();
  nameEl.focus();
}

// A click on the dim around a dialog closes it, like Escape does.
for (const d of [FACE_DIALOG, NEW_DIALOG]) d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
$("portrait-button").onclick = openFace;
PICK.onclick = (e) => {
  e.stopPropagation();
  if (MENU.hidden) openAssistantMenu(); else closeAssistantMenu();
};
document.addEventListener("click", (e) => {
  if (!MENU.hidden && !e.target.closest(".assistant-switch")) closeAssistantMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !MENU.hidden) closeAssistantMenu();
});
