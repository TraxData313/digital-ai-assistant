"""The automatic memory: before every turn, a small local model looks at the
last few lines of the room and searches the assistant's essences for it.

Four steps, and every one of them is shown -- to the owner in the room, and
to the assistant in its prompt, the same content both ways:

    1. write    the model sees a few real essences for their style and the
                last lines of the room, and writes what an essence about the
                talk would look like, a few keywords, and a date range if
                the talk points at a time.
    2. search   that essence against the shelf by likeness, plain BGE-M3
                cosine, no bar: the top few come back with their scores,
                and the best keyword finds that are not already among them.
    3. review   the same model reads each result and leaves one short line --
                related or not, and why -- and how much the assistant would
                want to read it, 0-100.
    4. hand     the assistant gets the lot before it speaks. It suggests; the
                assistant decides whether to read anything.

"Show more" is the same search, further down the list, without the review.

It has a deadline. If it misses, the turn goes ahead without it and says so;
the run finishes anyway and is kept for the Developer tab.

The model is hosted by LM Studio (list, load, unload, download). This file
drives it; it writes no inference engine.

    data/recall.json          the model and the knobs, editable under Developer
    server/recall_prompts.py  what the model is told, the owner's to edit
"""

import json
import os
import runpy
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from . import db, embed, people, pictures
from . import home

SETTINGS_PATH = home.DATA / "recall.json"
# One line per turn: what was searched, what came back, what the reviewer
# said, and which essences the assistant then actually pulled in -- kept for
# measuring, and one day for training.
LOG_PATH = home.DATA / "recall_log.jsonl"
CONFIG_PATH = home.prompt_path("recall_prompts.py")

# LM Studio's local server. Its own port, not ours.
LM = os.environ.get("ASSISTANT_LM_STUDIO", "http://127.0.0.1:1234")

# What the dropdown offers: LM Studio's key for the model. `about_gb` is only
# for the download prompt; once on disk the real size is read from LM Studio.
MODELS = [
    {"key": "google/gemma-4-e4b", "label": "Gemma 4 E4B", "about_gb": 6.3},
    {"key": "qwen/qwen3-4b", "label": "Qwen 3 4B", "about_gb": 2.5},
    {"key": "qwen/qwen3-8b", "label": "Qwen 3 8B", "about_gb": 5.0},
]
KEYS = {m["key"]: m for m in MODELS}

KNOBS = {
    # the hard limit, seconds; a turn never waits longer than this for it
    "timeout_s":  {"default": 60.0, "min": 1.0, "max": 60.0, "type": float},
    # how many of the last lines of the room the writer reads
    "lines":      {"default": 5, "min": 1, "max": 12, "type": int},
    # and how many the reviewer reads to judge what came back against
    "review_lines": {"default": 5, "min": 1, "max": 12, "type": int},
    # how many results by likeness are shown and reviewed
    "top":        {"default": 3, "min": 1, "max": 10, "type": int},
    # how many keyword finds are added on top, when they are not already shown
    "keyword_top": {"default": 2, "min": 0, "max": 5, "type": int},
    # how much of each result the reviewer reads, in characters from the top
    "read_chars": {"default": 1500, "min": 200, "max": 6000, "type": int},
}

# How many example essences the writer sees for their style, and how much of
# each.
STYLE_EXAMPLES = 3
STYLE_CHARS = 500
# A line of the room longer than this keeps its start and its end.
LINE_CHARS = 1200
# A keyword found in more than this share of the searched essences is in
# nearly everything, finds nothing in particular, and is not used for keyword
# finds. Its count is still shown, marked too common.
COMMON = 0.2
# "Show more" hands back this many at a time.
MORE = 10

CONTEXT = 8192
# Gemma 4 thinks before it answers, and LM Studio counts that thinking against
# max_tokens: at 600 the thinking alone often ate the budget and the JSON came
# back cut off. The prompt (about 2.4k) plus this still sits well inside CONTEXT.
MAX_OUT = 2500

# How often the room re-asks LM Studio whether the model is still there, and
# only while a page is open on it: every ten seconds while something is
# changing -- LM Studio down, the model on disk and not in memory, a load
# that failed -- and once a minute while it is loaded, when the only news
# would be LM Studio letting it go.
RECHECK_S = 10.0
RECHECK_LOADED_S = 60.0
# A download LM Studio calls complete can take a moment to show in its list.
APPEAR_S = 90.0

STATE = {
    "model": None,         # the key chosen, or None
    "phase": "off",        # off | checking | no server | not downloaded |
                           # downloading | loading | on disk | loaded |
                           # working | failed
    "detail": "",
    "size_bytes": None,
    "instance_id": None,
    "download": None,      # {job_id, downloaded, total, bps} while downloading
    "embedder": "cold",    # cold | warming | warm | unavailable
    "last": None,          # the last run, whole -- even one that missed
    "last_sent": None,     # what the model was sent on the last run
    "checked": 0.0,
    "loading_since": None,
    "config_error": None,
}
LOCK = threading.RLock()
# Loading, unloading and downloading take turns.
SWITCH = threading.Lock()


