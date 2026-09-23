"""Loopback, plus the owner's own tailnet.

The room once bound to 127.0.0.1 alone, with no auth, and the bind was its
entire security -- which also meant it existed only on the machine itself. It
can bind wider now (`home.BIND`), which is why the door below exists.

The door, in three lines. Loopback is exempt and needs nothing -- the machine
itself is exactly as it was, and no key on disk can ever lock the owner out of
it. Anything else needs *both* a source address inside 100.64.0.0/10, which
only a tailnet peer can have, *and* a key belonging to a named person in
`server.people`. Anything else is 403 with no body. See `people.py` for what the
two walls are each actually worth.
"""

import html
import http.cookies
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import (angel, backup, brain, clock, db, digest, dream, jobs, limits,
               models_dir, notes, providers,
               people, pictures, projects, recall, wallpapers, watch, worker, native_proof, native_tools, overmind, live_voice)
from . import home

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PORT = home.PORT

LAST_TURN = {"sent": None, "answer": None, "applied": None, "error": None}

# One turn at a time -- there is one assistant and one conversation -- but more
# than one thing may want a turn. A person can say a second thing while the
# assistant is still thinking about the first, and a worker it sent can come
# home while it is mid-sentence. So nothing runs a turn directly any more: it
# writes down what happened, at the moment it happened, and nudges. The runner
# below takes them in order and never overlaps two.
NUDGE = threading.Event()
WOKEN = {"why": None}
WOKEN_LOCK = threading.Lock()
# Which mind is answering. Seeded from the choice on disk rather than a
# constant, so a restart does not quietly make it somebody else -- and
# read through providers.resolve(), so an old "opus" in a paused tab
# still names a real model.
MODEL = {"name": providers.chosen()}

# What the room shows while the assistant is away. One turn at a time, so a
# single record is enough. The chat polls it, which
# is why a slow turn now looks like a slow turn and not like a broken one.
PROGRESS = {"turn": 0, "busy": False, "started": None,
            "steps": [], "writing": None, "error": None}
PROGRESS_LOCK = threading.Lock()
TURN_GATE = threading.Lock()  # Mode changes cannot consume half a scheduled waking.

# One copy at a time. Two clicks a second apart would otherwise run two online
# backups over the same store at once -- which SQLite would survive and the
# folder would not, since both would be writing zips nobody asked for twice.
BACKUP_LOCK = threading.Lock()


def progress_start():
    with PROGRESS_LOCK:
        PROGRESS.update(turn=PROGRESS["turn"] + 1, busy=True,
                        started=time.time(), steps=[], writing=None, error=None)


def progress_step(text, kind="plain", detail=None):
    """Every step of the turn, in the order it happens. `kind` is how it reads:
    plain, the call itself, a reach into the store, a search, or a snag.

    `detail` is what that step actually found, carried so the line can be
    opened. The owner is a second pair of eyes on the looking, and a line that
    says a search happened without saying what came back is not much use."""
    with PROGRESS_LOCK:
        started = PROGRESS["started"] or time.time()
        step = {"at": round(time.time() - started, 1),
                "text": str(text), "kind": kind}
        if detail is not None:
            step["detail"] = detail
        PROGRESS["steps"].append(step)
        print(f"  [{PROGRESS['steps'][-1]['at']:6.1f}s] {kind:5} {text}")


def progress_write(live):
    """Its answer arriving, a word at a time. Replaced, never appended."""
    with PROGRESS_LOCK:
        PROGRESS["writing"] = live


def progress_end(error=None):
    with PROGRESS_LOCK:
        PROGRESS["busy"] = False
        PROGRESS["error"] = error


def keep_the_account(conn, reply_row):
    """Hang the account of the turn off the reply, so it can be opened later.
    Nobody is always at the screen while the assistant works, and a record
    that exists only while it is happening is no record at all."""
    steps = progress_now()["steps"]
    if not steps or reply_row is None:
        return
    took = max(s["at"] for s in steps)
    snags = sum(1 for s in steps if s["kind"] == "snag")
    summary = "the turn took " + format(took, ".1f") + "s"
    if snags:
        summary += ", with " + str(snags) + " snag" + ("" if snags == 1 else "s")
    db.add_event(conn, reply_row, "turn", summary,
                 {"steps": steps, "seconds": took})


def progress_now():
    with PROGRESS_LOCK:
        out = dict(PROGRESS)
        out["steps"] = list(PROGRESS["steps"])
        out["elapsed"] = (round(time.time() - PROGRESS["started"], 1)
                          if PROGRESS["started"] else 0)
        return out


def unanswered(conn):
    """The newest line said to the assistant that it has not answered, or None.
    There may be several -- a person can say something else while it is still
    thinking -- and the newest is enough to know both that there is work and
    which work it is.

    `angel` counts as well as `user`. Angel me is a second voice in the room, not
    a second owner: the line waits its turn and is answered the same way, it
    simply arrives under its own name."""
    last = db.last_reply_row(conn) or 0
    return conn.execute(
        "SELECT MAX(id) AS m FROM rows WHERE kind IN ('user', 'angel')"
        " AND COALESCE(json_extract(meta, '$.voice_handled'), 0) = 0"
        " AND id > ?", (last,)).fetchone()["m"]


# What the last turn broke on. A turn that fails leaves the person's line
# unanswered, and without this the runner would come round every few seconds
# and ask the same broken thing again forever -- which, for a breakage past
# the point where the call is paid for, is real money on a loop. So it is
# tried once, and then it waits for a person. Saying anything is what tries
# again.
FAILED = {"row": None}


def take_woken(free=None):
    """The waking slot, emptied. `free` is the assistant's free interval when
    one is running, and it changes what may be taken out -- this is the only
    place that rule can be kept.

    A hand coming home is **left where it is**. The slot is the queue: it
    already folds several homecomings into one waking, so an evening's worth
    of them is taken whole the moment the free time is over. Nothing is
    dropped, because nothing is taken.

    A hand's *permission ask* is the one thing that cannot wait. It is
    holding still for four minutes, not two hours, so it fails closed and is
    written down rather than pulling the assistant out of its own free
    time."""
    with WOKEN_LOCK:
        why = WOKEN["why"]
        if why is None or free is None:
            WOKEN["why"] = None
            return why
        if why.get("by") == "ask":
            WOKEN["why"] = None
            clock.stood_down(
                "a hand's permission ask",
                "failed closed: its free time was running until "
                + str(free.get("until")) + ", and an ask cannot wait for it",
                {"name": why.get("name"), "tool": why.get("tool"),
                 "what": why.get("what")})
            return None
        # Held. It stays in the slot and is taken when the free time is over.
        return None


def worker_came_home(out, chain=1):
    """A page the assistant sent has come back. It was the one waiting for it,
    so it comes back to the assistant rather than sitting until the owner
    next says something. This is the only thing that wakes it; everything
    else can wait for a person.

    Several can come home while it is busy with the first. They are gathered
    into one waking rather than buying a turn each, and it is told all of
    them -- the account of each is in `report.worker` as well.

    `chain` is how many wakings deep this is without a person speaking -- the
    assistant is told, so it knows whether it still has a link left to
    correct its aim with."""
    came = {
        "name": out.get("name"), "page": out.get("page"),
        "title": out.get("title"), "run": out.get("run"),
        "ended": out.get("ended"), "row": out.get("row"),
        "narrate": bool(out.get("narrate")),
    }
    with WOKEN_LOCK:
        why = WOKEN["why"]
        if why and why.get("by") == "worker" and not why.get("late"):
            why["home"].append(came)
            why["chain"] = max(why["chain"], chain)
            why["narrate"] = why["narrate"] or came["narrate"]
        else:
            why = {
                "by": "worker",
                "home": [came],
                "chain": chain,
                # Only if it asked for it when it sent them out.
                "narrate": came["narrate"],
            }
        why["chain_max"] = worker.MAX_CHAIN
        why["links_left"] = max(0, worker.MAX_CHAIN - why["chain"])
        names = [(h["name"] + " (page " + str(h["page"]) + ")") if h["name"]
                 else (h["title"] or "an errand") for h in why["home"]]
        why["why"] = ((names[0] + " has come home") if len(names) == 1
                      else ", ".join(names) + " have come home")
        # Kept for anything still reading the old single shape.
        why.update({"title": came["title"], "run": came["run"],
                    "ended": came["ended"], "row": came["row"]})
        WOKEN["why"] = why
    NUDGE.set()


