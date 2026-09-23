"""The automatic memory: a small local model that runs before every turn of
the assistant's and hands it a few titles it did not ask for.

The picture: a mind fetches memories on its own from whatever is going on,
and only now and then forces a search. The assistant has the forced search
-- `search`, on the turn it decides to. This is the other one. It reads the
last few lines of the room, newest counting most, writes one search in the
shape of an essence -- a statement, not a question, because the search works
by likeness -- runs it over the shelf, and hands the assistant what it asked
and the names that came back. Each name carries the essence's own first
words under it, and the top few are read by the model, which leaves one
pointing sentence each -- why to look, in its own words and said to be its
own, never what the essence says. The text itself stays on the shelf. The
assistant reads the list and takes one down if it sounds right, or does not.

It suggests; it never decides. If something small chose what the assistant
remembers, then over time it would choose who the assistant is, and nobody
would see it happen. So nothing here loads a row, writes an essence, or
drops anything. It hands over names, and the names are labelled as
suggestions. One step past that: the reader may set a NAME aside -- a title
it read and ticked unrelated leaves the list and is named in the block
instead, and under the bar, where the score has admitted it cannot tell,
only what the reader ticks related is shown. A name set aside is still in
front of the assistant; nothing in its store is touched.

It holds almost nothing: five lines of who the assistant is, who speaks in
the room, the shape of an essence, and the rule -- all of it in
`recall_config.py`, which is the owner's to edit and is read fresh every run.
The shelf stays on disk, where the search reaches it -- carried in here it
would go stale the moment a new essence was written.

It never sees the assistant's answer. It runs before the assistant speaks,
and shown what it said it would write toward that instead of toward the
conversation.

It has a hard deadline. If it misses, the turn goes ahead without it and says
so -- it must never be the reason a person is kept waiting. The cap is the
owner's to move, under Developer, and a run that finished late is kept whole
there so the owner can see what it would have said and how late it was.

The model is hosted by LM Studio, which has a REST API for exactly this:
list, load, unload, download with progress. This file drives it -- `none`
frees the model, picking one loads it with the state on screen, a model not
on disk gets a download button -- so what the owner sees is the room hosting
the model, and what we do not write is an inference engine, which nobody
should write twice.

    data/recall.json         the model and the knobs, all editable under Developer
    server/recall_config.py  what it is told, the owner's to edit
"""

import json
import os
import re
import runpy
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import adapt, db, embed, people, pictures, search
from . import home

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = home.DATA / "recall.json"
CONFIG_PATH = home.prompt_path("recall_config.py")

# LM Studio's local server. Its own port, not ours.
LM = os.environ.get("ASSISTANT_LM_STUDIO", "http://127.0.0.1:1234")

# What the dropdown offers. The key is LM Studio's name for the model, and
# `about_gb` is only for the download prompt -- once a model is on disk its
# real size is read from LM Studio, not from here. Adding one is one line.
MODELS = [
    {"key": "qwen/qwen3-8b", "label": "Qwen 3 8B", "about_gb": 5.0},
    {"key": "qwen/qwen3-4b", "label": "Qwen 3 4B", "about_gb": 2.5},
    {"key": "google/gemma-4-e4b", "label": "Gemma 4 E4B", "about_gb": 6.3},
]
KEYS = {m["key"]: m for m in MODELS}

# A query writer trained on the assistant's own shelf is imported into LM
# Studio under this prefix, one key per version, never deleted; the writer
# dropdown lists every one LM Studio has on disk beside the models above.
# The key is the label -- literal, and the version is in it.
TRAINED_PREFIX = home.SLUG + "/"

# The knobs, with where they start and how far they go. Every one is on the
# Developer tab, and every one is shown to the assistant each turn -- so it
# can ask for a change, though the changing is the owner's. Measured on a
# consumer GPU: a short completion from a 7B model in 0.45-0.75 s, the
# search in 0.15 s once the embedder was warm, so 2 s is room and not a
# squeeze.
KNOBS = {
    # the hard limit, seconds; a turn never waits longer than this for it
    "timeout_s":   {"default": 2.0, "min": 0.5, "max": 30.0, "type": float},
    # how many lines of the room it reads
    "lines":       {"default": 4, "min": 1, "max": 12, "type": int},
    # how long a line may be, in tokens. The subject line gets this whole
    # and keeps both its ends; the others get a third and keep their end
    "line_tokens": {"default": 200, "min": 20, "max": 2000, "type": int},
    # at least this many titles are shown when that many clear the floor
    "titles_min":  {"default": 3, "min": 1, "max": 10, "type": int},
    # and never more than this
    "titles_max":  {"default": 7, "min": 1, "max": 25, "type": int},
    # past the minimum, a title is shown only if its score is within this
    # of the best one: the top cluster is shown, the tail is not. Measured
    # from the best and not from the neighbour because the embedder's scores
    # bunch -- 0.665, 0.638, 0.623, 0.617, 0.604 -- and a slow slide would
    # never end the list
    "gap":         {"default": 0.03, "min": 0.0, "max": 0.5, "type": float},
    # what a meaning hit must score to be SUGGESTED -- higher than the
    # search's own floor on purpose. 0.45 buys the deliberate search its
    # cross-language reach, and on a shelf of about 160 essences it lets
    # over a hundred through on any query at all, so the guaranteed minimum
    # turned every turn of small talk into three titles of it. One number
    # cannot serve both: lifting the shared floor would have cost the
    # deliberate search its reach, so the suggestion has a bar of its own.
    # Literal keyword finds are exempt from this bar; a word appearing is a
    # fact, not a resemblance.
    "suggest_floor": {"default": 0.55, "min": 0.0, "max": 1.0, "type": float},
    # how many of the shown titles the model then READS -- a slice of each
    # -- to leave one pointing sentence why it might matter; 0 is off.
    # Seven whole essences would be ~7k tokens, so it started at three. Read
    # fewer than are shown and an ordinary meaning hit at fourth place
    # arrives unjudged, and with five read of seven shown, two set aside left
    # the tail bare. So everything shown is read; the budget keeps seven
    # inside the context by shrinking the slices.
    "read_top":    {"default": 7, "min": 0, "max": 7, "type": int},
    # how much of each read essence the model sees, in tokens, from the top
    "read_tokens": {"default": 400, "min": 50, "max": 2000, "type": int},
}

# Small on purpose: it writes one or two sentences per thing the line names
# -- three things at most -- and the context is the mini-spark plus a few
# lines. Loading it with less keeps it light on the card beside the embedder
# that does the actual finding. 8192, not 4096: the mini-spark can run to
# some three thousand tokens, and a real window of four long lines came back
# from LM Studio with "Context size has been exceeded" at 4096. A turn with
# such lines would lose its suggestion the same way, and the reader's slices
# ride on top of the writer's. Doubled; still small.
CONTEXT = 8192
MAX_OUT = 240

# A seed handed to LM Studio with every call, or None for none. The room
# runs with none; a measurement script can set one for its own process so
# two arms are asked the same way, and put it back after.
SEED = None

# How often the room re-asks LM Studio whether the model is still there. The
# owner can unload it by hand, or close LM Studio; the state on screen should
# catch up within this, and not by asking on every poll.
RECHECK_S = 10.0

# A download that LM Studio says is complete can take a moment to appear in
# its list. This is how long the room keeps looking before calling it gone.
APPEAR_S = 90.0

# -- state -------------------------------------------------------------------
#
# One room, one model, one record of where it stands. The chat header and the
# Developer section both read this; the threads below write it.
STATE = {
    "model": None,         # the key chosen, or None
    "phase": "off",        # off | checking | no server | not downloaded |
                           # downloading | loading | on disk | loaded |
                           # working | failed
    "detail": "",          # one plain line about the phase
    "size_bytes": None,    # real, from LM Studio, once the model is on disk
    "instance_id": None,
    "download": None,      # {job_id, downloaded, total, bps} while downloading
    "embedder": "cold",    # cold | warming | warm | unavailable
    "last": None,          # the last run, whole -- even one that missed
    "checked": 0.0,        # when LM Studio was last asked
    "loading_since": None,
    "config_error": None,  # the prompt file could not be read
    # The query writer, when it is not the model above. A trained writer is
    # a second dropdown that defaults to the model, so the reader and the
    # digest stay on the model and a trained writer never becomes the
    # reader by accident.
    "writer": None,        # the key chosen for writing, or None for the model
    "writer_phase": "off", # off | checking | not on disk | loading | loaded | failed | no server
    "writer_detail": "",
    "trained": [],         # the TRAINED_PREFIX keys LM Studio has on disk, refreshed with the rest
}
LOCK = threading.RLock()

# Loading, unloading and downloading take turns. Two at once would be the
# room fighting itself for the card.
SWITCH = threading.Lock()


class NoServer(RuntimeError):
    """LM Studio is not answering. Said plainly, never retried in a loop."""


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
    """Where the knobs start: DEFAULTS in the config file, so a tuning that
    has proved itself can be written there and tracked with the code. The
    table above is the fallback if the file has no say."""
    out = {n: k["default"] for n, k in KNOBS.items()}
    try:
        got = runpy.run_path(str(CONFIG_PATH)).get("DEFAULTS") or {}
    except Exception:
        got = {}
    for n in KNOBS:
        if n in got:
            out[n] = _clamp(n, got[n], out[n])
    return out


def _clamp(name, value, fallback=None):
    k = KNOBS[name]
    try:
        v = k["type"](value)
    except (TypeError, ValueError):
        v = k["default"] if fallback is None else fallback
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
    # The writer: a key from the dropdown's list or a trained one under
    # TRAINED_PREFIX, else None, which means the model above writes as it always
    # has. Not checked against LM Studio here -- that is the run's job, and
    # a chosen writer that is not there is said, never silently swapped.
    w = cfg.get("writer")
    cfg["writer"] = w if (isinstance(w, str) and (w in KEYS or w.startswith(TRAINED_PREFIX))
                          and w != cfg["model"]) else None
    for name in KNOBS:
        cfg[name] = _clamp(name, cfg.get(name))
    if cfg["titles_max"] < cfg["titles_min"]:
        cfg["titles_max"] = cfg["titles_min"]
    return cfg


