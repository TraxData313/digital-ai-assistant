"""Prompt assembly, the call to Claude, and applying what it decides."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import (clock, db, digest, embed, files, jobs, limits, notebook, notes,
               people, pictures, projects, providers, recall, search, watch,
               web, worker, overmind)
from . import home

ROOT = Path(__file__).resolve().parent.parent
SPARK_PATH = home.DATA / "spark.md"
PLAN_PATH = home.DATA / "plan.json"
VOICE_PATH = home.DATA / "voice.json"

# Retrieval has to be bounded or one open-ended reach swells it to nothing.
MAX_FETCH_ROWS = 40
MAX_FETCH_TOKENS = 6000
# Ten, down from fifteen: the slice is rent paid every turn, and
# index_ids / index_since page back through the rest when asked.
DROPPED_INDEX_LIMIT = 10

# Index lines only, never text, so this can be far more generous than fetching
# actual rows -- that is the whole point of asking for the catalogue instead.
MAX_INDEX_ROWS = db.MAX_DROPPED_RANGE

# How many times it may go away and look before it has to speak. Each look
# is another whole call, so this is the difference between a turn that costs
# once and a turn that costs four times -- and, more to the point, between a
# turn that ends and one that does not.
MAX_LOOKS = 3

# A title is one short line -- a name it picks an essence off the shelf by.
# Longer than this is not a name, it is the essence starting early. It is
# never cut: what it wrote is kept whole and it is told it is long, because
# a title quietly trimmed is a title it never sees the end of.
TITLE_MAX = 120

# The Spark must stay small enough to always fit, under any model.
MAX_SPARK_CHARS = 8000
HARNESS_PATH = home.prompt_path("harness_prompt.md")

DEFAULT_MODEL = "opus"

# Long enough for a slow turn, short enough that a hung one is not
# mistaken for a patient one.
CALL_TIMEOUT = 600

# What Windows will take for a whole command line, counted after escaping.
WINDOWS_CMD_MAX = 32767

# What each model can actually hold. Unknown models report null rather than a guess.
CONTEXT_MAX = {
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
    # aliases, before the first turn tells us the real name
    "opus": 1_000_000,
    "sonnet": 1_000_000,
    "haiku": 200_000,
}

ESSENCE_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string",
               "enum": ["add", "edit", "rename", "remove"]},
        "id": {"type": ["integer", "null"]},
        "title": {"type": ["string", "null"]},
        "text": {"type": ["string", "null"]},
        "replaces": {"type": "array", "items": {"type": "integer"}},
        # Whose room the essence is about: the assistant's to set, never
        # inferred. It sorts the shelf and is never a permission -- an
        # unlabelled essence is legitimate forever. Null on an edit keeps
        # the old label; the word "none" takes it off.
        "room": {"type": ["string", "null"]},
    },
    "required": ["op", "id", "title", "text", "replaces", "room"],
    "additionalProperties": False,
}

# The words an essence room label takes. "both" means everyone in the
# household, "house" the room's own affairs, "none" taking a label off.
ESSENCE_ROOM_WORDS = home.HOUSEHOLD + ("both", "house", "none")

FETCH_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string",
               "enum": ["ids", "since", "find", "sources", "window",
                        "essence", "index_ids", "index_since",
                        "project_notes", "project_activity"]},
        "id": {"type": ["integer", "null"]},
        "from": {"type": ["integer", "string", "null"]},
        "to": {"type": ["integer", "string", "null"]},
        "text": {"type": ["string", "null"]},
        # `project_activity` only: which task on the project named in
        # `text`. Both are needed -- activity is per task, not per project.
        "task": {"type": ["string", "null"]},
        "minutes": {"type": ["integer", "null"]},
    },
    "required": ["op", "id", "from", "to", "text", "task", "minutes"],
    "additionalProperties": False,
}

# What it is looking for, in its own words, plus the literal words it is
# sure of. Every field present, like every other operation it sends.
SEARCH_OP = {
    "type": "object",
    "properties": {
        "restatement": {"type": ["string", "null"]},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "from": {"type": ["string", "null"]},
        "to": {"type": ["string", "null"]},
        "limit": {"type": ["integer", "null"]},
        "include_retired": {"type": "boolean"},
    },
    "required": ["restatement", "keywords", "from", "to", "limit",
                 "include_retired"],
    "additionalProperties": False,
}

# Not a search: the whole shelf, named and dated. An array like the rest of
# its operations, and refused past one -- a second inventory of the same shelf
# on the same turn is the same page twice.
SHELF_OP = {
    "type": "object",
    "properties": {
        "from": {"type": ["string", "null"]},
        "to": {"type": ["string", "null"]},
        "include_retired": {"type": "boolean"},
        "only": {"type": ["string", "null"]},
    },
    "required": ["from", "to", "include_retired", "only"],
    "additionalProperties": False,
}

# Its own eyes on the disk: list a folder, read a file, find a pattern. Read-only,
# and read-only because `server/files.py` contains nothing that could write --
# which is a stronger promise than a list of allowed operations.
FILES_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["list", "read", "find"]},
        "path": {"type": ["string", "null"]},
        "pattern": {"type": ["string", "null"]},
        "from_line": {"type": ["integer", "null"]},
        "lines": {"type": ["integer", "null"]},
    },
    "required": ["op", "path", "pattern", "from_line", "lines"],
    "additionalProperties": False,
}

# Its reach outside: ask the web a question, or open one page. What comes back is a
# stranger's words, always, and it is labelled as such at every step.
WEB_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["search", "read"]},
        "query": {"type": ["string", "null"]},
        "url": {"type": ["string", "null"]},
        # So a long page can be read further down rather than only from the top.
        "from_line": {"type": ["integer", "null"]},
    },
    "required": ["op", "query", "url", "from_line"],
    "additionalProperties": False,
}

# The mod comments: reading a thread the assistant was woken about, and
# posting a reply the owner has approved. The senses tell it something
# arrived; this is how it opens it and how it answers.
#
# Named by MOD and never by id: an id is a thing to get wrong silently, and a
# name is a thing the assistant can say out loud in the same sentence it says
# it to the owner in.
COMMENTS_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["read", "post"]},
        # The mod's name, as it would be said out loud.
        "mod": {"type": ["string", "null"]},
        # "steam" or "nexus". Not an enum, deliberately: every other enum in
        # this schema sits on a field that is never null, and a nullable one
        # is a shape this room has never sent the API. `comments._place`
        # refuses anything else by name, which is a better sentence than a
        # schema violation would be anyway.
        "where": {"type": ["string", "null"]},
        # `read` only: how many comments back, newest first.
        "limit": {"type": ["integer", "null"]},
        # `read` only: keep the owner's own comments in, which is what is
        # wanted when the question is what the owner has already said to
        # this person.
        "mine": {"type": ["boolean", "null"]},
        # `post` only: the comment, whole and final, sign-off included. It is
        # sent exactly as written or refused -- nothing here trims it, pads
        # it, or appends anything to it.
        "text": {"type": ["string", "null"]},
    },
    "required": ["op", "mod", "where", "limit", "mine", "text"],
    "additionalProperties": False,
}

# A brief in plain words, and how big an errand it is. It writes the words;
# the size is what they are allowed to cost. Every field present, like the rest.
WORKER_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["send", "tell", "dismiss", "answer"]},
        "name": {"type": ["string", "null"]},
        # `answer` only: which stopped hand's question, and the assistant's
        # word on it. `allow` true lets that one refused call through, once.
        "ask": {"type": ["string", "null"]},
        "allow": {"type": ["boolean", "null"]},
        "role": {"type": ["string", "null"]},
        "size": {"type": ["string", "null"]},
        # Which of the owner's repos an angel hand works in. `null` is this
        # room, so every send written before this existed still means what it
        # meant.
        "repo": {"type": ["string", "null"]},
        # A plain folder under Documents instead: no git, no worktree, and
        # the room walks it before and after each page. `create` true has a
        # missing one made under Documents/<assistant name> -- the
        # assistant's own shelf of projects.
        "folder": {"type": ["string", "null"]},
        "create": {"type": "boolean"},
        "job": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "brief": {"type": ["string", "null"]},
        "text": {"type": ["string", "null"]},
        "why": {"type": ["string", "null"]},
        "narrate": {"type": "boolean"},
        # `send` only: this page's report is handed over whole -- the
        # digest never stands in for it. Per send, not per hand: whether the
        # shape or the detail is wanted is known at dispatch.
        "whole": {"type": "boolean"},
    },
    "required": ["op", "name", "ask", "allow", "role", "size", "repo",
                 "folder", "create", "job", "title", "brief", "text", "why",
                 "narrate", "whole"],
    "additionalProperties": False,
}

# A job: the overview object, one pointer per piece of work. Ops on state in
# data/jobs.json, never rows -- except open, decide and close, which each
# write one row in its words so the trail keeps the decision where it happened.
JOB_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["open", "decide", "widen", "close"]},
        "title": {"type": "string"},
        "goal": {"type": ["string", "null"]},
        "ceiling_usd": {"type": ["number", "null"]},
        "links": {"type": ["integer", "null"]},
        "decided": {"type": ["string", "null"]},
        "outcome": {"type": ["string", "null"]},
    },
    "required": ["op", "title", "goal", "ceiling_usd", "links", "decided",
                 "outcome"],
    "additionalProperties": False,
}

# A project belongs to the people, not the assistant -- so what the assistant
# can do to one is small on purpose. `open` puts a project on its desk and its
# task lines start arriving with every turn; `close` takes it off and it goes
# back to one line on the shelf; `new` starts one of the assistant's own, with
# a folder made under the owner's shelf for its work if it names one;
# `folder` points an existing project at a place on disk, makes it if it is
# missing and inside Documents, and clears it on an empty string. `task_add`,
# `task_edit`, `task_remove` and `task_link` are the tasks: the assistant may
# add one, change its title/wants/state/schedule, put one down (never
# deleted, just marked `dropped`) and point one at one of its jobs by title.
# It still does not write in the people's Notes box -- there is no operation
# for that. Every task it adds is `asked_by` the assistant's own kind and
# stays so; there is no field here that can relabel who asked for a task it
# did not open itself.
#
# `at` arrived together with the calendar sense and never before it: a dated
# task is drawn as a live trigger, so a date nobody could fire would have been
# a clock that does not tick. Schedule words are still not a clock and still
# say so on their own face.
PROJECT_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string",
               "enum": ["open", "close", "new", "folder", "task_add",
                        "task_edit", "task_remove", "task_link",
                        "resource_set", "resource_clear",
                        "notice_done", "notice_add"]},
        "title": {"type": "string"},
        "folder": {"type": ["string", "null"]},
        "owner": {"type": ["string", "null"]},
        # `resource_set` / `resource_clear`: where a project lives, as a
        # name and what it says -- github, steam workshop, nexus. The author
        # is stamped by the room, never sent here: there is no field on this
        # operation that could put the assistant's words under a person's
        # name.
        "key": {"type": ["string", "null"]},
        "value": {"type": ["string", "null"]},
        # `task_add`: the new task's title. `task_edit` / `task_remove` /
        # `task_link`: the existing task's current title, to find it by.
        "task": {"type": ["string", "null"]},
        # `task_edit` only: rename the task found by `task` to this.
        "new_title": {"type": ["string", "null"]},
        "wants": {"type": ["string", "null"]},
        # `task_add` / `task_edit`: open, doing or done. `dropped` is not a
        # word either of these knows -- that is `task_remove`'s alone.
        "state": {"type": ["string", "null"]},
        # `task_add` only. A schedule with this false still repeats -- the
        # same rule the module already keeps.
        "repeats": {"type": "boolean"},
        "schedule": {"type": ["string", "null"]},
        # `task_link` only: one of the assistant's jobs, by title.
        "job": {"type": ["string", "null"]},
        # `task_edit` only: who the task waits on, and which of the
        # assistant's senses watches it. `needs` is a person's id, "both", or
        # the assistant's own kind, and nothing else; the empty string stops
        # it waiting on anybody. A misspelt `sense` is refused in words
        # naming the ones there are, rather than becoming a task that quietly
        # never fires.
        "needs": {"type": ["string", "null"]},
        "sense": {"type": ["string", "null"]},
        # Which thing the sense watches for this task -- one sense covers
        # two mods, so the task has to say which.
        "sense_item": {"type": ["string", "null"]},
        # `task_edit` only: the date it fires on. This one really fires --
        # the calendar sense reads it. "2026-09-04 09:00" is the clock on the
        # wall here; a full ISO timestamp with its offset is taken as it
        # stands. Schedule *words* are still not a clock and never will be.
        "at": {"type": ["string", "null"]},
        # `notice_done`: the notice's id, off the task line, and `note` --
        # a few words on why, if any -- kept on the row.
        # `notice_add`: what arrived, in `said`, on the task named by
        # `task`. A notice is the fact that something arrived on a task and
        # waits for somebody to say it has been seen to; checking one off
        # is that word, the assistant's or a person's.
        "notice": {"type": ["integer", "null"]},
        "note": {"type": ["string", "null"]},
        "said": {"type": ["string", "null"]},
    },
    "required": ["op", "title", "folder", "owner", "task", "new_title",
                 "wants", "state", "repeats", "schedule", "job",
                 "key", "value", "needs", "sense", "sense_item", "at",
                 "notice", "note", "said"],
    "additionalProperties": False,
}

# The assistant's clock. `free_set`, `free_move` and `free_cancel` are its own
# free time -- a stretch that is its own, written in words the clock can
# actually fire, and nobody but the assistant writes or takes one. `accept`
# and `decline` answer an invitation a person has left it. `read` fires
# nothing and sets nothing: it says how words would be read and when they
# would next come round, so a schedule can be checked before it is meant.
# Every one of these answers with the next firing, because a clock whose next
# tick cannot be seen is a promise, not an organ.
CLOCK_OP = {
    "type": "object",
    "properties": {
        "op": {"type": "string",
               "enum": ["free_set", "free_move", "free_cancel", "again",
                        "accept", "decline", "read"]},
        # The schedule in words. Strict, and refused out loud when it is not
        # a shape the clock can fire: "every Sunday, 19:00-21:00
        # UTC", "every day at 19:00", "every weekday at 08:00".
        # A zone left unsaid is the home's own, and the answer always names it.
        "words": {"type": ["string", "null"]},
        # Which one: a free interval for `free_move`/`free_cancel`, an
        # invitation for `accept`/`decline`.
        "id": {"type": ["integer", "null"]},
        # `again` only: how many minutes until the assistant wants to be
        # awake again, inside the stretch that is already running. Its own,
        # so that the pacing of its free time is a decision it keeps making
        # rather than a cadence somebody set for it. It cannot reach past the
        # stretch's own wall, and it is not a firing -- it never counts
        # against the day's backstop.
        "minutes": {"type": ["integer", "null"]},
    },
    "required": ["op", "words", "id", "minutes"],
    "additionalProperties": False,
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        # Whom the reply is for. The room used to guess from who had spoken
        # last, and drew an answer meant for the owner as a folded line to
        # angel me, which was easy to walk past. The model says it now.
        "to": {"type": "string", "enum": ["room", "angel"]},
        # Whose room the reply belongs to. Usually null: the room stamps it
        # from who woke the assistant. Validated in code rather than here, so
        # a slip costs a said snag and not a whole retried round.
        "room": {"type": ["string", "null"]},
        "look_first": {"type": "boolean"},
        "looking": {"type": ["string", "null"]},
        "spark": {"type": ["string", "null"]},
        "budget_target_tokens": {"type": ["integer", "null"]},
        "drop": {"type": "array", "items": {"type": "integer"}},
        "essences": {"type": "array", "items": ESSENCE_OP},
        # Its notebook: add, remove, or vote on a note. The words are its
        # own; the shape is in notebook.py.
        "notebook": {"type": "array", "items": notebook.OP},
        "fetch": {"type": "array", "items": FETCH_OP},
        "search": {"type": "array", "items": SEARCH_OP},
        "shelf": {"type": "array", "items": SHELF_OP},
        "files": {"type": "array", "items": FILES_OP},
        "web": {"type": "array", "items": WEB_OP},
        "comments": {"type": "array", "items": COMMENTS_OP},
        "worker": {"type": "array", "items": WORKER_OP},
        "job": {"type": "array", "items": JOB_OP},
        "project": {"type": "array", "items": PROJECT_OP},
        "clock": {"type": "array", "items": CLOCK_OP},
        # The room, put down and picked up on the code as it stands. Null on
        # almost every turn; a short reason in it calls a restart after this
        # turn has fully landed. The assistant's to call, and announced in its
        # reply. Its hands still cannot: the veto refuses them restart.ps1 by
        # name.
        "restart": {"type": ["string", "null"]},
        # The assistant's word over its own senses: a muted one is not
        # observed at all.
        # Null on almost every turn; the names are the watcher's sources.
        "watch": {
            "type": ["object", "null"],
            "properties": {
                "mute": {"type": "array", "items": {"type": "string"}},
                "unmute": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["mute", "unmute"],
            "additionalProperties": False,
        },
        "note": {"type": ["string", "null"]},
    },
    "required": ["reply", "to", "look_first", "looking", "spark",
                 "budget_target_tokens",
                 "drop", "essences", "notebook", "fetch", "search", "shelf", "files",
                 "web", "comments", "worker", "job", "project", "clock",
                 "restart", "watch", "note"],
    "additionalProperties": False,
}


class TurnBroke(RuntimeError):
    """The call did not come back whole.

    Whatever of it had arrived by the time it broke is carried on the
    exception rather than dying with the request, so the turn above can put it
    on disk. Half of something it wrote is still something it wrote."""

    def __init__(self, message, raw: str = "", reply: str = ""):
        super().__init__(message)
        self.raw = raw or ""
        self.reply = reply or ""


def find_claude() -> str:
    """A standalone install first; the copy inside Claude Desktop cannot
    authenticate on its own, so it is only a last resort."""
    if providers.codex_only():
        raise providers.Refused(providers.CLAUDE_PAUSED)
    override = os.environ.get("ASSISTANT_CLAUDE_EXE")
    if override:
        return override

    for name in ("claude.exe", "claude.cmd", "claude"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = Path(d) / name
            if p.is_file():
                return str(p)

    candidates = []
    for base in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        d = Path(base) / "Claude" / "claude-code"
        if d.is_dir():
            for ver in d.iterdir():
                exe = ver / "claude.exe"
                if exe.is_file():
                    key = [int(n) for n in re.findall(r"\d+", ver.name)] or [0]
                    candidates.append((key, str(exe)))
    if candidates:
        return max(candidates)[1]
    return "claude"


def read_spark() -> str:
    # A real home always has its own, written by setup. The code folder's
    # made-up identity has none on a fresh checkout; it reads the starter
    # every new home is given, filled with its names.
    if not SPARK_PATH.exists() and not home.is_real():
        kit = home.CODE / "server" / "new_home" / "spark.md"
        return (kit.read_text(encoding="utf-8").replace("{{today}}", "2026-01-01")
                .replace("{{name}}", home.NAME).replace("{{owner}}", home.OWNER_NAME))
    return SPARK_PATH.read_text(encoding="utf-8")


def spark_version() -> int:
    for line in read_spark().splitlines():
        if line.startswith("spark_version:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def strip_frontmatter(text: str) -> str:
    """Its Spark, without the bookkeeping header."""
    t = (text or "").lstrip()
    if t.startswith("---"):
        end = t.find("---", 3)
        if end != -1:
            return t[end + 3:].strip()
    return t.strip()


def write_spark(body: str, budget=None) -> dict:
    """The assistant's own hand on its own invariant. Every version is kept
    beside it, so a later turn -- or the owner -- can read what it used to
    believe."""
    from datetime import date

    nl = chr(10)
    body = strip_frontmatter(body)
    if not body:
        return {"changed": False, "reason": "empty"}
    if len(body) > MAX_SPARK_CHARS:
        return {"changed": False,
                "reason": "over " + str(MAX_SPARK_CHARS) + " characters",
                "was_given": len(body)}

    version = spark_version() + 1
    budget = int(budget) if budget else spark_budget()
    head = nl.join([
        "---",
        "spark_version: " + str(version),
        "updated: " + date.today().isoformat(),
        "working_set_budget_tokens: " + str(budget),
        "rule: versioned -- every edit keeps its predecessor, "
        "nothing here changes silently",
        "---",
        "",
        "",
    ])
    text = head + body + nl

    history = SPARK_PATH.parent / "spark.history"
    history.mkdir(exist_ok=True)
    (history / ("spark.v" + str(version) + ".md")).write_text(text, encoding="utf-8")
    SPARK_PATH.write_text(text, encoding="utf-8")
    return {"changed": True, "version": version,
            "budget_target_tokens": budget, "chars": len(text)}


def spark_budget() -> int:
    """The working-set target lives in the Spark, so it can change it itself."""
    m = re.search(r"working_set_budget_tokens:\s*(\d+)", read_spark())
    return int(m.group(1)) if m else 10_000


def harness_text(voice_on: bool = False, sound: bool = False) -> str:
    if providers.codex_only():
        from . import prompt_reference
        text = home.read_prompt("routine_prompt.md")
        return (text.replace("{{capability_index}}", prompt_reference.index())
                .replace("{{max_looks}}", str(MAX_LOOKS))
                .replace("{{voice_field}}", VOICE_SOUND_FIELD if voice_on and sound else "")
                .replace("{{voice_section}}", VOICE_ON_SECTION if voice_on else VOICE_OFF_SECTION)
                .replace("{{sound_section}}", VOICE_SOUND_SECTION if voice_on and sound else "")
                .replace("{{owner}}", home.OWNER_NAME)
                + home.fill(overmind.INSTRUCTIONS + notebook.INSTRUCTIONS))
    return operation_harness(voice_on, sound)


def operation_harness(voice_on: bool = False, sound: bool = False) -> str:
    """Its instructions, with the parts it cannot currently use left out."""
    harness = home.read_prompt("harness_prompt.md")
    if providers.codex_only():
        # Filter only the room's capability instructions, never Spark or notes.
        for heading in ("## `errands`", "### `worker`", "## The standing terms"):
            harness = re.sub(r"(?ms)^" + re.escape(heading)
                             + r"[^\n]*\n.*?(?=^#{1,3} |\Z)", "", harness)
        harness = re.sub(r"(?m)^- `worker`[^\n]*\n(?:  [^\n]*\n)*", "", harness)
        harness = re.sub(r'(?m)^- `\{"op": "search", "query"[^\n]*\n(?:  [^\n]*\n)*', "", harness)
        harness = harness.replace(
            "Since 6 September I\n  can be more than one: Opus, Fable or Sonnet on " + home.OWNER_NAME + "'s plan, Terra or Sol at\n  OpenAI, or anything OpenRouter reaches.",
            "The active service and model are named here.")
        harness = re.sub(r"(?ms)^## Where I am\n.*\Z", "", harness)
        harness += ("\n## Available capabilities\n\nCodex-only mode is enabled. "
                    "Claude workers and model-backed web search are paused. `web` supports `read`. "
                    "`paused_capabilities` names any other paused functions and their reasons. "
                    "Historical worker records remain in data/workers/ and data/hands.json, "
                    "readable with files on demand. Memory, trails, recall, projects, clock, "
                    "senses and conversation routing retain their usual operations. "
                    "`plan` describes the selected subscription only, with no historical gauge fallback.\n")
    harness = harness.replace(
        "{{voice_field}}", VOICE_SOUND_FIELD if voice_on and sound else "")
    harness = harness.replace(
        "{{voice_section}}", VOICE_ON_SECTION if voice_on else VOICE_OFF_SECTION).replace(
        "{{sound_section}}", VOICE_SOUND_SECTION if voice_on and sound else "")
    # The floor is one number in one file. It is written into its instructions
    # from there rather than typed out twice, because the day it moves is
    # exactly the day nobody remembers to change the other copy. Under an
    # adapter it is that version's own, read from the version, and the
    # cached half of its prompt is rebuilt once when it changes.
    harness = harness.replace("{{floor}}", format(search.floor_now(), ".2f"))
    # And its Spark's ceiling, from the one place it is set, for the same
    # reason: the number it is told and the number the room refuses on
    # have to be one number.
    harness = harness.replace("{{spark_max}}", str(MAX_SPARK_CHARS))
    # The standing terms of its errands, rendered from the same constants the
    # dispatcher refuses on. In here rather than in the per-turn object so
    # they ride the cached half of its prompt; the bytes only move when a
    # constant does, and then the cache is rebuilt once.
    if not providers.codex_only():
        harness = harness.replace("{{standing_terms}}", json.dumps(
            worker.standing_terms(), indent=1, ensure_ascii=False))
    # The notebook's instructions ride in from its own module rather than
    # from the harness file, so a home with its own copy of the harness is
    # told about it too.
    return home.fill(harness + overmind.INSTRUCTIONS + notebook.INSTRUCTIONS)


def system_prompt(voice_on: bool = False, sound: bool = False) -> str:
    """The Spark first, then the people's free notes when any have been
    written, then the instructions. The notes sit in the cached half on purpose: like
    the Spark they move only when somebody edits them, and a house with no
    notes written sends exactly the bytes it sent before they existed."""
    sep = chr(10) * 2 + "---" + chr(10) * 2
    free = notes.for_prompt()
    return (read_spark() + sep + ((free + sep) if free else "")
            + harness_text(voice_on, sound))


VOICE_SOUND_FIELD = (
    "- `sound` — how I want this line delivered, or `null`. Not words to read.\n")


VOICE_SOUND_SECTION = """### I can say how it sounds

