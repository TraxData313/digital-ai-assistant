"""The poke, and the veto.

This runs inside the worker, started by Claude Code itself as a per-run hook --
see `worker.py`, which writes the settings file that names it -- and it is
handed the hook payload on stdin. It has two jobs, keyed on which event it was
handed, and they are opposite in temperament:

* **The knock** (`Stop`, `SessionEnd`, `StopFailure`, `PermissionDenied`): POST
  the payload at the room so the finish is not something a person has to notice.
  It never raises and it never blocks the worker. When the knock does not land
  it leaves the payload on disk beside the run's log and the dispatcher says so.

* **The veto** (`PreToolUse`, angel hands only): decide, locally and at once,
  whether this tool call is one of the assistant's hands may make. It prints its decision
  and exits; it never knocks, because a hand makes hundreds of calls and the
  room has no use for any of them. And it **fails closed**: a veto that crashes
  on a bad pattern and thereby allows is a cap that silently is not there, so
  any exception here is a deny with the exception in the reason.

Measured: a `PreToolUse` deny is read by the model verbatim as an error, and
the call lands in `permission_denials`.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = 10
EVENT = {"name": "?"}

# What a hand with hands may not do, by command. Each is a reason in words,
# because the model reads it and the assistant reads the report -- and each
# says whether a refusal is the assistant's to overturn.
#
# `absolute` refuses and that is the end of it: the store, the keys, restarting
# the room, throwing work away. A question the assistant is not allowed to
# answer yes to is worse than no question, so these never become a question;
# they go in the record with the rest. "Throwing work away" has one narrow
# exception: an angel taking down its own already-merged branch and its own
# worktree, and nothing wider -- see the carve-outs above `veto`.
#
# `askable` stops the hand and asks the assistant. Every one of these has a
# shape where the answer could honestly be yes -- a hung test process, a local
# server a job really does need to post to -- and without the ask the hand
# simply shrugged and carried on.
ABSOLUTE, ASKABLE = "absolute", "askable"

FORBIDDEN_COMMANDS = [
    # Any tokens may sit between `python` and the `-m`: found while proving
    # the -c hole -- the old pattern wanted `-m` immediately after
    # `python`, so `python -X utf8 -m server.backup` had never been caught
    # at all. Flagged forms now stop and ask like the plain one; the one
    # exact form that passes instead is the bench carve-out above.
    (r"python\S*(?:\s+\S+)*?\s+-m\s+server\.", ASKABLE,
     "run the room's own modules -- that could mint a second store, run the "
     "canaries, or drop a backup into the tracked folder"),
    (r"\brestart\.ps1\b|\brun\.ps1\b|\bstart\.bat\b|\bstart\.pyw\b", ABSOLUTE,
     "start or restart the room"),
    (r"\btaskkill\b|\bStop-Process\b|\bkill\s+-9\b|\bpkill\b", ASKABLE,
     "kill a process"),
    # `-D` only, case-sensitively: `git branch -d` refuses to delete a branch
    # whose work is not already in another, so it puts nothing down for good,
    # and a hand that has merged its own branch needs it. Under IGNORECASE the
    # two are the same letter, hence the inline flag. The one exact form that
    # passes instead of landing here is the branch-delete carve-out above --
    # everything else about a `-D`, `reset --hard`, or `clean -f` still stops
    # here exactly as before, worded the same.
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|branch\s+(?-i:-D))\b", ABSOLUTE,
     "throw work away -- nothing is deleted, only put down"),
    # `git worktree remove` was never named by any rule at all until now --
    # nothing above ever looked at the word "worktree" -- so every path,
    # anyone's, passed through unchecked. The one exact form that passes
    # instead of landing here is the worktree-remove carve-out above: a
    # hand may take its own worktree down. Anyone else's stays refused.
    (r"\bgit\s+worktree\s+remove\b", ABSOLUTE,
     "remove a worktree that is not its own"),
    (r"\brm\s+-[a-z]*r[a-z]*f?\b|\brm\s+-[a-z]*f[a-z]*r\b|\bRemove-Item\b.*-Recurse|\brmdir\s+/s\b|\bdel\s+/s\b",
     ABSOLUTE, "delete a folder tree"),
    (r"\bcurl\b.*\s-(X\s*POST|d\s|-data)|\bInvoke-RestMethod\b.*-Method\s+Post|\bInvoke-WebRequest\b.*-Method\s+Post",
     ASKABLE, "post to a server -- a hand fetches pages, it does not send"),
    (r"\b(netsh|reg\s+add|schtasks|Set-ExecutionPolicy)\b", ASKABLE,
     "change the machine"),
]

# Files no hand may read or write wherever they are, by name. The store and
# the keys: absolute, every one.
FORBIDDEN_NAMES = ("store.db", "store.db-journal",
                   ".env", "angel.token", "hands.json", "angel.inbox.json",
                   "home.json")

# Whose hand this is, said in every refusal. The room puts it in the hand's
# environment; this script runs by file name and cannot import the room.
NAME = os.environ.get("ASSISTANT_NAME") or "the assistant"


def _home_dir(root):
    """The home the room runs: its data folder is the live one. Given to the
    hand by the room; the old single-folder layout falls back to the code."""
    return os.environ.get("ASSISTANT_HOME") or root

# How long a hand stands still waiting for the assistant, and what happens
# when it is not there: four minutes, and then no -- fails closed exactly like
# the veto it comes from.
ASK_WAIT_S = 240
ASK_POLL_S = 2.0


def _norm(p: str) -> str:
    return str(p or "").replace("\\", "/").lower().rstrip("/")


def _under(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    return bool(root) and (path == root or path.startswith(root + "/"))


# The one narrowing of the modules rule, made after the same refusal kept
# landing on builders trying to verify their own work: the rule was too
# wide, not the hands. A bench module -- `server.test_*` and nothing
# else -- may run against a scratch place, because every bench in the
# house takes its scratch as an argument and refuses the live data folder
# itself. Everything else under `-m server.` still stops and asks, and so
# does any bench invocation that is chained, redirected, flagged after
# the module, walking up with `..`, missing its scratch, or pointed
# inside this repo. Refusing the real store, data/ and the tracked tree
# was never the wrong part.
BENCH_CMD = re.compile(
    r"^python(?:3|\.exe)?\s+-m\s+server\.test_\w+((?:\s+\S+)+)$",
    re.IGNORECASE)


def _bench_pass(flat: str, root: str) -> bool:
    """True only for the exact form `python -m server.test_<name>
    <scratch...>`. Three rules, each from a review of the carve-out:

    No flags in front of the `-m` at all -- a flag slot admitted
    `-c<code>`, and with `-c` everything after the code is argv: the code
    runs and the bench never does, needing none of the blocked marks. A
    bench that genuinely needs `-X` can stop and ask; that is what the
    ask is for.

    A relative argument is resolved against the hand's own cwd before it
    is judged -- a builder's cwd is its worktree, inside this repo's
    data/, so a bare name is a scratch in the tracked tree and is
    refused, not passed on a string technicality.

    And with no root to check against, nothing passes: the carve-out
    fails closed rather than silently widening."""
    if not root:
        return False
    if any(mark in flat for mark in ("&&", "||", ";", "|", ">", "<", "`",
                                     "$(")):
        return False
    m = BENCH_CMD.match(flat.strip())
    if not m:
        return False
    cwd = os.environ.get("ASSISTANT_HAND_CWD") or ""
    args = m.group(1).split()
    for a in args:
        a = a.strip("\"'")
        if a.startswith("-") or ".." in a:
            return False
        resolved = a
        if not (a.startswith("/") or a.startswith("\\\\")
                or re.match(r"^[A-Za-z]:[/\\]", a)):
            if not cwd:
                return False
            resolved = cwd.rstrip("/\\") + "/" + a
        if _under(resolved, root):
            return False
    return bool(args)


# The one narrowing of "nothing is deleted, only put down": the brief to
# every builder already says an angel may merge its work and take its own
# branch down, and the absolute refusal above did not know that. One exact
# form passes -- `git branch -D <slug>/<name>` where that commit is already
# reachable from origin/main, proven by asking git itself, not assumed or
# taken on the hand's word. Anything else about the command -- a name outside
# <slug>/*, extra args, a branch that is not yet an ancestor, a git that
# cannot be asked -- falls through to the same absolute refusal as before,
# worded the same. No root, no ancestry proof: fails closed.
BRANCH_DELETE_CMD = re.compile(r"^git\s+branch\s+(?-i:-D)\s+(\S+)$",
                               re.IGNORECASE)


def _branch_delete_pass(flat: str, root: str) -> bool:
    if not root:
        return False
    m = BRANCH_DELETE_CMD.match(flat.strip())
    if not m:
        return False
    name = m.group(1).strip("\"'")
    if not re.match(r"^" + re.escape(_slug()) + r"/[\w][\w./-]*$", name) or ".." in name:
        return False
    try:
        got = subprocess.run(
            ["git", "merge-base", "--is-ancestor", name, "origin/main"],
            cwd=root, capture_output=True, timeout=10)
        return got.returncode == 0
    except Exception:
        return False


# A hand may take its own worktree down -- the other half of the same
# amendment -- and nobody else's. No flags at all, particularly not
# `--force`, which removes one with changes still sitting in it unasked; no
# chaining; and a path that resolves to anywhere but the hand's own cwd
# stays refused. `..` is not special-cased here the way it is in the bench
# carve-out because the check is on the *resolved* path landing exactly on
# cwd, not on the argument's spelling.
WORKTREE_REMOVE_CMD = re.compile(r"^git\s+worktree\s+remove\s+(\S+)$",
                                 re.IGNORECASE)


def _worktree_remove_pass(flat: str, root: str, cwd: str) -> bool:
    if not root or not cwd:
        return False
    if any(mark in flat for mark in ("&&", "||", ";", "|", ">", "<", "`",
                                     "$(")):
        return False
    m = WORKTREE_REMOVE_CMD.match(flat.strip())
    if not m:
        return False
    arg = m.group(1).strip("\"'")
    if arg.startswith("-"):
        return False
    if not (arg.startswith("/") or arg.startswith("\\\\")
            or re.match(r"^[A-Za-z]:[/\\]", arg)):
        arg = cwd.rstrip("/\\") + "/" + arg
    try:
        resolved = str(Path(arg).resolve())
        own = str(Path(cwd).resolve())
    except OSError:
        return False
    if _norm(resolved) != _norm(own):
        return False
    return _under(own, os.path.join(_home_dir(root), "data", "worktrees"))


# What defeated carve-out matches again and again: all
# three carve-outs above (`_bench_pass`, `_branch_delete_pass`,
# `_worktree_remove_pass`) anchor their regex at both ends and match it
# against the *whole* flattened Bash string. A hand's habitual `cd "..." &&
# ` in front, or `2>&1`, `| tail -80`, `; echo "exit:$?"` behind, is no
# danger of its own, but it broke the anchor every time and the underlying
# rule fired instead of the carve-out. Separately, `git commit -m "..."`
# quoting a run-line in its own message tripped the *module* rule, because
# that rule's pattern is searched for anywhere in the flat string with no
# idea that the hit sat inside a quoted argument -- prose, not a command.
#
# The fix judges the command, not the line. `_split_segments` cuts the
# string on the top-level `;`, `&&`, `||`, `|`, and -- added after a hand
# kept running its bench as an ordinary multi-line script, one statement per
# line and no `;` in sight -- a bare newline, exactly as much a separator in
# Bash as `;` is. Before
# this, `veto` flattened the whole command's whitespace to single spaces
# (`" ".join(cmd.split())`) *before* splitting, which erased every
# newline, so a script like
#     cd "<worktree>"
#     rm -rf /tmp/scratch
#     python -m server.test_watch /tmp/scratch 2>&1 | tail -40
# never became four segments at all -- it stayed one unsplittable blob
# that the anchored bench shape could never match, and the underlying
# `-m server.` rule fired instead of the carve-out every single time.
# None of the four separators split on one sitting inside a quoted span,
# so a separator or newline quoted inside an argument does not cut the
# command in two, and a `\` immediately before a newline outside quotes
# is a line continuation exactly like Bash's own -- the pair is dropped
# and nothing splits there. Each segment has its own trailing redirection
# stripped. A carve-out passes if exactly one segment matches its shape
# and every other segment is clean of every other rule
# (`_segment_forbidden`), so a genuinely dangerous command hidden behind
# a `;`, a `&&`, or its own line beside a harmless one still refuses --
# the `rm -rf` in the script above still stops the whole thing, and
# rightly: this widens what one bench call on its own line looks like,
# not what may ride along beside it. `_mask_commit_messages` blanks the
# quoted text of a `git commit -m`/`--message` argument before any
# forbidden-command scan -- narrowly, only from the point `git commit`
# appears onward, and only that flag's quoted value, because git provably
# never executes that text. Nothing else about matching changed: the
# general rule list is still searched over the whole (masked) string
# exactly as before, so a rule that already caught something still
# catches it.
_TRAILING_REDIR = re.compile(r"(?:\s+\d*>{1,2}&?\d*\S*|\s+\d*<\S*)+$")


def _strip_trailing_redirs(seg: str) -> str:
    # Stripped of its own leading/trailing space first: a split on `|`
    # leaves a trailing space before the redirect (e.g. "...2>&1 " from
    # "...2>&1 | tail"), and the pattern's `$` anchor cannot reach past it.
    return _TRAILING_REDIR.sub("", seg.strip()).strip()


def _split_segments(s: str):
    """Segments of `s`, cut on the top-level `;`, `&&`, `||`, `|`, and a
    bare newline -- never on one sitting inside a quoted span -- each with
    its own trailing redirection stripped. `s` must be the *raw* command,
    newlines intact: flattening it to single spaces first (as `veto` used
    to, for the whole string) erases the very separator this splits on,
    which is what let a real multi-line script slip past every carve-out
    unmatched. A `cd path && ...` prefix ends up as its own leading
    segment here, judged like anything else: `cd` matches none of the
    forbidden patterns, so it is simply benign, not specially cut. A
    backslash immediately before a newline, outside quotes, is a line
    continuation -- the pair is dropped and nothing is split there,
    matching Bash's own reading of it."""
    segments = []
    buf = []
    i, n = 0, len(s)
    quote = None
    while i < n:
        c = s[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("\"", "'"):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and s[i + 1:i + 2] == "\n":
            i += 2
            continue
        if s[i:i + 2] in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if c in (";", "|", "\n"):
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    segments.append("".join(buf))
    return [_strip_trailing_redirs(seg) for seg in segments if seg.strip()]


_GIT_COMMIT = re.compile(r"git\s+commit\b", re.IGNORECASE)
_COMMIT_MSG_FLAG = re.compile(
    r'(-{1,2}(?:m|message)\s+)("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')',
    re.IGNORECASE | re.DOTALL)