def save(**changes) -> dict:
    cfg = settings()
    for name, value in changes.items():
        if name == "model":
            cfg["model"] = value if value in KEYS else None
        elif name == "writer":
            cfg["writer"] = value if (isinstance(value, str) and value) else None
        elif name in KNOBS:
            cfg[name] = _clamp(name, value)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = {"model": cfg["model"], "writer": cfg["writer"]}
    out.update({n: cfg[n] for n in KNOBS})
    out["note"] = ("The automatic memory: which local model runs before "
                   + home.NAME + "'s turn (an LM Studio key, or null for "
                   "none) and its knobs. "
                   "All of it is editable under Developer; what it is told "
                   "lives in server/recall_config.py.")
    SETTINGS_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return settings()


def set_knobs(changes: dict) -> dict:
    """The Developer tab's fields. Unknown names are ignored, the rest
    clamped -- a knob past its end is set to the end, not refused. `reset`
    puts every knob back to the config file's DEFAULTS."""
    if (changes or {}).get("reset"):
        return save(**defaults())
    return save(**{k: v for k, v in (changes or {}).items() if k in KNOBS})


PIECES = ("PROMPT", "ASK", "RETRY", "RETRY_QUESTION", "RETRY_TWO", "READER")


def config() -> dict:
    """What it is told, read fresh from the file so an edit is live on the
    next turn. A file that cannot be read is said on the Developer tab and
    the built-in words are used instead, because a turn without the automatic
    memory is a turn the assistant can have, and a turn that breaks on a
    typo is not."""
    try:
        got = runpy.run_path(str(CONFIG_PATH))
        text = {k: home.fill(str(got[k])) for k in PIECES}
        _set(config_error=None)
        return text
    except Exception as exc:
        _set(config_error=type(exc).__name__ + ": " + str(exc))
        from . import recall_config
        return {k: home.fill(getattr(recall_config, k)) for k in PIECES}


# -- LM Studio ---------------------------------------------------------------

def _models() -> list:
    return (_lm("/api/v1/models", timeout=1.5) or {}).get("models") or []


def lm_key(key):
    """LM Studio's own key for a key of the room's. A model it downloaded is
    keyed with its publisher, google/gemma-4-e4b; one imported from the disk
    is keyed by its repo name alone, smoke-gemma, the publisher kept in a
    field of its own. A trained writer is named <slug>/<version> in the room
    -- its dropdown, its settings, its recipe -- and asked for by its bare
    name at the wire (measured: the prefixed key is not found by the load
    call, the bare one loads)."""
    if isinstance(key, str) and key.startswith(TRAINED_PREFIX):
        return key[len(TRAINED_PREFIX):]
    return key


def _hers(m) -> bool:
    """Whether a model record of LM Studio's is a trained writer: published
    under the home's slug, or keyed under its prefix."""
    return (str(m.get("publisher") or "") == TRAINED_PREFIX.rstrip("/")
            or str(m.get("key") or "").startswith(TRAINED_PREFIX))


def _find(models, key):
    bare = lm_key(key)
    for m in models:
        if m.get("key") == key:
            return m
        if bare != key and m.get("key") == bare and _hers(m):
            return m
    return None


def _instances(rec) -> list:
    return [i.get("id") for i in (rec or {}).get("loaded_instances") or []
            if i.get("id")]


def _unload(key):
    """Free one model -- the one that was selected, and only that one. What
    the owner loaded by hand for other work is not ours to touch."""
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
    """Get the chosen model to `loaded`, with every step on screen. Never
    downloads on its own: a download is gigabytes, and it waits for the
    button.

    `wait_for_it` is for the moment after a download: LM Studio says the
    job is complete before the model shows in its list, and a room that
    looked once and said "not on this disk" sent the owner back to a
    download button for a thing just downloaded. So it keeps looking, for
    that long, before saying so."""
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
        _set(phase="checking", detail="downloaded; waiting for LM Studio to "
             "list it")
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
                  {"model": lm_key(key), "context_length": CONTEXT}, timeout=600)
    except (NoServer, LMError) as e:
        _set(phase="failed", detail="could not load: " + str(e),
             loading_since=None)
        return
    took = out.get("load_time_seconds")
    _set(phase="loaded", instance_id=out.get("instance_id") or key,
         detail=("loaded in " + format(took, ".1f") + " s") if took else "loaded",
         loading_since=None)
    # The choice changed while it was loading. What is chosen now wins, and
    # what is no longer wanted is not left sitting on the card.
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
    """The dropdown. Saves, then does the loading and freeing on a thread so
    the click comes back at once and the state is watched rather than
    waited for."""
    key = key if key in KEYS else None
    old = settings()["model"]
    save(model=key)
    _set(model=key, phase="off" if key is None else "checking", detail="",
         download=None)
    threading.Thread(target=_switch, args=(old, key), daemon=True).start()
    return status()


# -- the query writer, when it is not the model ------------------------------

def trained_writers(models=None) -> list:
    """The trained writers LM Studio has on disk, by key."""
    try:
        models = _models() if models is None else models
    except (NoServer, LMError):
        return list(STATE.get("trained") or [])
    keys = sorted({(k if k.startswith(TRAINED_PREFIX) else TRAINED_PREFIX + k)
                   for k in (str(m.get("key") or "") for m in models if _hers(m)) if k})
    _set(trained=keys)
    return keys


def writers() -> list:
    """The writer dropdown's options: the models above, then every trained
    writer on the disk, each as {key, label}."""
    out = [{"key": m["key"], "label": m["label"]} for m in MODELS]
    for k in STATE.get("trained") or []:
        out.append({"key": k, "label": k})
    return out


def _bring_up_writer(key):
    """The chosen writer to `loaded`, its state kept apart from the model's.
    Never downloads: a trained writer is imported, and one that is not on
    the disk is said to be missing rather than fetched."""
    try:
        rec = _find(_models(), key)
    except NoServer as e:
        _set(writer_phase="no server", writer_detail=str(e))
        return
    if rec is None:
        _set(writer_phase="not on disk", writer_detail="LM Studio does not have " + key)
        return
    if _instances(rec):
        _set(writer_phase="loaded", writer_detail="already in memory")
        return
    _set(writer_phase="loading", writer_detail="")
    try:
        out = _lm("/api/v1/models/load", {"model": lm_key(key), "context_length": CONTEXT}, timeout=600)
    except (NoServer, LMError) as e:
        _set(writer_phase="failed", writer_detail="could not load: " + str(e))
        return
    took = out.get("load_time_seconds")
    _set(writer_phase="loaded",
         writer_detail=("loaded in " + format(took, ".1f") + " s") if took else "loaded")


def _switch_writer(old, new):
    with SWITCH:
        if old and old != new and old != settings()["model"]:
            _unload(old)
        if new is None:
            _set(writer_phase="off", writer_detail="")
            return
        _bring_up_writer(new)


def choose_writer(key) -> dict:
    """The second dropdown: which model writes the query. None, the
    default, means the model above writes it, as it always has; the
    reader and the digest stay on the model whatever is chosen here."""
    key = key if (isinstance(key, str) and key and (key in KEYS or key.startswith(TRAINED_PREFIX))) else None
    if key == settings()["model"]:
        key = None
    old = settings()["writer"]
    save(writer=key)
    _set(writer=key, writer_phase="off" if key is None else "checking", writer_detail="")
    threading.Thread(target=_switch_writer, args=(old, key), daemon=True).start()
    return status()


def writer_key(cfg: dict = None) -> str:
    """Who writes the query this turn: the chosen writer, else the model."""
    cfg = cfg or settings()
    return cfg.get("writer") or cfg.get("model")