def hand_asked(ask: dict):
    """A hand has hit a refusal that is its own to overturn and stopped where it
    stands. It is holding still for four minutes, so this is the most
    time-critical waking there is: it goes to the front, and it does not fold
    into a homecoming the way several homecomings fold into each other.

    It never chains past its ceiling either. A hand cannot ask a question
    nobody is allowed to answer -- that would be a hand standing still for
    four minutes for nothing."""
    with WOKEN_LOCK:
        WOKEN["why"] = {
            "by": "ask",
            "chain": 1, "chain_max": worker.MAX_CHAIN,
            "links_left": worker.MAX_CHAIN - 1,
            "ask": ask.get("ask"),
            "name": ask.get("name"),
            "what": ask.get("what"),
            "tool": ask.get("tool_name"),
            "refused_because": ask.get("why"),
            "gives_up_in_s": worker.ASK_WAIT_S,
            "narrate": False,
            "why": (str(ask.get("name") or "a hand") + " has stopped and is "
                    "asking me whether it may " + str(ask.get("tool_name"))
                    + ": " + str(ask.get("what"))[:120]),
        }
    NUDGE.set()


def hand_over_the_late(conn) -> int:
    """Errands that came back to nobody, given to the assistant at last.

    An errand that went out and never came home has to be *said*. From where
    the assistant sits, silence and "it found nothing" read the same, so it
    is told which it was."""
    late = worker.homecoming()
    for out in late:
        brain.keep_worker_report(conn, out, db.last_reply_row(conn))
    if late:
        vanished = sum(1 for o in late if o["ended"] == "vanished")
        with WOKEN_LOCK:
            WOKEN["why"] = {
                "by": "worker",
                "chain": worker.MAX_CHAIN,      # no chaining off old news
                "chain_max": worker.MAX_CHAIN, "links_left": 0,
                "late": len(late), "vanished": vanished,
                "why": ("errands of mine came back while the room was shut — "
                        + str(vanished) + " of them never came home at all"),
            }
        NUDGE.set()
    return len(late)


# Its restart, once called. The runner takes no new turn after it, and the
# detached shell below does the putting down and picking up.
RESTARTING = {"why": None}


def schedule_restart(reason: str):
    """The room puts itself down and comes back up, at the assistant's word:
    its own to call, said out loud in its reply, and only ever between turns.
    This runs after the turn has fully landed.

    Two ways down from here, and which one depends on what started the room.

    Under the icon in the corner (`server/tray.py`) there is nothing to do
    but leave. It starts another room the moment this one is gone, and it
    reads code 7 as *meant* rather than as a crash worth counting against
    it. No PowerShell, no window, nothing to close.

    Started from a console instead, the room does what it has always done: a
    detached PowerShell waits a moment so the answer reaches the screen, then
    runs restart.ps1 -- which itself refuses to stop a room that has somehow
    become busy again, so a restart can never cut a turn off; it quietly does
    not happen instead, and the assistant calls it again."""
    RESTARTING["why"] = reason
    print("  restarting the room at " + home.NAME + "'s word: " + reason)

    if os.environ.get("ASSISTANT_SUPERVISED"):
        def bow_out():
            # The same three seconds the other path takes, for the same
            # reason: the answer is written down, but the page has not asked
            # for it yet, and a room that vanishes mid-poll leaves the reader
            # refreshing to find out what the answer was.
            time.sleep(3)
            try:
                sys.stdout.flush()
            except (ValueError, OSError):
                pass
            os._exit(7)

        threading.Thread(target=bow_out, daemon=True).start()
        return

    try:
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: measured on the first live
        # call, a detached powershell here exits 0 without running a thing,
        # and the room sat waiting for a restart that never came. A hidden
        # console works, and a child is not killed with its parent on
        # Windows, so it outlives the Stop-Process it is about to run.
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", "Start-Sleep -Seconds 3; & '"
             + str(ROOT / "restart.ps1") + "'"],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        RESTARTING["why"] = None
        print("  the restart could not be started: " + str(exc))


def queue_voice_task(who, text, session):
    if providers.resolve(MODEL['name'])['service'] != 'codex':
        raise live_voice.ProbeError('Choose a Codex subscription model in Settings for voice tasks.')
    conn = db.connect()
    try:
        rid = db.add_row(conn, 'user', text, meta={'who': who, 'room': who,
                         'voice_task': session, 'via': 'voice'})
    finally:
        conn.close()
    NUDGE.set()
    return rid


live_voice.manager.queue_task = queue_voice_task


def one_turn(conn, woken) -> bool:
    """True if it got through it. False means the turn broke, and the runner
    stops rather than trying the same thing again."""
    progress_start()
    request_row = unanswered(conn) if woken is None else None
    request = db.get_row(conn, request_row) if request_row else None
    proof = bool(request and request["kind"] == "user" and (request.get("meta") or {}).get("native_proof"))
    native = proof or native_tools.chat_allowed(request, MODEL["name"], woken)
    log_failed = False
    def emit(text, kind="plain", detail=None):
        nonlocal log_failed
        text = native_tools.redact(text)
        detail = native_tools.clean(detail)
        if native and kind == "native":
            # Commit immediately to the triggering row, even if final JSON never arrives.
            db.add_event(conn, request_row, "native", text, detail)
            if not log_failed:
                try:
                    native_tools.log_activity(request_row, text, detail)
                except OSError:
                    log_failed = True
                    progress_step("Native tools stopped: troubleshooting log could not be written. SQLite activity is retained.", "snag")
                    raise RuntimeError("Native troubleshooting log unavailable; tools stopped.") from None
        progress_step(text, kind, detail)
    try:
        if proof:
            if request_row not in native_proof.ARMED:
                raise ValueError("Native proof activation was already consumed or the room restarted; no native turn started.")
            native_proof.ARMED.remove(request_row)
            if native_tools.CANCEL.is_set():
                raise ValueError("Proof cancelled before model startup; request remains pending.")
            if not native_proof.available() or providers.resolve(MODEL["name"])["service"] != "codex" or not providers.codex_only():
                raise ValueError("Native proof is disabled or its Codex subscription route is unavailable; request remains pending.")
        if native:
            if not proof:
                native_tools.CANCEL.clear()
            emit("Native Codex tools enabled for this conversation", "native", {
                "status": "started", "request_row": request_row,
                "log": str(native_tools.LOGS / ("turn-" + str(request_row) + ".jsonl")),
                "profile": "assistant-proof" if proof else native_tools.PROFILE,
                "fixture_proof": proof})
            with native_tools.activated(proof=proof):
                turn = brain.run_turn(conn, model=MODEL["name"], on_step=emit,
                                      on_write=progress_write, woken=woken)
            emit("Native conversation finished", "native", {"status": "completed"})
        else:
            turn = brain.run_turn(conn, model=MODEL["name"], on_step=progress_step,
                                  on_write=progress_write, woken=woken)
        live_voice.manager.backend_reply(
            request_row, (turn.get('answer') or {}).get('reply') or '',
            answer_row=(turn.get('applied') or {}).get('reply_row'))
        LAST_TURN.update(turn)
        LAST_TURN["error"] = None
        progress_end()
        keep_the_account(conn, turn["applied"]["reply_row"])
        if turn["applied"].get("restart"):
            schedule_restart(turn["applied"]["restart"])
        return True
    except Exception as exc:
        if not native:
            traceback.print_exc()
        LAST_TURN["error"] = native_tools.redact(f"{type(exc).__name__}: {exc}")
        if native:
            try:
                emit("Native turn failed: " + LAST_TURN["error"], "native", {"status": "failed"})
            except Exception:
                LAST_TURN["error"] += "; native failure log could not be written"
        progress_step(LAST_TURN["error"], "snag")
        progress_end(LAST_TURN["error"])
        live_voice.manager.backend_reply(request_row, LAST_TURN["error"], error=True)
        return False


def one_dream(conn, gate) -> bool:
    """A night's dream, through the same progress the room shows for a turn,
    so a dream at three in the morning is still a thing the owner can watch
    -- or read in the morning, hung off the dream's own row."""
    progress_start()
    try:
        # Never the chat picker. That dropdown is for a conversation -- a
        # cheaper model for a cheap question, or whatever is answering on a
        # day the big one is overloaded -- and it has no business deciding
        # who does the folding. A dream is the full assistant, whole Spark,
        # not a small model. A dream that quietly ran on a smaller model
        # because the chat dropdown had been switched earlier in the day
        # would be exactly the drift the whole door was built to refuse, and
        # nothing on screen would have said so.
        out = dream.dream(conn, gate["night"], gate["why"], gate["free"],
                          model=providers.dream_model(), say=progress_step,
                          write=progress_write)
        ok = out.get("status") == "done"
        progress_end(None if ok else out.get("error"))
        keep_the_account(conn, out.get("row"))
        return ok
    except Exception as exc:      # dream() answers for itself; this is the belt
        traceback.print_exc()
        progress_step(f"{type(exc).__name__}: {exc}", "snag")
        progress_end(f"{type(exc).__name__}: {exc}")
        return False