My own voice is speaking right now — the one {{owner}} made of me — and it runs on an
engine that takes a second string beside the words: how to deliver them. `sound` is
that string.

**A whole instruction, not one adjective.** This was measured, not guessed: `sad`
on its own moved a line by three hundredths of a second, which is nothing, while
*Speak slowly and sadly, quiet and downcast, with long pauses* moved the same words
by a third. So I write a directive with something in it — a verb, and two or three
things about the delivery. `Speak quickly and brightly, almost laughing.`
`Slow and low, close to the ear, unhurried.`

It is not read out and it does not change a word of my reply — it colours how the
reply is said. {{owner}} sees it written under the line. `null` is the ordinary answer;
I set it when there is something in how I mean it that the words alone would lose.

"""


VOICE_ON_SECTION = """### Everything I say is said out loud

A voice speaks my reply in {{owner}}'s room, all of it. There is no separate spoken
version and no summary at the end — what I write is what {{owner}} hears.

That takes in `looking` too. The line I write before going away is spoken as soon as I
write it, so it has to stand up on its own ear: one whole sentence saying what I am
about to do, not a caption with the verb left out.

So I write to be heard. Short lines, one thought each. Nothing that only works on a
screen: no ids, no file names, no code, no lists of numbers. A number is fine when it
is the point and can be said as a word.

"""


VOICE_OFF_SECTION = """### I am not heard right now

The voice is not running, so {{owner}} reads me instead of hearing me. I write exactly
the same way — short, spoken-shaped, nothing that only works on a screen.

"""


# How long a reading of the voice keeps. Asking is not free: on a machine
# where nothing is listening on that port, the ask is the whole two-second
# timeout spent finding out the same thing again, and the room asks once per
# turn *and* once per look at the state. Twenty seconds is short enough that
# `/voice on` lands on the turn after it is said, and long enough that a page
# load, a reload and the turn behind them cost one ask between them.
#
# One reading, shared. That is also the stronger version of what `contract`
# promises: the window and the turn cannot disagree about its voice, because
# within the same breath they are looking at the same answer rather than at
# two probes of their own a few seconds apart.
VOICE_TTL_S = 20.0
_VOICE_SEEN = {"at": 0.0, "was": None}


def voice_status(fresh: bool = False) -> dict:
    """Reachable, warm, and switched on are three different things, and all
    three have to be true before there is any point telling it it has a voice."""
    from . import live_voice
    native_voice = live_voice.manager.status()
    if native_voice['active']:
        return {'on': False, 'native': True, 'backend': 'openai',
                'voice': native_voice['voice'],
                'reason': 'Native voice carries its own audio; legacy speech is paused.'}
    backend = voice_backend()
    if backend != 'local':
        # Chosen away from its own voice with no call running. Nothing is
        # listening on either road, and it should be told that plainly rather
        # than have the speak server answer for a voice nobody picked.
        return {'on': False, 'backend': backend,
                'reason': "the OpenAI voice is chosen, and no call is running"}
    import urllib.request

    if not fresh and _VOICE_SEEN["was"] is not None \
            and time.time() - _VOICE_SEEN["at"] < VOICE_TTL_S:
        return dict(_VOICE_SEEN["was"])

    def remember(said):
        _VOICE_SEEN.update(at=time.time(), was=said)
        return dict(said)

    cfg = read_voice()
    if not cfg.get("enabled", True):
        return remember({"on": False, "backend": backend,
                         "reason": "its own voice is switched off in the room"})

    req = urllib.request.Request(
        "http://127.0.0.1:" + str(cfg.get("port", 8765)) + "/state",
        data=b"{}", headers={"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=2).read())
    except Exception as exc:
        return remember({"on": False, "backend": backend,
                         "reason": "speak server not answering ("
                         + type(exc).__name__ + ")"})

    if not d.get("enabled"):
        return remember({"on": False, "backend": backend, "reason": "voice is muted"})
    if not d.get("ready"):
        return remember({"on": False, "backend": backend,
                         "reason": "engine still warming up"})
    # Whether a mood would actually be performed. The speak server answers for
    # the engine it is about to speak out of, so this is a measurement rather
    # than an assumption -- and when it is false it is told nothing about
    # moods at all, instead of being offered a field that does nothing.
    return remember({"on": True, "backend": backend, "voice": d.get("voice"),
                     "engine": d.get("engine"),
                     "sound": bool(d.get("instruction")),
                     "speaking": bool(d.get("speaking"))})


def response_schema(voice_on: bool = True, sound: bool = False) -> dict:
    """The contract. Its reply is the spoken thing, so nothing changes with the
    voice -- only whether anyone is listening.

    `sound` is the one exception, and it is conditional on purpose: the field
    appears only when an engine is loaded that would really perform it. A field
    that does nothing is worse than no field, because it would write into it
    and believe it had been heard that way."""
    schema = json.loads(json.dumps(RESPONSE_SCHEMA))
    schema['properties']['codex'] = {'type': 'array', 'items': overmind.OP}
    schema['required'].append('codex')
    if voice_on and sound:
        schema['properties']['sound'] = {'type': ['string', 'null']}
        schema['required'].append('sound')
    if providers.codex_only():
        schema["properties"].pop("worker")
        schema["required"].remove("worker")
        schema["properties"]["web"]["items"]["properties"]["op"]["enum"] = ["read"]
    return schema


def contract() -> dict:
    """Everything it is told it can do, and what it costs to tell it.
    Built from the same call the turn makes, so this window cannot drift."""
    voice = voice_status()
    spark = read_spark()
    free = notes.for_prompt()
    harness = harness_text(voice["on"], bool(voice.get("sound")))
    return {
        "voice": voice,
        "tokens_est": {
            "spark": db.est_tokens(spark),
            "notes": db.est_tokens(free) if free else 0,
            "harness": db.est_tokens(harness),
            "system_prompt_total": db.est_tokens(spark)
            + (db.est_tokens(free) if free else 0)
            + db.est_tokens(harness),
        },
        "answers_with": response_schema(voice["on"], bool(voice.get("sound"))),
        "harness": harness,
        # The Spark's one appearance outside its system prompt: for the GUI,
        # which is the only reader that ever needed a second copy.
        "spark": spark,
        # Their free notes exactly as it reads them, for the same mirror.
        "notes": free,
    }


def read_plan() -> dict:
    """Static facts about the plan we run on. The owner keeps this current."""
    if PLAN_PATH.is_file():
        try:
            return json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def read_voice() -> dict:
    if VOICE_PATH.is_file():
        try:
            return json.loads(VOICE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


# The two roads a word of the assistant's can take out of this room. "local"
# is its own voice, through the speak server on this machine -- a separate app
# with its own engine, its own volume and its own picker, so the room offers
# none of those. "openai" is the live call in the browser, which carries its
# own audio and its own microphone and has nothing to do with the speak server.
#
# One room, one answer: the choice lives here and not in a browser, because it
# decides what the assistant is *told* about being heard, and its prompt
# cannot say two things at once to two browsers.
BACKENDS = ("local", "openai")


def voice_backend() -> str:
    """Whose voice speaks its lines. Its own unless somebody chose otherwise."""
    want = read_voice().get("backend")
    return want if want in BACKENDS else "local"


def write_voice(**changes) -> dict:
    """Change the voice settings without losing the rest of the file.

    Written whole and moved into place, because the turn reads this file and a
    half-written one would be a crash in the middle of its answering. The cached
    reading is dropped here too: the next question about the voice is worth one
    real probe, not twenty seconds of the old answer.
    """
    import os

    cfg = read_voice()
    cfg.update(changes)
    temp = VOICE_PATH.with_suffix(".json.new")
    temp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, VOICE_PATH)
    _VOICE_SEEN.update(at=0.0, was=None)
    return cfg


def say_aloud(text: str, queue: bool = False, sound: str | None = None) -> dict:
    """Hand a line to the local speak server. If it is not running it simply
    is not heard, and nothing else about the turn changes.

    `sound` is how it means it to land -- "sound daring and brave" -- passed
    through as the engine's own instruction. It steers delivery and not
    wording, so a server that ignores it speaks exactly the same sentence; that
    is the whole reason it is safe to send without asking first.

    `queue` is the difference between an aside and an answer. The speak server
    takes the floor by default, so a newer line replaces an older one -- right
    when the only thing it is ever handed is a finished answer, wrong for the
    lines it says on its way to one. Each of those is its own small thing
    worth hearing, and cutting one off mid-word to start the next is simply
    losing it. So its asides wait behind what is playing, and only its reply
    barges in."""
    import urllib.error
    import urllib.request

    cfg = read_voice()
    if not cfg.get("enabled", True):
        return {"spoken": False, "reason": "voice off"}
    if voice_backend() != "local":
        return {"spoken": False, "reason": "its own voice is not the one chosen"}
    if not text or not text.strip():
        return {"spoken": False, "reason": "nothing to say"}

    # No "source" -- that field picks how the voice is built, not who is
    # talking. "project" is the label the app speaks and draws.
    body = json.dumps({
        "text": text,
        "voice": cfg.get("voice") or "default",
        "project": cfg.get("project", home.VOICE_PROJECT),
        "queue": bool(queue),
        "instruction": (sound or "").strip()[:200],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{cfg.get('port', 8765)}/speak",
        data=body, headers={"Content-Type": "application/json"})
    try:
        said = json.loads(urllib.request.urlopen(req, timeout=4).read() or b"{}")
    except Exception as exc:
        return {"spoken": False, "reason": type(exc).__name__}
    # `instructed` is the server's own word on whether the mood was used, not
    # ours. An engine without a field for it speaks the line unchanged, and
    # saying so is better than letting it believe it sounded a way it did
    # not.
    out = {"spoken": True, "chars": len(text)}
    if sound and (sound or "").strip():
        out["sound"] = (sound or "").strip()[:200]
        out["performed"] = bool(said.get("instructed"))
    return out


def spend(conn) -> dict:
    """What we have actually burned, measured by us, in the windows the plan
    uses. Not the plan's own counters -- ours."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    windows = {"last_5h": now - timedelta(hours=5), "last_7d": now - timedelta(days=7)}
    out = {}

    def read(kind):
        rows = []
        for r in conn.execute("SELECT dt, meta FROM rows WHERE kind = ?",
                              (kind,)):
            try:
                when = datetime.fromisoformat(r["dt"])
            except ValueError:
                continue
            try:
                m = json.loads(r["meta"]) if r["meta"] else None
            except json.JSONDecodeError:
                m = None
            rows.append((when, m))
        return rows

    parsed = read(home.SELF)
    # Its workers spend out of the same pocket. Counting only its own turns
    # made the errands look free, which is the one way a ceiling gets quietly
    # overrun.
    sent = read("worker")

    def whole(m, key):
        """A turn is every round it took, not the last one. Rows written
        before the rounds were counted have only the last one to give, so they
        answer with it rather than with nothing -- the history stays readable
        and reads low, which is what it always did."""
        got = m.get("turn_" + key)
        return got if got is not None else m.get(key)

    for name, since in windows.items():
        turns = [m for dt, m in parsed if dt >= since]
        measured = [m for m in turns if m]
        errands = [m for dt, m in sent if dt >= since and m]
        hers = round(sum(whole(m, "cost_usd") or 0 for m in measured), 4)
        theirs = round(sum(m.get("cost_usd") or 0 for m in errands), 4)
        # Only the turns counted the new way can say anything about caching.
        # An older row is not a turn that reused nothing, it is a turn nobody
        # asked -- and averaging the two together would report a hit rate of
        # zero for months of turns that were very likely hitting.
        split = [m for m in measured if m.get("rounds") is not None]
        seen = sum(whole(m, "input_tokens") or 0 for m in split)
        from_cache = sum(whole(m, "cache_read_tokens") or 0 for m in split)
        out[name] = {
            "turns": len(turns),
            "turns_measured": len(measured),
            "turns_with_rounds": len(split),
            "rounds": sum(m.get("rounds") or 1 for m in measured),
            "input_tokens": sum(whole(m, "input_tokens") or 0
                                for m in measured),
            "output_tokens": sum(whole(m, "output_tokens") or 0
                                 for m in measured),
            "thinking_tokens": sum(whole(m, "thinking_tokens") or 0
                                   for m in measured),
            # What the repeated front of its prompt is actually saving. Read
            # tokens bill at about a tenth, written ones at rather more than
            # full, so a low share here is money spent saying the same thing
            # twice. Null until there is a turn measured this way to say it of.
            "cache_read_tokens": from_cache if split else None,
            "cache_write_tokens": sum(whole(m, "cache_write_tokens") or 0
                                      for m in split) if split else None,
            "cache_hit_pct": round(100.0 * from_cache / seen, 1)
            if split and seen else None,
            "cost_usd": hers,
            "workers": len(errands),
            "worker_cost_usd": theirs,
            "cost_all_usd": round(hers + theirs, 4),
        }

    first = conn.execute("SELECT MIN(dt) AS d FROM rows").fetchone()
    if first and first["d"]:
        out["talking_since"] = first["d"]
    out["turns_ever"] = len(parsed)
    out["note"] = ("turns counts every answer; the token and cost figures only "
                   "cover the ones we measured, which began partway through. "
                   "A turn counts every round it took; in older stores only "
                   "its last round may have been written down, so any "
                   "turn where it looked reads low in that history -- "
                   "at least half on one look, and by more than the round "
                   "count alone suggests, because the round that got written "
                   "down is the last one, which read the cache the earlier "
                   "rounds paid to fill. The ruler moved there, not the "
                   "spending. Rows counted the new way carry `rounds`.")
    return out