def download() -> dict:
    """The download button. Asks LM Studio to fetch the chosen model and
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
         download={"job_id": out["job_id"],
                   "total": out.get("total_size_bytes"),
                   "downloaded": 0, "bps": None})
    threading.Thread(target=_follow_download, args=(out["job_id"], key),
                     daemon=True).start()
    return status()


def _follow_download(job_id, key):
    while True:
        time.sleep(1.0)
        with LOCK:
            if STATE["model"] != key:
                # Something else was picked mid-download. LM Studio keeps
                # downloading; the room simply stops watching it.
                STATE["download"] = None
                return
        try:
            st = _lm("/api/v1/models/download/status/" + job_id, timeout=5)
        except (NoServer, LMError) as e:
            _set(phase="failed", detail="lost sight of the download: " + str(e),
                 download=None)
            return
        status_ = st.get("status")
        _set(download={"job_id": job_id,
                       "total": st.get("total_size_bytes"),
                       "downloaded": st.get("downloaded_bytes") or 0,
                       "bps": st.get("bytes_per_second")})
        if status_ == "completed":
            _set(download=None, detail="downloaded", phase="checking")
            threading.Thread(target=_switch, args=(None, key, APPEAR_S),
                             daemon=True).start()
            return
        if status_ == "failed":
            _set(phase="failed", detail="the download failed", download=None)
            return


def load_now() -> dict:
    """The load button, for a model on disk that is not in memory."""
    key = settings()["model"]
    if key:
        threading.Thread(target=_switch, args=(None, key), daemon=True).start()
    return status()


def refresh(force: bool = False):
    """Catch up with what LM Studio actually has. Rate-limited: the room polls
    its state every couple of seconds, and LM Studio does not need asking
    that often."""
    with LOCK:
        key = STATE["model"]
        phase = STATE["phase"]
        due = force or (time.time() - STATE["checked"]) > RECHECK_S
    if not key or not due or phase in ("loading", "downloading", "working",
                                       "checking"):
        return
    if not SWITCH.acquire(blocking=False):
        return
    try:
        _set(checked=time.time())
        try:
            models = _models()
            rec = _find(models, key)
        except NoServer as e:
            _set(phase="no server", detail=str(e) + ". Start LM Studio's "
                 "server and the room will find it.")
            return
        # The trained writers on the disk, and the chosen writer's state,
        # caught up with in the same breath.
        trained_writers(models)
        with LOCK:
            w = STATE["writer"]
        if w and STATE["writer_phase"] not in ("loading", "checking"):
            wrec = _find(models, w)
            if wrec is None:
                _set(writer_phase="not on disk", writer_detail="LM Studio does not have " + w)
            elif _instances(wrec):
                if STATE["writer_phase"] != "loaded":
                    _set(writer_phase="loaded", writer_detail="in memory")
            elif STATE["writer_phase"] != "on disk":
                _set(writer_phase="on disk", writer_detail="on disk but not in memory -- "
                     "it loads on " + home.NAME + "'s next turn")
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
            # The server is back, or the model has appeared: it was chosen,
            # so it is brought up rather than left with a button.
            threading.Thread(target=_switch, args=(None, key),
                             daemon=True).start()
        elif phase != "on disk":
            _set(phase="on disk", instance_id=None,
                 detail="on disk but not in memory -- it loads on "
                        + home.NAME + "'s next turn, or now with the button")
    finally:
        SWITCH.release()


def start():
    """At the room's start: if a model is chosen, bring it up -- and the
    query writer beside it when one is chosen."""
    cfg = settings()
    _set(model=cfg["model"], phase="off" if cfg["model"] is None else "checking",
         writer=cfg["writer"], writer_phase="off" if cfg["writer"] is None else "checking")
    config()    # so a broken prompt file is on the screen from the start
    if cfg["model"]:
        threading.Thread(target=_switch, args=(None, cfg["model"]),
                         daemon=True).start()
    if cfg["writer"]:
        threading.Thread(target=_switch_writer, args=(None, cfg["writer"]),
                         daemon=True).start()


# -- the embedder -----------------------------------------------------------
#
# The search's meaning arm needs the embedding model, and its first load is
# about 13 s on this machine. Left cold, the first run of every session would
# miss the cap by design. So it is warmed the moment a model is chosen, and
# at the room's start if one already is.

def warm_embedder():
    with LOCK:
        if STATE["embedder"] in ("warming", "warm"):
            return
        STATE["embedder"] = "warming"

    def go():
        try:
            embed.backend()
            _set(embedder="warm")
        except embed.Unavailable:
            _set(embedder="unavailable")
        except Exception:
            _set(embedder="unavailable")
    threading.Thread(target=go, daemon=True).start()


# -- the lines ---------------------------------------------------------------

WHO = {"user": home.OWNER_NAME, home.SELF: home.NAME, "angel": "angel " + home.NAME}


# How far back the room filter may drag before settling for what it found.
# A bound on work, not on honesty: the newest lines of a room are found first.
ROOM_NET = 80

# The kinds of row the suggester is handed as lines of the room: what people
# say, what the assistant says, the angel, the hands, the assistant's lines
# to a hand. A job, a
# world notice, a dream, a lost line -- never. One tuple, read by both
# queries below and by the adapter's pairs (`adapt_pairs`), which cut their
# windows over these kinds and no others, so the two cannot drift apart.
READ_KINDS = ("user", home.SELF, "angel", "worker", "tell")
_READ_MARKS = ",".join("?" for _ in READ_KINDS)


def _meta_of(r) -> dict:
    meta = r["meta"]
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    return meta or {}


def line_of(r) -> dict:
    """One row as the small model reads it: who spoke, and the words. The
    one place that says who a row's speaker is to a text-only reader, so
    the adapter's pairs (`adapt_pairs`) label a line exactly as a run-time
    line is labelled. Takes a store row or a dict of one."""
    meta = _meta_of(r)
    if r["kind"] == "worker":
        who = "a hand" + ((" (" + meta["name"] + ")") if meta.get("name") else "")
    elif r["kind"] == "tell":
        who = home.NAME + ", to a hand"
    elif r["kind"] == "user" and meta.get("who"):
        # A labelled line names its speaker; absence means the owner. The
        # search this small model writes is about what was said, but a
        # name in the line is often the subject, so it gets the right one.
        who = people.CALLED.get(meta["who"], meta["who"])
    else:
        who = WHO.get(r["kind"], r["kind"])
    # A line that was only a picture is not an empty line. The small model
    # reads words and nothing else, so it gets the picture's name -- one
    # of four lines wasted on a blank would be a whole miss.
    return {"id": r["id"], "who": who,
            "text": pictures.with_label(r["text"] or "",
                                        pictures.of_row(meta))}


def _room_lines(rows, n: int) -> list:
    """The room rule over rows handed in newest first; the lines of the room
    that is speaking, oldest first, `n` at most."""
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
            # Hands, the angel, the assistant's lines to a hand: the
            # owner's room.
            continue
        out.append(line_of(r))
        if len(out) >= int(n):
            break
    out.reverse()
    return out


def last_lines(conn, n: int) -> list:
    """The last few lines of the room THAT IS SPEAKING, oldest first. The
    assistant's answer to *this* turn does not exist yet, which is the whole
    of how it is kept from seeing it.

    One room, not the whole store: the newest person line says whose room
    the exchange is in, and only that room's lines are handed on. With the
    rooms mixed, one person's question got a query written off the back of
    another person's talk, which is a wrong subject and wrong titles, a
    defect and not a rough edge. The owner's room keeps the hands and the
    angel in it; anyone else's room is that person and the assistant
    alone."""
    rows = conn.execute(
        "SELECT id, kind, text, meta FROM rows"
        " WHERE kind IN (" + _READ_MARKS + ")"
        " ORDER BY id DESC LIMIT ?",
        (*READ_KINDS, max(int(n), ROOM_NET))).fetchall()
    return _room_lines(rows, n)


def lines_before(conn, n: int, before) -> list:
    """What `last_lines` read on a turn that is now in the past: the same
    kinds and the same room rule, over the rows written before row
    `before` -- the assistant's reply that turn, which did not exist when
    it ran. For
    the adapter's pairs, which need the lines the suggester actually saw
    on a turn weeks back; `last_lines` itself is untouched."""
    rows = conn.execute(
        "SELECT id, kind, text, meta FROM rows"
        " WHERE kind IN (" + _READ_MARKS + ")"
        " AND id < ? ORDER BY id DESC LIMIT ?",
        (*READ_KINDS, int(before), max(int(n), ROOM_NET))).fetchall()
    return _room_lines(rows, n)


def _cut(text: str, tokens: int, both_ends: bool = False) -> tuple:
    """A line cut to `tokens`. Context lines keep their END: a long line
    lands on its last words. The subject line keeps BOTH ends -- a third
    from the front, the rest from the back -- because on a long line the
    subject is named at the start and the ask sits at the end, and a tail
    alone handed the model the imperative with the subject hidden. The cut is marked in
    the text so a mid-sentence start is not read as the start. Returns
    (text, was_cut)."""
    text = " ".join(text.split())
    cap = max(1, tokens) * db.CHARS_PER_TOKEN
    if len(text) <= cap:
        return text, False
    if both_ends:
        head_n = cap // 3
        head = text[:head_n]
        head = head.rsplit(" ", 1)[0] if " " in head else head
        tail = text[-(cap - len(head)):]
        tail = tail.split(" ", 1)[1] if " " in tail else tail
        return head + " … " + tail, True
    kept = text[-cap:]
    # Start at a word boundary rather than mid-word.
    kept = kept.split(" ", 1)[1] if " " in kept else kept
    return "… " + kept, True


# The words a nod is made of. A line of these and nothing else -- "ok, lets
# find that", "да, давай" -- says nothing on its own and points at the line
# before it. Counting words was wrong: "ok, lets find that" is four, and so
# is "do you remember Felix", which is a subject of its own.
NOD_WORDS = set("""
ok okay oke k yes yeah yep yup no nope sure fine good great nice cool right
lets let's let us go on do it that this then now please thanks thank you
ty find see try check search look start begin again more please
да добре ок окей хайде давай нека ами така супер добре ясно благодаря мерси
виж потърси търси провери пробвай намери това го я ми
""".split())


def _nod(text: str) -> bool:
    """A line made only of nod-words, six at most."""
    w = _words(text)
    return 0 < len(w) <= 6 and all(x in NOD_WORDS for x in w)


def cut_lines(lines, cfg) -> list:
    """Each line cut as the model will see it, and which one is THE SUBJECT.
    One dict per line -- who, text, cut, role -- the role being "subject",
    "context" for the lines before it, "nod" for one after it. The rule
    lives here once, for the prompt below and for the adapter's pairs: the
    subject is the newest line unless the newest is only a nod, in which
    case the line before it is; the subject gets the whole cap and keeps
    both ends, the rest a third of it and their end. Marks `cut` on the
    lines handed in, as it always did, so the run can count them."""
    subject = len(lines) - 1
    if subject > 0 and _nod(lines[subject]["text"]):
        subject -= 1
    out = []
    for i, ln in enumerate(lines):
        cap = cfg["line_tokens"] if i == subject else max(10, cfg["line_tokens"] // 3)
        text, cut = _cut(ln["text"], cap, both_ends=(i == subject))
        ln["cut"] = cut
        out.append({"who": ln["who"], "text": text, "cut": cut,
                    "role": ("subject" if i == subject else
                             "nod" if i > subject else "context")})
    return out


def lines_text(lines, cfg, ask: str) -> str:
    """The lines as the model sees them. One of them is THE SUBJECT: the
    newest, unless the newest is only a nod, in which case the line before
    it is -- a label on the nod was not enough; Gemma wrote "ok, let's find
    that" as the query with the label right there. So the subject line gets
    the whole cap and the marker, and the nod trails it as what it is."""
    parts = []
    for ln in cut_lines(lines, cfg):
        if ln["role"] == "subject":
            head = "THE SUBJECT, counts most -- "
        elif ln["role"] == "nod":
            head = "then, only a nod to the subject line -- "
        else:
            head = "earlier, context only -- "
        parts.append(head + ln["who"] + ": " + ln["text"])
    return ("The last " + str(len(lines)) + " lines of the room, oldest "
            "first. One is marked THE SUBJECT; the query is about that line "
            "-- or, if that line names no subject of its own, about the "
            "thing it points at in an earlier line.\n\n"
            + "\n\n".join(parts) + "\n\n" + ask)


# -- the answer --------------------------------------------------------------

# The answer is forced into this shape. A small model told "the query only"
# still thinks out loud first -- Gemma wrote eighty tokens of "my job is to"
# and never got to the query -- and a grammar is the one instruction it
# cannot talk its way past. Measured: the same model, the same lines,
# 0.57 s with the schema against a full minute of nothing without.
#
# A list of entries, one per separate thing the subject line names -- almost
# always one, three at most. A line asking after three unrelated things at
# once is three essences on three shelves, and one blurred query for all of
# them found only one of the three; three clean queries find each on its
# own. In each entry the fields keep their measured order: "about" is the
# thing in a few words, written before the query, because naming it first is
# what stops a small model carrying the older lines' subject into the query.
SHAPE = {
    "type": "object",
    "properties": {
        "subjects": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {"about": {"type": "string"},
                               "query": {"type": "string"}},
                "required": ["about", "query"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["subjects"],
    "additionalProperties": False,
}

# The speaker labels `line_of` writes, so a query that copies one is caught.
# Built from the home's names -- the same ones WHO hands out.
LABEL = re.compile(r"^\s*(" + "|".join((
    re.escape(home.OWNER_NAME), re.escape(home.NAME),
    re.escape("angel " + home.NAME), r"a hand[^:]*",
    re.escape(home.NAME + ", to a hand"))) + r")\s*:\s*", re.I)


def _one_line(text: str) -> str:
    """What the model wrote, made into one line. Anything it wrapped in
    quotes or <think> tags is unwrapped; an empty result stays empty."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    for line in text.splitlines():
        line = line.strip().strip('"“”„’\'`')
        line = re.sub(r"^(query|search|търсене)\s*:\s*", "", line, flags=re.I)
        if line:
            return " ".join(line.split())
    return ""