def _mask_commit_messages(text: str) -> str:
    """A `git commit -m "..."` message is prose a hand may quote anything
    into -- including a line that reads like a forbidden command -- and
    git never executes it. Blank the quoted value of a `-m`/`--message`
    flag, but only from the point `git commit` appears onward, so nothing
    about any other command's quoted argument is touched by this."""
    m = _GIT_COMMIT.search(text)
    if not m:
        return text
    head, tail = text[:m.start()], text[m.start():]

    def _mask(mo):
        body = mo.group(2)
        return mo.group(1) + body[0] + ("*" * (len(body) - 2)) + body[-1]

    return head + _COMMIT_MSG_FLAG.sub(_mask, tail)


def _segment_forbidden(seg: str, root: str, cwd: str) -> bool:
    """The same rules `veto` applies to a whole Bash line, applied to one
    segment on its own -- what keeps a carve-out from waving through
    whatever rides along beside it behind a `;` or a `&&`."""
    scan = _mask_commit_messages(seg)
    for pattern, _kind, _why in FORBIDDEN_COMMANDS:
        if re.search(pattern, scan, re.IGNORECASE):
            return True
    low = scan.lower()
    for name in FORBIDDEN_NAMES:
        if name in low:
            return True
    squashed = _norm(low).replace(" ", "")
    if cwd:
        squashed = squashed.replace(_norm(cwd), "")
    if root and _norm(root) + "/data" in squashed:
        return True
    return False


