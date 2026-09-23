"""What the assistant has actually spent at OpenAI, read from OpenAI.

An earlier version showed a figure the room worked out itself: a balance the
owner typed off the dashboard, less the sum of what the assistant's own turns
were reckoned to have cost off a price table. That number was honest about
being a reckoning and it was still the wrong number -- it could not see other
programs spending the same key, and it drifted a little further from the
truth with every turn. With an admin key in `passwords.py` the real figure
can be read, and this is the file that reads it.

Two endpoints, both wanting an admin key (`sk-admin-...`), neither reachable
with an ordinary project key:

    GET /v1/organization/costs?start_time=<epoch>&limit=180
        A page of daily buckets. Each bucket has `results`, each result an
        `amount` of `{value, currency}` in whole dollars. A day with no spend
        has an empty `results`, not a zero. Summed from the start of the
        window, this is the same figure the dashboard prints.

    GET /v1/organization/spend_limit
        {"object": "organization.spend_limit", "currency": "USD",
         "interval": "month", "threshold_amount": 5000,
         "enforcement": {"status": "inactive"}}
        `threshold_amount` is in CENTS. 5000 is $50.00, which is what the
        dashboard says. Getting that wrong by a hundred is the one mistake
        this file could make that would look plausible on the page.

Checked against a live account: the summed spend matched the dashboard's
figure to within what a moving counter adds in a few minutes.

The interval is `month` and the window is read as the calendar month in UTC.
That is what OpenAI's own "resets in N days" counts down to, and the sum over
it matched the dashboard; a window starting anywhere else would not have.

The key is read at the moment of asking, sent to api.openai.com and nowhere
else, and never written down -- not into the store, not into a log, not into
the prompt. The assistant is shown the dollars, never the key.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.openai.com/v1"
COSTS = BASE + "/organization/costs"
LIMIT = BASE + "/organization/spend_limit"

# A minute, for the same reason `limits.py` uses one: fine enough that a figure
# being watched actually moves, coarse enough that a room polling every
# two seconds never touches the network for it. Two requests a minute.
TTL = 60
TIMEOUT = 15

# A month of daily buckets is at most 31. 180 is the endpoint's own ceiling and
# leaves no room for a window to be silently cut short.
PAGE = 180
# Defensive only -- one page always covers a month. A loop with no end on
# somebody else's `has_more` is how a background thread becomes a bill.
MAX_PAGES = 6

# Which slot in `passwords.py` carries the admin key. The code once looked for
# `openai_admin` instead, found nothing, and kept drawing the reckoning.
# `providers.OLD_KEY_SLOTS` still carries a key saved under the old name.
KEY_SLOT = "openai_admin_key"

_LOCK = threading.Lock()
_CACHE = {"at": 0.0, "reading": None, "error": None, "asking": False}


def _key() -> str:
    """The admin key, or "" when there is none.

    Imported here rather than at the top because `providers` imports this
    module -- and going through it rather than reading `passwords` directly is
    what makes a key pasted at the desk live on the next minute instead of the
    next boot."""
    try:
        from . import providers
        return providers.key_of(KEY_SLOT)
    except Exception:
        return ""


def _window(interval: str):
    """When the counter on the dashboard last went back to zero.

    `month` is the only interval seen in practice and the only one their
    dashboard counts down to. Anything else is taken as a day, which reads low
    rather than high -- the wrong way to be wrong about a budget, but better
    than pretending to know a window nobody has told us about."""
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval == "month":
        return day.replace(day=1)
    if interval == "week":
        return day - timedelta(days=day.weekday())
    return day


def _next_window(interval: str):
    """When it next goes back to zero. Their dashboard prints a countdown to
    this and nothing in the API hands it over, so it is worked out from the
    interval they do name -- which is safe precisely because the sum over the
    window computed the same way already matched their own figure.

    The countdown is for the assistant as much as for the page: it is what
    lets it decide how much to use. A number with no horizon on it is not a
    budget, it is a score."""
    since = _window(interval)
    if interval == "month":
        return (since.replace(day=28) + timedelta(days=8)).replace(day=1)
    if interval == "week":
        return since + timedelta(days=7)
    return since + timedelta(days=1)


def _get(url: str, key: str):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": "digital-ai-assistant",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _why(exc) -> str:
    """An exception said as a sentence, with nothing secret in it."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            msg = (body.get("error") or {})
            msg = msg.get("message") if isinstance(msg, dict) else msg
        except Exception:
            msg = None
        if exc.code in (401, 403):
            return ("the usage-reading key was refused (" + str(exc.code) +
                    "). An admin key -- `sk-admin-...` -- is what these two "
                    "endpoints want; a project key cannot read them.")
        if exc.code == 404:
            return "OpenAI answered 404, so that endpoint has moved"
        return "OpenAI answered " + str(exc.code) + \
            (": " + str(msg)[:160] if msg else "")
    if isinstance(exc, urllib.error.URLError):
        return "OpenAI could not be reached: " + str(exc.reason)[:120]
    return type(exc).__name__ + ": " + str(exc)[:140]