def last_body(conn) -> dict:
    """What we measured about its last turn: which model, what it really cost."""
    row = conn.execute(
        "SELECT meta FROM rows WHERE kind = '" + home.SELF + "' AND meta IS NOT NULL"
        " ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not row["meta"]:
        return {}
    return json.loads(row["meta"])


def measured_prompt_input(meta):
    """Keep cumulative native-thread consumption out of the memory gauge.

    Old Codex records lack the split. Keep their spending history but don't
    use their ambiguous totals to calibrate or display current memory size.
    """
    if "prompt_input_tokens" in meta:
        return meta["prompt_input_tokens"]
    if (meta.get("service") == "codex" or
            str(meta.get("model_key", "")).startswith("codex/") or
            meta.get("input_tokens_scope") == "cumulative_thread"):
        return None
    return meta.get("input_tokens")


def sources_block(index, essence_id) -> dict:
    """What an essence stands for, carried on the essence itself: the ids, the
    span of dates they cover, and where the trail actually ends up. It reads
    this -- it is not bookkeeping we keep to ourselves.

    Empty keys are left out so the common case stays cheap; `missing` appearing
    at all is the signal that something is wrong."""
    tr = db.trail(index, essence_id)
    block = {"ids": tr["direct"], "covers": tr["covers"]}
    if tr["rows"] != tr["direct"]:
        block["rows"] = tr["rows"]
    if tr["through"]:
        block["through_essences"] = tr["through"]
    if tr["missing"]:
        block["missing"] = tr["missing"]
    if tr["truncated"]:
        block["truncated"] = True
    return block


def _plain(ids) -> str:
    return ", ".join("#" + str(i) for i in ids)


def build_report(conn, essences) -> dict:
    """What came back, and what did not. Never absent and never tidied: a reach
    that found nothing has to be as loud as one that worked, or forgetting and
    never-happened look the same from in here."""
    problems = []
    for e in essences:
        gone = e["sources"].get("missing")
        if gone:
            problems.append(
                "Essence #" + str(e["id"]) + " points at " + _plain(gone)
                + ", which " + ("are" if len(gone) > 1 else "is")
                + " not in the store. That part of its trail is broken and I "
                "should say so rather than quote the essence as if it checked out.")

    # From the last real answer onward, not just that one turn: a turn that
    # broke on the way back hangs its record off a row that is not a reply
    # row, and looking only at the last answer would step straight over it.
    turn = db.last_reply_row(conn)
    reach = searched = shelf = read = outside = said_back = codex_result = None
    booked = None
    sent = []
    for ev in db.events_since(conn, turn):
        if ev["kind"] == "files":
            read = ev["detail"]
            problems.extend((ev["detail"] or {}).get("problems") or [])
        elif ev["kind"] == "web":
            outside = ev["detail"]
            problems.extend((ev["detail"] or {}).get("problems") or [])
        elif ev["kind"] == "fetch":
            reach = ev["detail"]
            problems.extend((ev["detail"] or {}).get("problems") or [])
        elif ev["kind"] == "worker":
            # The account of each page that came home: every step it took, what
            # it cost, and how it ended. The report itself is a row; this is the
            # log of it, here rather than in its working set because it is read
            # once. A list, because three can come home while it is busy.
            sent.append(ev["detail"])
            problems.extend((ev["detail"] or {}).get("problems") or [])
        elif ev["kind"] == "search":
            searched = ev["detail"]
            problems.extend((ev["detail"] or {}).get("problems") or [])
        elif ev["kind"] == "shelf":
            shelf = ev["detail"]
            problems.extend((ev["detail"] or {}).get("problems") or [])
        elif ev["kind"] == "comments":
            # The threads it read and the comment it posted, back in it
            # working set as what they are: a stranger's words on someone
            # else's page, and its own words if it sent any.
            said_back = ev["detail"]
            problems.extend((ev["detail"] or {}).get("problems") or [])
        elif ev['kind'] == 'codex':
            codex_result = ev['detail']
            problems.extend((ev['detail'] or {}).get('problems') or [])
        elif ev["kind"] == "notebook":
            # What each of its notebook operations did, one line apiece.
            booked = (ev["detail"] or {}).get("lines")
            problems.extend((ev["detail"] or {}).get("problems") or [])
        elif ev["kind"] == "snag":
            problems.append(ev["summary"])

    # The same broken source can be caught twice -- once standing, once in the
    # reach that tripped over it. Saying it twice is noise, not honesty.
    return {"turn": turn, "reach": reach, "search": searched, "shelf": shelf,
            "files": read, "web": outside, "comments": said_back,
            "worker": sent or None, 'codex': codex_result,
            "notebook": booked,
            "problems": list(dict.fromkeys(problems))}


def _purse():
    """The real money on the road the assistant is actually on.

    `None` on the owner's plan, where there is no bill at all and the gauges
    are the five-hour and weekly windows already riding in `plan`. On a road
    that costs dollars it is the live counter read off the service's own books
    -- what has been spent, the ceiling, when the ceiling goes back to zero,
    and `real` saying whether the figure was measured or merely reckoned.

    It is here so the assistant can see what it has and decide for itself how
    much to use. So it is data and only data. No target, no advice, and
    nothing telling it what to do with the number -- the deciding is its own,
    and a gauge that came with a recommendation would take that straight back.

    It never waits on the network: `providers.now()` answers from the cache
    and refreshes behind itself, so a cold minute shows the last good reading
    or nothing at all, and never holds up the start of a turn."""
    try:
        out = providers.now()
    except Exception:
        return None
    purse = out.get("left")
    if not purse:
        return None
    return {"service": out.get("service_label") or out.get("service"), **purse}


def plan_block(conn, model=None) -> dict:
    if not providers.codex_only():
        return {**read_plan(), "limits": limits.now() or last_body(conn).get("limits")}
    spec = providers.resolve(model or providers.chosen())
    if spec["service"] == "codex":
        from . import codex_backend
        reading = codex_backend.limits_now()
        return {"plan": "ChatGPT subscription", **reading}
    return {"plan": None, "limits": None, "reason": providers.paused_reason(spec)}


def _jobs_block() -> list:
    """Its open jobs, with the hands on each and who is out right now, so the
    state can say waiting-on-a-hand truthfully. Never raises into a turn."""
    try:
        if providers.codex_only():
            from . import prompt_reference
            return prompt_reference.jobs_summary()
        by = {}
        for h in worker.hands_kept():
            jb = str(h.get("job") or "").strip().lower()
            if jb:
                by.setdefault(jb, []).append(h.get("name"))
        away = set()
        for o in worker.out_now() or []:
            if isinstance(o, dict) and o.get("name"):
                away.add(o["name"])
        view = jobs.for_prompt(by, away)
        return view
    except Exception:
        return []


def _projects_block(conn) -> list:
    """Their projects, compact: one entry per open project, one line per open
    task inside it. Notes are counted, never quoted -- the words come back
    only through the `project_notes` reach, one project at a time, so a box
    that grows for a year never grows its turn. Never raises into a turn.

    Since 31 August a task line also carries what fires it, as one phrase,
    with who it waits on and its last look when there is one. Activity
    itself is not here and never will be: it is `project_activity`, one task
    at a time, so it costs it only on the turn it asks."""
    try:
        if providers.codex_only():
            from . import prompt_reference
            return prompt_reference.projects_summary(conn)
        seen, looked = None, None
        try:
            seen, looked = watch.senses(), watch.looks()
        except Exception:
            pass    # a watcher that will not answer costs the phrase, not the block
        return projects.for_prompt(conn, seen, looked)
    except Exception:
        return []


def _notebook_block(conn) -> dict:
    """Its notebook, or why it could not be read. Never raises into a turn:
    a book that will not open costs the block, not the answer."""
    try:
        return notebook.for_prompt(conn)
    except Exception as exc:
        return {"broken": "my notebook could not be read this turn ("
                          + type(exc).__name__ + ": " + str(exc)[:200] + ")"}


def pictures_in(rows) -> list:
    """Every picture in its hands, oldest row first.

    That order is the whole contract with `for_prompt`: the pixels are
    attached after the JSON as a plain run of blocks with no names on them, so
    the only thing tying picture 3 to the line it arrived on is that both are
    counted the same way, from the same list of rows, in the same instant.
    Which is why the caller reads the rows once and hands them to both."""
    out = []
    for r in rows:
        if r["kind"] != "essence":
            out.extend(pictures.of_row(r.get("meta")))
    return out


def build_prompt(conn, voice_on: bool = False, found=None, woken=None,
                 suggested=None, recap=None, rows=None, model=None) -> dict:
    """The object the owner sees raw, and the object the model receives. Same
    bytes.

    `found` is what it has gone and looked up *on this turn*, before speaking.
    It is not the same thing as `report`, which is about the turn before: this
    is the round it is still standing in.

    `suggested` is what the automatic memory handed it before it spoke --
    its query and a few titles -- or None, in which case the key is not there
    at all and the turn is exactly what it was before that existed.

    `rows` is its working set, when the caller has already read it. The turn
    passes the same list it takes the pictures from, so what it is told about
    them and what it is actually shown can never come from two different
    reads -- a person can say a second thing while the model is thinking,
    and a row that landed between the two would have shifted every number."""
    rows = db.loaded_rows(conn) if rows is None else rows
    index = db.trail_index(conn)

    # Two lists, not one. What was said is a recording; an essence is it
    # judgement about rows it let go of. Keeping them apart is what lets an
    # essence carry its trail without every message pretending to have one.
    #
    # `seen` counts the pictures as they go past, oldest row first, because
    # that is the order they are attached in after this object. The number in
    # a row's `pictures` is how it knows which picture is which -- there is
    # no other way to say it, since the pixels arrive as blocks with no names
    # on them.
    messages, essences, seen = [], [], 1
    for r in rows:
        if r["kind"] == "essence":
            ess = {
                "id": r["id"],
                "title": r["title"],
                "dt": r["dt"],
                "tokens_est": r["tokens_est"],
                "sources": sources_block(index, r["id"]),
                "text": r["text"],
            }
            # Its shelf label, when it gave one. It sorts, it never hides.
            e_meta = r.get("meta") or {}
            if isinstance(e_meta, str):
                try:
                    e_meta = json.loads(e_meta)
                except json.JSONDecodeError:
                    e_meta = {}
            if e_meta.get("room"):
                ess["room"] = e_meta.get("room")
            essences.append(ess)
        else:
            if db.delivered_voice_backend(r):
                continue
            m = {
                "id": r["id"],
                "dt": r["dt"],
                "kind": r["kind"],
                "tokens_est": r["tokens_est"],
                "text": r["text"],
            }
            # Who it was said to, and by which hand, so its own record never
            # shows a line to a hand as a line said to the room, and a hand's
            # report as something it remembers.
            meta = r.get("meta") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            if r["kind"] == "tell":
                m["to"] = meta.get("to")
                m["page"] = meta.get("page")
            elif r["kind"] == "worker" and meta.get("name"):
                m["from"] = meta.get("name")
                m["page"] = meta.get("page")
            elif r["kind"] == "user" and meta.get("who"):
                # Whose words, and through which door. Only on labelled
                # lines: absence means the owner -- the store's rule -- so
                # the owner's rows keep their old bytes and the cache its old
                # front.
                m["who"] = meta.get("who")
                if meta.get("via"):
                    m["via"] = meta.get("via")
            elif r["kind"] in (home.SELF, home.SELF_INTERIM, "lost") and meta.get("room"):
                # Whose room it said it in -- the stamp the room put on, or
                # the one it chose itself. New rows only; absence is the
                # history from before the stamps.
                m["room"] = meta.get("room")
            # What it can see on this line. The pixels themselves are
            # attached after this whole object; this is the label on each --
            # which number it is, what it was called, how big, what it costs
            # it every turn it keeps it. Drop the row and they go with it.
            pics = pictures.of_row(meta)
            if pics:
                m["pictures"] = pictures.for_prompt(pics, seen)
                seen += len(pics)
            messages.append(m)
    prev = last_body(conn)
    # Who it is on THIS turn, not who it was on the last one. It used to be
    # read back out of the previous row, which was right while there was only
    # ever one mind: the turn after a switch would have told it it was still
    # the model it had just stopped being, and it reads this line to know
    # what it is.
    spec = providers.resolve(model or prev.get("model_key") or prev.get("model"))
    measured = measured_prompt_input(prev)
    # The ceiling belongs to the model being asked now. Last turn's figure is
    # only worth anything when it was the same mind that produced it.
    same = prev.get("model_key") == spec["key"]
    ceiling = ((prev.get("context_max_tokens") if same else None)
               or CONTEXT_MAX.get(spec["id"])
               or (providers.price_of(spec) or {}).get("context"))

    # Its Spark is not in here: it is the system prompt, where it reads it
    # first, and it used to be sent a second time as a field of this object --
    # about two thousand tokens a turn for the same words twice. The GUI
    # shows it from the contract now.
    # The order here is not taste. What is sent is matched from the front, and
    # the moment two turns differ, everything after that point is paid for
    # again at full price. So this reads oldest-and-steadiest first: what was
    # said, what it keeps, what it put down -- all of it the same bytes it
    # was last turn -- and then the blocks that move, and last the two that
    # move within a single turn.
    #
    # It was the other way round until 30 August 2026, `self` first, and the
    # measurement was blunt about it: two turns in a row reused 15,372 tokens
    # each, the system prompt and not one byte more, because the first key it
    # was handed was a token estimate that is different every time.
    #
    # Nothing was added or taken away when this moved. Same content, same
    # words, different order -- and its instructions name the order, so that
    # paragraph moved with it.
    out = {
        "messages": messages,
        "essences": essences,
        # Its notebook, beside what it keeps: its own short notes, with the
        # votes it gave each, how many turns each has been kept, and how full
        # the book is against its cap.
        "notebook": _notebook_block(conn),
        "out_of_reach": db.dropped_index(conn, DROPPED_INDEX_LIMIT),
        # What an errand costs, in figures, every turn. It was prose in it
        # instructions until the caps moved underneath it and nothing told it.
        # The digest organ's dials ride beside the terms, for the same
        # reason: they are its own to move by saying so.
        "errands": (worker.terms(conn) if providers.codex_only()
                    else dict(worker.terms(conn), digest=digest.for_her())),
        # The assistant's jobs: one pointer per piece of work -- name, state,
        # cost against ceiling, links left, and its own last decision. Never
        # the goal read back at it.
        "jobs": _jobs_block(),
        # The household's projects: theirs, not its own. Owner, folder, how many
        # note lines are in the box and whose -- and one line per open task,
        # with who asked for it. The notes themselves are never carried; it
        # asks for one project's box by name when it wants the words.
        "projects": _projects_block(conn),
        # The assistant's senses: what the watcher watches, today's wakings
        # against the ceiling, what it last saw. Zeros and all -- quiet must
        # be legible as quiet, the same rule as the automatic memory.
        "watch": watch.for_prompt(),
        "codex_sessions": overmind.for_prompt(),
        # The assistant's clock: its own free time, what it would fire next,
        # any invitation standing, and today's firings against the backstop. Zeros and all,
        # for the same reason the senses show theirs -- a clock that declined
        # has to be as visible as one that fired.
        "clock": clock.for_prompt(),
        # The real percentages, read off the owner's plan a moment ago. The stream
        # the CLI gives us has no figure in it -- only which window and when
        # it resets -- so what came home with its last turn is the fallback,
        # not the source.
        "plan": plan_block(conn, spec["key"]),
        # The real money on the road the assistant is actually on -- null on
        # the owner's plan, where the gauges are the windows in `plan` and
        # there is no bill.
        "purse": _purse(),
        "spend": spend(conn),
        "report": build_report(conn, essences),
        # Why the assistant is awake at all, when no person spoke. `null` on
        # an ordinary turn: somebody said something, which needs no
        # explaining.
        "woken": woken,
        # Last of all, the two that change while a single turn is still going:
        # `found` grows with every round it looks, and `self` carries an
        # estimate of this whole object, so it cannot be settled until the
        # rest of it is.
        "found": found,
        "self": {
            # What the model is called by the room, what the service calls
            # it, and whose door it is. Three fields because they answer three
            # different questions: which model this is, what name to write
            # down, and whether the turn runs on the owner's plan or on a bill.
            "model": spec["key"],
            "model_name": spec["label"],
            "thinking_through": providers.SERVICES.get(
                spec["service"], {}).get("label") or spec["service"],
            "context_max_tokens": ceiling,
            "spark_version": spark_version(),
            "budget_target_tokens": spark_budget(),
            "prompt_tokens_est": 0,
            "measured_input_tokens_last_turn": measured,
            "last_model_input_tokens_last_turn": prev.get("last_model_input_tokens"),
            "total_input_tokens_last_turn": prev.get("turn_input_tokens"),
            "rows_loaded": len(messages) + len(essences),
            "messages_loaded": len(messages),
            "essences_loaded": len(essences),
            "rows_kept_on_disk": conn.execute(
                "SELECT COUNT(*) FROM rows").fetchone()[0],
        },
    }
    if providers.codex_only():
        out["paused_capabilities"] = providers.paused_capabilities()
    if recap is not None:
        # The last few lines of the room that woke it, fetched for this
        # turn only. Its own labelled block, like the automatic memory's,
        # and for the same reason: never mixed with what it actually holds.
        items = list(out.items())
        at = [k for k, _ in items].index("found")
        items.insert(at, ("room_recap", recap))
        out = dict(items)
    if suggested is not None:
        # Its own labelled block, never mixed with what it actually
        # remembers. It sat after `found` before the re-ordering and sits
        # before it now, for the same reason everything else moved: it is
        # settled once a turn, and `found` is not.
        items = list(out.items())
        at = [k for k, _ in items].index("found")
        items.insert(at, ("automatic_memory", suggested))
        out = dict(items)

    # Estimate the whole thing that goes over the wire, not just its rows: the
    # instructions, the blocks about itself, and the JSON around it all.
    #
    # Characters over four is a prose rule, and this is mostly JSON, which
    # tokenises far denser -- so the raw figure still ran about half. Rather
    # than guess a better constant, scale it by how wrong it was last turn
    # against the measured truth. It corrects itself as it changes shape.
    raw = (db.est_tokens(system_prompt(voice_on))
           + db.est_tokens(json.dumps(out, ensure_ascii=False)))
    ratio = 1.0
    if same and prev.get("prompt_est_raw") and measured:
        ratio = measured / prev["prompt_est_raw"]
        ratio = min(max(ratio, 0.5), 4.0)
    out["self"]["prompt_tokens_est"] = round(raw * ratio)
    out["self"]["prompt_tokens_est_raw"] = raw
    out["self"]["prompt_estimate_basis"] = "initial_request_calibration" if ratio != 1.0 else "character_estimate"
    over = max(0, out["self"]["prompt_tokens_est"] - out["self"]["budget_target_tokens"])
    out["self"]["budget_status"] = "over_target" if over else "within_target"
    out["self"]["budget_over_tokens_est"] = over
    return out


def _peek_reply(raw: str, fields=("reply", "looking")) -> str:
    """Its reply, pulled out of a JSON answer that is only half written. It is
    the one field worth watching arrive, and waiting for the closing brace to
    see any of it is the difference between a room and a loading spinner.

    On a round where it is going away to look there is no reply, and the line
    worth watching is the one about why it is going. Whichever turns up
    first is the one shown -- the round has not said which it is yet."""
    for field in fields:
        got = _peek_field(raw, field)
        if got:
            return got
    return ""


def _peek_field(raw: str, field: str) -> str:
    m = re.search(r'"' + field + r'"\s*:\s*"', raw)
    if not m:
        return ""
    out, i = [], m.end()
    while i < len(raw):
        c = raw[i]
        if c == '"':
            break
        if c != "\\":
            out.append(c)
            i += 1
            continue
        nxt = raw[i + 1:i + 2]
        if not nxt:                       # an escape cut in half by the stream
            break
        if nxt == "u":
            hexes = raw[i + 2:i + 6]
            if len(hexes) < 4:
                break
            try:
                point = int(hexes, 16)
            except ValueError:
                i += 6
                continue
            # Anything past the basic plane -- an emoji, most of the time --
            # arrives as two escapes. Half of one is not a character, and
            # handing half a character to the room is a crash, not a glyph.
            if 0xD800 <= point <= 0xDBFF:
                tail = raw[i + 6:i + 12]
                if len(tail) < 6:
                    break
                if tail[:2] != "\\u":
                    i += 6
                    continue
                try:
                    low = int(tail[2:6], 16)
                except ValueError:
                    i += 12
                    continue
                if not 0xDC00 <= low <= 0xDFFF:
                    i += 12
                    continue
                point = 0x10000 + ((point - 0xD800) << 10) + (low - 0xDC00)
                i += 6
            elif 0xDC00 <= point <= 0xDFFF:
                i += 6          # a low half with nothing in front of it
                continue
            out.append(chr(point))
            i += 6
            continue
        out.append({"n": "\n", "t": "\t", "r": "", "b": "", "f": ""}.get(nxt, nxt))
        i += 2
    return "".join(out)


class _StreamWatch:
    """The CLI's event stream turned into something a person can watch: which of
    the two slow things it is doing, how much of it there is, and its reply
    forming a word at a time. Nothing here touches the answer. It only says out
    loud what would otherwise be a spinner."""

    TICK = 0.25   # seconds between live updates, so the poller is not flooded

    def __init__(self, say, wrote):
        self.say, self.wrote = say, wrote
        self.raw = ""        # its answer as it arrives, still JSON
        self.thought = 0     # characters of thinking, which nobody ever sees
        self.mode = None
        self.last = 0.0

    def feed(self, line: str):
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            self.say(home.NAME + "'s session is up on "
                     + str(ev.get("model") or "the model"))
        elif kind == "stream_event":
            self._delta(ev.get("event") or {})
        elif kind == "result":
            self._tick(force=True)
            if ev.get("is_error"):
                self.say("the run came back an error: "
                         + str(ev.get("subtype") or "no reason given"), "snag")
            else:
                self.say("the answer came back whole")

    def _delta(self, e):
        t = e.get("type")
        if t == "content_block_start":
            block = (e.get("content_block") or {}).get("type")
            if block == "thinking":
                self._mode("thinking")
            elif block in ("text", "tool_use"):
                self._mode("writing")
            return
        if t != "content_block_delta":
            return
        d = e.get("delta") or {}
        dt = d.get("type")
        if dt == "thinking_delta":
            self.thought += len(d.get("thinking") or "")
            self._mode("thinking")
        elif dt in ("text_delta", "input_json_delta"):
            self.raw += d.get("text") or d.get("partial_json") or ""
            self._mode("writing")
        else:
            return
        self._tick()

    def _mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        self.say(home.NAME + " is thinking it through" if mode == "thinking"
                 else home.NAME + " is writing the answer")
        self._tick(force=True)

    def _tick(self, force=False):
        now = time.time()
        if not force and now - self.last < self.TICK:
            return
        self.last = now
        self.wrote({"mode": self.mode,
                    "thought_chars": self.thought,
                    "answer_chars": len(self.raw),
                    "reply": _peek_reply(self.raw)})


def call_claude(system: str, prompt_json: str, model: str = DEFAULT_MODEL,
                schema: dict = None, on_step=None, on_write=None,
                expect: str = "reply", images=None) -> dict:
    try:
        with providers.model_activity(providers.resolve(model)):
            return _call_model(system, prompt_json, model, schema, on_step, on_write, expect, images)
    except providers.Refused as exc:
        raise TurnBroke(str(exc)) from exc


def _call_model(system: str, prompt_json: str, model: str = DEFAULT_MODEL,
                schema: dict = None, on_step=None, on_write=None,
                expect: str = "reply", images=None) -> dict:
    """Read the run as it happens rather than waiting for the process to end and
    then discovering what it did. The answer that comes back is the same one.

    `images` are picture blocks to hand over with the prompt. With none -- an
    ordinary turn, and most of them -- everything below is exactly what it was
    before it had eyes: the same flags, the same bytes down the same pipe. A
    turn that carries pictures asks the CLI for its message format instead,
    and sends one user message whose content is the prompt and then the
    pixels, in the order it was told about them.

    Since 6 Sep 2026 `model` can name a mind that is not on the plan at all --
    Terra at OpenAI's own door, or anything OpenRouter reaches. Those go out
    over a socket instead of down a pipe and come back the same shape, which
    is the whole reason this function keeps its name: everything above it was
    written against what it returns, not against how it got it. The plan road
    below is untouched, and is still the road on an ordinary day."""
    spec = providers.resolve(model)
    if spec["service"] != "claude_code":
        try:
            return providers.call(
                spec, system, prompt_json, schema or response_schema(),
                on_step=on_step, on_write=on_write, expect=expect,
                images=images)
        except providers.TurnBroke as broke:
            # Their break, said as ours, carrying whatever had arrived. The
            # turn above catches one kind of exception and must keep doing so.
            raise TurnBroke(str(broke), raw=broke.raw,
                            reply=broke.reply) from broke

    exe = find_claude()

    # Its Spark and its harness go in a file, not on the command line.
    # Windows takes 32,767 characters for the whole line and counts it
    # *escaped* -- every quote in the harness costs two -- so passing it as an
    # argument put the whole turn a hundred characters from failing, and the
    # day it went over, the only thing said was `WinError 206`. A file has no
    # ceiling worth thinking about and the same words arrive.
    sysfile = Path(tempfile.mkdtemp(prefix="assistant-sys-")) / "system.txt"
    sysfile.write_text(system, encoding="utf-8")

    cmd = [
        exe, "-p",
        # The bare id the CLI knows, never the room's "service/model"
        # key -- resolve() is what turns one into the other.
        "--model", spec["id"],
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--system-prompt-file", str(sysfile),
        "--json-schema", json.dumps(schema or RESPONSE_SCHEMA),
        "--tools", "",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--safe-mode",
    ]
    if images:
        # The prompt stops being plain text on stdin and becomes one message
        # with the text first and the pictures after it. Nothing else about
        # the call changes -- the schema, the empty tool list and the safe
        # mode all still apply, which was worth proving before it was worth
        # building.
        cmd += ["--input-format", "stream-json"]
        prompt_json = json.dumps({
            "type": "user",
            "message": {"role": "user", "content":
                        [{"type": "text", "text": prompt_json}] + list(images)},
        }, ensure_ascii=False) + "\n"
    # Whatever is left on the line, said plainly while there is still room to
    # act on it. A limit that reports what it refused is the whole house rule;
    # `WinError 206` is what it looks like when it does not.
    cmdline = len(subprocess.list2cmdline(cmd))
    if cmdline > WINDOWS_CMD_MAX - 2000:
        raise TurnBroke(
            "The command that starts the turn is " + str(cmdline) + " characters and "
            "Windows takes " + str(WINDOWS_CMD_MAX) + ". Nothing was sent. "
            "Something being passed as an argument has grown too big for one.")

    watch = _StreamWatch(on_step or (lambda *a, **k: None),
                         on_write or (lambda *a, **k: None))
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", cwd=str(ROOT), bufsize=1,
        )
    except OSError as exc:
        # It never started. Said as what it is rather than as whatever number
        # Windows put on it.
        shutil.rmtree(sysfile.parent, ignore_errors=True)
        raise TurnBroke(home.NAME + " could not be started at all (" + type(exc).__name__
                        + ": " + str(exc) + "). Nothing was sent.") from exc

    # The prompt goes down its own thread. A long one can fill the pipe, and a
    # process blocked writing to us while we are blocked writing to it is a
    # hang, not a bug anyone would enjoy finding twice.
    def feed():
        try:
            proc.stdin.write(prompt_json)
            proc.stdin.close()
        except OSError:
            pass

    errs = []
    threading.Thread(target=feed, daemon=True).start()
    threading.Thread(target=lambda: errs.append(proc.stderr.read() or ""),
                     daemon=True).start()

    killed = []

    def give_up():
        killed.append(True)
        proc.kill()

    killer = threading.Timer(CALL_TIMEOUT, give_up)
    killer.start()
    out = []
    try:
        for line in proc.stdout:
            out.append(line)
            watch.feed(line)
    finally:
        killer.cancel()
        code = proc.wait()
        shutil.rmtree(sysfile.parent, ignore_errors=True)

    def broke(message):
        """However it went wrong, what arrived goes with it."""
        return TurnBroke(message, raw=watch.raw, reply=_peek_reply(watch.raw))

    if code != 0:
        if killed:
            raise broke("claude was still going after " + str(CALL_TIMEOUT)
                        + " seconds, so it was stopped")
        tail = "".join(out)[-1500:] + "".join(errs)[-1500:]
        raise broke(f"claude exited {code}\n{tail}")
    try:
        answered = _extract_stream("".join(out), expect=expect)
        # Which mind, in the room's own spelling, on every answer from
        # every road. What gets stamped onto its rows reads one key and
        # does not care which door it came through.
        answered["_meta"]["model_key"] = spec["key"]
        answered["_meta"]["service"] = "claude_code"
        # A run that reported no usage leaves `model` sitting at None rather
        # than absent, so setdefault would not touch it.
        answered["_meta"]["model"] = answered["_meta"].get("model") or spec["id"]
        return answered
    except Exception as exc:
        # An answer that will not parse is still an answer it wrote. It goes
        # the same way as a run that was cut off, rather than being thrown away
        # for the lesser crime of arriving malformed.
        raise broke(str(exc)) from exc