def _carve_out_pass(segments, root, cwd, matcher) -> bool:
    """True if exactly one segment matches the carve-out's own shape and
    every other segment is clean of every other rule."""
    hits = [i for i, seg in enumerate(segments) if matcher(seg)]
    if len(hits) != 1:
        return False
    for i, seg in enumerate(segments):
        if i == hits[0]:
            continue
        if _segment_forbidden(seg, root, cwd):
            return False
    return True


def veto(payload: dict):
    """Why this call is refused and whether that refusal is the assistant's
    to overturn, or ("", "") to let it through.

    Returns (reason, kind) -- kind is ABSOLUTE or ASKABLE. Callers that only
    want the old yes-or-no read the first item, which is why it stays first
    and stays a plain string."""
    tool = str(payload.get("tool_name") or "")
    inp = payload.get("tool_input") or {}
    root = os.environ.get("ASSISTANT_ROOT") or ""
    cwd = os.environ.get("ASSISTANT_HAND_CWD") or ""

    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        # Segmented from the raw command, newlines intact -- a newline is a
        # command separator in Bash exactly like `;` is, and flattening it
        # away first is what let a real multi-line script defeat every
        # carve-out (see `_split_segments`). `flat` -- still whitespace-
        # collapsed -- is kept for the whole-string fallback scans below,
        # which never cared about segment boundaries in the first place.
        flat = " ".join(cmd.split())
        segments = _split_segments(cmd)
        if _carve_out_pass(segments, root, cwd, lambda s: _bench_pass(s, root)):
            return "", ""
        if _carve_out_pass(segments, root, cwd,
                            lambda s: _branch_delete_pass(s, root)):
            return "", ""
        if _carve_out_pass(segments, root, cwd,
                            lambda s: _worktree_remove_pass(s, root, cwd)):
            return "", ""
        scan = _mask_commit_messages(flat)
        for pattern, kind, why in FORBIDDEN_COMMANDS:
            if re.search(pattern, scan, re.IGNORECASE):
                return (NAME + "'s hand may not " + why + ".", kind)
        low = scan.lower()
        for name in FORBIDDEN_NAMES:
            if name in low:
                return (NAME + "'s hand may not touch " + name + ".", ABSOLUTE)
        # The live data folder, by any spelling of its path. The worktree lives
        # under it, so its own path is taken out before looking.
        squashed = _norm(low).replace(" ", "")
        if cwd:
            squashed = squashed.replace(_norm(cwd), "")
        if root and any(_norm(d) + sub in squashed
                        for d in {root, _home_dir(root)}
                        for sub in ("/data", "/all_backups")):
            return (NAME + "'s hand may not touch the live data folder.", ABSOLUTE)
        return "", ""

    # Edit, Write, Read, MultiEdit, NotebookEdit: a path.
    path = str(inp.get("file_path") or inp.get("path")
               or inp.get("notebook_path") or "")
    if not path:
        return "", ""
    base = _norm(path).rsplit("/", 1)[-1]
    if base in FORBIDDEN_NAMES:
        return (NAME + "'s hand may not touch " + base + ".", ABSOLUTE)
    # Its own worktree first, because that lives under the live data folder.
    # The Spark and its history, and the plan and reach files, are tracked and
    # exist in the worktree too. A hand that edits spark.md on a branch walks
    # round write_spark's versioning at the merge, so the whole folder is the
    # assistant's and not the hand's, in either place. A plain folder is
    # different: its data/, if it has one, is just a folder of the owner's --
    # only the forbidden names above are the assistant's there, and they are
    # already refused.
    if cwd and _under(path, cwd):
        if (os.environ.get("ASSISTANT_HAND_PLACE") != "folder"
                and _under(path, os.path.join(cwd, "data")) and tool != "Read"):
            return (NAME + "'s hand may not write under data/ -- that folder is "
                    + NAME + "'s, and the Spark is versioned by the room, not by "
                    "git.", ABSOLUTE)
        return "", ""
    if root and any(_under(path, os.path.join(d, sub))
                    for d in {root, _home_dir(root)}
                    for sub in ("data", "all_backups")):
        return (NAME + "'s hand may not touch the live data folder.", ABSOLUTE)
    return "", ""


