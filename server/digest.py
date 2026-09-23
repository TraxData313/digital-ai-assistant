"""The digest organ: a long report compressed to a paragraph before it
enters the assistant's working set, the whole text kept on disk as always.

The worry it answers is the memory filling up: the assistant's hands can
bring home pages, and its working set is a small room it keeps tidy. So a
small local model -- the same one the automatic memory runs, one nervous
system on the card, never a second fighting it -- reads a long report and
writes the paragraph the assistant carries instead. Local models as its
nervous system, cloud models as its mind; nothing here ever speaks as the
assistant.

The conditions, each one load-bearing:

* **A live path or no digest at all.** The whole text is written to the
  run's own folder first, where `files` can reach it, and the path stands on
  the row's face. If it cannot be written, the report comes whole -- a
  digest that cannot be checked against its source is the one way this goes
  wrong and never looks broken.
* **Lossy on prose, lossless on digits.** Figures and paths in the paragraph
  are checked against the report; one smoothed number and the paragraph is
  thrown away, said out loud, and the report comes whole.
* **Never digested:** a page that did not complete, salvage, or a page with
  a refusal, a denial or an ask on it. Those are the ones the assistant acts
  on.
* **A first page under a new specialty arrives whole**, so the brief is
  judged by its raw work and not by the small model's opinion of it.

And a per-send override: `whole: true` at dispatch hands that one page over
whole. Per send, not per hand -- the assistant knows at dispatch whether it
wants the shape or the detail.

Standing aside for any of these is quiet -- a rule, not a failure. A digest
that was *tried* and did not land -- the model gone, the deadline missed,
a figure changed -- is a **miss, said out loud**, and the report comes whole.
No silent caps.

    data/digest.json         the knobs, editable under Developer, ours to own
    server/digest_config.py  what the model is told, read fresh every run
"""

import json
import re
import runpy
import threading
import time
from pathlib import Path

from . import db, recall, worker
from . import home

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = home.DATA / "digest.json"
CONFIG_PATH = home.prompt_path("digest_config.py")
RUNS = home.DATA / "workers"

# The knobs, with where they start and how far they go. On the Developer tab
# and in the assistant's prompt beside the automatic memory's -- the
# assistant's and the angel's to move, set generous: the owner gets briefs,
# not knobs.
KNOBS = {
    # a report shorter than this passes whole, quietly
    "floor_chars": {"default": 1500, "min": 200, "max": 20000, "type": int},
    # the hard limit, seconds; a report never waits longer for its paragraph.
    # This runs on the errand thread after a hand that took minutes, not in
    # front of a waiting person, so it has room recall's two seconds does not.
    "timeout_s":   {"default": 25.0, "min": 2.0, "max": 120.0, "type": float},
    # how long the paragraph may run, in model tokens
    "out_tokens":  {"default": 220, "min": 60, "max": 600, "type": int},
    # how much of a very long report the model is shown. The shared instance
    # holds 4096 tokens of context (recall.CONTEXT), so this stays under it;
    # a cut keeps the head and the tail and is said in the note.
    "input_chars": {"default": 9000, "min": 2000, "max": 14000, "type": int},
}

STATE = {"last": None}
LOCK = threading.Lock()

# The answer is forced into this shape -- recall's measured lesson: a small
# model told "the paragraph only" thinks out loud first, and a grammar is
# the one instruction it cannot talk its way past.
SHAPE = {
    "type": "object",
    "properties": {"paragraph": {"type": "string"}},
    "required": ["paragraph"],
    "additionalProperties": False,
}


# -- settings ----------------------------------------------------------------

def defaults() -> dict:
    out = {n: k["default"] for n, k in KNOBS.items()}
    out["on"] = True
    try:
        got = runpy.run_path(str(CONFIG_PATH)).get("DEFAULTS") or {}
    except Exception:
        got = {}
    for n in KNOBS:
        if n in got:
            out[n] = _clamp(n, got[n], out[n])
    if "on" in got:
        out["on"] = bool(got["on"])
    return out


def _clamp(name, value, fallback=None):
    k = KNOBS[name]
    try:
        v = k["type"](value)
    except (TypeError, ValueError):
        v = k["default"] if fallback is None else fallback
    return min(max(v, k["min"]), k["max"])