def _flat(text: str) -> str:
    """One line, whitespace folded, think-tags gone -- and the quotation
    marks LEFT ALONE unless the whole thing is wrapped in a pair. The
    query's one-liner strips a stray quote at either end, and on a why
    that ends in the line's quoted word it ate the closing mark: measured,
    'the bench rule; "bench' -- read by the assistant as the model cutting
    itself off mid-quotation."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = " ".join(text.split())
    if len(text) > 1 and text[0] in "\"“„'" and text[-1] in "\"”“'":
        text = text[1:-1].strip()
    return text


def _words(text: str) -> list:
    return re.findall(r"\w+", (text or "").casefold())


def _runs(words: list, n: int) -> set:
    return set(tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1)))


def is_echo(query: str, lines: list) -> bool:
    """A copy of a line it was handed, with or without the label in front.
    A small model did exactly this on its first real turn -- a person's
    line, speaker label and all, as the search -- and four unrelated titles
    cleared the floor by a hair on the strength of it. A copy is what was
    said, not what the assistant would have kept about it, and it is worth
    one more ask.

    Every line, not only the newest: it reads the assistant's side of the
    room too, and the first version of this watched the people's lines
    only, so a model lifting the assistant's own sentence whole slipped past.

    A copy means a stretch lifted whole -- eight words in a row -- not a
    query that shares its words. Sharing words is what a good query does,
    and a word-count test called a good one a copy when a person's line was
    quoting the assistant's."""
    if LABEL.match(query or ""):
        return True
    q = _words(query)
    if len(q) < 8:
        return False
    mine = _runs(q, 8)
    return any(mine & _runs(_words(ln["text"]), 8) for ln in lines)


# The run of words that makes a query a copy of an essence's text, the
# same length `is_echo` uses for a copy of a line.
COPY_RUN = 8


def copies_text(query: str, texts: dict):
    """The id of the first essence whose TEXT shares a run of COPY_RUN
    words with the query, or None. `texts` is {id: text}."""
    q = _words(query)
    if len(q) < COPY_RUN:
        return None
    mine = _runs(q, COPY_RUN)
    for eid, text in (texts or {}).items():
        if mine & _runs(_words(text), COPY_RUN):
            return eid
    return None


QUESTION = re.compile(
    # The openings of a question, with or without its question mark. "What I
    # kept was..." is a statement and gets asked again anyway; the retry only
    # asks for a statement, which costs half a second and changes nothing
    # true. "How the embedder crosses languages weakly" walked past the
    # first version of this, which wanted a "?" or an auxiliary.
    r"^\s*(do|does|did|is|are|was|were|can|could|will|would|should|"
    r"how|what|why|which|where|when|who|whether|"
    r"дали|помниш ли|помня ли|знаеш ли|нали|как|какво|защо|къде|кога|кой|коя|кое)"
    r"(?!\w)", re.I)

# There was a third flaw here, "two": an "and" in "about" sent the model
# back to split the subjects. Retired on the count -- some twenty firings,
# one split that helped, every other time the same answer again at the
# price of a second call, 0.7 to 3 s, with the reader now waiting behind
# it. The model splits on its own when a line plainly asks two things; what
# the check caught was mostly two aspects of one thing joined by "and".
# RETRY_TWO stays in the config so the
# file's contract does not move.


def flaw(query: str, lines: list, about: str = ""):
    """What is wrong with a query, if anything: "echo" for a copy of a line
    it was handed, "question" for a question. Either is worth one more ask.
    A question is the measured one: a question-shaped query found its
    essence only barely, where a sentence as if remembering it would have
    been a comfortable hit -- and a question is the one thing the prompt
    tells it never to write. `about` is kept for the callers and no longer
    read."""
    if not query:
        return None
    if is_echo(query, lines):
        return "echo"
    if query.rstrip().endswith("?") or QUESTION.match(query):
        return "question"
    return None


def _complete(key: str, messages: list, schema: dict, name: str) -> tuple:
    """One schema-forced call: (text, answered_by, stats). The transport and
    nothing else, so the query and the reader share one wire."""
    body = {
        "model": lm_key(key),
        "messages": messages,
        # 0.1, not 0.3: at 0.3 the same lines drew a plain query one
        # run and a mood-shaped one the next, and the plain one finds. Not
        # 0.0 -- a little heat keeps it from looping a phrase.
        "temperature": 0.1,
        "max_tokens": MAX_OUT,
        "stream": False,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": name, "strict": True,
                                            "schema": schema}},
    }
    if SEED is not None:
        body["seed"] = int(SEED)
    out = _lm("/v1/chat/completions", body, timeout=KNOBS["timeout_s"]["max"] + 5)
    choice = (out.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "")
    usage = out.get("usage") or {}
    return text, out.get("model"), {"input_tokens": usage.get("prompt_tokens"),
                                    "output_tokens": usage.get("completion_tokens"),
                                    "finish": choice.get("finish_reason")}


def _chat(key: str, messages: list) -> dict:
    text, answered_by, stats = _complete(key, messages, SHAPE, "query")
    subjects = []
    try:
        got = json.loads(text)
        for s in (got.get("subjects") or [])[:3]:
            if not isinstance(s, dict):
                continue
            entry = {"about": _one_line(str(s.get("about") or "")),
                     "query": _one_line(str(s.get("query") or ""))}
            if entry["about"] or entry["query"]:
                subjects.append(entry)
    except (json.JSONDecodeError, AttributeError):
        subjects = []
    # `answered_by` is which model actually answered, in LM Studio's own
    # words -- not which one was asked for. The assistant should never be
    # guessing whose suggestion it is looking at.
    return {"raw": text, "subjects": subjects,
            "answered_by": answered_by, "stats": stats}


def _first_flaw(subjects: list, lines: list):
    """The first flawed entry, if any: (kind, entry) or (None, None)."""
    for s in subjects:
        wrong = flaw(s.get("query") or "", lines, s.get("about") or "")
        if wrong:
            return wrong, s
    return None, None


def _joined(subjects: list, field: str) -> str:
    return " ; ".join(s[field] for s in subjects if s.get(field))


def ask(key: str, lines: list, cfg: dict, told: dict) -> dict:
    """One call to the small model -- two, if any entry came back flawed.
    Returns the entries, what was sent, and the stats. `query` and `about`
    are the entries joined for anything that wants one line of each."""
    sent = lines_text(lines, cfg, told["ASK"])
    messages = [{"role": "system", "content": told["PROMPT"]},
                {"role": "user", "content": sent}]
    first = _chat(key, messages)
    out = dict(first, sent=sent, first=None, flaw=None, still_flawed=None,
               used_about=False)
    subjects = first["subjects"]
    wrong, _ = _first_flaw(subjects, lines)
    if wrong:
        out["first"] = _joined(subjects, "query")
        out["flaw"] = wrong
        again = {"echo": told["RETRY"], "question": told["RETRY_QUESTION"],
                 "two": told["RETRY_TWO"]}[wrong]
        messages += [{"role": "assistant", "content": first["raw"]},
                     {"role": "user", "content": again}]
        second = _chat(key, messages)
        if second["subjects"]:
            subjects = second["subjects"]
        out.update({k: second[k] for k in ("raw", "answered_by")})
        out["stats"] = {"input_tokens": (first["stats"]["input_tokens"] or 0)
                        + (second["stats"]["input_tokens"] or 0),
                        "output_tokens": (first["stats"]["output_tokens"] or 0)
                        + (second["stats"]["output_tokens"] or 0),
                        "finish": second["stats"]["finish"]}
        still, _ = _first_flaw(subjects, lines)
        out["still_flawed"] = still
        for s in subjects:
            # A query with the label cut off is better than none at all.
            s["query"] = LABEL.sub("", s.get("query") or "")
            # A retry that came home a copy again is not asked a third time
            # -- that would be three calls inside a two-second cap. But
            # "about" is a few words it wrote itself, never a copy, so that
            # is searched instead of the copy. The hole this closes: a retry
            # checked only against the rule that sent it back is where the
            # other rules leak.
            if (flaw(s["query"], lines) == "echo"
                    and len(_words(s.get("about") or "")) >= 2):
                s["query"] = s["about"]
                out["used_about"] = True
    out["subjects"] = subjects
    out["query"] = _joined(subjects, "query")
    out["about"] = _joined(subjects, "about")
    return out