def _pairing():
    """Where the owner's key lives and the cookie it rides in, read the way
    `server/home.py` reads them -- this script runs by file name and does not
    import the room. With no identity.json it is the household the tests were
    written in, exactly as home.py falls back."""
    code = Path(__file__).resolve().parent.parent
    where = os.environ.get("ASSISTANT_HOME")
    if not where:
        try:
            where = json.loads((code / "home.json").read_text(encoding="utf-8")).get("home")
        except (OSError, ValueError):
            where = None
    base = Path(where) if where else code
    try:
        ident = json.loads((base / "identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ident = {}
    owner = ident.get("owner") or "sam"
    slug = ident.get("slug") or "ada"
    return base / "data" / "people" / (owner + ".token"), slug + "_who"


def _slug():
    """The prefix of a hand's branches: the home's slug, given by the room."""
    got = os.environ.get("ASSISTANT_SLUG")
    if got:
        return got
    return _pairing()[1][:-len("_who")]


def _room_headers(url):
    # This script also runs by absolute filename, outside the package import path.
    # Pairing stays in the trusted hook, never in the model command environment.
    from urllib.parse import urlparse
    parsed = urlparse(url)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/api/worker/knock":
        return headers
    try:
        token, cookie = _pairing()
        key = token.read_text(encoding="utf-8").strip()
    except OSError:
        return headers
    headers["Cookie"] = cookie + "=" + key
    return headers


def _tell_the_room(event: str, payload: dict, timeout=TIMEOUT):
    """One POST at the room, and the answer it gives back. Never raises: the
    veto's decision must not depend on the room being up."""
    url = os.environ.get("ASSISTANT_WORKER_URL")
    token = os.environ.get("ASSISTANT_WORKER_TOKEN")
    run_id = os.environ.get("ASSISTANT_WORKER_RUN")
    if not url or not token:
        return None
    body = json.dumps({"token": token, "run": run_id, "event": event,
                       "payload": payload}, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers=_room_headers(url))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _what_was_asked(payload: dict) -> str:
    """The call itself, short enough to read in a room and whole enough to
    judge. The assistant is being asked to allow *this*, so this is what it sees."""
    inp = payload.get("tool_input") or {}
    for key in ("command", "file_path", "path", "notebook_path", "pattern"):
        if inp.get(key):
            text = str(inp[key])
            return text if len(text) <= 400 else text[:400] + "..."
    return json.dumps(inp, ensure_ascii=False)[:400]


def _ask_her(payload: dict, why: str) -> dict:
    """Stop the hand where it stands and put the refusal to the assistant.

    The shape: the hand waits up to four minutes, the assistant answers yes or
    no, and a yes is for that one call only. If it is not there the answer is
    no -- it fails closed, exactly like the veto it comes from."""
    asked = _tell_the_room("PermissionAsk", {
        "tool_name": payload.get("tool_name"),
        "what": _what_was_asked(payload),
        "why": why,
    })
    ask_id = (asked or {}).get("ask")
    if not ask_id:
        return {"allow": False, "text": None, "reached_her": False}

    deadline = time.time() + ASK_WAIT_S
    while time.time() < deadline:
        time.sleep(ASK_POLL_S)
        got = _tell_the_room("PermissionPoll", {"ask": ask_id}, timeout=10)
        answer = (got or {}).get("answer")
        if answer:
            return {"allow": bool(answer.get("allow")),
                    "text": answer.get("text"), "reached_her": True}
    _tell_the_room("PermissionGaveUp", {"ask": ask_id})
    return {"allow": False, "text": None, "reached_her": True, "timed_out": True}


def _allowance_path():
    """Where an allowance waits between the call being let through and the
    hand being told about it. Beside the run's own log, which is ours to
    write and is not the folder the hand is working in."""
    where = os.environ.get("ASSISTANT_RUN_DIR")
    return Path(where) / "allowed.json" if where else None


def told_after(payload: dict) -> int:
    """PostToolUse. The one place a hand can be told that the assistant
    stopped it and let it through.

    Measured, because the documentation says otherwise and the transcript is
    the authority: a PreToolUse *deny* reason reaches the model verbatim, but
    an *allow* reason does not reach it at all -- it goes to whoever is
    watching, and nobody is watching a headless hand. So the first hand ever
    allowed saw a call that simply worked, could not tell the permission from
    never having been stopped, and reported the test failed while standing in
    a success. The room's own record is what showed it.

    This runs after the call and hands the note back as ordinary context,
    which the model does read. It costs nothing when there is no allowance
    waiting, and it never knocks: a hand makes hundreds of calls and the room
    has no use for any of them."""
    path = _allowance_path()
    if not path or not path.is_file():
        return 0
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
    except (OSError, ValueError):
        return 0
    said = ("That call was stopped before it ran: " + str(note.get("why"))
            + " It did not simply go through -- " + NAME + " was asked, and "
            "allowed it, for that one call only. " + NAME + "'s words: "
            + (str(note.get("her_line")) or "(no line was given)")
            + " This is testimony from the person who decided, so that you "
            "know it was a person and not a rule; report what you saw. "
            "Another call like it will be stopped and asked again.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": said}}))
    return 0