class NoServer(RuntimeError):
    """LM Studio is not answering."""


class LMError(RuntimeError):
    """LM Studio answered with an error of its own."""


def _set(**changes):
    with LOCK:
        STATE.update(changes)


def _lm(path: str, body=None, timeout: float = 2.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        LM + path, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = (err.get("error") or {})
            msg = msg.get("message") if isinstance(msg, dict) else str(msg)
        except Exception:
            msg = None
        raise LMError(msg or ("HTTP " + str(e.code))) from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise NoServer("LM Studio is not answering at " + LM
                       + " (" + type(e).__name__ + ")") from e


# -- settings ----------------------------------------------------------------

def defaults() -> dict:
    return {n: k["default"] for n, k in KNOBS.items()}


def _clamp(name, value):
    k = KNOBS[name]
    try:
        v = k["type"](value)
    except (TypeError, ValueError):
        v = k["default"]
    return min(max(v, k["min"]), k["max"])


def settings() -> dict:
    cfg = {"model": None}
    cfg.update(defaults())
    if SETTINGS_PATH.is_file():
        try:
            cfg.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    if cfg.get("model") not in KEYS:
        cfg["model"] = None
    for name in KNOBS:
        cfg[name] = _clamp(name, cfg.get(name))
    return cfg


def save(**changes) -> dict:
    cfg = settings()
    for name, value in changes.items():
        if name == "model":
            cfg["model"] = value if value in KEYS else None
        elif name in KNOBS:
            cfg[name] = _clamp(name, value)
    out = {"model": cfg["model"]}
    out.update({n: cfg[n] for n in KNOBS})
    out["note"] = ("The automatic memory: which local model runs before "
                   + home.NAME + "'s turn (an LM Studio key, or null for "
                   "none) and its knobs, editable under Developer. What it "
                   "is told lives in server/recall_prompts.py.")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return settings()


def set_knobs(changes: dict) -> dict:
    """The Developer tab's fields. Unknown names are ignored, the rest
    clamped. `reset` puts every knob back to its default."""
    if (changes or {}).get("reset"):
        return save(**defaults())
    return save(**{k: v for k, v in (changes or {}).items() if k in KNOBS})


PIECES = ("WRITER", "REVIEWER")


def config() -> dict:
    """What the model is told, read fresh so an edit is live next turn. A
    file that cannot be read is said on the Developer tab and the code's own
    words are used instead."""
    try:
        got = runpy.run_path(str(CONFIG_PATH))
        text = {k: home.fill(str(got[k])) for k in PIECES}
        _set(config_error=None)
        return text
    except Exception as exc:
        _set(config_error=type(exc).__name__ + ": " + str(exc))
        from . import recall_prompts
        return {k: home.fill(getattr(recall_prompts, k)) for k in PIECES}


# -- LM Studio ---------------------------------------------------------------

def _models() -> list:
    return (_lm("/api/v1/models", timeout=1.5) or {}).get("models") or []


def _find(models, key):
    for m in models:
        if m.get("key") == key:
            return m
    return None


def _instances(rec) -> list:
    return [i.get("id") for i in (rec or {}).get("loaded_instances") or []
            if i.get("id")]


def _unload(key):
    """Free the model that was selected, and only that one."""
    if not key:
        return
    try:
        rec = _find(_models(), key)
    except (NoServer, LMError):
        return
    for inst in _instances(rec):
        try:
            _lm("/api/v1/models/unload", {"instance_id": inst}, timeout=60)
        except (NoServer, LMError):
            pass


def _bring_up(key, wait_for_it: float = 0.0):
    """Get the chosen model to `loaded`. Never downloads on its own: that
    waits for the button. `wait_for_it` keeps looking for a model LM Studio
    has just finished downloading and not yet listed."""
    _set(checked=time.time())
    until = time.time() + wait_for_it
    while True:
        try:
            models = _models()
        except NoServer as e:
            _set(phase="no server", detail=str(e) + ". Start LM Studio's "
                 "server and the room will find it.")
            return
        rec = _find(models, key)
        if rec is not None or time.time() >= until:
            break
        _set(phase="checking", detail="downloaded; waiting for LM Studio to list it")
        time.sleep(2.0)
    if rec is None:
        _set(phase="not downloaded", size_bytes=None, instance_id=None,
             detail="not on this disk yet")
        return
    _set(size_bytes=rec.get("size_bytes"))
    have = _instances(rec)
    if have:
        _set(phase="loaded", instance_id=have[0], detail="already in memory",
             loading_since=None)
        return
    _set(phase="loading", detail="", loading_since=time.time())
    try:
        out = _lm("/api/v1/models/load",
                  {"model": key, "context_length": CONTEXT}, timeout=600)
    except (NoServer, LMError) as e:
        _set(phase="failed", detail="could not load: " + str(e), loading_since=None)
        return
    took = out.get("load_time_seconds")
    _set(phase="loaded", instance_id=out.get("instance_id") or key,
         detail=("loaded in " + format(took, ".1f") + " s") if took else "loaded",
         loading_since=None)
    with LOCK:
        still = STATE["model"]
    if still != key:
        _unload(key)


def _switch(old, new, wait_for_it: float = 0.0):
    with SWITCH:
        if old and old != new:
            _unload(old)
        if new is None:
            _set(phase="off", detail="", size_bytes=None, instance_id=None,
                 download=None, loading_since=None)
            return
        _bring_up(new, wait_for_it)
    warm_embedder()


def choose(key) -> dict:
    """The dropdown. Saves, then loads and frees on a thread."""
    key = key if key in KEYS else None
    old = settings()["model"]
    save(model=key)
    _set(model=key, phase="off" if key is None else "checking", detail="",
         download=None)
    threading.Thread(target=_switch, args=(old, key), daemon=True).start()
    return status()


def download() -> dict:
    """The download button: LM Studio fetches the chosen model, and the room
    follows the job until it is on disk, then loads it."""
    key = settings()["model"]
    if not key:
        return {"error": "no model is chosen"}
    try:
        out = _lm("/api/v1/models/download", {"model": key}, timeout=30)
    except NoServer as e:
        _set(phase="no server", detail=str(e))
        return {"error": str(e)}
    except LMError as e:
        _set(phase="failed", detail="the download was refused: " + str(e))
        return {"error": str(e)}
    if out.get("status") == "already_downloaded" or not out.get("job_id"):
        threading.Thread(target=_switch, args=(None, key, APPEAR_S),
                         daemon=True).start()
        return status()
    _set(phase="downloading", detail="",
         download={"job_id": out["job_id"], "total": out.get("total_size_bytes"),
                   "downloaded": 0, "bps": None})
    threading.Thread(target=_follow_download, args=(out["job_id"], key),
                     daemon=True).start()
    return status()


def _follow_download(job_id, key):
    while True:
        time.sleep(1.0)
        with LOCK:
            if STATE["model"] != key:
                STATE["download"] = None
                return
        try:
            st = _lm("/api/v1/models/download/status/" + job_id, timeout=5)
        except (NoServer, LMError) as e:
            _set(phase="failed", detail="lost sight of the download: " + str(e),
                 download=None)
            return
        _set(download={"job_id": job_id, "total": st.get("total_size_bytes"),
                       "downloaded": st.get("downloaded_bytes") or 0,
                       "bps": st.get("bytes_per_second")})
        if st.get("status") == "completed":
            _set(download=None, detail="downloaded", phase="checking")
            threading.Thread(target=_switch, args=(None, key, APPEAR_S),
                             daemon=True).start()
            return
        if st.get("status") == "failed":
            _set(phase="failed", detail="the download failed", download=None)
            return


def load_now() -> dict:
    """The load button, for a model on disk that is not in memory."""
    key = settings()["model"]
    if key:
        threading.Thread(target=_switch, args=(None, key), daemon=True).start()
    return status()


def refresh(force: bool = False):
    """Catch up with what LM Studio actually has, at most every RECHECK_S,
    or RECHECK_LOADED_S while the model is loaded. Called for a page that is
    open on the room; see `status`."""
    with LOCK:
        key = STATE["model"]
        phase = STATE["phase"]
        every = RECHECK_LOADED_S if phase == "loaded" else RECHECK_S
        due = force or (time.time() - STATE["checked"]) > every
    if not key or not due or phase in ("loading", "downloading", "working", "checking"):
        return
    if not SWITCH.acquire(blocking=False):
        return
    try:
        _set(checked=time.time())
        try:
            rec = _find(_models(), key)
        except NoServer as e:
            _set(phase="no server", detail=str(e) + ". Start LM Studio's "
                 "server and the room will find it.")
            return
        if rec is None:
            _set(phase="not downloaded", size_bytes=None, instance_id=None,
                 detail="not on this disk yet")
            return
        have = _instances(rec)
        _set(size_bytes=rec.get("size_bytes"))
        if have:
            if phase != "loaded":
                _set(phase="loaded", instance_id=have[0], detail="in memory")
        elif phase in ("no server", "not downloaded"):
            threading.Thread(target=_switch, args=(None, key), daemon=True).start()
        elif phase != "on disk":
            _set(phase="on disk", instance_id=None,
                 detail="on disk but not in memory -- it loads on "
                        + home.NAME + "'s next turn, or now with the button")
    finally:
        SWITCH.release()


def start():
    """At the room's start: if a model is chosen, bring it up."""
    cfg = settings()
    _set(model=cfg["model"], phase="off" if cfg["model"] is None else "checking")
    config()    # so a broken prompt file is on the screen from the start
    if cfg["model"]:
        threading.Thread(target=_switch, args=(None, cfg["model"]),
                         daemon=True).start()


def warm_embedder():
    """The embedder's first load is some seconds; it is warmed when a model is
    chosen so the first run of a session does not spend its deadline on it."""
    with LOCK:
        if STATE["embedder"] in ("warming", "warm"):
            return
        STATE["embedder"] = "warming"

    def go():
        try:
            embed.backend()
            _set(embedder="warm")
        except Exception:
            _set(embedder="unavailable")
    threading.Thread(target=go, daemon=True).start()


# -- the lines of the room ---------------------------------------------------

WHO = {"user": home.OWNER_NAME, home.SELF: home.NAME, "angel": "angel " + home.NAME}

# The kinds of row read as lines of the room.
READ_KINDS = ("user", home.SELF, "angel", "worker", "tell")
_READ_MARKS = ",".join("?" for _ in READ_KINDS)
# How far back the room filter may look before settling for what it found.
ROOM_NET = 80


def _meta_of(r) -> dict:
    meta = r["meta"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    return meta or {}


def _local(dt) -> str:
    """A stored UTC time as the local "2026-09-24 15:17"."""
    try:
        return datetime.fromisoformat(str(dt)).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(dt or "")


def line_of(r) -> dict:
    """One row as the small model reads it: when, who, and the words."""
    meta = _meta_of(r)
    if r["kind"] == "worker":
        who = "a hand" + ((" (" + meta["name"] + ")") if meta.get("name") else "")
    elif r["kind"] == "tell":
        who = home.NAME + ", to a hand"
    elif r["kind"] == "user" and meta.get("who"):
        who = people.CALLED.get(meta["who"], meta["who"])
    else:
        who = WHO.get(r["kind"], r["kind"])
    return {"id": r["id"], "when": _local(r["dt"]), "who": who,
            "text": pictures.with_label(r["text"] or "", pictures.of_row(meta))}


def last_lines(conn, n: int) -> list:
    """The last few lines of the room that is speaking, oldest first. The
    newest person line says whose room; only that room's lines are read, so
    one person's question is never searched off another person's talk."""
    rows = conn.execute(
        "SELECT id, dt, kind, text, meta FROM rows"
        " WHERE kind IN (" + _READ_MARKS + ")"
        " ORDER BY id DESC LIMIT ?",
        (*READ_KINDS, max(int(n), ROOM_NET))).fetchall()
    room = home.OWNER
    for r in rows:
        if r["kind"] == "user":
            room = _meta_of(r).get("who") or home.OWNER
            break
    out = []
    for r in rows:
        if r["kind"] in ("user", home.SELF):
            if room not in db.rooms_of(r["kind"], _meta_of(r)):
                continue
        elif room != home.OWNER:
            continue
        out.append(line_of(r))
        if len(out) >= int(n):
            break
    out.reverse()
    return out


def _cut(text: str, chars: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= chars:
        return text
    head = chars // 3
    return text[:head] + " [...] " + text[-(chars - head):]


def lines_text(lines) -> str:
    return "\n".join("[" + ln["when"] + "] " + ln["who"] + ": "
                     + _cut(ln["text"], LINE_CHARS) for ln in lines)


# -- the shelf ---------------------------------------------------------------

def name_of(row) -> str:
    """What an essence is called on screen: its title, or its first words."""
    if row.get("title"):
        return str(row["title"])
    text = " ".join((row.get("text") or "").split())
    if len(text) <= 90:
        return text
    cut = text[:90]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut) + " …"


def style_examples(shelf: list) -> list:
    """A few real essences for the writer to see the style in: the newest,
    one from the middle, one of the oldest -- spread out so their subjects
    differ and the writer copies the voice, not a topic."""
    if not shelf:
        return []
    by_date = sorted(shelf, key=lambda r: r["dt"])
    n = len(by_date)
    picks = sorted({n - 1, n // 2, 0})[-STYLE_EXAMPLES:]
    return [_cut(by_date[i]["text"], STYLE_CHARS) for i in reversed(picks)]


def _in_range(shelf, lo, hi):
    return [r for r in shelf
            if (lo is None or r["dt"] >= lo) and (hi is None or r["dt"] < hi)]


def ranked(conn, asked: dict) -> dict:
    """Every essence in the asked range, ranked by likeness to the asked
    essence -- no bar -- each with the keywords it contains. Also the
    keyword finds, ranked by how many keywords they hold, then likeness."""
    shelf = db.essence_shelf(conn)
    notes = []
    lo, lo_bad = db.dt_bound(asked.get("from"), end=False)
    hi, hi_bad = db.dt_bound(asked.get("to"), end=True)
    if lo_bad or hi_bad:
        notes.append("the date range could not be read, so the whole shelf was searched")
        lo = hi = None
    within = _in_range(shelf, lo, hi)
    if (lo or hi) and not within:
        notes.append("no essences in that date range, so the whole shelf was searched")
        within = shelf
    vectors = db.vectors_for(conn, embed.MODEL)
    probe = embed.read([asked["essence"]])[0]["vec"] if asked.get("essence") else None
    keywords = [k for k in asked.get("keywords") or [] if k.strip()]
    rows = []
    for r in within:
        v = vectors.get(r["id"])
        fresh = v is not None and v["text_hash"] == db.text_hash(r["text"])
        score = (sum(x * y for x, y in zip(probe, v["vec"]))
                 if (probe is not None and fresh) else None)
        hay = ((r["text"] or "") + " " + (r["title"] or "")).casefold()
        rows.append({"id": r["id"], "title": r["title"], "text": r["text"],
                     "dt": r["dt"], "score": score,
                     "keywords_in_it": [k for k in keywords if k.casefold() in hay]})
    unscored = sum(1 for r in rows if r["score"] is None)
    if unscored and probe is not None:
        notes.append(str(unscored) + " essence" + (" has" if unscored == 1 else "s have")
                     + " no fresh vector and could not be scored")
    by_meaning = sorted((r for r in rows if r["score"] is not None),
                        key=lambda r: -r["score"])
    counts = {k: sum(1 for r in rows if k in r["keywords_in_it"]) for k in keywords}
    common = {k for k, n in counts.items() if n > COMMON * max(1, len(rows))}
    useful = lambda r: [k for k in r["keywords_in_it"] if k not in common]
    by_keyword = sorted((r for r in rows if useful(r)),
                        key=lambda r: (-len(useful(r)), -(r["score"] or 0)))
    counts = {k: {"in": n, "too_common": k in common} for k, n in counts.items()}
    return {"searched": len(within), "by_meaning": by_meaning,
            "by_keyword": by_keyword, "keyword_counts": counts, "notes": notes}


def _result(r, found_by, in_hand) -> dict:
    return {"id": r["id"], "name": name_of(r), "date": _local(r["dt"]),
            "score": None if r["score"] is None else round(r["score"], 3),
            "found_by": found_by, "keywords_in_it": r["keywords_in_it"],
            "in_hand": r["id"] in in_hand}


def _in_hand(conn, ids) -> set:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {r["id"] for r in conn.execute(
        "SELECT id FROM rows WHERE loaded = 1 AND id IN (" + marks + ")", list(ids))}


def search(conn, asked: dict, cfg: dict) -> dict:
    """The top `top` by likeness, then up to `keyword_top` keyword finds that
    are not already among them. Returns the results plus the texts the
    reviewer will read."""
    got = ranked(conn, asked)
    picked = [(r, "likeness") for r in got["by_meaning"][:cfg["top"]]]
    have = {r["id"] for r, _ in picked}
    extra = [r for r in got["by_keyword"] if r["id"] not in have][:cfg["keyword_top"]]
    picked += [(r, "keyword") for r in extra]
    hand = _in_hand(conn, [r["id"] for r, _ in picked])
    return {"searched": got["searched"], "notes": got["notes"],
            "keyword_counts": got["keyword_counts"],
            "results": [_result(r, how, hand) for r, how in picked],
            "texts": {r["id"]: r["text"] or "" for r, _ in picked}}


def more(conn, asked: dict, skip: int, n: int = MORE) -> dict:
    """"Show more": the same search by likeness, the next `n` after `skip`,
    without the review. Read-only."""
    got = ranked(conn, asked)
    page = got["by_meaning"][skip:skip + n]
    hand = _in_hand(conn, [r["id"] for r in page])
    return {"searched": got["searched"], "skip": skip,
            "total": len(got["by_meaning"]), "notes": got["notes"],
            "results": [_result(r, "likeness", hand) for r in page]}


# -- the model's two calls ---------------------------------------------------

WRITE_SHAPE = {
    "type": "object",
    "properties": {
        "essence": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "from": {"type": "string"},
        "to": {"type": "string"},
    },
    "required": ["essence", "keywords", "from", "to"],
    "additionalProperties": False,
}

REVIEW_SHAPE = {
    "type": "object",
    "properties": {
        "results": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "integer"},
                           "note": {"type": "string"},
                           "want": {"type": "integer"}},
            "required": ["id", "note", "want"],
            "additionalProperties": False}},
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _complete(key: str, messages: list, schema: dict, name: str,
              max_out: int = MAX_OUT) -> dict:
    body = {"model": key, "messages": messages, "temperature": 0.1,
            "max_tokens": max_out, "stream": False,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "strict": True,
                                                "schema": schema}}}
    out = _lm("/v1/chat/completions", body, timeout=KNOBS["timeout_s"]["max"] + 5)
    choice = (out.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if choice.get("finish_reason") == "length":
            raise LMError("the model ran out of tokens before it finished its answer: "
                          + text[:200])
        raise LMError("the model's answer was not the JSON asked for: " + text[:200])


def write_messages(told: dict, examples: list, lines: list) -> list:
    today = datetime.now().astimezone().strftime("%Y-%m-%d, %A")
    user = ("Some of her essences, only for the style:\n\n"
            + "\n\n".join("---\n" + e for e in examples)
            + "\n---\n\nToday is " + today + ".\n\nThe last lines of the room, oldest first:\n"
            + lines_text(lines)
            + "\n\nWrite the search for the newest line.")
    return [{"role": "system", "content": told["WRITER"]},
            {"role": "user", "content": user}]


# All the results the reviewer reads together, in characters; each gets
# `read_chars` or its share of this, whichever is smaller.
REVIEW_BUDGET = 16000


def review_messages(told: dict, lines: list, results: list, texts: dict, chars: int,
                    looking_for: str = None) -> list:
    """The reviewer's call. Against the room's last lines, or, from the
    Menu's search box, against what was typed there."""
    chars = min(chars, REVIEW_BUDGET // max(1, len(results)))
    parts = []
    for r in results:
        parts.append("id " + str(r["id"]) + " (" + r["date"] + "):\n"
                     + _cut(texts.get(r["id"], ""), chars))
    if looking_for:
        context = ("Instead of the room, judge against what " + home.OWNER_NAME
                   + " is looking for:\n" + looking_for)
    else:
        context = "The last lines of the room, oldest first:\n" + lines_text(lines)
    user = (context
            + "\n\nThe essences the search brought back:\n\n"
            + "\n\n".join(parts)
            + "\n\nWrite a note and a want for each id.")
    return [{"role": "system", "content": told["REVIEWER"]},
            {"role": "user", "content": user}]


# -- one run -----------------------------------------------------------------

HOW = ("A small local model's suggestions, not memories: it wrote the search "
       "under `asked`, and each result carries its likeness score and the "
       "model's note on it. Read one by id if it looks worth it; "
       "`search` with `asked.essence` as the restatement and a higher limit "
       "shows the ones further down.")


def _review(key, told, lines, found, cfg, block, sent, looking_for=None):
    """The reviewer's note and want on each result, written into `block`."""
    sent["review"] = review_messages(told, lines, found["results"],
                                     found["texts"], cfg["read_chars"], looking_for)
    t = time.time()
    try:
        reviewed = _complete(key, sent["review"], REVIEW_SHAPE, "review",
                             max_out=max(MAX_OUT, 160 * len(found["results"])))
        block["review_s"] = round(time.time() - t, 2)
        said = {}
        for x in reviewed.get("results") or []:
            if isinstance(x, dict) and "id" in x:
                said[int(x["id"])] = x
        for r in block["results"]:
            x = said.get(r["id"])
            if x is None:
                r["want"], r["note"] = None, "(the reviewer said nothing on this one)"
            else:
                r["want"] = max(0, min(100, int(x.get("want") or 0)))
                r["note"] = " ".join(str(x.get("note") or "").split())
    except (NoServer, LMError, ValueError, TypeError) as e:
        block["notes"].append("the review failed: " + str(e))


def _block(key, **fields) -> dict:
    m = KEYS.get(key) or {}
    block = {"how": HOW, "model": m.get("label") or key, "read_lines": 0,
             "asked": None, "searched": None, "keyword_counts": {},
             "results": [], "notes": [],
             "took_s": None, "missed": None}
    block.update(fields)
    return block


def _run(key, lines, cfg, box):
    """One whole run, on its own thread with its own connection. Its result
    goes into `box` and STATE['last'] -- the latter even if the turn has gone
    on without it."""
    started = time.time()
    block = _block(key, read_lines=min(len(lines), cfg["lines"]))
    sent = {}
    _set(phase="working")
    conn = db.connect()
    try:
        if not lines:
            block["missed"] = "there were no lines in the room to read"
            return
        block["line_ids"] = [ln["id"] for ln in lines]
        writer_lines = lines[-cfg["lines"]:]
        review_lines = lines[-cfg["review_lines"]:]
        block["read_lines"] = len(writer_lines)
        told = config()
        examples = style_examples(db.essence_shelf(conn))
        sent["write"] = write_messages(told, examples, writer_lines)
        t = time.time()
        wrote = _complete(key, sent["write"], WRITE_SHAPE, "search")
        block["write_s"] = round(time.time() - t, 2)
        asked = {"essence": " ".join(str(wrote.get("essence") or "").split()),
                 "keywords": [str(k).strip() for k in (wrote.get("keywords") or [])
                              if str(k).strip()][:6],
                 "from": str(wrote.get("from") or "").strip() or None,
                 "to": str(wrote.get("to") or "").strip() or None}
        block["asked"] = asked
        if not asked["essence"] and not asked["keywords"]:
            block["missed"] = "the model wrote an empty search"
            return
        t = time.time()
        found = search(conn, asked, cfg)
        block["search_s"] = round(time.time() - t, 2)
        block["searched"] = found["searched"]
        block["keyword_counts"] = found["keyword_counts"]
        block["notes"] = found["notes"]
        block["results"] = found["results"]
        if found["results"]:
            block["review_lines"] = len(review_lines)
            _review(key, told, review_lines, found, cfg, block, sent)
    except NoServer as e:
        _set(phase="no server", detail=str(e))
        block["missed"] = str(e)
    except LMError as e:
        block["missed"] = "LM Studio: " + str(e)
    except embed.Unavailable as e:
        block["missed"] = "the embedder is not available: " + str(e)
    except Exception as e:   # a turn is never broken by this
        block["missed"] = type(e).__name__ + ": " + str(e)
    finally:
        conn.close()
        block["took_s"] = round(time.time() - started, 2)
        box["out"] = block
        late = box.get("late", False)
        _set(last=dict(block, late=late) if late else block, last_sent=sent)
        with LOCK:
            if STATE["phase"] == "working":
                STATE["phase"] = "loaded"


# The Menu's search box shows more than a turn does.
TRY_TOP = 10
TRY_KEYWORD_TOP = 5


def _asked_from(spec: dict) -> dict:
    """What somebody typed, in the shape the writer's answer has."""
    words = spec.get("keywords") or []
    if isinstance(words, str):
        words = words.split(",")
    return {"essence": " ".join(str(spec.get("essence") or "").split()),
            "keywords": [str(k).strip() for k in words if str(k).strip()][:6],
            "from": str(spec.get("from") or "").strip() or None,
            "to": str(spec.get("to") or "").strip() or None}


def try_search(conn, spec: dict, review: bool = False) -> dict:
    """The Menu's search box: the automatic memory's own search on what the
    owner typed, shaped like a run so it is drawn the same way. The reviewer
    reads the results against the room as it stands only when asked.
    Read-only."""
    started = time.time()
    cfg = dict(settings(), top=TRY_TOP, keyword_top=TRY_KEYWORD_TOP)
    asked = _asked_from(spec or {})
    block = _block(cfg["model"], asked=asked, typed=True)
    if not asked["essence"] and not asked["keywords"]:
        block["missed"] = "type an essence or a keyword to search with"
        return block
    try:
        t = time.time()
        found = search(conn, asked, cfg)
        block["search_s"] = round(time.time() - t, 2)
        block.update(searched=found["searched"], notes=found["notes"],
                     keyword_counts=found["keyword_counts"], results=found["results"])
        if review and found["results"]:
            if not cfg["model"]:
                block["notes"].append("no model is chosen, so nothing was reviewed")
            else:
                wanted = ["essence: " + (asked["essence"] or "(none)"),
                          "keywords: " + (", ".join(asked["keywords"]) or "(none)")]
                if asked["from"] or asked["to"]:
                    wanted.append("dates: " + (asked["from"] or "the beginning")
                                  + " to " + (asked["to"] or "now"))
                block["reviewed_against"] = "what was typed"
                _review(cfg["model"], config(), [], found, cfg, block, {},
                        looking_for="\n".join(wanted))
    except embed.Unavailable as e:
        block["missed"] = "the embedder is not available: " + str(e)
    block["took_s"] = round(time.time() - started, 2)
    return block


def _reached(report) -> set:
    """Every row a fetch report says it brought into view: brought back,
    already in hand when asked for, or an essence whose sources it reached."""
    if not report:
        return set()
    return (set(report.get("brought_back") or []) | set(report.get("already_here") or [])
            | set(report.get("through_essences") or []))


def after_turn(conn, block: dict, looked: list, kept, reply_row=None) -> dict:
    """After the assistant's turn: which of the suggestions it actually pulled
    in -- while looking before it answered, or kept after answering -- and
    which essences it pulled that were never suggested. Logged, one line per
    turn, to LOG_PATH. Returns the `after` record for the room's card; it is
    not part of what the assistant was handed."""
    read = set()
    for report in looked or []:
        read |= _reached(report)
    kept_ids = _reached(kept)
    suggested = [r["id"] for r in block.get("results") or []]
    pulled = {}
    for i in suggested:
        if i in read:
            pulled[str(i)] = "while looking"
        elif i in kept_ids:
            pulled[str(i)] = "kept after answering"
    other_ids = sorted((read | kept_ids) - set(suggested))
    others = []
    if other_ids:
        marks = ",".join("?" * len(other_ids))
        for r in conn.execute("SELECT id, title, text, dt FROM rows WHERE kind = 'essence'"
                              " AND id IN (" + marks + ")", other_ids):
            others.append({"id": r["id"], "name": name_of(dict(r)), "date": _local(r["dt"]),
                           "how": "while looking" if r["id"] in read else "kept after answering"})
    after = {"pulled": pulled, "pulled_not_suggested": others}
    if block.get("asked"):
        entry = {"at": db.now(), "reply_row": reply_row, "model": block.get("model"),
                 "line_ids": block.get("line_ids"), "asked": block.get("asked"),
                 "keyword_counts": block.get("keyword_counts"),
                 "results": [dict({k: r.get(k) for k in
                                   ("id", "score", "found_by", "keywords_in_it",
                                    "in_hand", "want", "note")},
                                  pulled=pulled.get(str(r["id"])))
                             for r in block.get("results") or []],
                 "pulled_not_suggested": [{"id": o["id"], "how": o["how"]} for o in others],
                 "took_s": block.get("took_s"), "missed": block.get("missed")}
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            after["log_error"] = str(e)
    return after


def summary(block: dict) -> str:
    """One line for the room's progress and the event."""
    if not block:
        return "the automatic memory did not run"
    asked = block.get("asked") or {}
    if block.get("missed") and not asked:
        return "missed: " + block["missed"]
    line = 'searched for "' + _cut(asked.get("essence") or "", 80) + '"'
    res = block.get("results") or []
    if res:
        line += " · " + ", ".join(
            "#" + str(r["id"])
            + ("" if r.get("want") is None else " " + str(r["want"]) + "%")
            for r in res)
    else:
        line += " · nothing found"
    if block.get("took_s") is not None:
        line += " · " + format(block["took_s"], ".1f") + " s"
    if block.get("missed"):
        line += " · " + block["missed"]
    after = block.get("after")
    if after is not None:
        got = list(after.get("pulled") or {})
        line += (" · " + home.NAME + " pulled " + ", ".join("#" + i for i in got)
                 if got else " · " + home.NAME + " pulled none of them")
    return line


def before_she_speaks(conn, say=None):
    """Called once per turn, before the assistant's first call. Returns the
    block for its prompt, or None when no model is chosen."""
    cfg = settings()
    key = cfg["model"]
    if not key:
        return None
    say = say or (lambda *a, **k: None)
    lines = last_lines(conn, max(cfg["lines"], cfg["review_lines"]))
    timeout = cfg["timeout_s"]
    box = {}
    worker = threading.Thread(target=_run, args=(key, lines, cfg, box), daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        box["late"] = True
        block = _block(key, read_lines=len(lines), took_s=timeout,
                       missed="it ran out of time at " + format(timeout, ".0f")
                              + " s, so the turn went ahead without it")
    else:
        block = box.get("out") or _block(key, read_lines=len(lines),
                                         missed="it came back with nothing")
    say("automatic memory: " + summary(block), "recall", block)
    return block


def try_now(conn) -> dict:
    """The button under Developer: one run on the room as it stands, shown
    and not kept."""
    if not settings()["model"]:
        return {"error": "no model is chosen"}
    return before_she_speaks(conn)


def status() -> dict:
    """What the header and the Developer section show, as the room last found
    it. This does not ask LM Studio: the manager asks every room for it every
    two seconds, day and night, and LM Studio's log filled with the model
    list. A page that is open calls `refresh` first."""
    cfg = settings()
    with LOCK:
        st = dict(STATE)
    m = KEYS.get(st["model"]) or {}
    try:
        config_text = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        config_text = "(could not read: " + str(exc) + ")"
    return {
        "model": st["model"], "label": m.get("label"), "about_gb": m.get("about_gb"),
        "phase": st["phase"], "detail": st["detail"],
        "size_bytes": st["size_bytes"], "instance_id": st["instance_id"],
        "download": st["download"],
        "loading_for_s": (round(time.time() - st["loading_since"], 1)
                          if st["loading_since"] else None),
        "embedder": st["embedder"],
        "knobs": {n: cfg[n] for n in KNOBS},
        "knob_bounds": {n: {"min": k["min"], "max": k["max"], "default": k["default"]}
                        for n, k in KNOBS.items()},
        "last": st["last"], "last_sent": st["last_sent"],
        "models": MODELS, "server": LM,
        "config_path": str(CONFIG_PATH), "config_text": config_text,
        "config_error": st["config_error"],
    }


if __name__ == "__main__":
    # python -m server.recall [model-key]: one run on the room as it stands.
    import sys
    conn = db.connect()
    try:
        cfg = settings()
        key = sys.argv[1] if len(sys.argv) > 1 else cfg["model"]
        if not key:
            print("no model chosen; pass a key:", ", ".join(m["key"] for m in MODELS))
            raise SystemExit(1)
        lines = last_lines(conn, max(cfg["lines"], cfg["review_lines"]))
        print(lines_text(lines))
        _set(model=key)
        _switch(None, key)
        box = {}
        _run(key, lines, cfg, box)
        print(json.dumps(box["out"], ensure_ascii=False, indent=2))
    finally:
        conn.close()