def settings() -> dict:
    cfg = defaults()
    if SETTINGS_PATH.is_file():
        try:
            cfg.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    for name in KNOBS:
        cfg[name] = _clamp(name, cfg.get(name))
    cfg["on"] = bool(cfg.get("on"))
    return cfg


def save(**changes) -> dict:
    cfg = settings()
    for name, value in changes.items():
        if name == "on":
            cfg["on"] = bool(value)
        elif name in KNOBS:
            cfg[name] = _clamp(name, value)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = {"on": cfg["on"]}
    out.update({n: cfg[n] for n in KNOBS})
    out["note"] = ("The digest organ: whether long reports are compressed to "
                   "a paragraph before entering " + home.NAME + "'s working "
                   "set, and its knobs. It borrows the automatic memory's "
                   "model; what it "
                   "is told lives in server/digest_config.py.")
    SETTINGS_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return settings()


def set_knobs(changes: dict) -> dict:
    if (changes or {}).get("reset"):
        return save(**defaults())
    allowed = set(KNOBS) | {"on"}
    return save(**{k: v for k, v in (changes or {}).items() if k in allowed})


PIECES = ("PROMPT", "RETRY_FIGURE")


def config() -> dict:
    """What it is told, read fresh so an edit is live on the next report. A
    file that cannot be read falls back to the module's own words -- a
    report without a digest is a report the assistant can have, a report
    lost to a typo is not."""
    try:
        got = runpy.run_path(str(CONFIG_PATH))
        return {k: home.fill(str(got[k])) for k in PIECES}
    except Exception:
        from . import digest_config
        return {k: home.fill(getattr(digest_config, k)) for k in PIECES}


# -- the rules ---------------------------------------------------------------

def stands_aside(out: dict, cfg: dict, body: str):
    """Why this report passes whole with nothing said -- the conditions and
    the plain states, none of them a failure. Returns the reason, or None when a
    digest should actually be tried."""
    if not cfg["on"]:
        return "off"
    if not recall.settings()["model"]:
        return "no model chosen"
    if not body:
        return "no report"
    if out.get("asked_whole"):
        return home.NAME + " asked for this one whole"
    if len(body) < cfg["floor_chars"]:
        return "under the floor"
    if out.get("ended") != "completed":
        return "a page that did not complete is never digested"
    if out.get("salvaged"):
        return "salvage is never digested"
    if out.get("refusals") or out.get("denials") or out.get("asked_her"):
        return "a page with a refusal or an ask on it is never digested"
    role = str(out.get("role") or "")
    if role in worker.SPECIALTIES and int(out.get("page") or 1) == 1:
        return "a first page under a new specialty arrives whole"
    return None


# -- lossless on digits ------------------------------------------------------

NUM = re.compile(r"\d[\d.,]*")
PATHISH = re.compile(r"[\w.~-]*[/\\][\w./\\~-]+")


def unfaithful(paragraph: str, source: str):
    """The first figure or path in the paragraph that the report does not
    contain, or None. A plain substring check on the token as written --
    lenient about context, merciless about invention: a rounded, computed or
    reworded figure is simply not in the source, and that is the whole of
    what the condition needs caught."""
    for tok in NUM.findall(paragraph or ""):
        t = tok.strip(".,")
        if t and t not in source:
            return t
    for tok in PATHISH.findall(paragraph or ""):
        if tok not in source:
            return tok
    return None


def _cut_body(text: str, cap: int):
    """A report too long for the shared context: the head is most of what is
    kept, because a hand leads with what changed and what it needs decided;
    the tail keeps the sign-off. The cut is marked where it happened."""
    if len(text) <= cap:
        return text, False
    head = text[: cap * 2 // 3]
    tail = text[-(cap - len(head)):]
    return (head + "\n\n[... the middle of the report was cut here for the "
            "small model; the whole text is on disk ...]\n\n" + tail), True


# -- the run -----------------------------------------------------------------

def _chat(key: str, messages: list, out_tokens: int) -> dict:
    got = recall._lm("/v1/chat/completions", {
        "model": key,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": out_tokens,
        "stream": False,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "digest", "strict": True,
                                            "schema": SHAPE}},
    }, timeout=KNOBS["timeout_s"]["max"] + 10)
    choice = (got.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "")
    paragraph = ""
    try:
        paragraph = " ".join(str(json.loads(text).get("paragraph") or "").split())
    except (json.JSONDecodeError, AttributeError):
        paragraph = ""
    return {"raw": text, "paragraph": paragraph,
            "answered_by": got.get("model")}