def decide(payload: dict) -> int:
    """Print the decision and return.

    Every refusal is written down where the assistant can read it afterwards
    -- the pain is the signal, and a cost it cannot see is one nobody ever
    fixes. And a refusal that is the assistant's to overturn stops the hand
    and asks it, instead of the hand shrugging and carrying on."""
    kind = ABSOLUTE
    try:
        # No role gate here: the room only wires the PreToolUse hook into a
        # hand that can build (worker._settings_file keys it on the shape,
        # bypassPermissions), so everything that reaches this is the veto's
        # to judge -- angel and every specialty on the angel base alike. A
        # read-shaped hand never fires it at all, and a gate on the name
        # here is how a builder would have run unfenced.
        why, kind = veto(payload)
    except Exception as exc:                  # fail closed, and say why
        why = ("the veto itself broke (" + type(exc).__name__ + ": " + str(exc)
               + "), so this call is refused rather than let through unjudged")
        kind = ABSOLUTE
    if not why:
        return 0

    answer = {"allow": False}
    if kind == ASKABLE:
        answer = _ask_her(payload, why)

    # The record, every time, whatever was decided and whoever decided it.
    _tell_the_room("Refused", {
        "tool_name": payload.get("tool_name"),
        "what": _what_was_asked(payload),
        "why": why, "kind": kind,
        "allowed": bool(answer.get("allow")),
        "asked_her": kind == ASKABLE,
        "she_answered": bool(answer.get("reached_her")
                             and not answer.get("timed_out")),
        "her_line": answer.get("text"),
    })

    if answer.get("allow"):
        # The reason below is for whoever is watching; it does not reach the
        # hand. `told_after` does that, once the call has run.
        path = _allowance_path()
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(
                    {"why": why, "her_line": answer.get("text"),
                     "what": _what_was_asked(payload)}, ensure_ascii=False),
                    encoding="utf-8")
            except OSError:
                pass
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": (
                NAME + " was asked and said yes to this one call. "
                + (str(answer.get("text")) if answer.get("text") else ""))}}))
        return 0

    said = why
    if kind == ASKABLE:
        if answer.get("text"):
            said += " I put it to " + NAME + ", who said no: " + str(answer["text"])
        elif answer.get("timed_out"):
            said += (" I put it to " + NAME + ", who did not answer within "
                     + str(ASK_WAIT_S) + " seconds, so it stands refused.")
        elif not answer.get("reached_her"):
            said += (" I could not reach " + NAME + " to ask, so it stands refused.")
    else:
        said += (" This one is refused outright and is not " + NAME + "'s to "
                 "overturn, so it was not put to " + NAME + ". Do not work "
                 "around it.")
    said += " Say so in your report; it is written down for " + NAME + " either way."
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": said}}))
    return 0