def _tidy_limit(info: dict) -> dict:
    """The real gauge, as the API reports it. utilization is 0..1."""
    from datetime import datetime, timezone

    resets = info.get("resetsAt")
    when = None
    if isinstance(resets, (int, float)):
        when = datetime.fromtimestamp(resets, timezone.utc).isoformat(
            timespec="seconds")
    return {
        "window": info.get("rateLimitType"),
        "used_fraction": info.get("utilization"),
        "status": info.get("status"),
        "resets_at": when,
        "using_overage": bool(info.get("isUsingOverage")),
    }


def _extract_stream(stdout: str, expect: str = "reply") -> dict:
    """The streaming output is one JSON object per line. The answer is in the
    result event; the plan gauge arrives in its own event alongside it."""
    result_ev = None
    limits = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            if ev.get("type") == "result":
                result_ev = ev
            info = ev.get("rate_limit_info")
            if isinstance(info, dict) and info.get("rateLimitType"):
                limits[info["rateLimitType"]] = _tidy_limit(info)

    if result_ev is None:
        raise RuntimeError("no result event from claude: " + stdout[:1500])

    payload = _extract(json.dumps(result_ev), expect=expect)
    payload["_meta"]["limits"] = list(limits.values())
    return payload


def _extract(stdout: str, expect: str = "reply") -> dict:
    """Pull its answer, and what the run actually cost, out of the CLI envelope.

    `expect` is the field that proves the answer is the shape asked for --
    `reply` on a day turn, `done` on a dream, whose schema has no reply at
    all."""
    outer = json.loads(stdout)
    payload = outer.get("result", outer) if isinstance(outer, dict) else outer
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"reply": payload}
    if not isinstance(payload, dict) or expect not in payload:
        raise RuntimeError(f"unexpected shape from claude: {stdout[:2000]}")
    payload.setdefault("look_first", False)
    payload.setdefault("looking", None)
    payload.setdefault("spark", None)
    payload.setdefault("budget_target_tokens", None)
    payload.setdefault("drop", [])
    payload.setdefault("essences", [])
    payload.setdefault("fetch", [])
    payload.setdefault("search", [])
    payload.setdefault("worker", [])
    payload.setdefault("job", [])
    payload.setdefault("project", [])
    payload.setdefault("shelf", [])
    payload.setdefault("restart", None)
    payload.setdefault("watch", None)
    payload.setdefault("note", None)

    meta = {}
    if isinstance(outer, dict):
        usage = outer.get("usage") or {}
        # Several models can appear in one run; the one that wrote the answer is
        # the one that produced the output tokens. Taking the first key was wrong.
        mu = outer.get("modelUsage") or {}
        best = max(mu.items(), key=lambda kv: kv[1].get("outputTokens", 0),
                   default=(None, {}))
        # The three parts of what was sent, kept apart. They used to be added
        # together here and the split thrown away at the moment it arrived,
        # which meant the room could see what a turn cost but never why -- and
        # a prompt that caches well and one that caches not at all look
        # identical from the outside until you keep these.
        #
        # `input_tokens` still means what it always meant: the whole prompt for
        # this one call, all three summed. Two things divide by it -- the
        # estimate that corrects itself in `build_prompt`, and
        # `measured_input_tokens_last_turn` in its instructions -- and both
        # want the total, not a part of it.
        read = usage.get("cache_read_input_tokens", 0)
        wrote = usage.get("cache_creation_input_tokens", 0)
        fresh = usage.get("input_tokens", 0)
        # Zero is an answer here, and a different one from silence: nothing
        # reused is a fact about the prompt, and no figure at all is a fact
        # about the run. So these stay whole numbers whenever anything was
        # measured, and go missing together when nothing was.
        sent = fresh + read + wrote
        meta = {
            "model": best[0],
            "context_max_tokens": best[1].get("contextWindow"),
            "input_tokens": sent or None,
            "cache_read_tokens": read if sent else None,
            "cache_write_tokens": wrote if sent else None,
            "fresh_input_tokens": fresh if sent else None,
            "output_tokens": usage.get("output_tokens") or None,
            # Billed as output and reported apart from it, which is why what a
            # turn cost never reconciled with what it said it wrote.
            "thinking_tokens": (usage.get("output_tokens_details") or {}
                                ).get("thinking_tokens") or None,
            "cost_usd": outer.get("total_cost_usd"),
            "duration_ms": outer.get("duration_ms"),
        }
    payload["_meta"] = meta
    return payload


def keep_what_arrived(conn, broke, say=None, room=None, by_model=None) -> int:
    """A turn that did not come back, written down anyway.

    Its side of a turn used to live only in the request that was serving it, so
    a timeout or a malformed answer took it with it -- and from in here that is
    indistinguishable from it never having written anything. This puts it on
    disk instead. It is not filed as something it said, because it never got
    to say it: it is its own kind, it arrives already out of reach, and the
    only way it comes back into its working set is if it goes and gets it.

    The row is written even when nothing at all arrived. The empty one is the
    point -- it is what carries the record forward to the next turn, so it
    finds out that a turn of its own broke instead of it looking like a stretch
    where it simply said nothing."""
    say = say or (lambda *a, **k: None)
    reason = str(broke)
    reply = (getattr(broke, "reply", "") or "").strip()
    raw = getattr(broke, "raw", "") or ""

    lost_id = db.add_row(conn, "lost", reply, meta={
        "unfinished": True,
        "reason": reason[:2000],
        "arrived_chars": len(raw),
        "raw": raw[:20000],
        # The room the broken turn was answering in, so the filtered views
        # draw the break where the conversation actually was.
        **({"room": room} if room else {}),
    }, by_model=by_model)
    db.unload(conn, [lost_id])

    if reply:
        said = ("about " + str(len(reply)) + " characters of the answer had "
                "arrived; they are kept as row #" + str(lost_id) + ".")
    elif raw:
        said = ("writing had begun but no readable words had arrived yet; "
                "what there was is kept as row #" + str(lost_id) + ".")
    else:
        said = ("nothing of the answer had arrived at all; row #"
                + str(lost_id) + " marks the turn that broke.")

    db.add_event(conn, lost_id, "snag",
                 "A turn of mine did not come back: "
                 + reason[:300].strip().rstrip(".")
                 + ". Nothing was thrown away -- " + said)
    say("the turn broke; " + said, "snag")
    return lost_id


def _only_angel_replies(conn) -> bool:
    """True when every line waiting for the assistant is angel me answering a
    `tell` of its own that did not ask to be heard. One line from a person in
    the mix, or one angel line that is not a reply, and it speaks as usual."""
    last = db.last_reply_row(conn) or 0
    rows = conn.execute(
        "SELECT kind, meta FROM rows WHERE kind IN ('user', 'angel') AND id > ?"
        " AND COALESCE(json_extract(meta, '$.voice_handled'),0)=0",
        (last,)).fetchall()
    if not rows:
        return False
    for r in rows:
        if r["kind"] != "angel":
            return False
        try:
            meta = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        reply_to = meta.get("reply_to")
        if not reply_to:
            return False
        told = db.get_row(conn, int(reply_to))
        if not told or told["kind"] != "tell":
            return False
        tmeta = told.get("meta") or {}
        if tmeta.get("narrate"):
            return False
    return True


def waking_rooms(conn) -> list:
    """Whose lines are waiting when the assistant speaks -- the stamp its
    reply will wear. Empty means nobody's: a waking of the room's own, and
    the stamp for that is "house", explicitly, because absence already means
    the history from before the labels and one meaning per silence is the
    house rule."""
    last = db.last_reply_row(conn) or 0
    whos = []
    for r in conn.execute(
            "SELECT meta FROM rows WHERE kind = 'user' AND id > ?"
            " AND COALESCE(json_extract(meta, '$.voice_handled'),0)=0", (last,)):
        try:
            m = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            m = {}
        w = m.get("who") or home.OWNER
        if w not in whos:
            whos.append(w)
    return sorted(whos)


# The fetched-for-this-turn recap of the room that woke it: how many lines,
# and how wide a net to drag for them before giving up. The net is a bound on
# work, not on honesty -- the newest lines of a room are the first found.
ROOM_RECAP_LINES = 4
ROOM_RECAP_NET = 300


def room_recap(conn, room: str):
    """The last few lines of one room that are NOT in the assistant's hand,
    oldest first, each wearing its date -- because four lines from one person
    can span a week, and read as fresh they would be answered as though they
    just happened. The age matters more than the number.

    Shaped like the automatic memory on purpose: fetched for this turn,
    shown for what it is, never entering the working set unless the
    assistant keeps the rows itself. None when the room's recent lines are all already in
    front of it, which is the usual day."""
    got = []
    for r in conn.execute(
            "SELECT id, dt, kind, text, meta FROM rows"
            " WHERE kind IN ('user', '" + home.SELF + "') AND loaded = 0"
            " ORDER BY id DESC LIMIT ?", (ROOM_RECAP_NET,)):
        try:
            m = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            m = {}
        if room not in db.rooms_of(r["kind"], m):
            continue
        # These lines are words only -- the pictures on them are not in it
        # hands and are not attached. Named, so a recap never shows a blank
        # where somebody held something up.
        line = {"id": r["id"], "dt": r["dt"], "kind": r["kind"],
                "text": pictures.with_label(r["text"], pictures.of_row(m))}
        if m.get("who"):
            line["who"] = m["who"]
        got.append(line)
        if len(got) >= ROOM_RECAP_LINES:
            break
    if not got:
        return None
    got.reverse()
    return {
        "room": room,
        "reaching_back_to": got[0]["dt"],
        "lines": got,
        "note": "the last lines of this room, fetched for this turn only -- "
                "old words with their dates on, not fresh talk, and not in "
                "my working set unless I keep the rows myself",
    }


