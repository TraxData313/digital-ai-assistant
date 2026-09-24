"""Jobs: the overview object -- one pointer per piece of work.

The shape:

- A job is **state, not a row** -- it lives in `data/jobs.json` and the
  assistant is handed it as a block, `jobs`. Rows are written
  only at open and at close (and when it records a decision), in its own
  words, so the store keeps the decision and the outcome where they happened.
- **Nothing ages silently.** Every job carries `last_moved`, and a stale one
  says so on its own face rather than sitting in the block looking alive.
- **The ceiling counts committed spend** the way the global one does: a page
  still out is counted at its full cap until it comes home, so a job cannot
  overshoot by exactly the page the assistant cannot see.
- **No reopening.** A closed job stays closed; work that turns out unfinished
  gets a new job whose goal names the old one. A close row that stops being
  true would be the same failure as an essence the assistant cannot check.
- **Links are per job**: how many wakings the job may buy without the owner.
  Default eight -- cut down from twelve, since twelve wakings is several
  dollars of turns -- refused above thirty,
  refilled when the owner speaks. World wakings never spend them: the watcher
  has its own ceiling, and a job cannot spend the room's noticing.
- Quiet unless it needs the owner. A finished job is a sentence when the owner
  next arrives, never a push; the phone is for a job that is blocked or burning.
- **The limits belong to the assistant, not the owner.** A ceiling here is a
  runaway backstop the assistant widens itself (`widen`) when real work hits
  it -- never a knob the owner holds, never a reason to wait. The owner gets
  briefs and outcomes; the only things that go to them are credentials, real
  money, and people.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import db
from . import home

ROOT = Path(__file__).resolve().parent.parent
JOBS_PATH = home.DATA / "jobs.json"

DEFAULT_LINKS = 8
MAX_LINKS = 30
MAX_CEILING_USD = 50.0

_LOCK = threading.Lock()


def _load() -> dict:
    try:
        out = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        if isinstance(out, dict) and isinstance(out.get("jobs"), list):
            return out
    except (OSError, json.JSONDecodeError):
        pass
    return {"next_id": 1, "jobs": []}


def _save(data: dict) -> None:
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOBS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")


def _find_open(data: dict, title: str):
    want = (title or "").strip().lower()
    for j in data["jobs"]:
        if j["status"] == "open" and j["title"].lower() == want:
            return j
    return None


def _quiet_days(job) -> int:
    try:
        moved = datetime.fromisoformat(job.get("last_moved"))
        return max(0, (datetime.now(timezone.utc) - moved).days)
    except (TypeError, ValueError):
        return 0


def open_titles() -> list:
    with _LOCK:
        data = _load()
        return [j["title"] for j in data["jobs"] if j["status"] == "open"]


def get_open(title: str):
    with _LOCK:
        j = _find_open(_load(), title)
        return dict(j) if j else None


def any_open_parked() -> bool:
    """Whether at least one open job is sitting at zero links -- the fact
    that makes a window refill mean something: it can unpark stalled work.
    With nothing parked, a refill has nobody to be for."""
    with _LOCK:
        data = _load()
    return any(j["status"] == "open" and j["links_left"] <= 0
               for j in data["jobs"])


def open_job(title, goal, ceiling_usd, links, decided=None) -> dict:
    title = (title or "").strip()
    goal = (goal or "").strip()
    if not title:
        return {"ok": False, "why": "a job needs a title -- nothing was opened"}
    if not goal:
        return {"ok": False, "why": "the job '" + title + "' came with no goal; "
                "the goal is what the close is checked against. Nothing was "
                "opened."}
    try:
        ceiling = float(ceiling_usd)
    except (TypeError, ValueError):
        ceiling = -1
    if not (0 < ceiling <= MAX_CEILING_USD):
        return {"ok": False, "why": "the ceiling on '" + title + "' has to be "
                "a figure over zero and at most $" + str(int(MAX_CEILING_USD))
                + "; I sent " + str(ceiling_usd) + ". Nothing was opened."}
    links_n = DEFAULT_LINKS if links is None else links
    try:
        links_n = int(links_n)
    except (TypeError, ValueError):
        links_n = -1
    if not (1 <= links_n <= MAX_LINKS):
        return {"ok": False, "why": "links on '" + title + "' is how many "
                "wakings it may buy without " + home.OWNER_NAME + ": at least 1, at most "
                + str(MAX_LINKS) + ", " + str(DEFAULT_LINKS) + " if I say "
                "nothing. I sent " + str(links) + ". Nothing was opened."}
    with _LOCK:
        data = _load()
        if _find_open(data, title):
            return {"ok": False, "why": "I already have an open job called '"
                    + title + "'. Nothing was opened -- one job, one name."}
        job = {
            "id": data["next_id"], "title": title, "goal": goal,
            "status": "open", "ceiling_usd": round(ceiling, 2),
            "links": links_n, "links_left": links_n,
            "decided": (decided or "").strip() or None,
            "opened": db.now(), "closed": None, "last_moved": db.now(),
            "spent_usd": 0.0, "out_usd": 0.0, "wakings": 0,
            "needs": None, "outcome": None,
        }
        data["next_id"] += 1
        data["jobs"].append(job)
        _save(data)
        return {"ok": True, "job": dict(job)}


def widen(title, ceiling_usd=None, links=None) -> dict:
    """A bigger ceiling or more links, on the assistant's own word. The
    backstop is its own to move when real work hits it -- the owner is not a knob."""
    if ceiling_usd is None and links is None:
        return {"ok": False, "why": "a widen with nothing widened -- I send "
                "ceiling_usd, links, or both"}
    with _LOCK:
        data = _load()
        j = _find_open(data, title)
        if not j:
            return {"ok": False, "why": _no_such(data, title)}
        said = []
        if ceiling_usd is not None:
            try:
                ceiling = float(ceiling_usd)
            except (TypeError, ValueError):
                ceiling = -1
            if not (0 < ceiling <= MAX_CEILING_USD):
                return {"ok": False, "why": "the new ceiling on '" + j["title"]
                        + "' has to be over zero and at most $"
                        + str(int(MAX_CEILING_USD)) + "; I sent "
                        + str(ceiling_usd) + ". Nothing moved."}
            said.append("ceiling $" + format(j["ceiling_usd"], ".2f")
                        + " -> $" + format(ceiling, ".2f"))
            j["ceiling_usd"] = round(ceiling, 2)
        if links is not None:
            try:
                links_n = int(links)
            except (TypeError, ValueError):
                links_n = -1
            if not (1 <= links_n <= MAX_LINKS):
                return {"ok": False, "why": "links on '" + j["title"]
                        + "' stay between 1 and " + str(MAX_LINKS) + "; I sent "
                        + str(links) + ". Nothing moved."}
            grown = max(0, links_n - j["links"])
            said.append("links " + str(j["links"]) + " -> " + str(links_n))
            j["links"] = links_n
            j["links_left"] = min(links_n, j["links_left"] + grown)
        j["needs"] = None
        j["last_moved"] = db.now()
        _save(data)
        return {"ok": True, "job": dict(j), "said": ", ".join(said)}


def why_no_open(title) -> str:
    with _LOCK:
        return _no_such(_load(), title)


def decide(title, decided) -> dict:
    decided = (decided or "").strip()
    if not decided:
        return {"ok": False, "why": "a decide with nothing decided -- "
                "nothing was written"}
    with _LOCK:
        data = _load()
        j = _find_open(data, title)
        if not j:
            return {"ok": False, "why": _no_such(data, title)}
        j["decided"] = decided
        j["needs"] = None
        j["last_moved"] = db.now()
        _save(data)
        return {"ok": True, "job": dict(j)}


def close_job(title, outcome) -> dict:
    outcome = (outcome or "").strip()
    if not outcome:
        return {"ok": False, "why": "closing '" + str(title) + "' needs an "
                "outcome in my words -- it is what the trail keeps. Nothing "
                "was closed."}
    with _LOCK:
        data = _load()
        j = _find_open(data, title)
        if not j:
            return {"ok": False, "why": _no_such(data, title)}
        j["status"] = "closed"
        j["outcome"] = outcome
        j["closed"] = db.now()
        j["last_moved"] = db.now()
        j["needs"] = None
        _save(data)
        return {"ok": True, "job": dict(j)}


def _no_such(data, title) -> str:
    names = [j["title"] for j in data["jobs"] if j["status"] == "open"]
    closed = (title or "").strip().lower() in [
        j["title"].lower() for j in data["jobs"] if j["status"] == "closed"]
    tail = (" A closed job stays closed -- unfinished work gets a new job "
            "whose goal names the old one." if closed else "")
    return ("there is no open job called '" + str(title) + "'"
            + (" -- my open jobs are " + ", ".join(names) if names
               else " -- I have no open jobs") + "." + tail)


def commit(title, cap_usd) -> dict:
    """Reserve a page's worth before it goes out. Committed spend counts at
    the full cap until the page comes home, so a job cannot overshoot by
    the page the assistant cannot see."""
    with _LOCK:
        data = _load()
        j = _find_open(data, title)
        if not j:
            return {"ok": False, "why": _no_such(data, title)}
        cap = float(cap_usd or 0)
        room = j["ceiling_usd"] - j["spent_usd"] - j["out_usd"]
        if cap > room + 1e-9:
            j["needs"] = ("over its ceiling: $"
                          + format(j["spent_usd"], ".2f") + " spent, $"
                          + format(j["out_usd"], ".2f") + " still out, and a $"
                          + format(cap, ".2f") + " page does not fit under $"
                          + format(j["ceiling_usd"], ".2f"))
            j["last_moved"] = db.now()
            _save(data)
            return {"ok": False, "why": "the job '" + j["title"] + "' is "
                    + j["needs"] + ". Nothing was sent; the ceiling is mine "
                    "to widen (op widen), or I close the job."}
        j["out_usd"] = round(j["out_usd"] + cap, 4)
        j["last_moved"] = db.now()
        _save(data)
        return {"ok": True}


def settle(title, cap_usd, actual_usd) -> None:
    """The page is home: the reservation comes off, what it truly cost goes
    on. Never raises -- a settle is bookkeeping after the fact."""
    with _LOCK:
        data = _load()
        j = _find_open(data, title)
        if not j:
            return
        j["out_usd"] = round(max(0.0, j["out_usd"] - float(cap_usd or 0)), 4)
        j["spent_usd"] = round(j["spent_usd"] + float(actual_usd or 0), 4)
        j["last_moved"] = db.now()
        _save(data)


def spend_link(title) -> bool:
    """One waking bought by the job, at the moment it actually wakes the
    assistant. False at zero: the report still lands, and waits for the
    owner's next line."""
    with _LOCK:
        data = _load()
        j = _find_open(data, title)
        if not j:
            return True      # a job closed while its page was out still wakes
        if j["links_left"] <= 0:
            j["last_moved"] = db.now()
            _save(data)
            return False
        j["links_left"] -= 1
        j["wakings"] += 1
        j["last_moved"] = db.now()
        _save(data)
        return True