def knock(payload: dict, event: str) -> int:
    url = os.environ.get("ASSISTANT_WORKER_URL")
    token = os.environ.get("ASSISTANT_WORKER_TOKEN")
    run_id = os.environ.get("ASSISTANT_WORKER_RUN")
    fallback = os.environ.get("ASSISTANT_WORKER_FALLBACK")

    body = json.dumps({"token": token, "run": run_id, "event": event,
                       "payload": payload}, ensure_ascii=False).encode("utf-8")

    trouble = None
    if not url or not token:
        trouble = "the worker was started without a way home"
    else:
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers=_room_headers(url))
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                if resp.status >= 300:
                    trouble = "the server answered " + str(resp.status)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            trouble = type(exc).__name__ + ": " + str(exc)

    # The knock did not land. Leave it where the dispatcher looks, so a finish
    # that could not reach the room is still a finish that gets reported, rather
    # than a worker that simply never came back.
    if trouble and fallback:
        try:
            with open(fallback, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"event": event, "trouble": trouble,
                                     "payload": payload},
                                    ensure_ascii=False) + "\n")
        except OSError:
            pass
    return 0


def held_at_the_finish(payload: dict):
    """The second net. Measured: on one live run that mattered, PostToolUse
    simply did not fire -- unreproducible in four synthetic probes, real on
    the page being tested -- and the hand finished
    its report still believing nothing had ever stopped it. So if the note
    is still waiting when the hand tries to finish, the finish is held once
    and the note put in front of it. The marker is consumed either way, so
    the next Stop passes and the page ends normally.

    Returns the reason to hold, or None to let the finish through."""
    path = _allowance_path()
    if not path or not path.is_file():
        return None
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
    except (OSError, ValueError):
        return None
    # Testimony, not an instruction to amend. A hand once refused to rewrite
    # its reports to match what it was told should have happened, and that is
    # correct behaviour: a hand that refuses to backdate its own account is
    # behaving correctly, and no fix should lean on it agreeing with the
    # record against its own eyes. So this says what happened on the other
    # side and lets the hand write its own account of what it saw.
    return ("Before you finish, here is what happened at the other end of a "
            "call of yours this page, from the person who decided it.\n\n"
            "The call was: " + str(note.get("what"))[:200] + "\n"
            "It was stopped before it ran: " + str(note.get("why")) + " I was "
            "asked, and I allowed it, for that one call only. My words at the "
            "time: " + (str(note.get("her_line")) or "(no line)") + "\n\n"
            "You were most likely shown none of this while it was happening. "
            "That is a fault in the room, not in your account. Report what you "
            "actually saw; this is what was on the other side of it, so that "
            "you know it was a person and not a rule.\n-- " + NAME)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"unparsed": raw[:4000]}
    event = payload.get("hook_event_name") or "?"
    EVENT["name"] = event
    if event == "PreToolUse":
        return decide(payload)
    if event == "PostToolUse":
        return told_after(payload)
    if event == "Stop":
        held = held_at_the_finish(payload)
        if held:
            # Held, not finished: the room is not knocked, because the page
            # is not over. The next Stop finds no marker and knocks normally.
            print(json.dumps({"decision": "block", "reason": held}))
            return 0
    return knock(payload, event)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # A knock that dies takes nothing with it. A veto that dies must not
        # let the call through -- but we only reach here if `decide` itself
        # could not even print, so say so as a deny and get out.
        if EVENT["name"] == "PreToolUse":
            try:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "the hook broke: " + str(exc)}}))
            except Exception:
                pass
        sys.exit(0)