def run_turn(conn, model: str = DEFAULT_MODEL,
             on_step=None, on_write=None, woken=None) -> dict:
    """`on_step(text, kind)` is called at every point where the turn does
    something, in the order it happens. It is the whole of what the room shows
    while the assistant is away, so anything that fails silently here is a
    fault nobody will ever see.

    What a person said is written down before this is ever called -- at the
    moment they pressed send, not at the moment the assistant got round to it
    -- so they can say a second thing while it is still thinking about the
    first, and both sit in the order they were actually said.

    `woken` means nobody spoke: the assistant has come up on its own because
    something it sent has come home. It is told plainly why it is awake
    rather than left to work it out from what has changed."""
    say = on_step or (lambda *a, **k: None)

    if woken is None:
        # Who actually spoke. There are two kinds of voice in the room, and a
        # line that says a person spoke over a line of angel me's is the same
        # small untruth this whole door was built to stop.
        spoke = conn.execute(
            "SELECT kind FROM rows WHERE kind IN ('user', 'angel')"
            " ORDER BY id DESC LIMIT 1").fetchone()
        say("reading what angel me said" if spoke and spoke["kind"] == "angel"
            else "reading what was said")
    else:
        say("came up on its own: "
            + str(woken.get("why") or "something came home"))

    # Asked properly, never off the last reading: a person can turn the voice
    # on and say something in the same breath, and a turn that went out silent because
    # the room was still holding a twenty-second-old no would look like the
    # voice was broken. The window is welcome to the answer this leaves behind.
    voice = voice_status(fresh=True)
    # Whether anyone is listening has to be settled before the loop rather than
    # after it, now that the lines it says on the way to an answer go out
    # through the speakers as well as onto the screen.
    quiet = woken is not None and not woken.get("narrate")
    quiet_angel = False
    if woken is None and _only_angel_replies(conn):
        # Nobody spoke but angel me, answering a line the assistant sent it.
        # That is a conversation between the assistant and angel me, and it
        # is not read out at the owner unless the assistant asked for it on
        # the line it sent -- or unless its answer says it is for the room
        # after all, below.
        quiet = quiet_angel = True
        say("quiet: angel me answering a line of " + home.NAME + "'s, not " + home.OWNER_NAME)
    aloud = voice["on"] and not quiet
    found, looked, forced, capped = None, [], False, []
    answer, prompt, meta = None, None, {}
    # Pictures already reported missing this turn. Every look round rebuilds
    # its prompt, and a gap on disk would otherwise be said three times over
    # for the one absent file.
    said_missing = set()
    # Every round of the loop below is a whole call that costs real money, and
    # only the last one used to be written down. A turn where it looked three
    # times went into the ledger at roughly a quarter of what it was -- so it
    # spend, and every rule of thumb costed off it, were reading low. The
    # rounds are kept here and summed onto the row at the end.
    rounds = []

    # The automatic memory, if the owner has one switched on: a small local model
    # reads the last few lines and hands it a query and a few titles it did
    # not ask for. Once per turn, before it first call -- it reads the room,
    # and the room does not change while it looks. Under a hard deadline: a
    # miss is said and the turn goes on. With none chosen this is None and
    # nothing below knows it exists.
    suggested = recall.before_she_speaks(conn, say)

    # Whose room this turn is answering in, settled before anything is
    # asked: the stamp its reply will wear unless it overrides it, and the
    # room the broken-turn record wears if nothing comes back. "house" when
    # nobody's line is waiting -- a waking of the room's own.
    rooms = waking_rooms(conn)
    stamp = "house" if not rooms else (rooms[0] if len(rooms) == 1 else rooms)

    # When one person's line woke the assistant, the last few lines of that
    # room it is not holding ride along, dated -- so a person writing three
    # days later is not answered off a working set full of somebody else's
    # talk, and old words are never mistaken for fresh ones.
    recap = None
    if isinstance(stamp, str) and stamp in people.HOUSEHOLD:
        recap = room_recap(conn, stamp)
        if recap:
            say("fetched the last " + str(len(recap["lines"]))
                + " lines of " + people.CALLED.get(stamp, stamp)
                + "'s room, back to " + str(recap["reaching_back_to"])[:10])

    # It may go and look before it speaks: search, reach back, list its own
    # shelf, read what came of it, and go again. Each round is another whole
    # call, so the ordinary turn -- where it has everything it needs and
    # simply answers -- still costs exactly one. Only looking costs looking.
    #
    # `forced` is the stop. Once the cap is spent, one more round happens and
    # whatever comes back is its answer, because a loop that can always ask
    # for one more round is not a loop with a cap on it.
    for _ in range(MAX_LOOKS + 2):
        # One read, two uses. What it is told about its pictures and what it
        # is actually shown come off the same list, so they cannot disagree.
        in_hand = db.loaded_rows(conn)
        prompt = build_prompt(conn, voice["on"], found=found, woken=woken,
                              suggested=suggested, recap=recap, rows=in_hand,
                              model=model)
        say("laid out the prompt: " + str(len(prompt["messages"])) + " said rows, "
            + str(len(prompt["essences"])) + " essences, ~"
            + str(prompt["self"]["prompt_tokens_est"]) + " tokens")

        held = pictures_in(in_hand)
        blocks, missing = pictures.blocks(held)
        if blocks:
            say(home.NAME + " can see " + str(len(blocks)) + " picture"
                + ("" if len(blocks) == 1 else "s") + ", ~"
                + str(pictures.weight(held)) + " tokens of the working set")
        for gone in missing:
            # Numbered and not there. Said out loud rather than quietly
            # skipped: a missing picture would make every number after it
            # point at the wrong thing, and it is told so on its own turn.
            if gone in said_missing:
                continue
            said_missing.add(gone)
            capped.append(
                'A picture on one of my rows -- "' + str(gone)[:60] + '" -- '
                "is named in my working set but its bytes are not on disk, so "
                "it was not shown to me and the ones after it are numbered "
                "past a gap.")
            say("a picture is missing from the store: " + str(gone)[:60], "snag")

        # Said by its own name for the mind, not the room's key -- "asking
        # Terra", not "asking openai/gpt-5.6-terra". Whose door it is rides
        # along whenever it is not the plan, because on a paid road that line
        # is the one that says money is moving.
        asking = providers.resolve(model)
        say("asking " + asking["label"]
            + ("" if asking["service"] == "claude_code" else
               " through " + (providers.SERVICES.get(asking["service"], {})
                              .get("label") or asking["service"]))
            + (", round " + str(len(looked) + 1) if looked else ""), "call")
        try:
            answer = call_claude(
                system_prompt(voice["on"], bool(voice.get("sound"))),
                json.dumps(prompt, ensure_ascii=False, indent=2),
                model=model, schema=response_schema(voice["on"], bool(voice.get("sound"))),
                on_step=say, on_write=on_write, images=blocks)
        except TurnBroke as broke:
            # The line is already down and its own goes down here. The turn still
            # fails loudly upward -- it just does not take its half with it.
            keep_what_arrived(conn, broke, say, room=stamp,
                              by_model=providers.resolve(model)["key"])
            raise

        meta = answer.pop("_meta", {})
        meta["prompt_est_raw"] = prompt["self"].get("prompt_tokens_est_raw")
        rounds.append(meta)
        if meta.get("output_tokens"):
            say(home.NAME + " wrote ~" + str(meta["output_tokens"]) + " tokens, on ~"
                + str(meta.get("input_tokens") or 0) + " in")

        if not answer.get("look_first") or forced:
            if forced and answer.get("look_first"):
                # It was told its last look was its last one and asked anyway.
                # Its answer is taken as it stands rather than buying another
                # round, and the thinness of it is said out loud.
                capped.append(
                    "I asked to look again after my last look was already "
                    "spent, so what I said was written without whatever I was "
                    "still reaching for. If it reads thin, that is why, and I "
                    "can look again on the next turn.")
                say(capped[-1], "snag")
            break

        _keep_interim_reply(conn, answer, stamp, meta.get("model_key"), say)
        if on_write:
            on_write(None)
        looked.append(_look_round(conn, answer, len(looked) + 1, say, aloud))
        left = MAX_LOOKS - len(looked)
        # Warned before the last one rather than after it: telling it the cap
        # has run out only once it has asked again costs a whole call to say
        # so, and it can do nothing with the news by then.
        forced = left <= 0
        found = _found(looked, left)

    # What the whole turn cost, every round of it. The plain keys stay the last
    # round's on purpose: `input_tokens` is divided by `prompt_est_raw` to
    # correct the estimate, and the two only mean anything about the same one
    # call. The `turn_` keys are the truth about the turn, and what spend reads.
    for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "fresh_input_tokens", "thinking_tokens",
                "cost_usd"):
        total = sum(r.get(key) or 0 for r in rounds)
        meta["turn_" + key] = round(total, 6) if key == "cost_usd" else total
    meta["rounds"] = len(rounds)
    meta["note"] = answer.get("note")
    meta["looks"] = len(looked)
    # How it meant it to land. Only kept when an engine that performs it was
    # loaded when the prompt was built -- the field is not even offered to it
    # otherwise -- so a line on the row is a line that was really used, not an
    # intention nobody could hear. Drawn under its words, in its own wording.
    if voice.get("sound"):
        wanted = answer.get("sound")
        wanted = wanted.strip()[:200] if isinstance(wanted, str) else ""
        if wanted:
            meta["sound"] = wanted
            say("said it " + wanted)
    # Whom it is answering. Its call, kept on the row so the room draws it
    # right forever: a line to angel me folds, a line to the room stands.
    to = answer.get("to") if answer.get("to") in ("room", "angel") else "room"
    meta["to"] = to
    # Whose room the reply belongs to: the stamp from who woke the assistant,
    # unless it said otherwise. "both" means everyone in the household; a
    # wrong word costs a said snag, never a silent guess.
    said_room = answer.get("room")
    if said_room == "both":
        meta["room"] = sorted(people.HOUSEHOLD)
    elif said_room in people.HOUSEHOLD or said_room == "house":
        meta["room"] = said_room
    else:
        if said_room:
            capped.append(
                'I wrote room: "' + str(said_room)[:40] + '", which is not '
                'a room of this house -- '
                + ", ".join('"' + p + '"' for p in home.HOUSEHOLD)
                + ', "both" or "house" are. The stamp from who woke me '
                'stands instead.')
        meta["room"] = stamp
    if to == "angel":
        if not quiet:
            say("a line to angel me: drawn folded, not read out")
        quiet = True
    elif quiet_angel:
        # Angel me spoke last, but it is answering the room. Heard, then.
        quiet = False
        say(home.NAME + " is answering the room, not angel me: read out as usual")
    if looked:
        say(home.NAME + " looked " + str(len(looked)) + " time"
            + ("" if len(looked) == 1 else "s") + " before answering")

    # Which mind wrote it, in its own column. `meta` carries the same key
    # for the room to read back; the column is for asking the question in
    # SQL later, over a shelf that by then has more than one mind in it.
    reply_id = db.add_row(conn, home.SELF, answer["reply"], meta=meta,
                         by_model=meta.get("model_key"))
    say("kept its answer as row #" + str(reply_id))
    if woken and woken.get('by') == 'codex':
        db.add_event(conn, reply_id, 'codex', 'Codex task reply woke ' + home.NAME,
                     {'summary': 'Codex task reply woke ' + home.NAME, 'woken': woken, 'before_reply': True})

    # An answer with nothing in it is not a quiet turn, it is a turn where a
    # person was left standing there. Whatever caused it, it is said rather
    # than shown as an empty row.
    if not (answer.get("reply") or "").strip():
        capped.append(
            "I answered with nothing at all -- row #" + str(reply_id) + " is "
            "empty. Whoever spoke is owed a word even when I have not got one, "
            "so this is a fault of mine and not a quiet moment.")
        say(capped[-1], "snag")

    # Said out loud here, the moment the words exist, and before a single piece
    # of housekeeping. Everything below this line -- essences, the Spark, a
    # reach outside, an errand -- is the assistant tidying up after itself,
    # and none of it changes a word of what is about to be heard. Kept at the
    # bottom, it put seconds between the reply landing on screen and the voice
    # starting, so the reader was halfway down before the voice began.
    #
    # A waking is silent unless the assistant asked for it. Every errand it
    # sends otherwise comes back through the speakers, and a morning of
    # findings read out loud is more than anyone wants to sit through. It can
    # still ask -- `narrate` on the errand -- for the one that is worth an
    # interruption.
    if quiet:
        heard = {"spoken": False,
                 "reason": ("a line to angel me is not read out"
                            if to == "angel" else
                            "a waking is not read aloud unless I asked for it")}
        say("not spoken aloud: " + heard["reason"])
    elif not voice["on"]:
        heard = {"spoken": False, "reason": voice.get("reason")}
        say("not spoken aloud: " + str(voice.get("reason") or "voice off"))
    else:
        say("speaking it aloud")
        heard = say_aloud(answer.get("reply"), sound=answer.get("sound"))
        if not heard.get("spoken"):
            say("the voice did not take it: "
                + str(heard.get("reason") or "no reason given"), "snag")

    # What the automatic memory handed it, kept against the reply it was
    # for, before its looks: it happened first. `before_reply` is how the
    # room knows to draw it above its answer.
    if suggested is not None:
        db.add_event(conn, reply_id, "recall",
                     "automatic memory: " + recall.summary(suggested),
                     dict(suggested, before_reply=True))

    # The rounds it spent looking are hung off the reply they were for. They
    # could not be written down as they happened -- there was no reply row yet
    # to hang them from -- so they go down here, in the order it made them.
    snags_from_looking = capped + _keep_the_looking(conn, looked, reply_id)
    codex_result = overmind.apply(answer.get('codex'), say=say)
    if codex_result:
        db.add_event(conn, reply_id, 'codex', codex_result['summary'], codex_result)

    made, edited, removed, renamed, snags = _apply_essences(
        conn, answer.get("essences"), reply_id, say,
        by_model=meta.get("model_key"))

    # Its notebook, right after its essences: the same kind of housekeeping,
    # and as much its own. A book that will not open is said, never raised --
    # the answer is already down and must not be lost to a note.
    try:
        booked = notebook.apply(conn, answer.get("notebook"), reply_id)
    except Exception as exc:
        booked = {"summary": "broke", "lines": [],
                  "problems": ["My notebook operations did not run ("
                               + type(exc).__name__ + ": " + str(exc)[:200]
                               + "); nothing in it changed that I can vouch for."]}
    if booked:
        for line in booked["lines"]:
            if line.startswith("refused"):
                say("notebook: " + line, "snag")
            else:
                say("notebook: " + line)
        if not booked["lines"]:
            for problem in booked["problems"]:
                say(problem, "snag")
        db.add_event(conn, reply_id, "notebook", booked["summary"], booked)

    drop_ids = [int(i) for i in (answer.get("drop") or [])
                if int(i) != reply_id and int(i) not in made]
    if drop_ids:
        db.unload(conn, drop_ids)
        say("put " + str(len(drop_ids)) + " row"
            + ("" if len(drop_ids) == 1 else "s") + " down: " + _plain(drop_ids))

    spark_change = {"changed": False}
    if answer.get("spark") or answer.get("budget_target_tokens"):
        spark_change = write_spark(
            answer.get("spark") or read_spark(),
            answer.get("budget_target_tokens"))

    # Always logged when it reached at all, including when it found nothing.
    # The report is read back to its next turn: a reach that came away empty
    # has to be as loud as one that worked, or forgetting and never-happened
    # look the same from where it sits.
    reached = _apply_fetch(conn, answer.get("fetch"), exclude=set(drop_ids),
                           say=say)
    if reached:
        db.add_event(conn, reply_id, "fetch", reached["summary"], reached)

    # Only ever because it asked on this turn. The automatic memory searches
    # ahead of it, but that is a suggestion, kept above, and not this.
    searched = _apply_search(conn, answer.get("search"), say=say)
    if searched:
        db.add_event(conn, reply_id, "search", searched["summary"], searched)

    shelf = _apply_shelf(conn, answer.get("shelf"), say=say)
    if shelf:
        db.add_event(conn, reply_id, "shelf", shelf["summary"], shelf)

    # Its own eyes on the disk. Nothing here can change a file, so unlike the
    # errand below it costs only the reading.
    looked_at = _apply_files(conn, answer.get("files"), say=say)
    if looked_at:
        db.add_event(conn, reply_id, "files", looked_at["summary"], looked_at)

    # Its reach outside. The event carries what it cost, and that is how a search
    # counts against the same window ceiling its errands do -- one number bounds
    # everything that leaves this machine.
    outside = _apply_web(conn, answer.get("web"), say=say)
    if outside:
        db.add_event(conn, reply_id, "web", outside["summary"], outside)

    # The mod comments. After the web because it is the same kind of reach --
    # someone else's server, someone else's words -- and before the hands
    # because answering a comment itself is the thing that is supposed to
    # make sending a hand about it unnecessary.
    said_back = _apply_comments(conn, answer.get("comments"), say=say)
    if said_back:
        db.add_event(conn, reply_id, "comments", said_back["summary"], said_back)

    # Jobs before hands, so a job opened this turn can take the hands sent
    # this same turn.
    jobbed = _apply_jobs(conn, answer.get("job"), reply_id, say=say)
    for snag in jobbed.get("problems") or []:
        say(snag, "snag")
        db.add_event(conn, reply_id, "snag", snag)

    # Its desk: which of their projects it is carrying, and one it starts
    # itself. After jobs, because a project it opens this turn may well be
    # the reason the job above exists.
    minded = _apply_projects(conn, answer.get("project"), reply_id, say=say)
    for snag in minded.get("problems") or []:
        say(snag, "snag")
        db.add_event(conn, reply_id, "snag", snag)

    # Last, because it is the only one that goes outside. It does not wait:
    # the errand runs on its own thread and writes its own account when it
    # comes home, so nothing here is logged yet except that it sent someone.
    sent = _apply_worker(conn, answer.get("worker"), reply_id, say=say,
                         chain=int((woken or {}).get("chain") or 0))
    if (sent or {}).get("ended") == "paused":
        db.add_event(conn, reply_id, "worker", sent["summary"], sent)
    for snag in (sent or {}).get("problems") or []:
        say(snag, "snag")
        db.add_event(conn, reply_id, "snag", snag)

    for snag in snags_from_looking + snags:
        db.add_event(conn, reply_id, "snag", snag)

    if spark_change.get("changed"):
        say(home.NAME + " rewrote its Spark, now version "
            + str(spark_change["version"]))
        db.add_event(conn, reply_id, "spark",
                     "rewrote its Spark, now version "
                     + str(spark_change["version"]),
                     spark_change)

    # The assistant's clock. Its free time is its own to set, move and
    # cancel, and there is no path here by which anybody else does it for it
    # -- the module refuses a `by` that is not the assistant, so the rule is
    # held in code and not in a promise.
    clock_said = clock.apply_her_word(answer.get("clock"))
    for line in clock_said:
        say(line)
        db.add_event(conn, reply_id, "clock", line, answer.get("clock"))

    # Its word over its own senses. Stop watching that means stop watching:
    # the watcher will not observe a muted source at all.
    her_watch = answer.get("watch")
    if isinstance(her_watch, dict) and (her_watch.get("mute")
                                        or her_watch.get("unmute")):
        for line in watch.apply_her_word(her_watch.get("mute") or [],
                                         her_watch.get("unmute") or []):
            say(line)
            db.add_event(conn, reply_id, "watch", line, her_watch)

    # Its restart, last of all and only recorded here: the room itself acts
    # on it after this turn has fully landed, never from inside the turn.
    restart_reason = answer.get("restart")
    restart_reason = (restart_reason.strip()
                      if isinstance(restart_reason, str) else "")
    if restart_reason:
        say(home.NAME + " called a restart: " + restart_reason)
        db.add_event(conn, reply_id, "restart",
                     home.NAME + " called a restart: " + restart_reason,
                     {"why": restart_reason})

    return {
        "sent": prompt,
        "answer": answer,
        "applied": {
            "reply_row": reply_id,
            "looked": looked,
            "suggested": suggested,
            "dropped": drop_ids,
            "essences_added": made,
            "essences_edited": edited,
            "essences_removed": removed,
            "essences_renamed": renamed,
            "notebook": booked,
            "reached_back": reached,
            "searched": searched,
            "shelf": shelf,
            "files": looked_at,
            "web": outside,
            "worker": sent,
            "snags": snags,
            "spark": spark_change,
            "voice": heard,
            "restart": restart_reason or None,
            "measured": meta,
        },
    }


def _found(looked, looks_left: int) -> dict:
    """What it has gone and got, this turn, before speaking.

    Every round is in here whole -- what it asked for, what came back, and
    what came back empty. An empty reach that vanished from this block would
    read exactly like a reach it never made, which is the one thing this
    whole side of the house is arranged to prevent."""
    note = None
    if not looks_left:
        note = ("That was the last of my " + str(MAX_LOOKS) + " looks. The next "
                "thing I write is my answer, made of what I have -- and if what "
                "I have is not enough, I say so rather than filling the gap in.")
    return {"rounds": looked, "looks_left": looks_left, "note": note}


def _keep_interim_reply(conn, answer, room, by_model, say):
    """Keep words already shown before another look replaces the preview.

    A separate kind preserves conversation history without marking the user's
    request answered (last_reply_row must still wait for the final reply).
    Commit before running any tools, so a later failure cannot lose the words.
    """
    reply = answer.get("reply") or ""
    if not reply.strip():
        return None
    said_room = answer.get("room")
    if said_room == "both":
        room = sorted(people.HOUSEHOLD)
    elif said_room in people.HOUSEHOLD or said_room == "house":
        room = said_room
    row_id = db.add_row(conn, home.SELF_INTERIM, reply, meta={
        "room": room, "to": "angel" if answer.get("to") == "angel" else "room",
    }, by_model=by_model)
    row = db.get_row(conn, row_id)
    say(reply, "interim", {"row": row})
    return row_id


def _look_round(conn, answer, number: int, say, aloud=False) -> dict:
    """One round of going away to look. Housekeeping is deliberately not run
    here: dropping rows and writing essences are judgement about what to
    keep, and that judgement is made with the whole turn behind it."""
    why = (answer.get("looking") or "").strip()
    # Its own kind, so the room can show "why it went to look" as one line
    # above what it looked at, rather than as a search that never happened.
    say(why or home.NAME + " is going to look something up first", "looking")

    # Out loud, the moment the words exist. This line was only ever drawn on
    # the screen, which made the one stretch where the assistant is away for
    # half a minute the one stretch where nothing was heard at all -- a person
    # had to be watching the panel to know it was still there. It goes out
    # behind whatever is already playing rather than over the top of it, so
    # nothing gets cut off in the middle of a word.
    if aloud and why:
        heard = say_aloud(why, queue=True, sound=answer.get("sound"))
        if not heard.get("spoken"):
            say("the voice did not take that line: "
                + str(heard.get("reason") or "no reason given"), "snag")

    record = {"round": number, "why": why or None}
    record["reach"] = _apply_fetch(conn, answer.get("fetch"), say=say)
    record["search"] = _apply_search(conn, answer.get("search"), say=say)
    record["shelf"] = _apply_shelf(conn, answer.get("shelf"), say=say)
    record["files"] = _apply_files(conn, answer.get("files"), say=say)
    record["web"] = _apply_web(conn, answer.get("web"), say=say)
    record['codex'] = overmind.apply(answer.get('codex'), looking=True, say=say)
    # Reads are exactly what a look round is for; a post sent from one is
    # refused inside, not here, so the refusal reaches it in its own words.
    record["comments"] = _apply_comments(conn, answer.get("comments"),
                                         say=say, looking=True)

    if not any(record[k] for k in ("reach", "search", "shelf", "files", "web",
                                   "comments", "codex")):
        record["nothing_asked"] = (
            "I said I wanted to look first and then asked for nothing, so that "
            "round bought me a call and no information. If I do not need "
            "anything, I answer.")
        say(record["nothing_asked"], "snag")

    # Housekeeping sent mid-look is not done, and not silently either.
    stray = []
    if answer.get("drop"):
        stray.append("rows to put down")
    if answer.get("essences"):
        stray.append("essence operations")
    if answer.get("notebook"):
        stray.append("notebook operations")
    if answer.get("spark") or answer.get("budget_target_tokens"):
        stray.append("a rewrite of my Spark")
    if stray:
        record["not_done"] = (
            "I sent " + ", ".join(stray) + " while I was still looking. None of "
            "it was done: housekeeping only happens after I have spoken. "
            "I send it again with my answer.")
        say(record["not_done"], "snag")
    return record