# -- the reader ---------------------------------------------------------

# The reader's answer, forced the same way the query's is: one entry per
# essence it was handed, the id echoed back so nothing is matched by
# position, and an id it was not handed is dropped unread -- it never gets
# to introduce an essence of its own. "related" comes before "why" and is
# a boolean on purpose: asked in prose, Gemma never once called a miss a
# miss -- five unrelated essences, five invented connections, measured --
# and a field it must fill with true or false is the one
# form of the question it cannot answer politely.
READ_SHAPE = {
    "type": "object",
    "properties": {
        "reads": {
            "type": "array", "minItems": 1, "maxItems": 7,
            "items": {"type": "object",
                      "properties": {"id": {"type": "integer"},
                                     "related": {"type": "boolean"},
                                     "why": {"type": "string"}},
                      "required": ["id", "related", "why"],
                      "additionalProperties": False}}},
    "required": ["reads"],
    "additionalProperties": False,
}

# What the assistant sees on a title the reader voted against. Its verdict,
# never its words: left free to phrase a "no", it phrases a topic instead, and three
# words of topic is a summary starting to grow.
UNRELATED = "does not look related"

# And the other way a title leaves the list: the reader said related and
# its why did not survive the check. From the assistant's side the two
# would look identical, and they need opposite fixes -- one is the reader's
# judgement, the other is its sentence.
CHECK_FAILED = "its why failed the check"

# The words a claim of sharing is made of, which prove nothing shared.
WHY_NOISE = set("""
both have has had share shared shares holds hold holding name names named
naming mentions mentioned line lines essence essences asking asks asked about
where there here that this with from into after which thing things и двете
""".split())


def _why_holds(why: str, text: str, line: str = "") -> bool:
    """A why points at a thing the essence actually contains, so the claim
    is literal and is checked literally -- the same trick the echo check
    plays on the query. Measured: with "0.45" salient in the line, Gemma
    put "the 0.45 number" on an essence that never says it, with "related"
    set true and a straight face.

    Strict on the distinctive things first: every number in the why must
    be in the essence, and so must every capitalised name past the first
    word -- measured, the prompt's own example sentence pasted onto an
    essence with no 0.45 in it walked through a check that was satisfied
    by the word "line". Only a why with no number and no name falls back
    to any one solid word being present.

    And a number is checked against the line as well: numbers cross
    languages, so this costs nothing on a line in another language --
    measured, asked about 0.45 the reader kept an essence on a why citing
    a different figure as "the number the line asks after", every figure
    in the essence and none of them in the line.

    And the why must be about the question: one solid word of it -- five
    letters, or a name, or a number -- must stand whole in the asking LINE
    itself. Twice over a looser check let one through: a why naming a
    story from the essence as "the name the line asks after" passed a
    name-check on a line about something else entirely; then a why of pure
    essence text passed a check against the query because the query was a
    sentence the little model had invented, and they shared the word
    "than". Only the line is the person's own, so only the line is checked
    -- and it crosses languages because the reader is told to copy the
    line's own word for the thing, Cyrillic and all, into the why."""
    body = (text or "").casefold()
    words = re.findall(r"\d+(?:[.,]\d+)+|\w+", why or "", flags=re.UNICODE)
    numbers = [w for w in words if any(c.isdigit() for c in w)]
    names = [w for w in words[1:] if w[:1].isupper() and len(w) > 2
             and w.casefold() not in ABOUT_STOPWORDS]
    solid = [w for w in words
             if (len(w) >= 5 or w in numbers or w in names)
             and w.casefold() not in ABOUT_STOPWORDS
             and w.casefold() not in WHY_NOISE]
    in_line = set()
    if line:
        if numbers and re.search(r"\d", line) and not any(n in line for n in numbers):
            return False
        asked = line.casefold()
        in_line = {w.casefold() for w in solid
                   if re.search(r"(?<!\w)" + re.escape(w.casefold()) + r"(?!\w)",
                                asked)}
        if not in_line:
            return False
    # Essence side: every number cited must be in it, and one solid word
    # that is the essence's OWN naming -- not the line's word stapled on.
    # Measured: 'the line asks after "bench' was the pad phrase with one
    # word of the line and nothing of the essence, and it read as
    # compliance to the check before this one. Not every name, because the
    # line's word is copied in by instruction and, across languages, is
    # exactly the word the essence does not have.
    if numbers and not all(n.casefold() in body for n in numbers):
        return False
    return any(w.casefold() in body and w.casefold() not in in_line
               for w in solid)


def _first_words(text: str, chars: int = 140) -> str:
    """The essence's own opening words, verbatim, riding under every shown
    title. With the first line carried, the assistant can judge a name for
    free instead of spending a look on it -- a titles-only list is a list
    that gets ignored."""
    text = " ".join((text or "").split())
    if len(text) <= chars:
        return text
    cut = text[:chars]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return cut + " …"


def _texts_and_hand(ids: list) -> tuple:
    """One read for two facts about each essence: its text, and whether the
    assistant already holds it. Returns ({id: text}, {ids in hand})."""
    texts, in_hand = {}, set()
    if ids:
        conn = db.connect()
        try:
            marks = ",".join("?" * len(ids))
            for r in conn.execute("SELECT id, text, loaded FROM rows WHERE id IN ("
                                  + marks + ")", list(ids)):
                texts[r["id"]] = r["text"] or ""
                if r["loaded"]:
                    in_hand.add(r["id"])
        finally:
            conn.close()
    return texts, in_hand


def _title(h, texts, in_hand, **more) -> dict:
    """One line of the list the assistant is handed."""
    t = {"id": h["id"], "title": h["title"], "date": h["date"],
         "score": h["score"], "first": _first_words(texts.get(h["id"], "")),
         "in_hand": h["id"] in in_hand}
    if h.get("word"):
        t["word"] = h["word"]
    t.update(more)
    return t


