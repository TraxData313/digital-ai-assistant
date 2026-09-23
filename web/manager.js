// The manager's page: every assistant on this machine, what each is doing,
// and Open / Start / Stop / Restart. Asks the manager every two seconds.
// Standing in for an assistant that is down (at /<slug>/), it says so at the
// top and walks into the room by itself the moment the room answers.
"use strict";

const FOCUS = document.body.dataset.focus || "";
const LIST = document.getElementById("list");
const FOCUS_BOX = document.getElementById("focus");
const PROBLEM = document.getElementById("problem");
const NEW = document.getElementById("new");
const ATTACH = document.getElementById("attach");
const NOTICE = document.getElementById("notice");
const DIALOG = document.getElementById("new-dialog");
const ATTACH_DIALOG = document.getElementById("attach-dialog");
let last = null;
let acting = "";      // the slug something is being done to, so its buttons wait

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

async function getJson(url) {
  const r = await fetch(url, {cache: "no-store", credentials: "same-origin"});
  if (!r.ok) throw new Error(r.status === 403 ? "this browser is not paired with any assistant here" : "the manager said " + r.status);
  return r.json();
}

async function post(url, body) {
  const r = await fetch(url, {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  let data = {};
  try { data = await r.json(); } catch (e) { /* an empty refusal */ }
  if (!r.ok) throw new Error(data.error || ("the manager said " + r.status));
  return data;
}

const UP = new Set(["awake", "thinking"]);
const DOWN = new Set(["stopped", "held", "down"]);

function buttons(a) {
  const out = [];
  const reachable = a.mode === "ours" && UP.has(a.phase);
  if (reachable) out.push(`<a class="button primary" href="/${esc(a.slug)}/">Open</a>`);
  if (!a.control || acting === a.slug) {
    if (acting === a.slug) out.push(`<button type="button" disabled>working…</button>`);
    return out.join("");
  }
  if (a.mode === "stopped" || a.mode === "held") out.push(`<button type="button" class="primary" data-do="start" data-slug="${esc(a.slug)}">Start</button>`);
  if (a.mode === "ours") {
    out.push(`<button type="button" data-do="restart" data-slug="${esc(a.slug)}">Restart</button>`);
    out.push(`<button type="button" data-do="stop" data-slug="${esc(a.slug)}">Stop</button>`);
  }
  if (!a.first) out.push(`<button type="button" class="plain detach" data-do="detach" data-slug="${esc(a.slug)}">Detach</button>`);
  return out.join("");
}

function explain(a) {
  if (a.mode === "stopped") return "Stopped: it does not answer, dream or run its jobs until it is started.";
  if (a.mode === "theirs") return "Started in another window, not by the manager, so it can only be opened there.";
  if (a.mode === "elsewhere") return "Its own old icon in the corner still holds it. Put that one down and the manager takes over.";
  return a.detail || "";
}

function card(a) {
  const down = DOWN.has(a.phase) || a.mode === "elsewhere";
  const detail = explain(a);
  return `<article class="card${down ? " down" : ""}${a.slug === FOCUS ? " focused" : ""}">` +
    `<img class="face" src="/_/face/${esc(a.slug)}" alt="">` +
    `<div class="who"><b>${esc(a.title || a.name)}</b><span class="state ${esc(a.phase)}">${esc(a.word)}</span></div>` +
    (detail ? `<div class="detail">${esc(detail)}</div>` : "") +
    (a.home ? `<div class="detail">${esc(a.home)}</div>` : "") +
    `<div class="buttons">${buttons(a)}</div></article>`;
}

function focusHtml(a) {
  if (!a) return `<p>That assistant is not one this browser can see.</p>`;
  const name = esc(a.title || a.name);
  if (a.mode === "stopped") return `<p>${name} is stopped.</p>` + (a.control
    ? `<button type="button" class="primary" data-do="start" data-slug="${esc(a.slug)}">Start ${name}</button>`
    : `<p class="quiet">Only the one who keeps ${name} can start it.</p>`);
  if (a.mode === "held") return `<p>${name} is staying down: ${esc(a.detail)}</p>` + (a.control
    ? `<button type="button" class="primary" data-do="start" data-slug="${esc(a.slug)}">Try again</button>` : "");
  if (a.mode === "theirs" || a.mode === "elsewhere") return `<p>${name}: ${esc(explain(a))}</p>`;
  return `<p>${name} is ${esc(a.word)}… this page opens the room by itself when it answers.</p>`;
}

// Drawn again only when something drawn has changed: a list redrawn every
// two seconds regardless would swallow a click that landed mid-redraw.
let drawn = "";

function render(data) {
  last = data;
  PROBLEM.hidden = true;
  const all = data.assistants || [];
  if (FOCUS) {
    const a = all.find((x) => x.slug === FOCUS);
    // The room is answering: go in. The address is already the room's own.
    if (a && a.mode === "ours" && UP.has(a.phase)) { location.reload(); return; }
  }
  const sig = JSON.stringify([all.map((x) => [x.slug, x.title, x.phase, x.word, x.detail, x.mode, x.control, x.home, x.first]),
    data.owner, acting]);
  if (sig === drawn) return;
  drawn = sig;
  LIST.innerHTML = all.length ? all.map(card).join("")
    : `<p class="quiet">No assistants here yet.</p>`;
  NEW.hidden = !data.owner;
  ATTACH.hidden = !data.owner;
  if (FOCUS) {
    const a = all.find((x) => x.slug === FOCUS);
    FOCUS_BOX.hidden = false;
    FOCUS_BOX.innerHTML = focusHtml(a);
    document.title = (a ? (a.title || a.name) + " · " : "") + document.title.replace(/^.* · /, "");
  }
}

async function refresh() {
  try {
    render(await getJson("/_/api/list"));
  } catch (e) {
    PROBLEM.textContent = String(e.message || e);
    PROBLEM.hidden = false;
  }
}

const VERB = {stop: "Stop", restart: "Restart", detach: "Detach"};

function say(text) {
  NOTICE.textContent = text;
  NOTICE.hidden = !text;
}

async function act(slug, what) {
  const a = ((last && last.assistants) || []).find((x) => x.slug === slug);
  const name = a ? (a.title || a.name) : slug;
  if (what === "stop" && !confirm(`Stop ${name}?\n\nIt will not answer, dream or run its jobs until you start it again.`)) return;
  if (what === "detach" && !confirm(`Detach ${name}?\n\nThe manager stops ${name} and lets go of its folder. ` +
      `Nothing in the folder is changed or deleted: move it wherever you like, then attach it again from there.`)) return;
  acting = slug;
  say("");
  if (last) render(last);
  try {
    let got = await post("/_/api/" + what, {slug});
    if (got.busy) {
      if (!confirm(`${name} is in the middle of a turn right now.\n\n${VERB[what] || what} anyway? The turn will be lost.`)) {
        acting = "";
        return refresh();
      }
      got = await post("/_/api/" + what, {slug, anyway: true});
    }
    if (what === "detach" && got.ok) {
      say(`${name} is detached. Its folder is untouched` + (got.home ? `: ${got.home}` : "") +
        `. Move it wherever you like, then use “Attach an assistant…” to bring ${name} back.`);
    }
  } catch (e) {
    PROBLEM.textContent = String(e.message || e);
    PROBLEM.hidden = false;
  }
  acting = "";
  refresh();
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-do]");
  if (b) act(b.dataset.slug, b.dataset.do);
});