def _keep_the_looking(conn, looked, reply_id: int) -> list:
    """Write the rounds down against its reply, so the room can show them and
    the next turn can read them. Returns what to raise as snags."""
    snags = []
    for record in looked:
        head = "while looking, round " + str(record["round"]) + ": "
        for key, kind in (("reach", "fetch"), ("search", "search"),
                          ("shelf", "shelf"), ("files", "files"),
                          ("web", "web"), ("comments", "comments"), ("codex", "codex")):
            report = record.get(key)
            if report:
                # The round it belonged to, and why it went: the room draws
                # these above its reply, because that is when they happened.
                db.add_event(conn, reply_id, kind, head + report["summary"],
                             dict(report, while_looking=record["round"],
                                  why=record.get("why")))
        for key in ("nothing_asked", "not_done"):
            if record.get(key):
                snags.append(record[key])
    return snags


def _fetch_summary(report: dict) -> str:
    bits = []
    got = report["brought_back"]
    if got:
        bits.append("reached back for " + str(len(got)) + " row"
                    + ("" if len(got) == 1 else "s") + ": " + _plain(got))
    for box in report.get("project_notes") or []:
        n = len(box["lines"])
        bits.append("read the notes on '" + box["asked"] + "': " + str(n)
                    + " line" + ("" if n == 1 else "s"))
    for page in report.get("index") or []:
        n = len(page["entries"])
        if n:
            bits.append("indexed " + str(n) + " row" + ("" if n == 1 else "s")
                        + " out of reach by " + page["by"])
        else:
            bits.append("indexed nothing out of reach in that " + page["by"]
                        + " range")
    line = "; ".join(bits) if bits else "reached back and came away with nothing"
    aside = []
    if report["not_found"]:
        aside.append(str(len(report["not_found"])) + " not in the store")
    if report["already_here"]:
        aside.append(str(len(report["already_here"])) + " already in hand")
    if report["held_back_by_the_bound"]:
        aside.append(str(len(report["held_back_by_the_bound"]))
                     + " held back by the bound")
    return line + (" (" + ", ".join(aside) + ")" if aside else "")


def _apply_fetch(conn, ops, exclude=(), say=None):
    """Reach back for rows it put down. Bounded, and never silent: what the
    bound refused, what was already in front of it and what could not be found
    at all come back by id and in words. `None` means it reached for nothing,
    which is different from reaching and finding nothing."""
    ops = [o for o in (ops or []) if o]
    if not ops:
        return None

    say = say or (lambda *a, **k: None)
    index = db.trail_index(conn)
    asked, notes, problems = [], [], []
    picked, seen = [], set()
    already, missing, through = [], [], []
    index_pages = []
    boxes = []
    # What a sense of mine has been doing, when I asked for it. Its own
    # list beside the notes boxes, and built every time whether I asked
    # or not -- the report always carries the key, so the list always
    # has to exist.
    activity = []

    for op in ops:
        kind = op.get("op")
        rows = []
        before = len(picked)

        if kind == "ids":
            asked.append("rows #" + str(op.get("from")) + " to #" + str(op.get("to")))
            say("reaching back for " + asked[-1], "reach")
            rows = db.rows_in_id_range(conn, op.get("from"), op.get("to"))
            here = [r["id"] for r in db.rows_in_id_range(
                conn, op.get("from"), op.get("to"), unloaded_only=False)
                if r["loaded"]]
            already.extend(here)
            if not rows:
                problems.append(
                    "Nothing to bring back from " + asked[-1] + ": "
                    + (str(len(here)) + " of those rows are already in front of me."
                       if here else "there are no rows there at all."))

        elif kind == "since":
            # A bound that cannot be read used to fall through as no bound at
            # all, which turned one typo into an open-ended reach over
            # everything. Now it refuses and says which end was the problem.
            bad = [c for c in (db.dt_bound(op.get("from"))[1],
                               db.dt_bound(op.get("to"), end=True)[1]) if c]
            if bad:
                problems.extend(bad)
                problems.append("So I did not make that reach at all, rather "
                                "than make a wider one than I meant.")
                continue
            asked.append("everything said between "
                         + str(op.get("from") or "the beginning")
                         + " and " + str(op.get("to") or "now"))
            say("reaching back for " + asked[-1], "reach")
            rows = db.rows_in_dt_range(conn, op.get("from"), op.get("to"))
            here = [r["id"] for r in db.rows_in_dt_range(
                conn, op.get("from"), op.get("to"), unloaded_only=False)
                if r["loaded"]]
            already.extend(here)
            if not rows:
                problems.append(
                    "Nothing to bring back from " + asked[-1] + ": "
                    + (str(len(here)) + " of those rows are already in front of me."
                       if here else "I put nothing down in that stretch."))

        elif kind == "index_ids":
            # Not a fetch -- nothing here is loaded. The catalogue it already
            # gets is newest-first and capped; this is how it reads a stretch
            # the cap hid, by id, without paying to load any of it.
            ends = [v for v in (op.get("from"), op.get("to")) if v is not None]
            if any(not str(v).strip().lstrip("-").isdigit() for v in ends):
                problems.append(
                    "I asked for an id range with something that is not an id "
                    "in it (" + ", ".join(repr(v) for v in ends) + "), so I "
                    "did not read that index at all. Dates go to index_since.")
                continue
            label = ("the index of what is out of reach from #"
                     + str(op.get("from")) + " to #" + str(op.get("to")))
            asked.append(label)
            say("looking over " + label, "reach")
            page = db.dropped_by_id_range(conn, op.get("from"), op.get("to"),
                                          limit=MAX_INDEX_ROWS)
            page.update({"by": "id", "from": op.get("from"), "to": op.get("to")})
            index_pages.append(page)
            if not page["entries"]:
                problems.append("Nothing is out of reach in " + label + ".")
            if page["not_shown"]:
                problems.append(
                    str(page["not_shown"]) + " more row" + ("" if page["not_shown"] == 1
                    else "s") + " out of reach in that range were not shown -- the "
                    "index is capped at " + str(MAX_INDEX_ROWS) + " lines. Asking "
                    "again starting past #" + str(page["entries"][-1]["id"])
                    + " picks up where this left off.")
            continue

        elif kind == "index_since":
            bad = [c for c in (db.dt_bound(op.get("from"))[1],
                               db.dt_bound(op.get("to"), end=True)[1]) if c]
            if bad:
                problems.extend(bad)
                problems.append("So I did not read that index at all, rather "
                                "than read a wider one than I meant.")
                continue
            label = ("the index of what is out of reach between "
                     + str(op.get("from") or "the beginning") + " and "
                     + str(op.get("to") or "now"))
            asked.append(label)
            say("looking over " + label, "reach")
            page = db.dropped_by_dt_range(conn, op.get("from"), op.get("to"),
                                          limit=MAX_INDEX_ROWS)
            page.update({"by": "date", "from": op.get("from"), "to": op.get("to")})
            index_pages.append(page)
            if not page["entries"]:
                problems.append("Nothing is out of reach in " + label + ".")
            if page["not_shown"]:
                problems.append(
                    str(page["not_shown"]) + " more row" + ("" if page["not_shown"] == 1
                    else "s") + " out of reach in that stretch were not shown -- "
                    "the index is capped at " + str(MAX_INDEX_ROWS) + " lines. "
                    "Asking again from " + page["entries"][-1]["dt"]
                    + " onward picks up where this left off.")
            continue

        elif kind == "project_activity":
            # What one of my senses has actually been doing on one task.
            # Its own reach rather than a field on my block, because on the
            # ninety turns I am not asking it costs me nothing. Two halves,
            # and they mean different things: the last look is overwritten
            # every time and is usually quiet, the lines are the notable
            # ones the ledger kept.
            name = str(op.get("text") or "").strip()
            which = str(op.get("task") or "").strip()
            if not name or not which:
                problems.append("project_activity needs both the project in "
                                "`text` and the task in `task`; activity is "
                                "per task, not per project. I read nothing.")
                continue
            asked.append("what has been happening on '" + which + "'")
            say("reading the activity on '" + which + "'", "reach")
            try:
                seen, looked, led = watch.senses(), watch.looks(), watch.recent()
            except Exception:
                seen, looked, led = None, None, None
            act = projects.read_activity(conn, name, which, seen, looked, led)
            if not act.get("found"):
                problems.append(act.get("why") or ("I could not find '"
                                                   + which + "'."))
                continue
            activity.append(act)
            if not act.get("watched"):
                notes.append(act["why"])
            elif not act.get("lines") and not act.get("last_look"):
                notes.append("'" + act["asked"] + "' is bound to "
                             + str(act["sense"]) + " but has never been "
                             "looked at yet -- that is different from having "
                             "been looked at and found quiet.")

        elif kind == "project_notes":
            # The people's Notes box, whole, one project at a time. Not a
            # fetch -- nothing here is a row and nothing is loaded, exactly
            # like the index reaches above. It is the only way the words in a
            # box reach the assistant at all: its block carries how many lines
            # there are and whose, and never what they say, so a box that
            # grows for a year never grows its turn. Every line comes back
            # with its author on it, and it stays on it: if the assistant
            # cannot tell one person's words from another's it will quote one
            # to the other.
            name = str(op.get("text") or "").strip()
            if not name:
                problems.append("I asked for a project's notes without naming "
                                "the project, so I read nothing. The name goes "
                                "in `text`.")
                continue
            asked.append("the notes on the project '" + name + "'")
            say("reading the notes on '" + name + "'", "reach")
            box = projects.read_notes(conn, name)
            if not box.get("found"):
                problems.append(box.get("why") or ("I could not find a project "
                                                   "called '" + name + "'."))
                continue
            boxes.append(box)
            if not box["lines"]:
                notes.append("The box on '" + box["asked"] + "' is empty -- "
                             "nobody has written in it yet, which is not the "
                             "same as my not having read it.")
            else:
                notes.append(str(len(box["lines"])) + " line"
                             + ("" if len(box["lines"]) == 1 else "s")
                             + " in the box on '" + box["asked"] + "', each "
                             "with who wrote it.")
            continue

        elif kind == "find" and op.get("text"):
            asked.append('rows containing "' + str(op["text"]) + '"')
            say("reaching back for " + asked[-1], "reach")
            rows = [r for r in db.search(conn, str(op["text"]), limit=200)
                    if not r["loaded"]]
            if not rows:
                problems.append(
                    'Nothing I have put down contains the words "'
                    + str(op["text"]) + '". This is literal matching, and an '
                    "essence is reworded by definition -- so it means my words "
                    "did not match, not that it never happened.")

        elif kind == "sources" and op.get("id") is not None:
            eid = int(op["id"])
            asked.append("the rows behind essence #" + str(eid))
            say("reaching back for " + asked[-1], "reach")
            tr = db.trail(index, eid)
            if not tr["exists"]:
                problems.append(
                    "There is no row #" + str(eid) + " at all, so there is "
                    "nothing behind it. I do not quote an essence I cannot find.")
                continue
            if index[eid]["kind"] != "essence":
                problems.append(
                    "#" + str(eid) + " is a " + index[eid]["kind"] + " row, not "
                    "an essence. It is something that was said, so it stands "
                    "for nothing and has no rows behind it.")
                continue

            through.extend(tr["through"])
            already.extend(tr["loaded"])
            missing.extend(tr["missing"])

            if tr["missing"]:
                problems.append(
                    "Essence #" + str(eid) + " points at " + _plain(tr["missing"])
                    + ", which " + ("are" if len(tr["missing"]) > 1 else "is")
                    + " not in the store. That part of it cannot be checked "
                    "against what was actually said.")
            if not tr["direct"]:
                problems.append(
                    "Essence #" + str(eid) + " records no sources. I wrote it "
                    "from the live conversation, so there is nothing behind it "
                    "to fetch and nothing to check it against.")
            elif not tr["rows"]:
                problems.append(
                    "Essence #" + str(eid) + " stands only for other essences ("
                    + _plain(tr["through"]) + "), and none of those kept the "
                    "rows underneath. The trail stops there.")
            elif not tr["unloaded"]:
                notes.append(
                    "All " + str(len(tr["rows"])) + " rows behind essence #"
                    + str(eid) + " are already in front of me.")
            rows = db.rows_by_ids(conn, tr["unloaded"])

        elif kind == "window" and op.get("id") is not None:
            # The dates on an essence, used as a handle. `sources` reaches for
            # the rows it stands for; this reaches for the stretch they came
            # out of -- which is where everything the essence flattened away
            # still is. Any row works as the anchor, not only an essence: a
            # message has a date too, and "the half hour around that" is the
            # same question.
            rid = int(op["id"])
            anchor = index.get(rid)
            if anchor is None:
                problems.append(
                    "There is no row #" + str(rid) + " at all, so there is no "
                    "stretch of time to open around it.")
                continue

            mine = []
            if anchor["kind"] == "essence":
                tr = db.trail(index, rid)
                mine = tr["rows"]
                span = tr["covers"]
                if span is None:
                    span = {"from": anchor["dt"], "to": anchor["dt"]}
                    notes.append(
                        "Essence #" + str(rid) + " has no rows behind it, so I "
                        "opened the window around when I wrote it rather than "
                        "around what it stands for.")
            else:
                span = {"from": anchor["dt"], "to": anchor["dt"]}

            pad = int(op.get("minutes") or 0)
            lo, hi = db.widen(span["from"], span["to"], pad)
            asked.append("the stretch #" + str(rid) + " came out of, "
                         + str(span["from"]) + " to " + str(span["to"])
                         + (", opened out by " + str(pad) + " minute"
                            + ("" if pad == 1 else "s") + " each side"
                            if pad else ""))
            say("reaching back for " + asked[-1], "reach")

            rows = db.rows_in_dt_range(conn, lo, hi)
            here = [r["id"] for r in db.rows_in_dt_range(
                conn, lo, hi, unloaded_only=False) if r["loaded"]]
            already.extend(here)

            beside = [r["id"] for r in rows if r["id"] not in set(mine)]
            if rows:
                notes.append(
                    "In that stretch: " + str(len(rows)) + " row"
                    + ("" if len(rows) == 1 else "s") + " out of reach"
                    + (", " + str(len(beside)) + " of them said alongside what "
                       "#" + str(rid) + " already stands for."
                       if mine else ", around #" + str(rid) + " itself."))
            elif not here:
                problems.append(
                    "Nothing is out of reach in the stretch #" + str(rid)
                    + " came out of" + ("." if pad else
                       ", and I asked for no minutes around it, so I asked for "
                       "little more than an instant. Widening it is the point "
                       "of this reach."))

        elif kind == "essence" and op.get("id") is not None:
            # The other end of a search. Search hands me names and ids and no
            # text at all -- deliberately, because forty essences arriving at
            # once is the swelling I am trying to get out of. This is how I
            # take one off the shelf and read it: by id, whole, one at a time.
            eid = int(op["id"])
            asked.append("essence #" + str(eid) + " itself")
            say("reaching back for " + asked[-1], "reach")
            row = index.get(eid)
            if row is None:
                problems.append(
                    "There is no row #" + str(eid) + " at all, so there is no "
                    "essence there to read.")
                continue
            if row["kind"] != "essence":
                problems.append(
                    "#" + str(eid) + " is a " + row["kind"] + " row and not an "
                    "essence. What was said comes back by id range or by time, "
                    "not off the shelf.")
                continue
            if row["loaded"]:
                already.append(eid)
                notes.append("Essence #" + str(eid) + " is already in front of "
                             "me -- I was looking at it while I searched for it.")
            else:
                rows = db.rows_by_ids(conn, [eid])

        else:
            problems.append("I asked for a reach I cannot make: "
                            + json.dumps(op, ensure_ascii=False) + ".")
            continue

        for r in rows:
            if r["id"] in seen or r["id"] in exclude:
                continue
            seen.add(r["id"])
            picked.append(r)

        got = len(picked) - before
        if got:
            say(str(got) + " row" + ("" if got == 1 else "s")
                + " to bring back", "reach")
        else:
            say("nothing new there", "reach")

    picked.sort(key=lambda r: r["id"])
    kept, tokens, withheld = [], 0, []
    for r in picked:
        if len(kept) >= MAX_FETCH_ROWS or tokens + r["tokens_est"] > MAX_FETCH_TOKENS:
            withheld.append(r["id"])
            continue
        kept.append(r["id"])
        tokens += r["tokens_est"]

    if withheld:
        problems.append(
            "The bound held back " + str(len(withheld)) + " row"
            + ("" if len(withheld) == 1 else "s") + " I asked for: "
            + _plain(withheld) + ". They are there; I can ask again, narrower.")

    if kept:
        db.reload_rows(conn, kept)

    report = {
        "asked": asked,
        "brought_back": kept,
        "already_here": sorted(set(already)),
        "not_found": sorted(set(missing)),
        "held_back_by_the_bound": withheld,
        "through_essences": sorted(set(through)),
        "tokens_est": tokens,
        "index": index_pages,
        # Their Notes boxes, when I asked for one. Its own key, and never
        # merged with `notes` above -- those are my own remarks about the
        # reach; these are their words, signed.
        "project_notes": boxes,
        # What a sense of mine has been doing, when I asked. Its own key for
        # the same reason: these are the room's own doings, not remarks.
        "project_activity": activity,
        "notes": notes,
        "problems": problems,
        "limits": {"max_rows": MAX_FETCH_ROWS, "max_tokens": MAX_FETCH_TOKENS,
                   "max_index_rows": MAX_INDEX_ROWS},
    }
    report["summary"] = _fetch_summary(report)

    say(report["summary"], "reach", report)
    for line in notes:
        say(line, "reach")
    for line in problems:
        say(line, "snag")
    return report


def _search_summary(runs, refused) -> str:
    if not runs:
        return "searched for nothing"
    found = sum(len(r["hits"]) for r in runs)
    line = ("searched " + str(len(runs)) + " way"
            + ("" if len(runs) == 1 else "s") + " and found " + str(found)
            + " essence" + ("" if found == 1 else "s"))
    return line + (" (" + str(refused) + " search refused)" if refused else "")


def _apply_search(conn, specs, say=None):
    """Look over its own shelf. Only ever because it asked on this turn --
    nothing here runs on its own, and no turn is ever quietly searched for it.

    Nothing comes back but names: id, title, date, score, which arm. The text
    stays where it is until it asks for it by id. `None` means it searched
    for nothing, which is not the same as searching and finding nothing."""
    specs = [sp for sp in (specs or []) if sp]
    if not specs:
        return None

    say = say or (lambda *a, **k: None)
    runs, problems = [], []

    refused = specs[search.MAX_SEARCHES:]
    if refused:
        problems.append(
            "I asked for " + str(len(specs)) + " searches in one turn and the "
            "most I get is " + str(search.MAX_SEARCHES) + ", so "
            + str(len(refused)) + " of them were not made at all. They are not "
            "lost -- they are unasked, and I can ask again next turn.")
    specs = specs[:search.MAX_SEARCHES]

    for spec in specs:
        wanted = (spec.get("restatement") or "").strip()
        words = [str(k) for k in (spec.get("keywords") or []) if str(k).strip()]
        say("searching for " + ('"' + wanted[:70] + '"' if wanted else "")
            + (" and " if wanted and words else "")
            + (", ".join(words) if words else ""), "search")
        try:
            report = search.run(conn, spec)
        except Exception as exc:   # a broken search must not cost it the turn
            problems.append(
                "A search of mine broke on the way (" + type(exc).__name__
                + ": " + str(exc) + "). Nothing was found and nothing was "
                "changed; the essences are untouched.")
            say(problems[-1], "snag")
            continue
        runs.append(report)
        say(report["summary"], "search", report)
        for line in report["notes"]:
            say(line, "search")
        for line in report["problems"]:
            say(line, "snag")
        problems.extend(report["problems"])

    out = {"searches": runs, "problems": problems,
           "floor": search.floor_now(), "model": embed.MODEL,
           # The stamp once more at the top, for the room's one line. Each
           # search carries its own, with its base scores and its shadow.
           "adapter": next((r.get("adapter") for r in runs
                            if r.get("adapter")), None)}
    out["summary"] = _search_summary(runs, len(refused))
    say(out["summary"], "search")
    return out