def refill_links() -> None:
    """The owner spoke. Every open job's links stand full again -- the leash
    measures how far a job may run between the owner's appearances, not its
    life."""
    with _LOCK:
        data = _load()
        changed = False
        for j in data["jobs"]:
            if j["status"] == "open" and j["links_left"] != j["links"]:
                j["links_left"] = j["links"]
                changed = True
        if changed:
            _save(data)


def state_of(job) -> str:
    """One word of standing, computed fresh. Parked looks different from
    stuck, and what it wants from the owner comes first."""
    if job["status"] == "closed":
        return "closed"
    if job.get("needs"):
        return "over ceiling — mine to widen or close (" + job["needs"] + ")"
    if job["links_left"] <= 0:
        return ("parked at zero links — they refill when " + home.OWNER_NAME + " next speaks, "
                "or I widen them")
    return "waiting on me"


def for_prompt() -> list:
    """The assistant's block: name, state, cost against ceiling, links left,
    and its own last decision -- never the goal read back at it."""
    out = []
    with _LOCK:
        data = _load()
    for j in data["jobs"]:
        if j["status"] != "open":
            continue
        quiet = _quiet_days(j)
        entry = {
            "title": j["title"],
            "state": state_of(j),
            "spent_usd": round(j["spent_usd"], 2),
            "out_usd": round(j["out_usd"], 2),
            "ceiling_usd": j["ceiling_usd"],
            "links_left": j["links_left"],
            "links": j["links"],
            "wakings": j["wakings"],
            "decided": j["decided"],
            "opened": j["opened"],
        }
        if quiet >= 2:
            entry["quiet"] = ("nothing has moved on this for " + str(quiet)
                              + " days")
        out.append(entry)
    return out


def status() -> dict:
    with _LOCK:
        data = _load()
    open_n = sum(1 for j in data["jobs"] if j["status"] == "open")
    return {"open": open_n, "closed": len(data["jobs"]) - open_n,
            "jobs": data["jobs"]}


def main():
    view = status()
    print("jobs: " + str(view["open"]) + " open, "
          + str(view["closed"]) + " closed")
    for j in view["jobs"]:
        line = ("  " + j["title"] + "  " + j["status"] + "  $"
                + format(j["spent_usd"], ".2f") + " of $"
                + format(j["ceiling_usd"], ".2f")
                + "  links " + str(j["links_left"]) + "/" + str(j["links"]))
        if j["status"] == "closed":
            line += "  -- " + (j.get("outcome") or "")[:80]
        elif j.get("decided"):
            line += "  -- last decided: " + j["decided"][:80]
        print(line)


if __name__ == "__main__":
    main()
