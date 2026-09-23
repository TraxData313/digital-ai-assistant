"""Small prompt views and read-only references over existing room records.

The files/read protocol pages these references. No new output fields, model
calls, store migrations, writes, generic URL access or execution capability.
"""
import json
import re

from . import files, jobs, projects, providers, quoted
from . import home

# The prefix of the room's own named references: the kind the assistant's
# lines are stored under, so a home's own prompts keep naming them as
# they always have.
H = home.SELF + ":"


# Headings, not copied manuals: changes to the existing operation instructions
# and rendered limits are seen on the next explicit read.
TOPICS = {
    "memory": ("recall, shelf search, fetch/source trails and essence operations",
               ("## `self`", "## `messages`", "## `out_of_reach`", "## `automatic_memory`",
                "### `essences`", "### `fetch`", "### `search`", "### Looking", "### `shelf`")),
    "projects": ("desk, tasks, notices, resources and attributed notes",
                 ("## `projects`", "### `project`")),
    "jobs": ("job bookkeeping, decisions and history; worker execution paused",
             ("## `jobs`", "### `job`")),
    "clock": ("free time, invitations, schedules and continuing a stretch",
              ("## `clock`", "### `clock`")),
    "senses": ("current senses, muting and unmuting; Claude quota senses paused",
               ("## `watch`", "### `watch`")),
    "files": ("read/list/find within the existing filesystem reach", ("### `files`",)),
    "web": ("read a public page; model-backed search paused", ("### `web`",)),
    "comments": ("read mod comments and guarded public posting", ("### `comments`",)),
    "conversation": ("reply routing, pictures, state and the JSON response contract",
                     ("## `self`", "## `plan`", "## `pictures`", "## `report`",
                      "## `woken`", "## `found`", "## `room_recap`", "## What I answer")),
    "spark": ("deliberate Spark and budget changes", ("### `spark`",)),
    "restart": ("the existing between-turn restart operation", ("### `restart`",)),
}


def index() -> str:
    return "\n".join("- `" + H + "help/" + topic + "` — " + detail
                     for topic, (detail, _) in TOPICS.items())


def instructions(topic: str) -> str:
    if topic not in TOPICS:
        raise files.Refused("Unknown operation reference. Available references:\n" + index())
    from . import brain
    source = brain.operation_harness(False)
    sections = re.split(r"(?m)(?=^#{1,3} )", source)
    wanted = TOPICS[topic][1]
    found = [section for section in sections if section.startswith(wanted)]
    if not found:
        raise files.Refused("The operation reference is missing from the room manual: " + topic)
    return ("Room operation reference: " + topic + ". Source: server/harness_prompt.md.\n"
            "Use the current output schema. Spark, attribution, safety boundaries and pause reasons still apply.\n"
            "The compact routine summaries omit detail; read their " + H + " handles for the complete records.\n"
            + ("The current task.clock/parser decides whether schedule words fire; consult the task reference, "
               "not an old example's claim that words never fire.\n" if topic in ("projects", "clock") else "")
            + "\n" + "\n".join(found))


def _preview(text, limit=100):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "… [read full record]"


def jobs_summary() -> list:
    out = []
    for job in jobs.status()["jobs"]:
        if job["status"] != "open":
            continue
        item = {"title": job["title"], "state": jobs.state_of(job),
                "worker_execution": "paused — Codex-only mode",
                "read": (H + "job/") + str(job["id"])}
        if job.get("decided"):
            item["decided_preview"] = _preview(job["decided"])
        quiet = jobs._quiet_days(job)
        if quiet:
            item["quiet_days"] = quiet
        out.append(item)
    return out


def _notice_count(conn, task_id):
    return conn.execute("SELECT COUNT(*) FROM task_notices WHERE task = ? AND done = 0",
                        (task_id,)).fetchone()[0]


