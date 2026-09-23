"""What is left on the owner's plan, read from where the numbers actually live.

The stream the CLI hands back names a window and when it resets and stops
there -- `rate_limit_info` carries no figure at all, and only the five-hour
window ever appears in it. That is why the header said "open" for months: not
a broken gauge, nothing to read. The percentages Claude Code shows in its own
footer come from a small endpoint on the account, so the room's come from the
same one.

The login is read at the moment of asking, sent to api.anthropic.com and
nowhere else, and never written down -- not into the store, not into a log,
not into the prompt. The assistant is shown the percentages, never the key.

Reading the owner's login is a grant the owner makes, not a default to assume.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CRED_PATH = Path.home() / ".claude" / ".credentials.json"
URL = "https://api.anthropic.com/api/oauth/usage"

# A minute is finer than the thing being measured -- a five-hour window moves
# about a third of a percent a minute at a busy pace -- and coarse enough that the
# room polling every two seconds never touches the network for it.
TTL = 60
TIMEOUT = 10

# The endpoint answers with far more than we show: dollar caps that are null on
# a subscription plan, and a row of internal codenames. `limits` is the display list
# Claude Code itself draws from, so that is the one to trust.
KIND_WINDOW = {
    "session": "five_hour",
    "weekly_all": "seven_day",
    "weekly_scoped": "seven_day_scoped",
}
KIND_LABEL = {
    "session": "5-hour limit",
    "weekly_all": "Weekly · all models",
    "weekly_scoped": "Weekly",
}

_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "limits": None, "error": None, "asking": False}


def _token():
    """The owner's access token, or None if there is no login to read."""
    try:
        cred = json.loads(CRED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    oauth = cred.get("claudeAiOauth") or {}
    return oauth.get("accessToken") or None


def _tidy(row: dict) -> dict:
    """One window, in the shape the header already knows how to draw."""
    kind = row.get("kind")
    window = KIND_WINDOW.get(kind, kind)
    label = KIND_LABEL.get(kind, kind)
    # A scoped weekly window is the same weekly limit counted for one model
    # only, and the model is the whole point of showing it twice.
    scope = (row.get("scope") or {}).get("model") or {}
    named = scope.get("display_name")
    if named:
        window = "seven_day_" + named.lower().replace(" ", "_")
        label = "Weekly · " + named
    pct = row.get("percent")
    return {
        "window": window,
        "label": label,
        "used_fraction": None if pct is None else pct / 100.0,
        "status": None,
        "severity": row.get("severity"),
        "resets_at": row.get("resets_at"),
        "using_overage": False,
    }


def _fetch():
    from . import providers
    try:
        with providers.model_activity("claude_code/claude-opus-5"):
            return _fetch_claude()
    except providers.Refused as exc:
        return None, str(exc)


def _fetch_claude():
    """Ask, once. Raises nothing -- it hands back a list or a reason."""
    token = _token()
    if not token:
        return None, "no login stored on this machine"
    req = urllib.request.Request(URL, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "digital-ai-assistant (its own room)",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 401 is the ordinary one: the token went stale between the owner's sessions
        # and Claude Code has not been run since to renew it.
        return None, "the plan answered " + str(exc.code)
    except Exception as exc:                      # network, timeout, bad JSON
        return None, type(exc).__name__ + ": " + str(exc)[:120]

    rows = body.get("limits")
    if not isinstance(rows, list):
        return None, "the plan answered in a shape we do not know"
    out = [_tidy(r) for r in rows if isinstance(r, dict)]
    return ([r for r in out if r["used_fraction"] is not None] or None,
            None if out else "the plan named no windows")


def _refresh():
    try:
        found, error = _fetch()
    except Exception as exc:                      # nothing should reach here
        found, error = None, type(exc).__name__ + ": " + str(exc)[:120]
    with _LOCK:
        # `asking` clears whatever happened, or one bad minute would stop us
        # ever asking again.
        _CACHE["asking"] = False
        _CACHE["at"] = time.time()
        _CACHE["error"] = error
        # A minute where the network was unhappy should not blank the header:
        # the last real reading stands until a better one arrives.
        if found is not None:
            _CACHE["limits"] = found


def now():
    """The percentages, or None if we have never managed to read them.

    It waits only when there is nothing at all to show. After that a stale
    answer goes back straight away and the asking happens on its own thread --
    so neither a page load nor the start of a turn is ever held up by it."""
    from . import providers
    if providers.codex_only():
        return None
    with _LOCK:
        stale = (time.time() - _CACHE["at"] >= TTL) and not _CACHE["asking"]
        if stale:
            _CACHE["asking"] = True
        cold = _CACHE["limits"] is None
    if stale:
        if cold:
            _refresh()
        else:
            threading.Thread(target=_refresh, daemon=True).start()
    with _LOCK:
        return _CACHE["limits"]


def status() -> dict:
    """For the Developer tab: what we have, and why we have not, if not."""
    from . import providers
    if providers.codex_only():
        return {"limits": None, "error": providers.CLAUDE_PAUSED, "read_at": None}
    with _LOCK:
        return {"limits": _CACHE["limits"], "error": _CACHE["error"],
                "read_at": _CACHE["at"] or None}