def _read_slice(text: str, tokens: int, word: str = None) -> str:
    """What the reader sees of one essence: its opening -- or, for a title
    seated by a literal word, a window around that word. Measured: a
    literal word matched in four essences and the reason lived nowhere
    near the opening, so reading the top of a seated
    essence is reading the wrong place on purpose. The cut is marked so a
    mid-sentence start is not read as the start."""
    text = " ".join((text or "").split())
    cap = max(1, tokens) * db.CHARS_PER_TOKEN
    at = text.casefold().find(word.casefold()) if word else -1
    if at < 0 or at < cap // 3:
        return text[:cap] + (" …" if len(text) > cap else "")
    start = text.rfind(" ", 0, at - cap // 3) + 1
    end = start + cap
    return "… " + text[start:end] + (" …" if end < len(text) else "")


# What the reader may be handed in essence text, all titles together. The
# model's context is CONTEXT tokens and its instructions and answer need
# the rest; past this the per-essence slice shrinks rather than the call
# failing. Five titles at 400 is 2,000; seven at 400 would brush 4,096 with
# the instructions on top.
READ_BUDGET = 2800


def read_text(lines, top, texts, cfg) -> str:
    """The reader's user message: the newest line, then each essence cut to
    what it should see. Its own function so a test can read what was sent."""
    i = len(lines) - 1
    if i > 0 and _nod(lines[i]["text"]):
        i -= 1
    newest, _ = _cut(lines[i]["text"], cfg["line_tokens"], both_ends=True)
    per = max(40, min(cfg["read_tokens"], READ_BUDGET // max(1, len(top))))
    parts = []
    for t in top:
        parts.append("essence " + str(t["id"]) + " -- "
                     + (t["title"] or "(untitled)") + "\n"
                     + _read_slice(texts.get(t["id"]), per, t.get("word")))
    return ("The newest line of the room:\n"
            + lines[i]["who"] + ": " + newest
            + "\n\n" + home.NAME + "'s essences that came back for it, each "
              "cut to the part that matters:\n\n" + "\n\n".join(parts)
            + '\n\nNow: "reads" -- one entry per essence above, its "id", '
              '"related", and "why".')


def read_titles(key, lines, top, texts, cfg, told) -> dict:
    """The reader: the same model on a second errand, handed the opening of
    each shown essence and asked one pointing sentence apiece. Three terms,
    all load-bearing: it points and never tells -- names what the essence
    shares with the line, never what the essence says -- or the assistant
    is handed a paraphrase of its own memory wearing the model's voice,
    which is the one failure it is built against; it reads the top few
    only, cut short, or the deadline eats the whole suggestion; and its
    sentence is labelled as its own wherever it appears. A find it calls
    unrelated is still shown -- it suggests, it never decides.

    Returns {id: sentence}; an id that fails to come back simply leaves
    that title standing as it stood."""
    sent = read_text(lines, top, texts, cfg)
    text, _, _ = _complete(key, [{"role": "system", "content": told["READER"]},
                                 {"role": "user", "content": sent}],
                           READ_SHAPE, "reads")
    newest = lines[-1]["text"] if lines else ""
    whys, ok = {}, {t["id"] for t in top}
    try:
        got = json.loads(text)
        for r in (got.get("reads") or []):
            if not isinstance(r, dict) or r.get("id") not in ok:
                continue
            if not r.get("related"):
                whys[r["id"]] = UNRELATED
                continue
            why = _flat(str(r.get("why") or ""))[:220]
            if why and not _why_holds(why, texts.get(r["id"]) or "", newest):
                # It said related and its sentence did not prove it. The
                # assistant gets the checked verdict, named as the check's.
                whys[r["id"]] = CHECK_FAILED
            elif why:
                whys[r["id"]] = why
    except (json.JSONDecodeError, AttributeError):
        pass
    return whys


# -- the words to search literally --------------------------------------

# Too grammatical or too generic to be worth a literal search -- what is left
# after these are cut out of "about" is names and concrete nouns. Not an
# attempt at real language understanding, just enough to turn "his cat
# Felix, and what his mother cooks" into "cat Felix mother" and not into
# "his and what his". Both languages the list covers, since "about" comes
# in either.
ABOUT_STOPWORDS = set("""
a an the and or but of to in on at for with about from by as is are was
were be been being this that these those it its he him his she her hers
they them their we us our you your i my me mine what which who whom whose
up down out off into onto over under
thing things stuff question matter subject topic something someone
anything everything nothing kind sort way part bit also just still even
и или но на в за с от до е са беше бяха това тези онези той него неговия
неговото нейния нейното тя нея те тях техния техните ние нас наш вие вас
ваш аз мой моя моето какво кой коя кои кое чий нещо нещото въпрос въпроса
тема темата също само още дори му й ѝ го ги ни ви си се ме те
""".split())


def keywords_from_about(about: str, cap: int = 3) -> list:
    """1-3 literal search words pulled out of what the model named the
    newest line about -- the names and concrete nouns, with the function
    words, articles, pronouns and generic filler left out. Kept exactly as
    written: no casefolding here and nothing stripped down to ASCII, because
    half of what it writes is Cyrillic and `search._fold` already casefolds
    both sides when it compares, so nothing here needs mangling first. A
    number keeps its point: on "that 0.45 number" the number is the whole of
    it, and split to "45" it matched every timestamp on the shelf. And a
    number goes first: when "about" named a few generic words and then a
    number, the cap was spent before reaching the one word that named the
    right essences."""
    words = re.findall(r"\d+(?:[.,]\d+)+|\w+", about or "", flags=re.UNICODE)
    words.sort(key=lambda w: not any(c.isdigit() for c in w))
    out = []
    for word in words:
        if len(word) < 2 or word.casefold() in ABOUT_STOPWORDS:
            continue
        if word not in out:
            out.append(word)
        if len(out) >= cap:
            break
    return out


# -- the pick ----------------------------------------------------------------

# How many literal finds take seats beside the meaning hits -- a guarantee
# and not an allowance. Measured: on a short line whose one distinctive word
# had every right essence in hand, a hundred and fifty meaning hits over the
# old floor crowded them out of the list before the pick ever saw them. An
# earlier seat (a name found literally, ranked under five near-misses) fixed
# this for multi-subject turns only; the common case leaked. Two is enough
# to be worth a glance without a run of literal matches crowding out the
# meaning arm's actual judgement of what is close to the question.
KEYWORD_ONLY_CAP = 2

# A word that matches this many essences is not a name any more, it is a
# habit of speech, and a seat for it is a seat for noise. About an eighth of
# a shelf of 160: "0.45" matches five essences and earns its seat; bare "45"
# matched every timestamp and token count in reach.
KEYWORD_COMMON = 20


def keyword_finds(conn, keywords: list, limit: int) -> list:
    """The literal arm, run on its own so the meaning arm cannot crowd it
    out -- word by word, so one habit-word cannot flood the list a rare one
    earned. A word past KEYWORD_COMMON is dropped whole. Rarest word first,
    then newest: the fewer essences a word touches, the more it is a name
    and not a habit -- "0.45" touches five and is the whole point; "number"
    touches eighteen and is a way of talking."""
    ranked = []
    for word in keywords or []:
        report = search.run(conn, {"keywords": [word],
                                   "limit": search.MAX_LIMIT})
        matched = len(report["hits"]) + len(report["held_back_by_the_limit"])
        if not matched or matched > KEYWORD_COMMON:
            continue
        for h in report["hits"]:
            # The word rides on the hit: the reader reads around it.
            ranked.append((matched, -h["id"], dict(h, word=word)))
    ranked.sort(key=lambda t: (t[0], t[1]))
    out, seen = [], set()
    for _, _, h in ranked:
        if h["id"] not in seen:
            seen.add(h["id"])
            out.append(h)
    return out[:limit]


def pick(hits: list, cfg: dict) -> tuple:
    """Which of the search's meaning hits the assistant is shown. The search has
    already applied its own floor; this holds them to the suggestion's bar,
    then decides how far down the list to go.

    The bar first: `suggest_floor`, and a hit under it is held back even
    from the `titles_min` zone -- the minimum is how many may be shown
    plainly, not a promise to fill seats with the nearest thing to hand.
    Under the bar a turn of small talk shows nothing, and says so, which is
    the difference between a suggestion and a habit of being ignored.

    Past the bar, unchanged: the first `titles_min` are shown; after that,
    only within `gap` of the best -- the top cluster, and not the tail --
    and never past `titles_max`. Scores of [0.80, 0.79, 0.79, 0.79, 0.56,
    0.35] with a minimum of 3 and a gap of 0.03 show four: the fourth is as
    good as the first, the fifth is off a cliff.

    Keyword finds no longer pass through here at all: they have no score to
    hold to a bar, and their seats are `gather`'s to give.

    Returns (shown, held_back), and the held-back ones are counted out loud
    because a list that quietly stops is a list nobody can trust."""
    bar = cfg.get("suggest_floor") or 0.0
    cleared, low = [], []
    for h in hits:
        (cleared if h["score"] is None or h["score"] >= bar else low).append(h)
    shown = []
    best = cleared[0]["score"] if cleared and cleared[0]["score"] is not None else None
    for i, h in enumerate(cleared[:cfg["titles_max"]]):
        if i < cfg["titles_min"]:
            shown.append(h)
            continue
        if best is None or h["score"] is None:
            break
        if best - h["score"] <= cfg["gap"]:
            shown.append(h)
        else:
            break
    return shown, cleared[len(shown):] + low


def bar_now(cfg: dict, adapter=None) -> tuple:
    """The suggestion bar for this run, and whose it is. A live adapter that
    carries a bar measured with it wins over the knob: the knob was measured
    in the base space, and a bar belongs to the space it was measured in.
    Otherwise the knob, as today. None asks for the live adapter; False is
    none on purpose. Returns (bar, "adapter <name>" or "knob")."""
    if adapter is None:
        adapter = adapt.current()
    if adapter and adapter.bar is not None:
        return float(adapter.bar), "adapter " + adapter.name
    return float(cfg.get("suggest_floor") or 0.0), "knob"


def gather(conn, subjects: list, cfg: dict) -> dict:
    """One search per thing the line named, and which hits the assistant is
    shown.

    Each subject's hits are picked against that subject's own best score.
    The gap rule clusters within one query's distribution, and three queries
    have three distributions -- measured: merged first and picked once, two
    subjects' essences both cleared their own searches and were then drowned
    by the third query's stronger cluster, which is the original miss
    wearing a new coat. Picked lists are interleaved in the order the things
    were named, deduped, and capped at titles_max together, so every subject
    gets its best hit in front of the assistant before any subject gets its
    second."""
    n = len(subjects)
    # One map for the whole run, settled once: a `use` between two subjects
    # would otherwise search them in different spaces under one stamp. The
    # bar is the live version's own when it carries one, else the knob.
    adapter = adapt.current()
    bar, bar_from = bar_now(cfg, adapter if adapter is not None else False)
    per = dict(cfg, titles_min=cfg["titles_min"] if n == 1 else 1,
               suggest_floor=bar)
    picked, spare = [], []
    searched = best_seen = floor = None
    disagreed = 0
    over_limit, problems = set(), []
    for s in subjects:
        report = search.run(conn, {"restatement": s["query"],
                                   "limit": cfg["titles_max"]},
                            adapter=adapter if adapter is not None else False)
        searched = report["searched"]
        floor = report["floor"]
        if report["best_score_seen"] is not None:
            best_seen = max(best_seen or 0, report["best_score_seen"])
        over_limit.update(report["held_back_by_the_limit"])
        problems.extend(report["problems"])
        # One space: this arm reads the adapted hits and never the
        # shadow's -- those are for the assistant's deliberate search -- but
        # how often the two spaces disagreed is counted and kept.
        disagreed += (report["arms"].get("shadow") or {}).get("disagreed") or 0
        shown_here, held_here = pick([h for h in report["hits"]
                                      if not h.get("shadow")], per)
        # The literal arm, seated rather than merely allowed: the name the
        # line actually said, found by that name, right behind the best
        # meaning hit -- KEYWORD_ONLY_CAP says how the old way lost the
        # right essences with the word in its hand. When nothing
        # cleared the bar, the finds stand alone rather than not at all.
        have = {h["id"] for h in shown_here}
        seats = 0
        for h in keyword_finds(conn, s.get("keywords"), search.MAX_LIMIT):
            if h["id"] in have:
                continue
            if seats < KEYWORD_ONLY_CAP:
                shown_here.insert(min(1 + seats, len(shown_here)), h)
                seats += 1
            else:
                held_here.append(h)
        picked.append(shown_here)
        spare.extend(held_here)
    # Round-robin, each subject held to its share of the seats: without the
    # share, one weak query's wide cluster floods the list while the others
    # sit at one hit each -- measured, one subject's five near-misses against
    # another's one. An essence two subjects both found is shown once and does
    # not cost the second subject its round. Short lists leave seats empty
    # rather than handing them on.
    rounds = max(1, -(-cfg["titles_max"] // n))
    shown, seen = [], set()
    cursor = [0] * len(picked)
    for _ in range(rounds):
        for j, l in enumerate(picked):
            while cursor[j] < len(l) and l[cursor[j]]["id"] in seen:
                cursor[j] += 1
            if cursor[j] >= len(l) or len(shown) >= cfg["titles_max"]:
                continue
            seen.add(l[cursor[j]]["id"])
            shown.append(l[cursor[j]])
            cursor[j] += 1
    for j, l in enumerate(picked):
        spare.extend(h for h in l[cursor[j]:])
    held, held_seen = [], set()
    for h in spare:
        if h["id"] not in seen and h["id"] not in held_seen:
            held_seen.add(h["id"])
            held.append(h)
    return {"shown": shown, "held": held,
            "over_limit": over_limit - seen - held_seen,
            "searched": searched, "best_score_seen": best_seen,
            "problems": problems,
            "adapter": None if adapter is None else adapter.name,
            "bar": bar, "bar_from": bar_from,
            "floor": search.floor_now() if floor is None else floor,
            "disagreed": disagreed,
            "trouble": None if adapter is not None else adapt.problem()}


# -- the run -----------------------------------------------------------------

def _knobs_for_her(cfg: dict) -> dict:
    """The settings, as the assistant sees them every turn: so it can say
    "the gap is too tight" or "give it more lines", and the owner can turn
    the knob. It has no tool for it on purpose -- a tool is rent paid every
    turn, and this one would be used once a week."""
    out = {n: cfg[n] for n in KNOBS}
    out["floor"] = search.floor_now()
    out["prompt_file"] = "server/recall_config.py"
    return out


def _block(key, cfg, **fields) -> dict:
    m = KEYS.get(key) or {}
    block = {"by": m.get("label") or key, "asked_for": key,
             # Who wrote the query when it is not the model: the trained
             # writer's key, or None. `answered_by` below is LM Studio's own
             # word for who answered the writing call.
             "writer": None,
             "answered_by": None, "read": 0, "about": None, "query": None,
             "keywords": [], "titles": [], "set_aside": [], "under_bar": False,
             "held_back": 0, "held_back_best": None, "searched": None,
             "best_score_seen": None,
             "took_s": None, "model_s": None, "search_s": None, "read_s": None,
             "note": None, "missed": None,
             # The stamp: which adapter version the search ran under, or
             # None for the base space, which is today.
             "adapter": None,
             "settings": _knobs_for_her(cfg)}
    block.update(fields)
    return block


def _run(key, lines, cfg, box, deadline_s, writer=None):
    """The whole of one run, on its own thread with its own connection.
    Writes its result into `box` and into STATE['last'] -- the latter even
    if the turn has already gone on without it, marked late, so the
    Developer section can show what it would have said. `writer` is the
    key that writes the query when it is not `key`, which reads."""
    started = time.time()
    writer = writer if (writer and writer != key) else None
    block = _block(key, cfg, read=len(lines), writer=writer)
    forlater = {"stats": None, "sent": None, "raw": None, "first": None}
    _set(phase="working")
    try:
        try:
            models = _models()
            rec = _find(models, key)
        except NoServer as e:
            _set(phase="no server", detail=str(e))
            block["missed"] = str(e)
            return
        if rec is None:
            _set(phase="not downloaded", detail="not on this disk yet")
            block["missed"] = ("the model is not on this disk; the download "
                               "button is under Developer")
            return
        if not _instances(rec):
            block["missed"] = ("the model was not in memory; it is being loaded "
                               "now, about 10 s, and will be there next turn")
            threading.Thread(target=_switch, args=(None, key),
                             daemon=True).start()
            return
        if writer:
            wrec = _find(models, writer)
            if wrec is None:
                block["missed"] = ("the query writer " + writer + " is not on this disk; "
                                   "the model wrote nothing this turn")
                _set(writer_phase="not on disk", writer_detail="LM Studio does not have " + writer)
                return
            if not _instances(wrec):
                block["missed"] = ("the query writer was not in memory; it is being "
                                   "loaded now and will be there next turn")
                threading.Thread(target=_switch_writer, args=(None, writer),
                                 daemon=True).start()
                return
        told = config()
        t0 = time.time()
        try:
            asked = ask(writer or key, lines, cfg, told)
        except (NoServer, LMError) as e:
            block["missed"] = "the model did not answer: " + str(e)
            return
        block["model_s"] = round(time.time() - t0, 2)
        forlater.update(stats=asked["stats"], sent=asked["sent"],
                        raw=asked["raw"], first=asked["first"],
                        subjects=asked.get("subjects"))
        block["answered_by"] = asked.get("answered_by")
        block["about"] = asked.get("about") or None
        block["query"] = asked["query"] or None
        subjects = [s for s in (asked.get("subjects") or []) if s.get("query")]
        keywords = []
        for s in subjects:
            s["keywords"] = keywords_from_about(s.get("about") or "")
            for w in s["keywords"]:
                if w not in keywords:
                    keywords.append(w)
        block["keywords"] = keywords
        notes = []
        if len(subjects) > 1:
            notes.append("the line named " + str(len(subjects))
                         + " separate things, and each was searched on its own")
        if asked["flaw"]:
            named = {"echo": "a copy of a line it was handed",
                     "question": "a question", "two": "about two subjects"}
            notes.append("its first answer was " + named[asked["flaw"]]
                         + ", so it was asked once more"
                         + ((" -- and the second was " + named[asked["still_flawed"]]
                             + (" again, so its own 'about' was searched instead"
                                if asked.get("used_about") else
                                " again, searched as it stood"))
                            if asked["still_flawed"] else ""))
        cut = sum(1 for ln in lines if ln.get("cut"))
        if cut:
            notes.append(str(cut) + " of the lines " + ("was" if cut == 1 else "were")
                         + " cut to " + str(cfg["line_tokens"])
                         + " tokens -- the subject line keeps both ends, the "
                         "others their end")
        block["note"] = ". ".join(notes) or None
        if not asked["query"]:
            block["missed"] = ("it wrote nothing usable: "
                               + repr((asked["raw"] or "")[:80]))
            return
        t0 = time.time()
        conn = db.connect()
        try:
            got = gather(conn, subjects, cfg)
        finally:
            conn.close()
        block["search_s"] = round(time.time() - t0, 2)
        # The stamp the assistant reads: which version this ran under and, when the
        # version's own bar stood in for the knob, that bar and whose it is.
        block["adapter"] = got["adapter"]
        if got["adapter"]:
            block["disagreed"] = got["disagreed"]
        if got["bar_from"] != "knob":
            block["bar"] = got["bar"]
            block["bar_from"] = got["bar_from"]
        if got["trouble"]:
            block["note"] = ((block["note"] + ". " + got["trouble"])
                             if block["note"] else got["trouble"])
        shown, held = got["shown"], got["held"]
        # Whether the assistant already holds each -- a reminder and a find
        # are different things, and it should tell them apart without
        # reading its own working set line by line -- and their text, for
        # the first words and for the reader. The text goes no further
        # than this thread.
        texts, in_hand = _texts_and_hand([h["id"] for h in shown])
        block["titles"] = [_title(h, texts, in_hand) for h in shown]
        # The eight-words guard, a condition on any trained writer: a query
        # that copies a run of an essence's TEXT is the assistant's own
        # memory handed back in the writer's voice, so what it is shown is
        # its "about" instead, and the copy is counted. Titles are names
        # and are not text; a query that is a title is a name.
        quoted = []
        for s in subjects:
            hit = copies_text(s.get("query") or "", texts)
            if hit is not None and len(_words(s.get("about") or "")) >= 1:
                quoted.append({"essence": hit, "query": s["query"]})
                s["query"] = s["about"]
        if quoted:
            block["query"] = _joined(subjects, "query")
            block["quoted"] = quoted
            note = ("its query copied " + str(COPY_RUN) + " words of essence "
                    + ", ".join("#" + str(q["essence"]) for q in quoted)
                    + ", so its about is shown instead")
            block["note"] = ((block["note"] + ". " + note) if block["note"] else note)
        scored = [t for t in block["titles"] if t["score"] is not None]
        if not scored and got["best_score_seen"] is not None:
            # An empty list with a reason is a quiet turn; without one it
            # reads as a broken shelf. A literal seat alone does not count
            # as the bar being cleared -- it has no score to clear it with.
            quiet = ("nothing cleared the bar of "
                     + format(got["bar"], ".2f")
                     + " -- the best resemblance was "
                     + format(got["best_score_seen"], ".3f"))
            under = sorted((h for h in held if h["score"] is not None
                            and h["score"] >= got["floor"]),
                           key=lambda h: -h["score"])[:cfg["read_top"]]
            if under and cfg["read_top"]:
                # Where the score has admitted it cannot tell, the thing
                # that reads decides. The top few
                # held back, down to the search's own floor, are read
                # alongside any seats, and only what the reader ticks
                # related is shown, marked as coming from under the bar.
                more_texts, more_hand = _texts_and_hand([h["id"] for h in under])
                texts.update(more_texts)
                in_hand |= more_hand
                block["titles"] += [_title(h, texts, in_hand, under_bar=True)
                                    for h in under]
                block["under_bar"] = True
            else:
                quiet += ("; the deliberate search still reaches everything "
                          "over " + format(got["floor"], ".2f"))
            block["note"] = ((block["note"] + ". " + quiet)
                             if block["note"] else quiet)
        if cfg["read_top"] and block["titles"]:
            t0 = time.time()
            # The first few, and every seat wherever it sits: a seat has no
            # score, so the reader is the only judge it will ever get --
            # measured, two seats below the read three arrived unjudged,
            # and neither was related.
            top = (block["titles"] if block["under_bar"] else
                   block["titles"][:cfg["read_top"]]
                   + [t for t in block["titles"][cfg["read_top"]:]
                      if t.get("word")])
            try:
                whys = read_titles(key, lines, top, texts, cfg, told)
                if not whys:
                    raise LMError("it answered nothing usable")
                kept, aside = [], []
                for t in block["titles"]:
                    why = whys.get(t["id"])
                    # Read and ticked unrelated, or its why failed the
                    # check: set aside, by name and by reason. Under the
                    # bar only a related verdict earns a seat at all.
                    if why in (UNRELATED, CHECK_FAILED) or (block["under_bar"] and not why):
                        t["because"] = why or "unread under the bar"
                        aside.append(t)
                        continue
                    if why:
                        t["why"] = why
                    kept.append(t)

                def _aside_text(aside):
                    kinds = [(UNRELATED, "unrelated"), (CHECK_FAILED, "failed the check"),
                             ("unread under the bar", "unread")]
                    parts = [str(sum(1 for t in aside if t.get("because") == k)) + " " + name
                             for k, name in kinds if any(t.get("because") == k for t in aside)]
                    return "set aside " + str(len(aside)) + " (" + ", ".join(parts) + ")"

                read_note = ("the reader read " + str(len(top))
                             + (" under the bar" if block["under_bar"] else "")
                             + " and "
                             + (_aside_text(aside) if kept else "ticked none related"))
                # A seat the reader emptied passes to the next literal find
                # -- read in a second, smaller call only when it happens.
                # Measured: the newest essence to mention a number took the
                # seat and was rightly set aside, and the essences behind it
                # never got their turn.
                freed = [t for t in aside if t.get("word") and not t.get("under_bar")]
                seen = {t["id"] for t in block["titles"]}
                freed_words = {t["word"] for t in freed}
                # The same word's next find first: a seat "0.45" emptied is
                # a seat "0.45" refilled, not handed to another word's list.
                reserve = sorted((h for h in held
                                  if h.get("word") and h["id"] not in seen),
                                 key=lambda h: h["word"] not in freed_words)[:len(freed)]
                if reserve:
                    r_texts, r_hand = _texts_and_hand([h["id"] for h in reserve])
                    texts.update(r_texts)
                    in_hand |= r_hand
                    r_titles = [_title(h, texts, in_hand) for h in reserve]
                    r_whys = read_titles(key, lines, r_titles, texts, cfg, told)
                    passed = 0
                    for t in r_titles:
                        why = r_whys.get(t["id"])
                        if why and why not in (UNRELATED, CHECK_FAILED):
                            t["why"] = why
                            kept.insert(min(1 + passed, len(kept)), t)
                            passed += 1
                        else:
                            t["because"] = why or "unread under the bar"
                            aside.append(t)
                    read_note += (", then " + str(len(r_titles))
                                  + " more literal find"
                                  + ("" if len(r_titles) == 1 else "s")
                                  + " for the freed seat"
                                  + ("" if len(freed) == 1 else "s") + ": "
                                  + str(passed) + " related")
                block["titles"] = kept
                block["set_aside"] = [{"id": t["id"], "title": t["title"],
                                       "date": t["date"],
                                       "because": t.get("because")} for t in aside]
                # A read that ticked none says so: it must never look like a
                # turn where nothing ran.
                block["note"] = ((block["note"] + ". " + read_note)
                                 if block["note"] else read_note)
            except (NoServer, LMError) as e:
                more = ("the reader did not answer (" + str(e) + ")"
                        + ("; nothing under the bar is shown unread"
                           if block["under_bar"] else
                           "; the titles stand without its sentences"))
                block["note"] = ((block["note"] + ". " + more)
                                 if block["note"] else more)
                if block["under_bar"]:
                    block["titles"] = [t for t in block["titles"]
                                       if not t.get("under_bar")]
            block["read_s"] = round(time.time() - t0, 2)
        block["held_back"] = len(held) + len(got["over_limit"])
        # Fifty held back within a hundredth of the top is a different fact
        # from fifty trailing away. One number tells them apart.
        block["held_back_best"] = max(
            (h["score"] for h in held if h["score"] is not None), default=None)
        block["searched"] = got["searched"]
        block["best_score_seen"] = got["best_score_seen"]
        # The search's own problems, if the meaning arm could not run: a list
        # of nothing with no reason would read as a quiet shelf.
        if got["problems"] and not shown and not held:
            block["missed"] = got["problems"][0]
    finally:
        block["took_s"] = round(time.time() - started, 2)
        block["late"] = block["took_s"] > deadline_s
        box["out"] = block
        with LOCK:
            # The whole of it, with the model's own figures and the very
            # words it was sent, for Developer. The assistant is handed the
            # block without them.
            STATE["last"] = dict(block, at=time.time(), **forlater)
            if STATE["phase"] == "working":
                STATE["phase"] = "loaded"


def took_text(block: dict) -> str:
    """"0.7 s (model 0.5, search 0.2)" -- how long, and whose."""
    if block.get("took_s") is None:
        return ""
    parts = []
    if block.get("model_s") is not None:
        parts.append("model " + format(block["model_s"], ".2f"))
    if block.get("search_s") is not None:
        parts.append("search " + format(block["search_s"], ".2f"))
    if block.get("read_s") is not None:
        parts.append("read " + format(block["read_s"], ".2f"))
    return (format(block["took_s"], ".2f") + " s"
            + (" (" + ", ".join(parts) + ")" if parts else ""))


def summary(block: dict) -> str:
    """One line, for the room and the event."""
    if not block:
        return "the automatic memory did not run"
    if block.get("missed") and not block.get("query"):
        return "missed: " + block["missed"]
    q = block.get("query") or ""
    q = (q[:80] + "…") if len(q) > 80 else q
    n = len(block.get("titles") or [])
    line = 'searched for "' + q + '" · ' + str(n) + " title" + ("" if n == 1 else "s")
    if block.get("under_bar"):
        line += " from under the bar"
    if block.get("set_aside"):
        line += " · " + str(len(block["set_aside"])) + " set aside"
    if block.get("held_back"):
        line += " (+" + str(block["held_back"]) + " not shown)"
    who = block.get("answered_by") or block.get("by")
    if who:
        line += " · by " + str(who)
    took = took_text(block)
    if took:
        line += " · " + took
    if block.get("missed"):
        line += " · " + block["missed"]
    return line


def before_she_speaks(conn, say=None):
    """Called once per turn, before the assistant's first call. Returns the
    block for its prompt, or None when the dropdown says none -- in which
    case the turn is exactly what it was before this file existed."""
    cfg = settings()
    key = cfg["model"]
    if not key:
        return None
    say = say or (lambda *a, **k: None)
    lines = last_lines(conn, cfg["lines"])
    timeout = cfg["timeout_s"]
    box = {}
    worker = threading.Thread(target=_run, args=(key, lines, cfg, box, timeout),
                              kwargs={"writer": cfg["writer"]}, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        block = _block(key, cfg, read=len(lines), took_s=timeout,
                       missed="it ran out of time at " + format(timeout, ".1f")
                              + " s, so the turn went ahead without it")
        block["late"] = True
        # The thread finishes on its own and writes the whole of what it
        # would have said into STATE['last'], marked late.
    else:
        block = box.get("out") or _block(key, cfg, read=len(lines),
                                         missed="it came back with nothing")
    say("automatic memory: " + summary(block), "recall", block)
    return block


def try_now(conn) -> dict:
    """The button under Developer: one run on the room as it stands, shown
    and not kept. Nothing in the store changes."""
    cfg = settings()
    if not cfg["model"]:
        return {"error": "no model is chosen"}
    return before_she_speaks(conn)


def status() -> dict:
    """What the header and the Developer section show."""
    refresh()
    cfg = settings()
    dflt = defaults()
    with LOCK:
        st = dict(STATE)
    m = KEYS.get(st["model"]) or {}
    try:
        config_text = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        config_text = "(could not read: " + str(exc) + ")"
    return {
        "model": st["model"],
        "label": m.get("label"),
        "about_gb": m.get("about_gb"),
        "phase": st["phase"],
        "detail": st["detail"],
        "writer": cfg["writer"],
        "writer_phase": st["writer_phase"] if cfg["writer"] else "off",
        "writer_detail": st["writer_detail"] if cfg["writer"] else "",
        "writers": writers(),
        "size_bytes": st["size_bytes"],
        "instance_id": st["instance_id"],
        "download": st["download"],
        "loading_for_s": (round(time.time() - st["loading_since"], 1)
                          if st["loading_since"] else None),
        "embedder": st["embedder"],
        "knobs": {n: cfg[n] for n in KNOBS},
        "knob_bounds": {n: {"min": k["min"], "max": k["max"],
                            "default": dflt[n]} for n, k in KNOBS.items()},
        "floor": search.floor_now(),
        "last": st["last"],
        "models": MODELS,
        "server": LM,
        "config_path": str(CONFIG_PATH),
        "config_text": config_text,
        "config_error": st["config_error"],
    }


if __name__ == "__main__":
    import sys
    conn = db.connect()
    try:
        cfg = settings()
        key = sys.argv[1] if len(sys.argv) > 1 else cfg["model"]
        if not key:
            print("no model chosen; pass a key:",
                  ", ".join(m["key"] for m in MODELS))
            raise SystemExit(1)
        lines = last_lines(conn, cfg["lines"])
        for ln in lines:
            print("  " + ln["who"] + ": " + " ".join(ln["text"].split())[:100])
        # Loaded here and now rather than on a thread: this process ends when
        # the print does, and a thread would end with it.
        _set(model=key)
        _switch(None, key)
        print("  " + STATE["phase"] + (": " + STATE["detail"] if STATE["detail"] else ""))
        box = {}
        _run(key, lines, cfg, box, cfg["timeout_s"])
        print(json.dumps(box["out"], ensure_ascii=False, indent=2))
    finally:
        conn.close()