// --- a new one ---

function folderFor(parent, name) {
  const safe = name.replace(/[<>:"\/\\|?*\x00-\x1f]/g, "").trim().replace(/[. ]+$/, "");
  if (!parent || !safe) return parent || "";
  return parent.replace(/[\\\/]+$/, "") + "\\" + safe;
}

function openNew() {
  const nameEl = document.getElementById("na-name"), folderEl = document.getElementById("na-folder");
  const create = document.getElementById("na-create"), browse = document.getElementById("na-browse");
  const note = document.getElementById("na-note");
  let parent = (last && last.default_parent) || "", typed = false, busy = false;
  nameEl.value = "";
  folderEl.value = "";
  browse.hidden = !(last && last.desk);
  const say = (text, bad) => { note.textContent = text; note.classList.toggle("bad", !!bad); };
  const ready = () => { create.disabled = busy || !nameEl.value.trim() || !folderEl.value.trim(); };
  nameEl.oninput = () => { if (!typed) folderEl.value = folderFor(parent, nameEl.value); ready(); };
  folderEl.oninput = () => { typed = true; ready(); };
  browse.onclick = async () => {
    say("A folder dialog is open on the desk's screen. Choose where it should live.", false);
    browse.disabled = true;
    try {
      const got = await post("/_/api/browse", {from: parent});
      if (got.folder) {
        const leaf = got.folder.split(/[\\\/]/).pop() || "";
        const name = nameEl.value.trim();
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
      location.href = got.url;
    } catch (e) {
      busy = false;
      ready();
      say(String(e.message || e), true);
    }
  };
  document.getElementById("na-cancel").onclick = () => DIALOG.close();
  ready();
  if (!DIALOG.open) DIALOG.showModal();
  nameEl.focus();
}

// --- one that already has a home ---

function openAttach() {
  const folderEl = document.getElementById("at-folder"), go = document.getElementById("at-go");
  const browse = document.getElementById("at-browse"), note = document.getElementById("at-note");
  let busy = false;
  folderEl.value = "";
  browse.hidden = !(last && last.desk);
  const tell = (text, bad) => { note.textContent = text; note.classList.toggle("bad", !!bad); };
  const ready = () => { go.disabled = busy || !folderEl.value.trim(); };
  tell("", false);
  folderEl.oninput = ready;
  browse.onclick = async () => {
    tell("A folder dialog is open on the desk's screen. Choose the assistant's folder.", false);
    browse.disabled = true;
    try {
      const got = await post("/_/api/browse", {from: (last && last.default_parent) || "", for: "attach"});
      if (got.folder) folderEl.value = got.folder;
      tell("", false);
    } catch (e) {
      tell(String(e.message || e), true);
    }
    browse.disabled = false;
    ready();
  };
  go.onclick = async () => {
    const folder = folderEl.value.trim();
    if (!folder || busy) return;
    busy = true;
    ready();
    tell("Attaching it and waking it…", false);
    try {
      const got = await post("/_/api/attach", {folder});
      if (got.note) {
        tell(got.note + ". Opening it…", false);
        await new Promise((r) => setTimeout(r, 2500));
      }
      location.href = got.url;
    } catch (e) {
      busy = false;
      ready();
      tell(String(e.message || e), true);
    }
  };
  document.getElementById("at-cancel").onclick = () => ATTACH_DIALOG.close();
  ready();
  if (!ATTACH_DIALOG.open) ATTACH_DIALOG.showModal();
  folderEl.focus();
}

NEW.onclick = openNew;
ATTACH.onclick = openAttach;
for (const d of [DIALOG, ATTACH_DIALOG]) d.addEventListener("click", (e) => { if (e.target === d) d.close(); });

refresh();
setInterval(() => { if (!document.hidden && !acting) refresh(); }, 2000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