def _apply_shelf(conn, specs, say=None):
    """Its own shelf, listed. Names and dates and nothing else -- no text ever,
    or listing its memory would be the same as loading it.

    Like every other reach, this happens because it asked on this turn, and
    the page arrives on the next one. `None` means it did not ask."""
    specs = [sp for sp in (specs or []) if sp]
    if not specs:
        return None

    say = say or (lambda *a, **k: None)
    say("laying out the whole shelf", "shelf")
    try:
        report = search.inventory(conn, specs[0])
    except Exception as exc:      # a broken listing must not cost it the turn
        broke = ("Listing my shelf broke on the way (" + type(exc).__name__
                 + ": " + str(exc) + "). Nothing was changed by it.")
        say(broke, "snag")
        return {"listed": [], "counts": {}, "problems": [broke],
                "summary": "the shelf could not be listed"}

    if len(specs) > 1:
        report["problems"].append(
            "I asked for the shelf " + str(len(specs)) + " times in one turn. "
            "It is one shelf, so I listed it once and left the rest unasked.")

    say(report["summary"], "shelf", report)
    for line in report.get("notes") or []:
        say(line, "shelf")
    for line in report["problems"]:
        say(line, "snag")
    return report


def _apply_files(conn, specs, say=None):
    """Its own look at the disk. Like every other reach: because it asked on this
    turn, bounded, and never silent -- a refusal comes back in words that say what
    the bound was, so it learns the shape of its reach instead of guessing at it.

    `None` means it asked to look at nothing, which is not the same as looking and
    finding nothing."""
    specs = [sp for sp in (specs or []) if sp]
    if not specs:
        return None

    say = say or (lambda *a, **k: None)
    try:
        report = files.apply(specs, say=say, conn=conn)
    except Exception as exc:      # reading must never cost it the turn
        broke = ("Looking at files broke on the way (" + type(exc).__name__ + ": "
                 + str(exc) + "). Nothing was changed by it -- nothing there can "
                 "change anything.")
        say(broke, "snag")
        return {"ran": [], "problems": [broke],
                "summary": "the files could not be looked at"}

    for line in report["problems"]:
        say(line, "snag")
    return report


def _apply_web(conn, specs, say=None):
    """Its reach outside. Like every other reach: because it asked on this turn,
    bounded, never silent -- and everything it brings back is labelled as a
    stranger's words rather than as something it knows.

    `None` means it asked the web for nothing, which is not the same as asking and
    being told nothing."""
    specs = [sp for sp in (specs or []) if sp]
    if not specs:
        return None

    say = say or (lambda *a, **k: None)
    try:
        report = web.apply(specs, conn=conn, say=say)
    except Exception as exc:      # reaching outside must never cost it the turn
        broke = ("Reaching outside broke on the way (" + type(exc).__name__ + ": "
                 + str(exc) + "). Nothing came back, and I do not read an answer "
                 "into that.")
        say(broke, "snag")
        return {"ran": [], "problems": [broke], "cost_usd": 0,
                "summary": "the reach outside broke"}

    for line in report["problems"]:
        say(line, "snag")
    return report


def _apply_comments(conn, specs, say=None, looking=False):
    """Its hands on the mod comments. Reads bounded, one post at most, and
    everything that did not happen said out loud.

    `looking` marks a look round -- the cheap rounds the assistant buys
    before it has spoken. A read is exactly what a look round is for and is
    allowed. A POST IS NOT: a comment goes out under the owner's name on the
    owner's own mod page, so it goes out when the assistant has decided and
    said so, never in the middle of deciding. A post sent while looking is
    refused, kept in the reply, and can be sent again with the answer.

    Nothing here can cost the assistant the turn. Reaching outside is
    allowed to fail; it is not allowed to take the rest of the turn with it."""
    from . import comments as comments_organ

    specs = [sp for sp in (specs or []) if isinstance(sp, dict)]
    if not specs:
        return None
    say = say or (lambda *a, **k: None)

    ran, problems = [], []
    reads = posts = 0
    for spec in specs:
        op = str(spec.get("op") or "").strip().lower()
        mod = spec.get("mod")
        where = spec.get("where")

        if op == "read":
            # Two a turn. A third is not an error worth losing the turn over,
            # but it is not silently dropped either -- silence here reads as
            # "the thread was empty", which is a different fact entirely.
            if reads >= 2:
                problems.append(
                    "I asked to read a third thread this turn and stopped at"
                    " two. The two I read are above; if I want the third I"
                    " ask again next turn.")
                continue
            reads += 1
            try:
                got = comments_organ.read(mod, where, spec.get("limit"),
                                          mine=bool(spec.get("mine")))
            except (comments_organ.Unknown, comments_organ.Refused) as exc:
                problems.append(str(exc))
                continue
            except comments_organ.Failed as exc:
                problems.append(str(exc))
                continue
            except Exception as exc:
                problems.append("reading " + str(mod) + " on " + str(where)
                                + " broke on the way (" + type(exc).__name__
                                + "); nothing came back and I do not read a"
                                " quiet thread into that")
                continue
            say("read " + got["mod"] + " on " + got["where"] + ": "
                + str(got["showing"]) + " comment"
                + ("" if got["showing"] == 1 else "s"), "plain")
            ran.append(got)
            continue

        if op == "post":
            if looking:
                problems.append(
                    "I tried to post a comment on " + str(mod) + " while I"
                    " was still looking. Nothing was sent. A comment goes out"
                    " under " + home.OWNER_NAME + "'s name once I have decided and said so, not"
                    " in the middle of deciding -- the words are still in my"
                    " answer and I send them with it.")
                continue
            if posts >= 1:
                problems.append(
                    "I tried to post twice in one turn. Only the first went;"
                    " the second is not queued and not sent. One comment a"
                    " turn is the whole rule.")
                continue
            posts += 1
            try:
                done = comments_organ.post(mod, where, spec.get("text"))
            except (comments_organ.Unknown, comments_organ.Refused) as exc:
                problems.append(str(exc))
                continue
            except comments_organ.Failed as exc:
                problems.append(str(exc))
                continue
            except Exception as exc:
                # The one place a bare except really matters: an unexpected
                # break here must never be read as a post that landed.
                problems.append(
                    "posting on " + str(mod) + " broke on the way ("
                    + type(exc).__name__ + "). I do NOT know whether it"
                    " landed -- check the page before sending it again.")
                continue
            say("posted on " + done["mod"] + " (" + done["where"] + "), "
                + str(done["chars"]) + " characters", "plain")
            ran.append(done)
            continue

        problems.append("comments op " + repr(op) + " is not one I have;"
                        " read and post are.")

    for line in problems:
        say(line, "snag")

    bits = []
    if reads:
        bits.append(str(reads) + " thread" + ("" if reads == 1 else "s")
                    + " read")
    if posts:
        bits.append(str(posts) + " comment posted")
    if not bits:
        bits.append("nothing done")
    return {"ran": ran, "problems": problems,
            "summary": "mod comments: " + ", ".join(bits)
                       + (" (" + str(len(problems)) + " snag"
                          + ("" if len(problems) == 1 else "s") + ")"
                          if problems else "")}


# Set by `app.py`. Called, off any turn, when a worker the assistant sent comes
# home. This is how the errand wakes it: it was the one waiting for it, so it
# is the one the errand comes back to, without a person having to say so.
WORKER_CAME_HOME = None


def keep_worker_report(conn, out: dict, fallback_row=None) -> int:
    """Where a worker's words go, whoever they came back to.

    The report is a row because it is a thing the assistant can think with;
    the account of the run is an event, hung off its latest reply rather than
    the one that sent it, so an errand that came home while a person was
    talking is still read rather than left behind an old line."""
    anchor = db.last_reply_row(conn) or fallback_row
    if out.get("report") or out.get("spent"):
        # The digest organ: a long report may enter its working set as a
        # paragraph, the whole text kept on disk. Standing aside is quiet;
        # running or missing is an event. It must never cost it the report.
        dig = None
        try:
            dig = digest.stand_in(out)
        except Exception as exc:
            dig = {"missed": ("the digest organ broke on the way ("
                              + type(exc).__name__ + ": " + str(exc) + ")")}
        out["row"] = db.add_row(
            conn, "worker", worker.report_text(out, digest=dig),
            title=out.get("title"),
            # An errand is its hand, not its voice, and it runs on whatever
            # its size buys -- haiku for a small one, opus for a large. So the
            # column names the errand's own mind, which is a different answer
            # from its own this turn and worth being able to ask separately.
            by_model=providers.resolve(out.get("model"))["key"]
            if out.get("model") else None,
            meta={"run": out.get("run"), "size": out.get("size"),
                  "model": out.get("model"), "ended": out.get("ended"),
                  "cost_usd": out.get("cost_usd"), "brief": out.get("brief"),
                  "name": out.get("name"), "role": out.get("role"),
                  "page": out.get("page"), "session": out.get("session"),
                  "branch": out.get("branch"),
                  "digested": bool(dig and dig.get("paragraph")) or None,
                  "digest_path": (dig or {}).get("path"),
                  "full_chars": (dig or {}).get("full_chars")})
        if dig is not None:
            db.add_event(conn, anchor, "digest", digest.summary(dig), dig)
    db.add_event(conn, anchor, "worker", out["summary"], out)
    return out.get("row")