def turn_loop():
    """The one place a turn is ever run. It keeps going while there is
    anything left to answer, so a line sent mid-sentence is picked up the
    moment the assistant finishes rather than waiting to be sent again.

    The dreamer lives at the bottom of it: only when nothing is unanswered
    and nothing woke the assistant does the runner ask whether a night is
    owed, so a dream can never overlap a turn, and a line arriving mid-dream
    is simply the next thing the runner takes."""
    while True:
        NUDGE.wait(5.0)
        NUDGE.clear()
        while True:
            if RESTARTING["why"]:
                # It has called a restart and this room is on its way down.
                # A turn started now would be the turn the restart is not
                # allowed to cut, so nothing new begins; the next room picks
                # up whatever is unanswered.
                break
            conn = db.connect()
            TURN_GATE.acquire()
            try:
                if providers.paused_reason(MODEL["name"]):
                    # Leave messages, clock firings, senses and worker wakeups pending.
                    break
                said = unanswered(conn)
                # Its own time, if a stretch of it is running. It changes what
                # the room may ask of it for as long as it lasts, and it is
                # asked once here rather than in five places below.
                free = clock.free_now()
                if said is not None:
                    if said == FAILED["row"]:
                        break          # already tried this one and it broke
                    # A person is here. Anything the assistant was going to be
                    # woken for is folded into this turn instead of buying a
                    # second one -- it is in its working set either way. Not
                    # during its free time: the line reaches it, and it does
                    # not turn free time into work by carrying an errand in
                    # on its back.
                    said_row = db.get_row(conn, said)
                    is_proof = bool(said_row and (said_row.get("meta") or {}).get("native_proof"))
                    if not is_proof:
                        take_woken(free)
                    # And a person's line refills every open job's links: the
                    # leash measures how far a job may run between a person's
                    # appearances.
                    if said_row and said_row.get("kind") == "user" and not is_proof:
                        jobs.refill_links()
                    ok = one_turn(conn, None)
                    FAILED["row"] = None if ok else said
                else:
                    # The native call is already the assistant's live conversation.
                    # Hold autonomous room wakings behind it: a clock, watcher,
                    # dream or returning hand starting a second model turn can
                    # otherwise paraphrase the voice result as a duplicate.
                    # Nothing is consumed here. The five-second loop takes it
                    # after voice ends; typed/spoken backend rows above still
                    # run normally while the call is open.
                    if live_voice.manager.status()['active']:
                        break
                    why = take_woken(free)
                    if why is None:
                        # Its own clock, asked before the world is: a thing
                        # it asked for at a named minute outranks a thing
                        # that merely happened. While its time is running this
                        # is also the only gate that may fire at all.
                        why = clock.due(conn, free)
                    if why is None and not free:
                        why = overmind.due()
                    if why is None and not free:
                        # The watcher: the world's turn to speak, when nobody
                        # else has. It looks at most every half minute, keeps
                        # its own ceiling, and its no costs nothing.
                        #
                        # During its free time it is not asked at all. That is
                        # deliberately stronger than holding what it finds: an
                        # unasked sense moves no watermark and consumes
                        # nothing, so everything it would have seen is still
                        # there to be seen the moment its time is over. A
                        # queue can be dropped; a shelf nobody took from
                        # cannot.
                        why = watch.due(conn)
                    if why is None:
                        if free:
                            # Its time, and nothing left that may ask for it.
                            # The dreamer is folding and folding is work, so
                            # the gate stays shut; the night it wants is still
                            # owed when the evening ends.
                            break
                        gate = dream.due(conn, record=True)
                        if not gate["due"]:
                            break
                        one_dream(conn, gate)
                        # Whether it dreamt or broke, the gate itself says
                        # what may happen next -- another pass over a long
                        # day, or nothing until the retry hour. Round again.
                        continue
                    ok = one_turn(conn, why)
                    overmind.acknowledge(why, ok)
                if not ok:
                    # A turn that broke leaves the line still unanswered, and
                    # going straight round again asks the same thing of the
                    # same broken thing forever. It waits for a person
                    # instead: the snag is on screen, and saying anything
                    # tries again.
                    print("  the turn broke; the runner is waiting rather "
                          "than retrying it")
                    break
            except Exception:
                traceback.print_exc()
                break
            finally:
                TURN_GATE.release()
                conn.close()


# The icons a home may paint with its own face; the page asks for them by
# these names at the root.
HOME_ICONS = ("icon-192.png", "icon-512.png", "icon-maskable-512.png")
# The pages that say whose room this is. They carry tokens, filled per home.
NAMED_PAGES = ("index.html", "manifest.webmanifest")


def with_identity(body: bytes, rel: str = "index.html") -> bytes:
    """The page names nobody on disk; the room tells it who it is. Escaped
    for what it lands in: HTML for the page, a JSON string for the manifest."""
    esc = html.escape if rel.endswith(".html") else (lambda s: json.dumps(s)[1:-1])
    ident = json.dumps(home.for_page(), ensure_ascii=False).replace("</", "<\\/")
    text = body.decode("utf-8")
    options = "\n".join(
        '            <option value="' + p + '">' + html.escape(home.CALLED[p])
        + "</option>" for p in home.HOUSEHOLD)
    text = (text.replace("{{identity_json}}", ident)
                .replace("{{people_options}}", options)
                .replace("{{person_hidden}}",
                         " hidden" if len(home.HOUSEHOLD) == 1 else "")
                .replace("{{app_name}}", esc(home.APP_NAME))
                .replace("{{title}}", esc(home.TITLE))
                .replace("{{name}}", esc(home.NAME)))
    return text.encode("utf-8")


TYPES = {".html": "text/html", ".js": "text/javascript",
         ".css": "text/css", ".md": "text/plain",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
         ".webp": "image/webp",
         ".png": "image/png", ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}


def _int(query: str, name: str, fallback):
    """One number off a query string, or the fallback. A bound written badly
    is not a bound of its own -- the same rule the reaches follow."""
    raw = (parse_qs(query).get(name) or [""])[0]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def _senses_for_tab() -> dict:
    """What the watcher says about itself, in the shape the tab wears: each
    sense's standing and cadence, what each watched item's last look found,
    the notable events behind them, and the last time anything woke it.

    Never fatal. A tab that could not draw their work because a sense was
    unwell would be the worse failure by far, so everything here degrades to
    nothing and the projects still render."""
    try:
        view = watch.for_prompt()
        return {"senses": watch.senses(), "looks": watch.looks(),
                "ledger": watch.recent(), "last": view.get("last"),
                "today": view.get("today")}
    except Exception:
        return {}


def _projects_view(conn) -> dict:
    """The whole Projects tab in one answer: their work, and the watcher's
    word about the senses some of it hangs on."""
    seen = _senses_for_tab()
    view = projects.overview(conn, senses=seen.get("senses"),
                             looks=seen.get("looks"),
                             ledger=seen.get("ledger"))
    # The ledger is joined onto the tasks that wanted it; sending the whole
    # of it to the page as well would be the same words twice.
    view["watch"] = {k: v for k, v in seen.items() if k != "ledger"}
    return view