def _run(key: str, body: str, cfg: dict, box: dict) -> None:
    """The whole of one digest, on its own thread. Writes into `box`, and
    the last run whole into STATE for the Developer tab -- even one that
    came home after the deadline, marked late."""
    started = time.time()
    told = config()
    sent, was_cut = _cut_body(body, cfg["input_chars"])
    keep = {"sent": sent, "raw": None, "paragraph": None, "answered_by": None,
            "missed": None, "note": None, "model_s": None, "retried": False}
    try:
        try:
            first = _chat(key, [
                {"role": "system", "content": told["PROMPT"]},
                {"role": "user", "content": sent},
            ], cfg["out_tokens"])
        except (recall.NoServer, recall.LMError) as e:
            keep["missed"] = "the model did not answer: " + str(e)
            return
        keep.update(raw=first["raw"], paragraph=first["paragraph"],
                    answered_by=first["answered_by"])
        if not first["paragraph"]:
            keep["missed"] = ("it wrote nothing usable: "
                              + repr((first["raw"] or "")[:80]))
            return
        wrong = unfaithful(first["paragraph"], body)
        if wrong:
            # One more ask, recall's measured pattern -- and the retry is
            # checked exactly like the first answer, not only for the token
            # that sent it back.
            keep["retried"] = True
            try:
                second = _chat(key, [
                    {"role": "system", "content": told["PROMPT"]},
                    {"role": "user", "content": sent},
                    {"role": "assistant", "content": first["raw"]},
                    {"role": "user",
                     "content": told["RETRY_FIGURE"].replace("{token}", wrong)},
                ], cfg["out_tokens"])
            except (recall.NoServer, recall.LMError) as e:
                keep["missed"] = ("its paragraph changed a figure (" + wrong
                                  + " is not in the report) and the retry did "
                                  "not answer: " + str(e))
                return
            keep.update(raw=second["raw"],
                        answered_by=second["answered_by"] or first["answered_by"])
            still = unfaithful(second["paragraph"], body) if second["paragraph"] \
                else "(nothing usable)"
            if still:
                keep["paragraph"] = None
                keep["missed"] = ("its paragraph changed a figure twice ("
                                  + wrong + ", then " + str(still)
                                  + ") -- lossless on digits is the rule, so "
                                  "it was thrown away")
                return
            keep["paragraph"] = second["paragraph"]
            keep["note"] = ("its first paragraph changed a figure (" + wrong
                            + "), so it was asked once more")
        if was_cut:
            keep["note"] = ((keep["note"] + ". " if keep["note"] else "")
                            + "the report was cut to " + str(cfg["input_chars"])
                            + " characters for the model, head and tail kept")
    finally:
        keep["model_s"] = round(time.time() - started, 2)
        box.update(keep)