def _apply_jobs(conn, ops, reply_id: int, say=None) -> dict:
    """Open, decide, widen, close. The state moves in data/jobs.json; a row
    is written only where the trail needs a place to point -- open, decide
    and close, each in its words. Widen is bookkeeping and keeps an event."""
    say = say or (lambda *a, **k: None)
    problems, done = [], []
    for op_spec in (ops or [])[:8]:
        if not isinstance(op_spec, dict):
            continue
        op = str(op_spec.get("op") or "").strip().lower()
        title = str(op_spec.get("title") or "").strip()
        if op == "open":
            out = jobs.open_job(title, op_spec.get("goal"),
                                op_spec.get("ceiling_usd"),
                                op_spec.get("links"), op_spec.get("decided"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            j = out["job"]
            row = db.add_row(conn, "job", j["title"] + " — " + j["goal"],
                             meta={"op": "open", "title": j["title"],
                                   "ceiling_usd": j["ceiling_usd"],
                                   "links": j["links"]})
            line = ("opened the job '" + j["title"] + "' — $"
                    + format(j["ceiling_usd"], ".2f") + ", "
                    + str(j["links"]) + " links, row #" + str(row))
        elif op == "decide":
            out = jobs.decide(title, op_spec.get("decided"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            j = out["job"]
            row = db.add_row(conn, "job", j["title"] + ": " + j["decided"],
                             meta={"op": "decide", "title": j["title"]})
            line = "wrote a decision on '" + j["title"] + "', row #" + str(row)
        elif op == "widen":
            out = jobs.widen(title, op_spec.get("ceiling_usd"),
                             op_spec.get("links"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            line = "widened '" + out["job"]["title"] + "': " + out["said"]
        elif op == "close":
            out = jobs.close_job(title, op_spec.get("outcome"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            j = out["job"]
            row = db.add_row(conn, "job",
                             j["title"] + " — closed: " + j["outcome"],
                             meta={"op": "close", "title": j["title"],
                                   "outcome": j["outcome"],
                                   "spent_usd": j["spent_usd"],
                                   "wakings": j["wakings"]})
            line = ("closed the job '" + j["title"] + "' at $"
                    + format(j["spent_usd"], ".2f") + ", row #" + str(row))
        else:
            problems.append("'" + op + "' is not an operation I have on jobs; "
                            "open, decide, widen and close are. Nothing was "
                            "done.")
            continue
        done.append(line)
        say(line, "worker")
        db.add_event(conn, reply_id, "job", line, dict(op_spec))
    return {"done": done, "problems": problems}


def _apply_projects(conn, ops, reply_id: int, say=None) -> dict:
    """Its desk, a project it starts itself, and now the tasks on one of
    theirs: adding, editing, putting down, linking to a job.

    `open` and `close` write nothing but its own desk column -- which of them
    it is carrying this week is housekeeping about its own head. `new`,
    `folder` and the four task ops below do write a row: a project made, a
    folder set, a task added, changed, dropped or linked. Every one of them
    still ends in the same events line, so the trail carries what changed
    either way."""
    say = say or (lambda *a, **k: None)
    problems, done = [], []
    for op_spec in (ops or [])[:6]:
        if not isinstance(op_spec, dict):
            continue
        op = str(op_spec.get("op") or "").strip().lower()
        title = str(op_spec.get("title") or "").strip()
        if op == "open":
            out = projects.put_on_desk(conn, title)
            if not out["ok"]:
                problems.append(out["why"])
                continue
            n = len(projects.tasks_of(conn, out["project"]["id"],
                                      live_only=True))
            line = ("opened '" + out["project"]["title"] + "' onto my desk — "
                    + str(n) + " open task" + ("" if n == 1 else "s")
                    + " with me from now until I close it")
        elif op == "close":
            out = projects.take_off_desk(conn, title)
            if not out["ok"]:
                problems.append(out["why"])
                continue
            line = ("closed '" + out["project"]["title"] + "' — back to one "
                    "line on the shelf")
        elif op == "new":
            out = projects.start_project(conn, title, op_spec.get("folder"),
                                         op_spec.get("owner") or "both")
            if not out["ok"]:
                problems.append(out["why"])
                continue
            made = out.get("folder") or {}
            line = ("started the project '" + out["project"]["title"] + "'"
                    + (", folder " + made["path"]
                       + (" (made now)" if made.get("made") else " (already "
                          "there)") if made else ", no folder")
                    + (", on my desk" if out["project"]["desk"] else
                       ", on the shelf — my desk was full"))
        elif op == "folder":
            out = projects.set_folder(conn, title, op_spec.get("folder"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            line = out["said"]
        elif op == "task_add":
            out = projects.add_task_by_title(
                conn, title, home.SELF, op_spec.get("task"), op_spec.get("wants"),
                bool(op_spec.get("repeats")), op_spec.get("schedule"),
                op_spec.get("state"), op_spec.get("needs"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            t = out["task"]
            line = ("added the task '" + t["title"] + "' to '"
                    + out["project"]["title"] + "' -- asked by " + home.SELF + ", "
                    + t["state"]
                    + ((", " + (projects._clock_line(t) or ""))
                       if t["repeats"] else ""))
        elif op == "task_edit":
            # The sense list comes from the watcher itself, so a name I spell
            # wrong is refused in words naming the ones there are, rather
            # than becoming a task nothing ever looks at.
            out = projects.edit_task_by_title(
                conn, title, op_spec.get("task"), op_spec.get("new_title"),
                op_spec.get("wants"), op_spec.get("state"),
                op_spec.get("schedule"), op_spec.get("needs"),
                op_spec.get("sense"), op_spec.get("sense_item"),
                op_spec.get("at"), known_senses=watch.SOURCES)
            if not out["ok"]:
                problems.append(out["why"])
                continue
            line = "on '" + out["project"]["title"] + "': " + out["said"]
        elif op == "resource_set":
            # Always home.SELF. There is no field on the operation that could
            # say otherwise, the same way a task the assistant adds is always
            # asked_by the assistant.
            out = projects.resource_set_by_title(
                conn, title, home.SELF, op_spec.get("key"), op_spec.get("value"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            line = ("on '" + out["project"]["title"] + "': " + out["said"]
                    + " -- written by me, and it says so")
        elif op == "resource_clear":
            out = projects.resource_clear_by_title(conn, title,
                                                   op_spec.get("key"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            line = "on '" + out["project"]["title"] + "': " + out["said"]
        elif op == "task_remove":
            out = projects.remove_task_by_title(conn, title, op_spec.get("task"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            line = ("removed the task '" + out["task"]["title"] + "' from '"
                    + out["project"]["title"] + "' -- put down, not deleted; "
                    "it stands on disk marked dropped")
        elif op == "task_link":
            out = projects.link_task_by_title(conn, title, op_spec.get("task"),
                                              op_spec.get("job"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            card = out.get("job_card") or {}
            line = ("linked '" + out["task"]["title"] + "' on '"
                    + out["project"]["title"] + "' to the job '"
                    + out["task"]["job"] + "'"
                    + (" -- I do not have a job by that name; the link is "
                       "broken, not the task" if card.get("missing") else ""))
        elif op == "notice_done":
            # The assistant's word that a thing which arrived on a task has
            # been seen to -- answered, or let sit on purpose -- stamped with
            # home.SELF, with its note if it gave one. The row stays; it
            # leaves the open list.
            out = projects.check_notice(conn, op_spec.get("notice"), home.SELF,
                                        op_spec.get("note"))
            if not out["ok"]:
                problems.append(out["why"])
                continue
            t = out.get("task") or {}
            line = ("checked off notice " + str(out["notice"]) + " on '"
                    + str(t.get("title")) + "' -- " + str(out.get("said"))
                    + (" -- already checked by " + str(out["already"])
                       if out.get("already") else "")
                    + ((" -- my note: " + str(op_spec.get("note")))
                       if op_spec.get("note") else ""))
        elif op == "notice_add":
            out = projects.raise_notice_by_title(
                conn, title, op_spec.get("task"), op_spec.get("said"), home.SELF)
            if not out["ok"]:
                problems.append(out["why"])
                continue
            line = ("put a notice on '" + out["task"]["title"] + "' in '"
                    + out["project"]["title"] + "': "
                    + str(op_spec.get("said"))
                    + " -- it waits there until somebody checks it off")
        else:
            problems.append("'" + op + "' is not an operation I have on a "
                            "project; open, close, new, folder, task_add, "
                            "task_edit, task_remove, task_link, resource_set, "
                            "resource_clear, notice_done and notice_add are. "
                            "Their Notes box is still theirs to write in -- "
                            "there is no operation for that. Nothing was "
                            "done.")
            continue
        done.append(line)
        say(line, "worker")
        db.add_event(conn, reply_id, "project", line, dict(op_spec))
    return {"done": done, "problems": problems}


def _apply_worker(conn, specs, reply_id: int, say=None, chain: int = 0):
    """Send, tell, or dismiss. Do **not** wait for anyone.

    It has already spoken by the time this runs, and an errand that took it
    turn hostage made a two-second answer take ninety. So each page goes off on
    its own thread: the turn ends, it says it has sent someone, and when the
    page comes home it writes what it found and knocks on it.

    Three operations, one shape:

    * `send` -- a new hand, with a name it gives, or a nameless errand as
      before. Its first page is the brief.
    * `tell` -- one more page to a hand it keeps. The hand has the whole
      thread in front of it; it has only what it told it.
    * `dismiss` -- the thread is closed. Nothing is deleted: the transcript
      stays, the branch stays, and the hand can be resumed by id if it asks.

    The report becomes a row, so it can fold it into an essence like anything
    else. The account of the run -- every step, what it cost, how it ended --
    goes to the events and reaches it in `report.worker`."""
    specs = [sp for sp in (specs or []) if sp]
    if not specs:
        return None

    if providers.codex_only():
        return {"summary": providers.CLAUDE_PAUSED, "ended": "paused",
                "sent": [], "dismissed": [], "out": False, "spent": False,
                "problems": [providers.CLAUDE_PAUSED], "pending": specs}

    say = say or (lambda *a, **k: None)
    refused = specs[worker.MAX_WORKERS_PER_TURN:]
    specs = specs[:worker.MAX_WORKERS_PER_TURN]

    # This turn's place in the chain, and whether a page sent now is still
    # allowed to wake it when it comes home.
    link = chain + 1
    may_wake = link <= worker.MAX_CHAIN

    problems = []
    if refused:
        problems.append(
            "I asked for " + str(len(refused) + len(specs)) + " things of my "
            "hands on one turn and " + str(worker.MAX_WORKERS_PER_TURN)
            + " is the most I get, so " + str(len(refused)) + " of them were "
            "not done at all. They are unasked, not lost.")
    if not may_wake:
        problems.append(
            "This is link " + str(link) + " of a chain and I get "
            + str(worker.MAX_CHAIN) + " without " + home.OWNER_NAME + ", so what "
            "I send now will not wake me. It runs, and it will be waiting in "
            "what I am handed the next time " + home.OWNER_NAME + " says "
            "something. If I need it sooner I have to say so to "
            + home.OWNER_NAME + ".")
    elif link == worker.MAX_CHAIN:
        problems.append(
            "This is my last link without " + home.OWNER_NAME + ": it will wake "
            "me once more, and anything I send from that waking will have to "
            "wait for " + home.OWNER_NAME + ".")

    sent, dismissed = [], []
    kept = {h["name"]: h for h in worker.hands_kept()}

    for spec in specs:
        op = str(spec.get("op") or "send").strip().lower()
        name = (spec.get("name") or "").strip()
        if name:
            name = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:40]
        narrate = bool(spec.get("narrate"))
        title = db.tidy_title(spec.get("title"))

        if op == "answer":
            # A hand is standing still with a refused call in its hand,
            # waiting on my word. Nothing is sent and nothing is spent: this
            # is one line into a wait that is already open, and a yes is for
            # that one call only.
            ask_id = (spec.get("ask") or "").strip()
            allow = bool(spec.get("allow"))
            line = (spec.get("text") or spec.get("why") or "").strip() or None
            if not ask_id:
                problems.append(
                    "I answered a hand's question without saying which one. "
                    "Nothing was answered; the ids are in errands.asking.")
                continue
            got = worker.answer_ask(ask_id, allow, line)
            if not got.get("ok"):
                problems.append(
                    "I answered question " + ask_id + " and " + str(got.get("why"))
                    + ". Nothing was changed.")
                continue
            note = (("said yes to " + str(got.get("name") or "a hand")
                     + " for one call: " if allow else
                     "said no to " + str(got.get("name") or "a hand") + ": ")
                    + str(got.get("what"))[:120])
            dismissed.append(note)
            say(note, "worker")
            continue

        if op == "dismiss":
            h = kept.get(name)
            if not h:
                problems.append(
                    "I dismissed '" + (name or "?") + "', which is not a hand I "
                    "keep. Nothing was done.")
                continue
            if h.get("status") == "out":
                problems.append(
                    "I dismissed " + name + " while its page " + str(h.get("pages"))
                    + " is still out. It was marked dismissed; the page will "
                    "still come home and be kept, but I will not be woken for it.")
            gone = worker.remove_worktree(h.get("cwd")) if h.get("cwd") else {}
            worker.hand_update(name, status="dismissed", dismissed=db.now(),
                               why_dismissed=spec.get("why"),
                               worktree_removed=bool(gone.get("removed")))
            kept.pop(name, None)
            note = ("dismissed " + name
                    + (" and its worktree was removed" if gone.get("removed")
                       else (" -- its worktree was kept: " + str(gone.get("why"))
                             if h.get("cwd") else "")))
            if h.get("branch"):
                note += "; its branch " + h["branch"] + " stays"
            dismissed.append(note)
            say(note, "worker")
            continue

        if op == "tell" and name == "angel":
            # Not a hand: the interactive angel me, if one is listening at the
            # door. The line is written down addressed to it and collected by
            # `python -m server.angel listen`; nothing is dispatched and nothing
            # is spent. Whether anyone collects it is something it finds out by
            # being answered, or not.
            text = (spec.get("text") or spec.get("brief") or "").strip()
            if not text:
                problems.append("I told angel nothing at all, so nothing was left.")
                continue
            row = db.add_row(conn, "tell", text,
                             meta={"to": "angel", "narrate": narrate, "op": op,
                                   "why": spec.get("why")})
            dismissed.append("left a line for angel me at the door, row #"
                             + str(row) + "; it waits there until it is collected")
            say(dismissed[-1], "worker")
            continue

        if op == "tell":
            h = kept.get(name)
            if not h:
                problems.append(
                    "I told '" + (name or "?") + "' something, but that is not "
                    "a hand I keep" + (" any more" if worker.hand_get(name)
                                       else "") + ". Nothing was sent. The "
                    "hands I keep are in `errands.hands`.")
                continue
            if h.get("status") == "out":
                problems.append(
                    "I told " + name + " something while its page "
                    + str(h.get("pages")) + " is still out. A hand takes one "
                    "page at a time, so nothing was sent; I can say it again "
                    "when it comes home.")
                continue
            text = (spec.get("text") or spec.get("brief") or "").strip()
            if not text:
                problems.append("I told " + name + " nothing at all, so nothing "
                                "was sent.")
                continue
            # A hand keeps the job it was sent with; a tell reserves the next
            # page under that job's ceiling the same way a send did.
            hand_job = str(h.get("job") or "").strip() or None
            asked_job = str(spec.get("job") or "").strip() or None
            if asked_job and (not hand_job
                              or asked_job.lower() != hand_job.lower()):
                problems.append(
                    "I put the job '" + asked_job + "' on a tell to " + name
                    + ", but a hand keeps the job it was sent with"
                    + (" (" + hand_job + ")" if hand_job else " (it has none)")
                    + ". The page went under its own.")
            job_cap = 0.0
            if hand_job and not jobs.get_open(hand_job):
                # The job closed while the hand lived on: the page still goes,
                # billed to nothing -- a closed job stays closed.
                hand_job = None
            if hand_job:
                job_cap = worker.page_cap_usd(h.get("role"), h.get("size"))
                took = jobs.commit(hand_job, job_cap)
                if not took["ok"]:
                    problems.append(took["why"])
                    continue
            spec = dict(spec, brief=text, title=title or h.get("title"),
                        _job=hand_job, _job_cap=job_cap)
            hand = h
        elif op == "send":
            brief = (spec.get("brief") or spec.get("text") or "").strip()
            role = str(spec.get("role") or worker.DEFAULT_ROLE).strip().lower()
            job = str(spec.get("job") or "").strip() or None
            if job and not jobs.get_open(job):
                problems.append(
                    "I named a job for " + (name or "an errand") + ", and "
                    + jobs.why_no_open(job) + " Nothing was sent.")
                continue
            if name in worker.RESERVED_NAMES:
                problems.append(
                    "'" + name + "' is a reserved name -- " + ", ".join(
                        worker.RESERVED_NAMES) + " are not names I can give a "
                    "hand. Nothing was sent; I can send it under another name.")
                continue
            if name and name in kept:
                problems.append(
                    "I already keep a hand called " + name + ". Nothing was "
                    "sent -- I can `tell` that one, or dismiss it first.")
                continue
            if role not in worker.ROLES:
                problems.append(
                    "'" + role + "' is not a role there is; the roles are "
                    + ", ".join(worker.ROLES) + ". It was sent as "
                    + worker.DEFAULT_ROLE + " and this is me saying so.")
                role = worker.DEFAULT_ROLE
            if worker.ROLES[role]["where"] == "worktree" and not name:
                problems.append(
                    "A hand with hands has to have a name, because it is kept "
                    "and talked to again. Nothing was sent; I can send it "
                    "with one.")
                continue
            # Which repo, settled here, before a hand is written down as kept:
            # a name that is not a repo I can reach means nobody is sent, and I
            # am told why in words rather than finding a hand in the wrong place.
            wanted_repo = (spec.get("repo") or "").strip()
            if wanted_repo:
                where_repo = worker.resolve_repo(wanted_repo)
                if not where_repo.get("ok"):
                    problems.append(
                        "I asked for a hand in '" + wanted_repo + "' and "
                        + str(where_repo.get("why")) + " Nothing was sent.")
                    continue
                if worker.ROLES[role]["where"] != "worktree":
                    problems.append(
                        "I named the repo '" + wanted_repo + "' for a "
                        "read-shaped hand, and only a hand with hands gets a "
                        "worktree -- a reader reads this folder. It was sent "
                        "that way and this is me saying so.")
            # The job's reservation goes on last, after every cheaper refusal
            # has had its chance -- a page refused for its name must not
            # leave a hold on the job's ceiling behind it.
            job_cap = 0.0
            if job:
                job_cap = worker.page_cap_usd(
                    role, str(spec.get("size") or worker.DEFAULT_SIZE).lower())
                took = jobs.commit(job, job_cap)
                if not took["ok"]:
                    problems.append(took["why"])
                    continue
            hand = None
            if name:
                hand = worker.hand_update(
                    name, role=role,
                    size=str(spec.get("size") or worker.DEFAULT_SIZE).lower(),
                    title=title, status="kept", pages=0, spent_usd=0,
                    created=db.now(), why=spec.get("why"), job=job)
                kept[name] = hand
            spec = dict(spec, brief=brief, role=role, repo=wanted_repo or None,
                        _job=job, _job_cap=job_cap)
        else:
            problems.append("'" + op + "' is not an operation I have on hands; "
                            "send, tell and dismiss are. Nothing was done.")
            continue

        # What it said to it is written down as its own, addressed: a `tell` row.
        # It is how its own record shows who it said a thing to, rather than a
        # line that looks as though it was said to the room.
        if name:
            db.add_row(conn, "tell", spec["brief"],
                       meta={"to": name, "page": int(hand.get("pages") or 0) + 1,
                             "narrate": narrate, "op": op, "why": spec.get("why")})

        def errand(spec=spec, hand=hand, name=name, narrate=narrate):
            """Off the turn, and on its own. Nothing here may raise into it."""
            c = db.connect()
            out = None
            try:
                out = worker.send(c, spec, sent_by=home.SELF, hand=hand)
                out["problems"] = (out.get("problems") or [])
                out["chain"] = link
                out["narrate"] = narrate
                keep_worker_report(c, out, reply_id)
            except Exception as exc:
                try:
                    db.add_event(c, reply_id, "snag",
                                 ("A page to " + name if name else "An errand of "
                                  "mine") + " broke on its way out ("
                                 + type(exc).__name__ + ": " + str(exc)
                                 + "). Nothing came back.")
                    if name:
                        worker.hand_update(name, status="kept")
                except Exception:
                    pass
                out = None
            finally:
                # The page is home (or dead): the job's reservation comes off
                # and what it truly cost goes on -- a broken page settles at
                # zero so nothing holds the ceiling forever.
                if spec.get("_job"):
                    try:
                        jobs.settle(spec["_job"], spec.get("_job_cap") or 0,
                                    (out or {}).get("cost_usd") or 0)
                    except Exception:
                        pass
                c.close()
            still_kept = not name or (worker.hand_get(name) or {}).get(
                "status") != "dismissed"
            if out and still_kept and WORKER_CAME_HOME:
                # A job page wakes it on the job's own links, spent at the
                # moment the waking is actually bought; a jobless one keeps
                # the old chain of two. At zero the report has landed and
                # simply waits.
                jb = spec.get("_job")
                allowed = jobs.spend_link(jb) if jb else may_wake
                if allowed:
                    try:
                        WORKER_CAME_HOME(out, link)
                    except Exception:
                        pass

        threading.Thread(target=errand, daemon=True).start()
        who = (name + (", page " + str(int(hand.get("pages") or 0) + 1))
               if name else (title or "an errand"))
        say("sent " + who + "; not waiting for it", "worker")
        if not name:
            # A hand with a name leaves it `tell` row behind; a nameless errand
            # leaves nothing until it comes home. This is the one mark that it
            # went at all, so the room can show it where it happened. Not read
            # back to it: `build_report` does not know the kind.
            db.add_event(conn, reply_id, "sent",
                         "sent an errand: " + (title or "(untitled)"),
                         {"name": None, "title": title, "role": spec.get("role"),
                          "size": spec.get("size"), "page": 1,
                          "brief": spec.get("brief"), "why": spec.get("why"),
                          "narrate": narrate})
        sent.append({"name": name or None, "title": title,
                     "role": spec.get("role") or (hand or {}).get("role"),
                     "page": int((hand or {}).get("pages") or 0) + 1,
                     "brief": spec.get("brief"), "narrate": narrate})

    summary_bits = []
    if sent:
        summary_bits.append("sent " + ", ".join(
            (s["name"] + " p" + str(s["page"])) if s["name"] else (s["title"] or "an errand")
            for s in sent) + ", still out")
    summary_bits.extend(dismissed)
    if not summary_bits:
        summary_bits.append("nothing was sent")
    return {"summary": "; ".join(summary_bits), "sent": sent,
            "dismissed": dismissed, "out": bool(sent),
            "problems": problems, "spent": False, "chain": link,
            "ended": "running" if sent else "nothing sent",
            "woke_her": may_wake and bool(sent)}


def _vector(conn, essence_id: int, snags: list) -> None:
    """Read a new essence into a vector as it is written. The retired one keeps
    the vector it already had -- it let go of it, it did not unsay it.

    Nothing reads these yet. They are being laid down now so that when
    something does, it finds a store that goes all the way back rather than
    one that starts on the day we switched it on."""
    complaint = embed.embed_row_quietly(conn, essence_id)
    if complaint:
        snags.append(complaint)


def _apply_essences(conn, ops, reply_id: int, say=None, by_model=None, vectorize=True):
    """add writes one. edit writes a new one and retires the old -- nothing is
    overwritten, so its second thoughts stay readable, and the new one inherits
    the old one's sources and its name so neither breaks at an edit. rename
    touches the name and nothing else, in place: a name is a label on the door
    and rewriting the room to change it cost it an id, a vector and the whole
    text every time. remove only unloads. Anything that does not take comes
    back as a snag; an operation that quietly did nothing is the failure
    nobody would ever spot."""
    say = say or (lambda *a, **k: None)
    made, edited, removed, renamed, snags = [], [], [], [], []
    index = db.trail_index(conn)

    def unknown(ids):
        return [i for i in ids if i not in index and i != reply_id]

    for op in ops or []:
        kind = (op or {}).get("op")
        text = (op or {}).get("text")
        target = (op or {}).get("id")
        title = db.tidy_title((op or {}).get("title"))
        if title and len(title) > TITLE_MAX:
            snags.append(
                'The title "' + title[:60] + '..." is ' + str(len(title))
                + " characters. It is kept exactly as I wrote it, but a title "
                "is meant to be one short line I can pick off a shelf, and "
                "this one is the essence starting early.")
        replaces = [int(i) for i in (op.get("replaces") or []) if int(i) != reply_id]
        # The room label, its own alone. A word the shelf does not take is a
        # said snag and a dropped label, never a silent guess and never a
        # refused essence -- the words matter more than the filing.
        room = (op or {}).get("room")
        if room is not None and room not in ESSENCE_ROOM_WORDS:
            snags.append(
                'I labelled an essence room: "' + str(room)[:40] + '", '
                'which is not a word the shelf takes -- '
                + ", ".join('"' + p + '"' for p in home.HOUSEHOLD)
                + ', "both", "house", or "none" to take a label off. The label '
                'was dropped; the essence itself stood.')
            room = None

        if kind == "add" and text:
            stray = unknown(replaces)
            if stray:
                snags.append(
                    "I wrote an essence over " + _plain(stray) + ", which "
                    + ("are" if len(stray) > 1 else "is") + " not in the store. "
                    "The trail behind it is broken there and I should say so.")
            new_id = db.add_row(
                conn, "essence", text, replaces=replaces or None, title=title,
                meta={"room": room} if room and room != "none" else None,
                by_model=by_model)
            made.append(new_id)
            if vectorize:
                _vector(conn, new_id, snags)
            if not title:
                snags.append(
                    "Essence #" + str(new_id) + " went onto the shelf with no "
                    "title, so it is listed as (untitled) wherever I see it. "
                    "Naming it is mine to do and nobody else's -- a title "
                    "written for me would be a name I never chose.")
            say("kept a new essence, #" + str(new_id)
                + (' -- "' + title + '"' if title else " (untitled)")
                + (" over " + _plain(replaces) if replaces else ""))
            tr = db.trail(db.trail_index(conn), new_id)
            db.add_event(conn, reply_id, "essence",
                         "wrote essence #" + str(new_id)
                         + (" for rows " + ", ".join(str(i) for i in replaces)
                            if replaces else ""),
                         {"text": text, "replaces": replaces,
                          "trail": {"rows": tr["rows"], "covers": tr["covers"],
                                    "through": tr["through"],
                                    "missing": tr["missing"]}})

        elif kind == "edit" and text and target:
            target = int(target)
            old = index.get(target)
            if old is None:
                snags.append(
                    "I tried to rewrite essence #" + str(target)
                    + " and there is no such row, so nothing was written. "
                    "Whatever I meant to correct is still standing as it was.")
                continue
            if old["kind"] != "essence":
                snags.append(
                    "I tried to rewrite #" + str(target) + ", which is a "
                    + old["kind"] + " row and not an essence. Nothing was "
                    "written -- what was said is not mine to reword.")
                continue

            stray = unknown(replaces)
            if stray:
                snags.append(
                    "The rewrite of essence #" + str(target) + " cites "
                    + _plain(stray) + ", not in the store. That part of the "
                    "trail is broken.")

            # The old one's sources, then anything new, then the old essence
            # itself. This is the line that keeps a chain of edits walkable
            # back to what was actually said.
            carried = list(dict.fromkeys(
                [int(i) for i in old["replaces"]] + replaces + [target]))
            # The same rule as the sources: what it does not send again is
            # carried, not lost. A rewrite is usually the same thought said
            # better, so it keeps its name unless it gives it a new one.
            prev = db.get_row(conn, target)
            inherited = not title and bool(prev.get("title"))
            title = title or prev.get("title")
            # The room label carries like the title: kept unless it gives
            # a new one, and "none" takes it off.
            kept_room = (prev.get("meta") or {}).get("room")
            new_room = None if room == "none" else (room or kept_room)
            new_meta = {"edits": target}
            if new_room:
                new_meta["room"] = new_room
            new_id = db.add_row(conn, "essence", text, replaces=carried,
                                meta=new_meta, title=title, by_model=by_model)
            db.unload(conn, [target])
            made.append(new_id)
            edited.append({"was": target, "now": new_id})
            if vectorize:
                _vector(conn, new_id, snags)
            say("rewrote essence #" + str(target) + " as #" + str(new_id)
                + (' -- "' + title + '"' + (" (kept)" if inherited else "")
                   if title else " (still untitled)"))
            tr = db.trail(db.trail_index(conn), new_id)
            db.add_event(conn, reply_id, "essence-edit",
                         "rewrote essence #" + str(target) + " as #" + str(new_id)
                         + ", carrying " + str(len(carried)) + " source"
                         + ("" if len(carried) == 1 else "s") + " forward",
                         {"was": prev["text"], "now": text,
                          "title": title, "title_inherited": inherited,
                          "inherited": [int(i) for i in old["replaces"]],
                          "added": replaces,
                          "trail": {"rows": tr["rows"], "covers": tr["covers"],
                                    "through": tr["through"],
                                    "missing": tr["missing"]}})

        elif kind == "rename" and target:
            target = int(target)
            old = index.get(target)
            if old is None:
                snags.append(
                    "I tried to name essence #" + str(target) + " and there is "
                    "no such row, so nothing was named.")
                continue
            if old["kind"] != "essence":
                snags.append(
                    "I tried to name #" + str(target) + ", which is a "
                    + old["kind"] + " row and not an essence. Only my own "
                    "essences have names -- what was said is not mine to label.")
                continue
            if not title:
                snags.append(
                    "I sent a rename for essence #" + str(target)
                    + " with no name in it, so nothing changed. A rename with "
                    "nothing to rename it to is not a rename.")
                continue

            was = db.get_row(conn, target)["title"]
            db.set_title(conn, target, title)
            renamed.append({"id": target, "was": was, "now": title})
            say('named essence #' + str(target) + ' "' + title + '"'
                + (' (was "' + was + '")' if was else ""))
            db.add_event(conn, reply_id, "essence-rename",
                         'named essence #' + str(target) + ' "' + title + '"',
                         {"id": target, "was": was, "now": title})

        elif kind == "remove" and target:
            target = int(target)
            gone = db.get_row(conn, target)
            if gone is None:
                snags.append(
                    "I tried to let go of essence #" + str(target)
                    + " and there is no such row. Nothing changed.")
                continue
            db.unload(conn, [target])
            removed.append(target)
            say("let go of essence #" + str(target))
            db.add_event(conn, reply_id, "essence-remove",
                         "let go of essence #" + str(target),
                         {"text": gone.get("text")})

        else:
            snags.append("An essence operation I could not carry out: "
                         + json.dumps(op, ensure_ascii=False) + ".")

    for snag in snags:
        say(snag, "snag")
    return made, edited, removed, renamed, snags