def task_summary(conn, task):
    out = {"title": task["title"], "state": task["state"], "asked_by": task["who"],
           "read": (H + "task/") + str(task["id"])}
    for key in ("needs", "at", "job", "sense"):
        if task.get(key):
            out[key] = task[key]
    if task.get("repeats"):
        out["repeats"] = True
        clock = projects._clock_line(task)
        if clock and not clock.startswith("on my clock:"):
            out["schedule_paused"] = clock
    if task.get("job"):
        linked = next((j for j in jobs.status()["jobs"]
                       if j["title"].casefold() == task["job"].casefold()), None)
        if linked:
            out["job_read"] = (H + "job/") + str(linked["id"])
        else:
            out["job_missing"] = "Linked job not found; the task remains pending."
    pending = _notice_count(conn, task["id"])
    if pending:
        out["notices"] = pending
    # Dormant work stays discoverable by title, author, state and handle. Work
    # in progress or with new arrivals keeps a short statement of what it wants.
    if task["state"] == "doing" or pending:
        if task.get("wants"):
            out["wants_preview"] = _preview(task["wants"])
    if task.get("sense") in ("quota_low", "window_reset"):
        out["pause_reason"] = providers.CLAUDE_PAUSED
    return out


def projects_summary(conn) -> dict:
    shelf, desk = [], []
    for row in conn.execute("SELECT * FROM projects WHERE status = 'open' ORDER BY id"):
        project = dict(row)
        tasks = projects.tasks_of(conn, project["id"], live_only=True)
        line = {"title": project["title"], "owner": project["owner"],
                "open_tasks": len(tasks), "read": (H + "project/") + str(project["id"])}
        pending = sum(_notice_count(conn, t["id"]) for t in tasks)
        if pending:
            line["notices"] = pending
        if project["desk"]:
            line["on_my_desk"] = True
            desk.append({"title": project["title"],
                         "open_tasks": [task_summary(conn, t) for t in tasks]})
        shelf.append(line)
    return {"shelf": shelf, "on_my_desk": desk, "read_all": (H + "projects")}


def _record(conn, kind, number):
    if kind == "job":
        job = next((j for j in jobs.status()["jobs"] if j["id"] == number), None)
        if job is not None:
            return {"job": job, "worker_execution": providers.CLAUDE_PAUSED,
                    "history": "data/jobs.json and job/decide rows via fetch"}
    else:
        if conn is None:
            raise files.Refused("This record requires the room's existing read connection.")
        if kind == "project":
            p = projects.get(conn, number)
            if p is not None:
                return {"project": p,
                        "tasks": [task_summary(conn, t) for t in projects.tasks_of(conn, number)],
                        "notes": projects.notes_head(conn, number),
                        "notes_fetch": {"op": "project_notes", "text": p["title"]},
                        "resources": projects.resources_of(conn, number, include_gone=True)}
        if kind == "task":
            t = projects.task(conn, number)
            if t is not None:
                p = projects.get(conn, t["project"])
                from . import watch
                return {"task": t, "project": {"title": p["title"], "owner": p["owner"]},
                        "clock": projects._clock_line(t),
                        "last_look": projects.look_for(t, watch.looks()),
                        "notices": [dict(n) for n in conn.execute(
                            "SELECT * FROM task_notices WHERE task = ? ORDER BY id", (number,))],
                        "activity_fetch": {"op": "project_activity", "text": p["title"], "task": t["title"]}}
    raise files.Refused("No retained " + kind + " has id " + str(number) + ". Nothing was changed.")


def read(conn, handle: str) -> str:
    if not providers.codex_only():
        raise files.Refused("Named " + H + " references are available in Codex-only mode; ordinary files still work.")
    if handle.startswith((H + "help/")):
        return instructions(handle[len((H + "help/")):])
    if handle == (H + "jobs"):
        data = [{"id": j["id"], "title": j["title"], "status": j["status"],
                 "read": (H + "job/") + str(j["id"])} for j in jobs.status()["jobs"]]
    elif handle == (H + "projects") and conn is not None:
        data = [{**dict(p), "read": (H + "project/") + str(p["id"])} for p in conn.execute(
            "SELECT id, title, owner, status FROM projects ORDER BY id")]
    else:
        match = re.fullmatch(re.escape(H) + r"(job|project|task)/([1-9][0-9]*)", handle)
        if not match:
            raise files.Refused("Unknown room reference. Read a handle from the capability/work index; "
                                + H + " references do not access URLs, arbitrary files or commands.")
        data = _record(conn, match[1], int(match[2]))
    text = json.dumps(data, ensure_ascii=False, indent=2)
    # Bound even a single enormous stored field. Continuations remain exact
    # substrings, with a visible line break; files/read can page every part.
    text = "\n".join(line[i:i + 800] for line in text.splitlines()
                     for i in range(0, max(1, len(line)), 800))
    return quoted.fence(text, source=handle, what="STORED ROOM RECORD — retain its attribution")