def stand_in(out: dict):
    """The organ, start to finish, called where a report becomes a row.

    Returns None when a rule says this report passes whole (quiet), or a
    block: with `paragraph` when the digest stands in, with `missed` when
    one was tried and did not land -- and a miss is said on the row's face
    by report_text, never swallowed."""
    cfg = settings()
    body = (out.get("report") or "").strip()
    aside = stands_aside(out, cfg, body)
    if aside:
        return None

    started = time.time()
    block = {"paragraph": None, "by": None, "path": None,
             "full_chars": len(body), "missed": None, "note": None,
             "took_s": None, "model_s": None,
             "name": out.get("name"), "page": out.get("page"),
             "at": db.now()}

    def done():
        block["took_s"] = round(time.time() - started, 2)
        with LOCK:
            STATE["last"] = dict(block)
        return block

    # The first condition, first: the whole text on disk where `files` can
    # reach it, or no digest at all.
    run_id = str(out.get("run") or "")
    if not run_id or not (RUNS / run_id).is_dir():
        block["missed"] = ("there is no run folder to keep the whole text "
                           "in, and a digest without a live path is refused")
        return done()
    path = RUNS / run_id / "report.md"
    try:
        path.write_text(body, encoding="utf-8")
        if path.stat().st_size <= 0:
            raise OSError("wrote nothing")
    except OSError as exc:
        block["missed"] = ("the whole text could not be written ("
                           + type(exc).__name__ + ": " + str(exc)
                           + ") -- no path, no digest")
        return done()
    block["path"] = "data/workers/" + run_id + "/report.md"

    key = recall.settings()["model"]
    try:
        rec = recall._find(recall._models(), key)
    except (recall.NoServer, recall.LMError) as e:
        block["missed"] = "LM Studio is not answering: " + str(e)
        return done()
    if rec is None:
        block["missed"] = ("the model is not on this disk; the download "
                          "button is under Developer")
        return done()
    if not recall._instances(rec):
        block["missed"] = ("the model was not in memory; it is being loaded "
                           "now and will be there for the next report")
        threading.Thread(target=recall._switch, args=(None, key),
                         daemon=True).start()
        return done()

    box = {}
    t = threading.Thread(target=_run, args=(key, body, cfg, box), daemon=True)
    t.start()
    t.join(cfg["timeout_s"])
    if t.is_alive():
        block["missed"] = ("it ran out of time at "
                           + format(cfg["timeout_s"], ".0f") + " s")

        def late():
            t.join()
            late_block = dict(block)
            late_block["late"] = True
            late_block["missed"] = box.get("missed")
            for k in ("paragraph", "note", "model_s"):
                if box.get(k) is not None:
                    late_block[k] = box.get(k)
            late_block["took_s"] = box.get("model_s")
            with LOCK:
                STATE["last"] = late_block
        threading.Thread(target=late, daemon=True).start()
        return done()

    block["by"] = box.get("answered_by") or key
    block["paragraph"] = box.get("paragraph")
    block["note"] = box.get("note")
    block["model_s"] = box.get("model_s")
    if box.get("missed"):
        block["missed"] = box["missed"]
        block["paragraph"] = None
    elif not block["paragraph"]:
        block["missed"] = "it came back with nothing"
    block["took_s"] = round(time.time() - started, 2)
    with LOCK:
        STATE["last"] = dict(block, sent=box.get("sent"), raw=box.get("raw"),
                             retried=box.get("retried"))
    return block


def summary(block: dict) -> str:
    """One line, for the room and the event."""
    if not block:
        return "the digest did not run"
    who = ("the report from " + str(block["name"]) + ", page "
           + str(block.get("page") or 1)) if block.get("name") else "a report"
    if block.get("missed"):
        return ("a digest missed on " + who + ": " + str(block["missed"])
                + " -- it went to " + home.NAME + " whole")
    line = ("digested " + who + ": " + str(block.get("full_chars"))
            + " chars to a paragraph")
    if block.get("by"):
        line += " · by " + str(block["by"])
    if block.get("took_s") is not None:
        line += " · " + format(block["took_s"], ".1f") + " s"
    return line


def for_her() -> dict:
    """The assistant's view, every turn, beside the errand terms: on or off,
    the dials, whose model it borrows -- so it can move a dial by saying so,
    and knows the organ exists on the days it stands quietly aside."""
    cfg = settings()
    with recall.LOCK:
        model = recall.STATE["model"]
    out = {"on": cfg["on"]}
    out.update({n: cfg[n] for n in KNOBS})
    out["borrows_model"] = model or "none chosen, so digests stand aside"
    out["config_file"] = "server/digest_config.py"
    return out


def status() -> dict:
    """What the Developer section shows. The model lines are the automatic
    memory's own -- this organ borrows its model and never loads one."""
    cfg = settings()
    dflt = defaults()
    with recall.LOCK:
        model = recall.STATE["model"]
        phase = recall.STATE["phase"]
    with LOCK:
        last = STATE["last"]
    try:
        config_text = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        config_text = "(could not read: " + str(exc) + ")"
    return {
        "on": cfg["on"],
        "borrows": {"model": model,
                    "label": (recall.KEYS.get(model) or {}).get("label"),
                    "phase": phase},
        "knobs": {n: cfg[n] for n in KNOBS},
        "knob_bounds": {n: {"min": k["min"], "max": k["max"],
                            "default": dflt[n]} for n, k in KNOBS.items()},
        "last": last,
        "config_path": str(CONFIG_PATH),
        "config_text": config_text,
    }