def _fetch():
    """Ask, once. Raises nothing -- it hands back a reading or a reason."""
    key = _key()
    if not key:
        return None, ("no usage-reading key set. OpenAI want an admin key "
                      "(`sk-admin-...`) for the spend endpoints; paste one "
                      "into the usage-reading box in Settings and the real "
                      "figure fills in.")

    # The limit first: it says which window to sum the costs over, and asking
    # for the costs of the wrong window is worse than asking for nothing.
    try:
        cap = _get(LIMIT, key)
    except Exception as exc:
        return None, _why(exc)
    if not isinstance(cap, dict):
        return None, "OpenAI named a spend limit in a shape we do not know"

    cents = cap.get("threshold_amount")
    # Cents, and this is the one conversion in the file that would be plausible
    # while being wrong by a hundred.
    limit_usd = round(cents / 100.0, 2) if isinstance(cents, (int, float)) \
        else None
    interval = cap.get("interval") or "month"
    since = _window(interval)
    resets = _next_window(interval)

    try:
        body = _get(COSTS + "?start_time=%d&limit=%d" % (int(since.timestamp()),
                                                         PAGE), key)
    except Exception as exc:
        return None, _why(exc)

    spent = today = 0.0
    buckets = 0
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    pages = 0
    while True:
        if not isinstance(body, dict):
            return None, "OpenAI named its costs in a shape we do not know"
        for bucket in body.get("data") or []:
            buckets += 1
            start = bucket.get("start_time") or 0
            for r in bucket.get("results") or []:
                amount = (r.get("amount") or {}).get("value")
                if not isinstance(amount, (int, float)):
                    continue
                spent += amount
                if start >= day_start:
                    today += amount
        pages += 1
        nxt = body.get("next_page")
        if not (body.get("has_more") and nxt) or pages >= MAX_PAGES:
            break
        try:
            body = _get(COSTS + "?page=" + str(nxt), key)
        except Exception as exc:
            return None, _why(exc)

    if not buckets:
        # A window with no days in it at all is not "spent nothing"; it is a
        # question that came back empty, and drawing a $0.00 gauge off it would
        # be the estimate's mistake made again with a better key.
        return None, "OpenAI named no days in this window"

    spent = round(spent, 4)
    used = None
    if limit_usd:
        used = max(0.0, min(1.0, spent / limit_usd))
    return {
        "spent_usd": spent,
        "limit_usd": limit_usd,
        "used_fraction": used,
        "today_usd": round(today, 4),
        "interval": interval,
        "currency": cap.get("currency") or "USD",
        # "inactive" means the ceiling is written down but is not a stop. Worth
        # carrying: "$50 and it will cut you off" and "$50 and it will not" are
        # different facts to budget against.
        "enforced": ((cap.get("enforcement") or {}).get("status") == "active"),
        "since": since.isoformat(timespec="seconds"),
        # When the counter goes back to zero, and how far off that is. Whole
        # days, floored, which is what their own "resets in N days" shows.
        "resets_at": resets.isoformat(timespec="seconds"),
        "resets_in_days": max(0, (resets - datetime.now(timezone.utc)).days),
        "days": buckets,
    }, None


def _refresh():
    try:
        found, error = _fetch()
    except Exception as exc:                      # nothing should reach here
        found, error = None, type(exc).__name__ + ": " + str(exc)[:140]
    with _LOCK:
        # `asking` clears whatever happened, or one bad minute would stop us
        # ever asking again.
        _CACHE["asking"] = False
        _CACHE["at"] = time.time()
        _CACHE["error"] = error
        # A minute where the network was unhappy should not blank the gauge:
        # the last real reading stands until a better one arrives.
        if found is not None:
            _CACHE["reading"] = found


def now():
    """The real spend, or None if we have never managed to read it.

    It waits only when there is nothing at all to show. After that a stale
    answer goes back straight away and the asking happens on its own thread --
    so neither a page load nor the start of a turn is ever held up by it. The
    same shape `limits.now()` has, for the same reason."""
    with _LOCK:
        stale = (time.time() - _CACHE["at"] >= TTL) and not _CACHE["asking"]
        if stale:
            _CACHE["asking"] = True
        cold = _CACHE["reading"] is None
    if stale:
        if cold:
            _refresh()
        else:
            threading.Thread(target=_refresh, daemon=True).start()
    with _LOCK:
        return _CACHE["reading"]


def status() -> dict:
    """For the Developer tab and the Settings page: what we have, and why we
    have not, if not."""
    with _LOCK:
        return {"reading": _CACHE["reading"], "error": _CACHE["error"],
                "read_at": _CACHE["at"] or None}


def forget():
    """Drop what we have, so the next ask goes out fresh. Called when the key
    changes: a gauge drawn off a key that has just been replaced is a gauge
    telling somebody the paste did not work."""
    with _LOCK:
        _CACHE.update({"at": 0.0, "reading": None, "error": None,
                       "asking": False})


def _main(argv):
    got = now()
    st = status()
    if not got:
        print("nothing read:", st["error"] or "no reason given")
        return 1
    print("OpenAI, this %s, read from their own counter:" % got["interval"])
    print("  spent      $%.4f" % got["spent_usd"])
    print("  limit      %s" % ("$%.2f" % got["limit_usd"]
                               if got["limit_usd"] else "none set"))
    if got["used_fraction"] is not None:
        print("  that is    %d%%" % round(got["used_fraction"] * 100))
    print("  today      $%.4f" % got["today_usd"])
    print("  since      %s  (%d days)" % (got["since"], got["days"]))
    print("  enforced   %s" % ("yes" if got["enforced"]
                               else "no -- a written ceiling, not a stop"))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