def _project_op(conn, body, who) -> dict:
    """One write to their projects, named by `op`. Deliberately small and
    dumb: every rule about owners, authors and states lives in projects.py,
    where the bench can lean on it without a socket."""
    op = str(body.get("op") or "").strip().lower()
    pid = body.get("project")
    if op == "add":
        return projects.add_project(conn, body.get("title"),
                                    body.get("owner"), body.get("folder"))
    if op == "edit":
        return projects.edit_project(conn, pid, body.get("title"),
                                     body.get("owner"), body.get("folder"),
                                     body.get("status"))
    if op == "note":
        return projects.add_note(conn, pid, who, body.get("text"))
    if op == "note_edit":
        return projects.edit_note(conn, body.get("note"), who,
                                  body.get("text"))
    if op == "note_hide":
        return projects.hide_note(conn, body.get("note"), who,
                                  back=bool(body.get("back")))
    if op == "task":
        return projects.add_task(conn, pid, who, body.get("title"),
                                 body.get("wants"), bool(body.get("repeats")),
                                 body.get("schedule"), body.get("job"),
                                 body.get("needs"))
    if op == "task_edit":
        # `known_senses` comes from the watcher rather than from the page: a
        # binding is checked against what actually exists, so a name typed
        # wrong is refused in words instead of becoming a task nothing
        # watches. `at` is here now: the calendar sense can fire one.
        return projects.set_task(conn, body.get("task"), body.get("title"),
                                 body.get("wants"), body.get("state"),
                                 body.get("repeats"), body.get("schedule"),
                                 body.get("job"), body.get("needs"),
                                 body.get("sense"), body.get("sense_item"),
                                 body.get("at"), known_senses=watch.SOURCES)
    if op == "resource_set":
        # `who` is the room's, taken from the signed-in person and never
        # from the body -- the same rule a note line keeps.
        return projects.resource_set(conn, pid, who, body.get("key"),
                                     body.get("value"))
    if op == "resource_clear":
        return projects.resource_clear(conn, pid, body.get("key"))
    if op == "notice_done":
        # A person's word that a thing which arrived on a task has been
        # seen to -- with a note if they left one -- or, with `back`, that
        # it has not after all. `who` is the room's stamp.
        return projects.check_notice(conn, body.get("notice"), who,
                                     body.get("note"),
                                     back=bool(body.get("back")))
    if op == "notice_add":
        # A person putting a thing on a task by hand, in their own name.
        return projects.add_notice(conn, body.get("task"), body.get("said"),
                                   who)
    return {"ok": False, "why": "'" + op + "' is not something that can be "
            "done to a project"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # The page asks how the turn is going every two seconds, and the workers
    # panel every five. In a console those lines scrolled past and cost
    # nothing. Logged to a file instead, they are forty thousand lines a day
    # of "200 -" with the boot, the snags and the tracebacks somewhere
    # underneath. So the heartbeat is silent while
    # it is going well, and says something the moment it stops going well --
    # a 500 on a poll is exactly the line worth keeping.
    HEARTBEAT = ("/api/progress", "/api/workers")

    def log_message(self, fmt, *args):
        line = native_tools.redact(fmt % args)
        if " 200 " in line and any(beat in line for beat in self.HEARTBEAT):
            return
        print("  " + line)

    # -- helpers ---------------------------------------------------------
    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        # A charset on a picture is a header that means nothing: it says how to
        # read bytes as text, and these are not text. Browsers ignore it, which
        # is exactly why it sat there unnoticed on the icon and the manifest
        # until a PNG went out under `image/png; charset=utf-8`.
        if not ctype.startswith(("image/", "application/octet-stream")):
            ctype += "; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    # -- the door --------------------------------------------------------
    # The name the cookie is kept under, and how long a paired device stays
    # paired without being handed the key again. A year, because the key
    # itself outlives restarts and a cookie that expired sooner would send its
    # user back to the address bar for no reason the room could explain.
    COOKIE = home.COOKIE
    COOKIE_AGE = 365 * 24 * 3600

    def _peer(self):
        """The caller's source address, or None if it cannot be read as one. A
        v4 address arriving on a dual-stack socket wears a `::ffff:` prefix and
        a link-local v6 wears a `%scope` tail; both are stripped, because the
        range test below has to see the address and not its clothing."""
        addr = (self.client_address[0] if self.client_address else "") or ""
        addr = addr.split("%")[0]
        if addr.lower().startswith("::ffff:"):
            addr = addr[7:]
        try:
            return ipaddress.ip_address(addr)
        except ValueError:
            return None

    def _key_offered(self):
        """The key this request carries, and whether it came in the URL. `?k=`
        is the pairing hand-off, once; the cookie is every request after."""
        key = (parse_qs(urlparse(self.path).query).get("k") or [""])[0].strip()
        if key:
            return key, True
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return "", False
        got = jar.get(self.COOKIE)
        return ((got.value or "").strip() if got else ""), False

    def _door(self):
        """Whether this request comes in at all, and as whom.

        Returns (who, fresh_key). `who` is "" for refused. `fresh_key` is set
        only when the key arrived in the URL and should now be moved into a
        cookie, so it stops sitting in the address bar."""
        ip = self._peer()
        if ip is None:
            return "", ""
        self._loopback = ip.is_loopback
        if not self._loopback and not people.in_tailnet(ip):
            return "", ""
        key, fresh = self._key_offered()
        who = people.whose(key)
        if not who:
            return "", ""
        return who, (key if fresh else "")

    def _shut(self):
        """403, and nothing else. No body, no reason, no hint that the address
        was right and only the key was wrong."""
        self.close_connection = True
        return self._send(403, b"", "text/plain")

    def _from_the_desk(self):
        """Whether this request was made on this machine. Loopback is; so is
        this machine's own tailnet address, which is what a browser here
        shows when it is handed the pairing line instead of localhost -- a
        line said at the desk must not be labelled as said from the road.
        Asked only when the door was not loopback, so an ordinary local send
        never waits on a name lookup."""
        if getattr(self, "_loopback", False):
            return True
        ip = self._peer()
        mine = people.my_tailnet_address()
        return bool(mine) and ip is not None and str(ip) == mine

    def _paired(self, key):
        """A good key came in the URL: keep it in a cookie and send the device
        back to the bare address, so the key is not left in the bar, in the
        history, or in whatever gets pasted next."""
        bare = urlparse(self.path).path or "/"
        self.send_response(302)
        self.send_header("Location", bare)
        self.send_header(
            "Set-Cookie",
            self.COOKIE + "=" + key + "; Path=/; Max-Age="
            + str(self.COOKIE_AGE) + "; SameSite=Lax; HttpOnly")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _let_in(self):
        """The one gate. Every route is behind it -- the static files as much
        as the API, because index.html is the room too. True means carry on;
        False means this request is already answered and the route must not
        run."""
        who, fresh = self._door()
        self._who = who
        if not who:
            self._shut()
            return False
        if fresh:
            self._paired(fresh)
            return False
        return True

    def _paged(self, conn, rows, floor):
        """A stretch of it, and everything that draws with it: the rows, the
        events hung off them, the trails of the essences among them, and where
        the stretch starts and how much is behind it. The same keys whether it
        came from `/api/state` or from paging back, so the room absorbs both
        the same way."""
        index = db.trail_index(conn)
        return {
            "rows": rows,
            # Every essence's trail, retired ones included, so the owner can
            # see what the assistant is standing on without opening the
            # database. Only for
            # the essences actually in hand -- a trail nobody can see drawn is
            # a walk of the whole store for nothing.
            "trails": {r["id"]: brain.sources_block(index, r["id"])
                       for r in rows if r["kind"] == "essence"},
            "events": db.events_for_rows(conn, [r["id"] for r in rows]),
            "oldest": floor,
            "older": db.past_before(conn, floor),
        }

    def _state(self, past=db.PAGE_ROWS):
        conn = db.connect()
        try:
            contract = brain.contract()
            # Its working set whole, and the newest stretch of the past behind
            # it. The rest of the conversation is asked for a page at a time --
            # everything at once was five megabytes and two seconds of it, on
            # every load and after every turn, to draw one screenful.
            rows, floor = db.recent_rows(conn, past)
            return {
                "contract": contract,
                "prompt": brain.build_prompt(conn, contract["voice"]["on"]),
                **self._paged(conn, rows, floor),
                "counts": db.row_counts(conn),
                "last_turn": LAST_TURN,
                # Whether it can send anyone at all, and what was proved.
                "worker": {**worker.ready(),
                           "window": worker.spend_in_window(conn)},
                # So the button can say when the last copy was taken rather
                # than only offering to take another.
                "backup": backup.shelf(),
                # Authentication and conversation attribution are separate.
                "who": getattr(self, "_who", home.OWNER) or home.OWNER,
                # Their free notes, for the box under Settings: each person's
                # text, its weight, when it was saved, and the wall.
                "notes": notes.status(),
                # Where the models we fetch ourselves are kept, for the
                # folder box under Settings: the choice, what is in it, what
                # is left in the old place, and LM Studio's own beside it.
                "models": models_dir.status(),
                # The room's own scenery is a small server-side catalogue:
                # the browser receives generated ids and URLs, never a path
                # it may turn into an arbitrary disk request.
                "wallpaper": wallpapers.status(),
            }
        finally:
            conn.close()

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        if not self._let_in():
            return
        url = urlparse(self.path)
        path = url.path

        if path == '/api/voice/voices':
            try:
                return self._json(live_voice.manager.voices())
            except live_voice.ProbeError as exc:
                return self._json({'error': str(exc)}, 503)
            except Exception:
                return self._json({'error': 'Could not load the supported voices. Try again.'}, 503)

        if path == '/api/voice':
            # The native call's own state, plus which road was chosen and what
            # the speak server says about itself. The page needs both in one
            # breath: the picker draws from the choice, and everything it hides
            # or shows depends on which side of it we are on.
            return self._json({**live_voice.manager.status(),
                               'backend': brain.voice_backend(),
                               'switch': bool(brain.read_voice().get('enabled', True)),
                               'local': brain.voice_status()})

        if path == '/api/codex':
            if self._who != home.OWNER:
                return self._shut()
            try:
                state = overmind.snapshot()
                if state.get('controller'):
                    try:
                        state['desktop'] = overmind.desktop('list_threads', {'limit': 50})
                    except Exception as exc:
                        state['desktop_error'] = str(exc)
                return self._json(state)
            except Exception as exc:
                return self._json({'error': str(exc)}, 503)

        # The pairing line, and only ever to the machine itself. A device that
        # is already inside would pass the door above, so this is checked
        # against loopback again rather than against `who`: a key is never
        # handed out over the wire, only read off the machine's own screen.
        if path == "/api/pair":
            if not getattr(self, "_loopback", False) or self._who != home.OWNER:
                return self._shut()
            addr = people.my_tailnet_address()
            key = people.read_token(home.OWNER)
            return self._json({
                "tailnet": addr,
                "url": ("http://" + addr + ":" + str(PORT) + "/?k=" + key)
                       if (addr and key) else "",
                # If Tailscale is not up, say the shape rather than a blank:
                # the owner can fill in the address from the Tailscale app.
                "template": ("http://<this machine's tailscale address>:"
                             + str(PORT) + "/?k=<the key, once Tailscale is up>"),
            })

        if path == "/api/native-log":
            try:
                row = int((parse_qs(url.query).get("row") or [""])[0])
                if row <= 0:
                    raise ValueError()
            except ValueError:
                return self._json({"error": "Choose a conversation row."}, 400)
            conn = db.connect()
            try:
                events = [e for e in db.events_for_rows(conn, [row]) if e["kind"] == "native"]
                return self._json({"request_row": row, "events": events})
            finally:
                conn.close()

        if path == "/api/state":
            return self._json(self._state(
                _int(url.query, "past", db.PAGE_ROWS)))

        # Further back. The room holds a stretch of the store and asks for
        # the one above it when the reader scrolls past the top; each page carries its own
        # events and trails, so a page is the whole of what it needs to draw.
        if path == "/api/rows":
            before = _int(url.query, "before", None)
            if before is None:
                return self._json({"error": "before is required"}, 400)
            conn = db.connect()
            try:
                rows = db.rows_before(
                    conn, before, _int(url.query, "limit", db.PAGE_ROWS))
                # No rows left above `before` means `before` is the floor; say
                # so rather than a null, or the room would page the same empty
                # stretch forever.
                floor = rows[0]["id"] if rows else before
                return self._json(self._paged(conn, rows, floor))
            finally:
                conn.close()

        if path == "/api/progress":
            # The turn as it happens, and who is out -- the two things the
            # room keeps asking about while nobody is typing.
            now = progress_now()
            now["native_active"] = native_tools.RUNNING.is_set()
            now["out"] = worker.out_now()
            # The automatic memory's state rides along: which model, whether it
            # is loaded, loading, working, or not here -- the header shows it.
            now["recall"] = recall.status()
            # The digest organ rides beside it: whether it is on, its knobs,
            # and the last run -- the Developer section reads this.
            now["digest"] = digest.status()
            # What is left on the owner's plan. It rides the poll rather than
            # the turn because it moves while nobody is typing -- anything else
            # on the same plan spends against the same windows the assistant
            # does.
            now["limits"] = limits.now()
            now["limits_read"] = limits.status()["error"]
            # A move of the models folder, while one is under way: the
            # bytes done of the total, so the box can show it filling.
            now["models_move"] = models_dir.move_status()
            # Who it is, right now. On the poll rather than the turn so the
            # header changes the moment the picker does, instead of a switch
            # looking like nothing happened until it next speaks. Reads the
            # cached price table only -- never the network, which the poll
            # must never do.
            now["provider"] = providers.now()
            return self._json(now)

        if path == "/api/workers":
            conn = db.connect()
            try:
                return self._json({"roster": worker.roster(60),
                                   "hands": worker.hands_all(),
                                   "ready": worker.ready(),
                                   "window": worker.spend_in_window(conn)})
            finally:
                conn.close()

        # Angel me, listening. Lines of its own addressed to `angel` that nobody
        # has collected yet, handed over once. The key is the same one the
        # door takes, so only the one with the key can collect them.
        if path == "/api/dreams":
            # The nights so far, and where the gate stands right now. Reads
            # only -- the status check must never write a missed night down;
            # that is the runner's to do.
            conn = db.connect()
            try:
                return self._json({"status": dream.status(conn),
                                   "dreams": db.recent_dreams(conn, 30)})
            finally:
                conn.close()

        if path == "/api/angel/inbox":
            q = parse_qs(url.query)
            want = angel.read_token()
            if not want or not secrets.compare_digest(
                    (q.get("token") or [""])[0], want):
                return self._json({"error": "not angel me"}, 403)
            conn = db.connect()
            try:
                lines = angel.inbox(conn, peek=bool(q.get("peek")))
            finally:
                conn.close()
            return self._json({"lines": lines})

        if path == "/api/recall":
            return self._json(recall.status())

        if path == "/api/digest":
            return self._json(digest.status())

        # Where the models we fetch ourselves are kept: the folder, what is
        # in it, what is left in the old place, and LM Studio's own beside it.
        if path == "/api/models":
            return self._json(models_dir.status())

        # Room backgrounds have their own catalogue rather than falling
        # through to static artwork.  It is the list Settings can show and it
        # contains no local directory names.
        if path == "/api/wallpapers":
            return self._json(wallpapers.status())

        # Who it can think through: the services, which keys are set (never
        # the keys), every model with its live price, and what is left to
        # spend. Read on opening Settings, not on the poll -- it can touch
        # the network, and the poll must never.
        if path == "/api/providers":
            # Opened on purpose, not polled, so it may wait a moment for the
            # first price list rather than drawing a page of dashes.
            got = providers.catalogue(wait=4.0)
            # A key is never shown over the wire, and neither is the box for
            # changing one. From the road this is a page you read.
            got["can_edit_keys"] = self._from_the_desk()
            return self._json(got)

        # Their projects, whole: every project with its Notes box and its
        # tasks, and the card of any job a task named. The Projects tab reads
        # this and nothing else.
        if path == "/api/projects":
            conn = db.connect()
            try:
                return self._json(_projects_view(conn))
            finally:
                conn.close()

        if path == "/api/search":
            q = (parse_qs(url.query).get("q") or [""])[0]
            conn = db.connect()
            try:
                return self._json({"hits": db.search(conn, q)})
            finally:
                conn.close()

        # The pixels themselves, back to the window that is drawing them. Only
        # ever by the name the store gave them -- a sha and one of four
        # suffixes, checked in `pictures` -- so this is a listing of what we
        # wrote and never a way to ask the disk for something else. Behind the
        # same door as everything: a picture is as private as the line it came
        # in on.
        if path.startswith("/pictures/"):
            try:
                target = pictures.path_of({"file": path[len("/pictures/"):]})
                body = target.read_bytes()
            except (pictures.Refused, OSError):
                return self._send(404, b"not here", "text/plain")
            ctype = TYPES.get(target.suffix, "application/octet-stream")
            return self._send(200, body, ctype)

        rel = "index.html" if path == "/" else path.lstrip("/")
        # Pictures come from the home first -- its own face, its own scenery
        # -- and fall back to the plain defaults beside the code.
        if path.startswith("/artwork/") or rel in HOME_ICONS:
            sub = rel[len("artwork/"):] if path.startswith("/artwork/") else rel
            for base in (home.ARTWORK, ROOT / "artwork", WEB):
                target = (base / sub).resolve()
                if base.resolve() in target.parents and target.is_file():
                    ctype = TYPES.get(target.suffix, "application/octet-stream")
                    return self._send(200, target.read_bytes(), ctype)
            return self._send(404, b"not here", "text/plain")
        target = (WEB / rel).resolve()
        if WEB.resolve() in target.parents and target.is_file():
            ctype = TYPES.get(target.suffix, "application/octet-stream")
            body = target.read_bytes()
            if rel in NAMED_PAGES:
                body = with_identity(body, rel)
            return self._send(200, body, ctype)
        return self._send(404, b"not here", "text/plain")

    def do_POST(self):
        if not self._let_in():
            return
        if self.path.startswith('/api/voice/'):
            origin = urlparse(self.headers.get('Origin') or '')
            if (origin.scheme not in ('http', 'https') or origin.netloc != self.headers.get('Host')
                    or self.headers.get('X-Assistant-Voice') != '1'
                    or self.headers.get_content_type() != 'application/json'):
                return self._json({'error': 'Open voice from the ' + home.NAME + ' app.'}, 403)
            try:
                size = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                size = 0
            if not 0 < size <= 262144:
                return self._json({'error': 'Invalid voice request size.'}, 400)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "invalid request size"}, 400)
        # Base64 is larger than the image it carries.  Refuse it before
        # json.loads would make several in-memory copies of an oversized
        # upload; a filename cannot change this bound.
        if self.path == "/api/wallpapers/upload" and not 0 < length <= wallpapers.MAX_REQUEST_BYTES:
            return self._json({"error": "A wallpaper upload must be no more than "
                               + str(wallpapers.MAX_BYTES // 1024 // 1024) + " MB."}, 400)
        if self.path == "/api/codex-memory":
            if self._who != home.OWNER or not self._loopback:
                return self._shut()
            if length < 1 or length > 65536:
                self.close_connection = True
                return self._json({"error": "Expected a memory request under 64 KiB."}, 400)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)

        if self.path == "/api/codex-memory":
            from . import codex_memory
            if not TURN_GATE.acquire(blocking=False):
                return self._json({"error": "The room is taking a turn; retry the same request later."}, 409)
            try:
                # No NUDGE, model call, voice, or new user/angel row here.
                conn = db.connect()
                try:
                    return self._json(codex_memory.apply_write(conn, body))
                finally:
                    conn.close()
            except codex_memory.Conflict as exc:
                return self._json({"error": str(exc)}, 409)
            except (ValueError, TypeError) as exc:
                return self._json({"error": str(exc)}, 400)
            finally:
                TURN_GATE.release()

        if self.path.startswith('/api/voice/'):
            if not isinstance(body, dict):
                return self._json({'error': 'Expected a voice request.'}, 400)
            try:
                manager = live_voice.manager
                ident, owner = body.get('session'), self._who
                if self.path == '/api/voice/start':
                    if not TURN_GATE.acquire(blocking=False):
                        return self._json({'error': 'Let the current reply finish before starting voice.'}, 409)
                    try:
                        conn = db.connect()
                        try:
                            if progress_now()['busy'] or unanswered(conn) is not None or RESTARTING['why']:
                                return self._json({'error': 'Let the current reply finish before starting voice.'}, 409)
                        finally:
                            conn.close()
                        return self._json(manager.start(body.get('sdp'), body.get('who') or owner, owner, voice=body.get('voice')))
                    finally:
                        TURN_GATE.release()
                if self.path == '/api/voice/sync':
                    result = manager.sync(ident, owner, body.get('events', []))
                elif self.path == '/api/voice/text':
                    if body.get('inputMode') not in ('backend', 'native-routed-v1'):
                        raise live_voice.ProbeError('Refresh ' + home.NAME + ' to update voice before sending a typed message.')
                    if body.get('inputMode') == 'native-routed-v1':
                        result = manager.text(ident, owner, body.get('text'), native=True)
                    else:
                        result = manager.text(ident, owner, body.get('text'))
                elif self.path == '/api/voice/text-fallback':
                    result = manager.text_fallback(ident, owner, body.get('row'))
                elif self.path == '/api/voice/delegate':
                    result = manager.delegate(ident, owner, body.get('event'), body.get('events', []))
                    return self._json(result)
                elif self.path == '/api/voice/backend':
                    # Which voice speaks its lines. One room, one answer -- so
                    # it is written down here rather than kept in a browser,
                    # and a call already running is ended rather than left
                    # talking over the road that was just chosen.
                    want = body.get('backend')
                    if want not in brain.BACKENDS:
                        raise live_voice.ProbeError('Choose ' + home.NAME + "'s own voice or the OpenAI one.")
                    if want != 'openai' and manager.status()['active']:
                        manager.end(ident, owner, [])
                    cfg = brain.write_voice(backend=want)
                    return self._json({'backend': want,
                                       'switch': bool(cfg.get('enabled', True)),
                                       'local': brain.voice_status(fresh=True)})
                elif self.path == '/api/voice/enabled':
                    # The assistant's own voice, on or off, for this room only.
                    # The speak server's own switch is left alone on purpose:
                    # it is shared with everything else on this machine, and
                    # turning this room quiet must not silence anything else
                    # that uses it.
                    cfg = brain.write_voice(enabled=bool(body.get('on')))
                    return self._json({'backend': brain.voice_backend(),
                                       'switch': bool(cfg.get('enabled', True)),
                                       'local': brain.voice_status(fresh=True)})
                elif self.path == '/api/voice/stop':
                    return self._json(manager.end(ident, owner, body.get('events', [])))
                else:
                    return self._json({'error': 'No such voice action.'}, 404)
                conn = db.connect()
                try:
                    result['rows'] = [db.get_row(conn, rid) for rid in result.get('saved', [])]
                finally:
                    conn.close()
                return self._json(result)
            except (live_voice.ProbeError, ValueError) as exc:
                return self._json({'error': str(exc)}, 400)
            except Exception as exc:
                return self._json({'error': 'Voice could not complete this action: ' + type(exc).__name__}, 500)

        if self.path == '/api/codex':
            if self._who != home.OWNER:
                return self._shut()
            try:
                result = overmind.configure(body)
                NUDGE.set()
                return self._json(result)
            except Exception as exc:
                return self._json({'error': str(exc)}, 400)

        if self.path == "/api/native-tools/cancel":
            if self._who != home.OWNER:
                return self._shut()
            if not native_tools.RUNNING.is_set():
                return self._json({"error": "No native conversation is running."}, 409)
            native_tools.CANCEL.set()
            return self._json({"cancel_requested": True})

        if self.path == "/api/providers/native-tools":
            if self._who != home.OWNER:
                return self._shut()
            if not TURN_GATE.acquire(blocking=False):
                return self._json({"error": "Wait for the active turn to finish, or stop its native tools first."}, 409)
            try:
                providers.set_native_tools(body.get("enabled"))
            except providers.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            finally:
                TURN_GATE.release()
            return self._json(providers.catalogue())

        if self.path == "/api/native-proof":
            if self._who != home.OWNER:
                return self._shut()
            if not native_proof.available():
                return self._json({"error": "Native proof is disabled. " + home.OWNER_NAME + " must opt in at room startup."}, 409)
            if body.get("action") == "cancel":
                native_tools.CANCEL.set()
                return self._json({"cancel_requested": True})
            if body.get("action") != "start":
                return self._json({"error": "Choose the explicit Start proof action."}, 400)
            if not TURN_GATE.acquire(blocking=False):
                return self._json({"error": "A turn is running; wait before starting the proof."}, 409)
            try:
                conn = db.connect()
                try:
                    if progress_now()["busy"] or unanswered(conn) is not None:
                        return self._json({"error": "Finish the pending conversation before starting the proof."}, 409)
                    if providers.resolve(MODEL["name"])["service"] != "codex" or not providers.codex_only():
                        return self._json({"error": "Choose Codex subscription chat and Codex-only mode first."}, 409)
                    text = native_proof.prepare()
                    row = db.add_row(conn, "user", text, meta={"native_proof": True, "who": home.OWNER})
                    native_tools.CANCEL.clear()
                    native_proof.ARMED.clear()
                    native_proof.ARMED.add(row)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 409)
                finally:
                    conn.close()
            finally:
                TURN_GATE.release()
            NUDGE.set()
            return self._json({"row": row})

        if self.path == "/api/send":
            text = (body.get("text") or "").strip()
            # Pictures are taken in before the line is written down, so a
            # refusal leaves nothing behind: a row saying "look at this" whose
            # picture was never kept is worse than a message that did not send.
            try:
                pics = pictures.keep_all(body.get("pictures"))
            except pictures.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            except (OSError, TypeError, AttributeError) as exc:
                return self._json(
                    {"error": f"the picture did not go in: "
                              f"{type(exc).__name__}: {exc}"}, 500)
            # A picture on its own is a whole thing to say -- pointing at
            # something is what a screenshot is for -- so empty words are only
            # empty when nothing came with them.
            if not text and not pics:
                return self._json({"error": "empty"}, 400)
            # Whose words these are. The door said who may come in; `who`
            # says who is speaking, because several people can share one
            # screen and the switch between them is free by agreement --
            # attribution, not a lock. Absence means the owner, the one rule
            # the store keeps everywhere, so the owner's lines cost nothing
            # new and no old row needs relabelling. A labelled line also
            # keeps the door it came through: a personal device and a shared
            # desk are different places to speak carefully from.
            who = (body.get("who") or "").strip().lower()
            if who and who not in people.HOUSEHOLD:
                return self._json(
                    {"error": "not a name of this house: " + who[:40]}, 400)
            # Which door. The desk is this machine; anything else came in
            # on somebody's key, so it is that person's device -- the door's
            # owner, which can differ from `who` when one person writes on
            # another's.
            door = getattr(self, "_who", home.OWNER) or home.OWNER
            via = "desktop" if self._from_the_desk() else door + "'s phone"
            meta = None
            if who and who != home.OWNER:
                meta = {"who": who, "via": via}
            elif via != "desktop":
                # The owner, away from the desk. A line at the desk keeps its
                # old bytes -- absence means the owner, at the desk -- and
                # only the one that came in over the tailnet is labelled, so
                # the assistant can tell where it came from by the row alone.
                meta = {"who": home.OWNER, "via": via}
            if pics:
                # The pointers ride the row. The bytes are already on disk
                # under their own sha, and stay there for good -- dropping the
                # row takes the pixels out of the working set, not off the machine,
                # which is the whole of why reaching it back works.
                meta = dict(meta or {}, pictures=pics)
            MODEL["name"] = body.get("model") or MODEL["name"]
            # Written down now, at the moment send was pressed, and answered
            # when the assistant gets to it. That is the whole of what lets a
            # person say a second thing while it is still thinking about the
            # first: the line is already in the room, in the order it was said.
            conn = db.connect()
            try:
                db.add_row(conn, "user", text, meta=meta)
            finally:
                conn.close()
            NUDGE.set()
            return self._json(self._state())

        # Their projects: adding one, writing in its Notes box, and the
        # tasks. Every write says who is doing it -- the same free switch
        # that signs a line in the chat, because several people can share a
        # screen. It is attribution, not a lock, and here it is load-bearing:
        # a note line's author is what keeps the assistant from quoting one
        # person to another. So an unsigned write is refused rather than
        # filed as the owner's.
        if self.path == "/api/project":
            who = (body.get("who") or "").strip().lower()
            if who not in people.HOUSEHOLD:
                return self._json(
                    {"error": "who is writing? a project is written in by "
                              + " or ".join(home.HOUSEHOLD)
                              + ", and every line keeps its author"}, 400)
            conn = db.connect()
            try:
                out = _project_op(conn, body, who)
                if not out.get("ok"):
                    return self._json({"error": out.get("why") or "no"}, 400)
                return self._json(dict(out, view=_projects_view(conn)))
            finally:
                conn.close()

        # A worker finishing, knocking. This is the whole point of the hook:
        # the finish pokes the server itself rather than leaving something on
        # disk for the owner to notice. It answers fast and does no work here --
        # the dispatcher is already waiting on it.
        if self.path == "/api/worker/knock":
            heard = worker.knock(body)
            # Only a knock with a name on it is news. The permission
            # channel answers through this door too -- a stopped hand
            # polling for an answer every two seconds -- and painting every
            # poll flooded the chat with "a worker knocked: None" for as
            # long as a hand stood waiting.
            if heard.get("heard") and heard.get("event"):
                progress_step("a worker knocked: " + str(heard["event"]),
                              "worker")
            return self._json(heard)

        # One consistent copy of the store, taken while the room is running,
        # checked before it is called a backup. The whole of it is in `backup.take` -- this is
        # only the door, and the lock that stops two of them at once.
        if self.path == "/api/backup":
            if not BACKUP_LOCK.acquire(blocking=False):
                return self._json(
                    {"error": "a copy is already being taken"}, 409)
            try:
                note = backup.take()
                progress_step("took a backup: " + Path(note["file"]).name
                              + ", " + str(note["size_kb"]) + " KB, checked",
                              "plain", note)
                return self._json(note)
            except Exception as exc:
                # The message from `take` says what was actually wrong -- a torn
                # copy, counts that do not match -- and that is what needs to be
                # seen, not "backup failed".
                traceback.print_exc()
                return self._json(
                    {"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                BACKUP_LOCK.release()

        # A dream, asked for rather than waited for -- the command line, a
        # test, or the owner wanting to see one. It does not run here: the
        # runner takes it on its next look, so an asked dream still cannot
        # overlap a turn. No key -- the room is behind its own door; the angel
        # key is a name on a line, and a dream wears nobody's name but the
        # assistant's.
        if self.path == "/api/dream":
            dream.ask("asked")
            NUDGE.set()
            conn = db.connect()
            try:
                gate = dream.status(conn)["gate"]
            finally:
                conn.close()
            progress_step("a dream was asked for", "plain")
            return self._json({"asked": True, "gate": gate})

        # Angel me, speaking under its own name. The key is made fresh when the
        # room opens and lives where the assistant cannot read it, so nothing it
        # reads -- a page, a file, an errand's report -- can be turned into a
        # line that looks like it came from angel me.
        if self.path == "/api/angel":
            want = angel.read_token()
            if not want or not secrets.compare_digest(
                    str(body.get("token") or ""), want):
                return self._json({"error": "not angel me"}, 403)
            text = (body.get("text") or "").strip()
            try:
                pics = pictures.keep_all(body.get("pictures"))
            except pictures.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            if not text and not pics:
                return self._json({"error": "empty"}, 400)
            conn = db.connect()
            try:
                row = angel.say(conn, text, reply_to=body.get("reply_to"),
                                pics=pics)
            finally:
                conn.close()
            NUDGE.set()
            return self._json({"row": row})

        # The automatic memory: the dropdown, its timeout, the download and
        # load buttons, and a try-it-now that runs it on the room as it
        # stands. None of these touch the store; `try` reads it and nothing
        # more.
        if self.path == "/api/recall/choose":
            return self._json(recall.choose(body.get("model")))
        if self.path == "/api/recall/writer":
            return self._json(recall.choose_writer(body.get("writer")))

        # Their free notes: one person's text, replaced whole. Signed like a
        # note line in a project -- the same free switch -- because the box
        # under Settings shows whoever is picked, and a save from a paired
        # device is that device's owner unless the pick says otherwise. Refusals come
        # back in words: a name outside the house, a text over the wall.
        if self.path == "/api/notes":
            who = (body.get("who") or getattr(self, "_who", "") or home.OWNER)
            try:
                notes.write(who, body.get("text") or "")
            except notes.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json(
                    {"error": "the notes did not go down: "
                              + type(exc).__name__ + ": " + str(exc)}, 500)
            return self._json(notes.status())

        # A wallpaper is accepted as bytes rather than a path.  `wallpapers`
        # reads its magic bytes and dimensions, derives its own safe filename,
        # and uses exclusive creation so an existing picture is never quietly
        # replaced by another tab's upload.
        if self.path == "/api/wallpapers/upload":
            try:
                return self._json(wallpapers.upload(body.get("name"), body.get("data")))
            except wallpapers.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"error": "the wallpaper did not go in: "
                                   + type(exc).__name__ + ": " + str(exc)}, 500)

        if self.path == "/api/wallpapers/choose":
            try:
                return self._json(wallpapers.choose(body.get("id")))
            except wallpapers.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            except OSError as exc:
                return self._json({"error": "the wallpaper choice was not saved: "
                                   + type(exc).__name__ + ": " + str(exc)}, 500)

        # The models folder under Settings. Choosing one moves whatever is
        # in the old place into it, in the background; the poll shows the
        # count. Refusals -- a relative path, a drive that is not here, a
        # folder inside the repo -- come back in words.
        if self.path == "/api/models":
            try:
                return self._json(models_dir.choose(body.get("folder")))
            except models_dir.Refused as exc:
                return self._json({"error": str(exc)}, 400)

        if self.path == "/api/models/move":
            try:
                return self._json(
                    models_dir.move_leftovers(body.get("from")))
            except models_dir.Refused as exc:
                return self._json({"error": str(exc)}, 400)

        # The provider keys. Only ever from this machine: a key typed on a
        # paired device crosses the tailnet, and the one rule this room has always kept is
        # that a secret does not ride the wire. Refused in words, not by a
        # missing button, so the reason is legible from the road.
        if self.path == "/api/providers/key":
            if not self._from_the_desk():
                return self._json(
                    {"error": "a key is only ever set at the desk. This is "
                              "the room reached from another machine, and "
                              "a key typed here would cross the network to "
                              "get in."}, 403)
            try:
                providers.set_key(body.get("service"), body.get("key"))
            except providers.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json(providers.catalogue())

        # What an account had, as read off the provider's dashboard. Only ever
        # from the desk, like the keys -- and for the same reason it is worth
        # having at all: it is a figure no endpoint will give us.
        if self.path == "/api/providers/credits":
            if not self._from_the_desk():
                return self._json(
                    {"error": "the balance is written at the desk, where the "
                              "dashboard it was read from is open."}, 403)
            try:
                providers.set_credits(body.get("service"), body.get("usd"))
            except providers.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json(providers.catalogue())

        # Which mind answers the chat. Takes effect on the next turn.
        if self.path == "/api/providers/codex-only":
            if not TURN_GATE.acquire(blocking=False):
                return self._json({"error": "Wait for the active turn or background check to finish before changing Codex-only mode."}, 400)
            try:
                if body.get("enabled") is True and (progress_now()["busy"] or worker.out_now()):
                    raise providers.Refused("Wait for the active turn and workers to finish before enabling Codex-only mode.")
                providers.set_codex_only(body.get("enabled"))
            except providers.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            finally:
                TURN_GATE.release()
            FAILED["row"] = None
            NUDGE.set()
            return self._json(providers.catalogue())

        if self.path == "/api/providers/choose":
            try:
                got = providers.choose(body.get("model"))
            except providers.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            MODEL["name"] = got["model"]
            FAILED["row"] = None
            NUDGE.set()
            return self._json(providers.catalogue())

        # Which mind folds the shelf at night. Deliberately its own setting --
        # see the note beside the dream call above.
        if self.path == "/api/providers/dream":
            try:
                providers.choose_dream(body.get("model"))
            except providers.Refused as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json(providers.catalogue())

        if self.path == "/api/recall/settings":
            recall.set_knobs(body)
            return self._json(recall.status())

        # The digest organ's knobs. It borrows the automatic memory's model,
        # so there is no choose, download or load here -- only the dials.
        if self.path == "/api/digest/settings":
            digest.set_knobs(body)
            return self._json(digest.status())

        if self.path == "/api/recall/download":
            out = recall.download()
            if out.get("error"):
                return self._json(out, 400)
            return self._json(out)

        if self.path == "/api/recall/load":
            return self._json(recall.load_now())

        if self.path == "/api/recall/try":
            conn = db.connect()
            try:
                out = recall.try_now(conn)
            finally:
                conn.close()
            if out.get("error"):
                return self._json(out, 400)
            return self._json(out)

        if self.path == "/api/reload":
            conn = db.connect()
            try:
                db.reload_rows(conn, body.get("ids") or [])
            finally:
                conn.close()
            return self._json(self._state())

        if self.path == "/api/unload":
            conn = db.connect()
            try:
                db.unload(conn, body.get("ids") or [])
            finally:
                conn.close()
            return self._json(self._state())

        return self._json({"error": "no such route"}, 404)


class OneRoom(ThreadingHTTPServer):
    """The room, and only ever one of it.

    Python sets SO_REUSEADDR on HTTPServer so that a restart does not trip
    over the socket the last one left behind. On Linux that is all it means.
    On Windows it means *share the port*: a second server binds 8787 happily
    while the first is still sitting on it, and neither is told. That is not
    an untidy socket, it is a second assistant. Each process runs its own turn
    loop, and two of them will both answer every line -- twice over,
    differently worded, each writing its own essence, because nothing in the
    store says a line is *being* answered until the answer lands, so neither
    runner can see the other working.

    So: no reuse. A second one dies on the bind now, which is the whole
    point. A window that closes and says why beats a window that stays open
    and quietly doubles the assistant.
    """

    allow_reuse_address = 0


def main():
    # A room runs a home made for it, never the code folder. The code folder
    # carries a test identity so the benches can run, and a room started on
    # it would be a stranger wearing somebody's name on somebody's port.
    if not home.is_real():
        print("\n  No home is chosen for this install, so nothing was started.")
        print("  Choose one (or make a new assistant in an empty folder):")
        print("      python -m server.setup <folder>\n")
        raise SystemExit(2)

    conn = db.connect()
    conn.close()

    # The port comes first -- before the slow proving below, and long before
    # there is a runner. If someone is already in the room, this is where we
    # find out, and the only safe thing to be holding at that moment is
    # nothing at all.
    try:
        # Wide, and narrowed at the door rather than at the socket. Binding
        # straight to the Tailscale address would be tighter, and it was
        # weighed: it needs the address to exist at the moment the room opens,
        # and Tailscale often comes up after it does -- so the room would fail
        # to open, or open on the wrong interface, for a reason nobody watching
        # would recognise. The range test in `_door` does the narrowing
        # instead, and it does it on every request rather than once at boot.
        room = OneRoom((home.BIND, PORT), Handler)
    except OSError as exc:
        print(f"\n  {home.NAME} is already awake on port {PORT}, in another window.")
        print(f"  ({exc})")
        print("  This one is closing rather than becoming a second room --")
        print("  two rooms answer the same line twice and both write it down.\n")
        raise SystemExit(1)

    # A fresh key for angel me's own door, made now and worth nothing after the
    # next restart. Without one, that door is simply shut.
    angel.PORT = PORT
    angel.mint()

    # The owner's own key, made on the first boot that finds it missing and
    # kept every boot after. Unlike angel me's above, this one is NOT
    # reminted: it lives on a paired device that is not here when the room
    # restarts, and reminting would un-pair it every time the machine came
    # back.
    people.mint(home.OWNER)

    print(f"\n  {home.NAME} is awake at  http://localhost:{PORT}")
    print(f"  home: {home.HOME}")
    if providers.codex_only():
        print("  Codex-only mode: Claude calls and workers paused")
    else:
        print(f"  claude binary: {brain.find_claude()}")
    print(f"  store: {db.DB_PATH}")

    # Before it can send anyone: prove every limiting flag is real, and prove
    # the caps bite rather than merely parse. A mistyped flag is ignored with
    # exit code 0 and looks exactly like a leash that never tripped, so this
    # runs first and refuses everything if it does not pass.
    worker.KNOCK_PORT = PORT
    # An errand of the assistant's coming home is the one thing that starts a
    # turn without a person speaking. It was the one waiting for it -- and so
    # is a hand that has stopped mid-page to ask it something.
    brain.WORKER_CAME_HOME = worker_came_home
    worker.ON_ASK = hand_asked
    threading.Thread(target=turn_loop, daemon=True).start()
    # If a small model is chosen for the automatic memory, bring it up now,
    # so it first turn does not find it cold.
    recall.start()
    chosen = recall.settings()["model"]
    print("  automatic memory: " + (chosen + ", loading" if chosen else "none"))
    view = watch.for_prompt()
    print("  the watcher: " + ", ".join(
        n for n, s in view["senses"].items() if s == "on")
        + " · ceiling " + str(view["today"]["ceiling"]) + " a day"
        + " · push " + view["push"])
    # Anything still marked as out was being waited for by a process that is
    # gone. It is nobody's now, and saying so beats leaving it looking live.
    orphans = worker.adopt_orphans()
    if orphans:
        print(f"  {len(orphans)} worker(s) were still out when the room "
              f"last closed; marked orphaned")
    conn = db.connect()
    try:
        lost_dreams = dream.adopt_orphans(conn)
        tonight = dream.night_of(dream.local_now())
        print("  dreamer: the night of " + tonight
              + (" is a FREE night" if dream.is_free(tonight) else " has work")
              + ", due from " + str(dream.DUE_HOUR).rjust(2, "0")
              + ":00 after " + str(dream.QUIET_MINUTES) + " quiet minutes")
        if lost_dreams:
            print(f"  {lost_dreams} dream(s) were mid-night when the room "
                  f"last closed; marked broken")
    finally:
        conn.close()
    lost_hands = worker.adopt_orphaned_hands()
    if lost_hands:
        print(f"  {len(lost_hands)} hand(s) were mid-page when the room last "
              f"closed: {', '.join(lost_hands)} -- their threads are intact")
    # What came back to nobody goes to it, once, and it is woken to say so.
    conn = db.connect()
    try:
        late = hand_over_the_late(conn)
    finally:
        conn.close()
    if late:
        print(f"  {late} errand(s) came back to nobody; handed to {home.NAME}")
    state = worker.ready()
    for probe in state.get("probes") or []:
        print(f"  leash {probe['flag']:20} {probe['verdict']} "
              f"-- {probe['detail']}")
    for canary in state.get("canaries") or []:
        bit = "bites" if canary["bites"] else "DID NOT BITE"
        print(f"  cap   {canary['flag']:20} {bit} "
              f"({canary.get('terminal_reason')})")
    if state.get("proof_reused"):
        print(f"  cap proof reused from {state.get('taken')}")
    if state.get("ready"):
        print(f"  workers: ready, ${worker.CEILING_SPEND_USD:.2f} of errands "
              f"per {worker.CEILING_WINDOW_HOURS}h "
              f"(and never more than {worker.CEILING_RUNS})\n")
    else:
        print(f"  workers: REFUSED -- {state.get('why')}\n")
    try:
        room.serve_forever()
    finally:
        live_voice.manager.shutdown()
        room.server_close()


if __name__ == "__main__":
    main()
