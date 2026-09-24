"""Which model the assistant thinks through, and what it costs.

The first road, and still the default, is Claude Code on the owner's own plan,
spoken to by running `claude -p` and reading its stream. Nothing here changes
that. What this adds is the other roads -- OpenAI's own door, and
OpenRouter's, which is one key into most houses at once -- so that "which
model is answering today" is a thing the Settings page can answer instead of
a thing baked into a constant.

Three services, and they are not equals:

  claude_code   The owner's plan. No key, no per-token bill, and the only
                road with real gauges: the five-hour and weekly windows that
                `limits.py` reads. Costs are estimates the CLI hands back.
                This is home.
  openai        Its own key, its own bill, per token. Terra and Sol. No plan
                windows to read -- the currency is dollars, and the gauge is
                what we have spent.
  openrouter    One key, most houses. The same Opus and Fable as the plan,
                plus everyone else's flagship, all at published prices. It is
                also the only service on this machine that will tell us a
                price list at all, which is why the OpenAI models borrow it.

The prices here are never typed in. Every figure the Settings page shows is
read from OpenRouter's live catalogue and cached for an hour -- because a
static price table is a table that is wrong the week after a price cut, and
this repo has been bitten by exactly that shape of stale number before. What
we cannot read live we say we cannot read, rather than showing a figure with
no year on it.

What is honestly not available, said here once so the page can say it too:

  * OpenAI publishes no price endpoint. Their model *list* is live and their
    prices are not, so Terra and Sol are priced off OpenRouter's entry for
    the same model, and the page labels that borrowing where it shows it.
  * OpenAI's credit *balance* is still not reachable with any API key -- the
    billing endpoints answer "session key only", meaning a browser. There is
    no number to have, so none is shown.

OpenAI's *spend* and their organization spend *limit* are both readable with
an admin key. With one in `passwords.py` the gauge on that road is a
measurement rather than a reckoning: real dollars against the real ceiling,
read by `openai_spend.py`. The older figure -- a balance the owner typed off
the dashboard, less what the assistant's own turns were reckoned to have
cost -- is kept only as a fallback, and wherever it is drawn it is labelled
an estimate. It could never see other programs spending the same key; the
real one can.

OpenRouter, by contrast, answers all three: prices, credits left, and spend
by day, week and month. Its gauge is the real one.

    python -m server.providers            where things stand
    python -m server.providers models     the catalogue, priced live
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from contextlib import contextmanager
from functools import wraps
from . import home

ROOT = Path(__file__).resolve().parent.parent
CHOICE_PATH = home.DATA / "provider.json"
PASSWORDS_PATH = Path(__file__).resolve().parent / "passwords.py"

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENAI_BASE = "https://api.openai.com/v1"

# An hour. Prices move on the scale of press releases, not minutes, and the
# room polls this page every couple of seconds while it is open.
PRICE_TTL = 3600
# Money moves faster than prices do, and a balance nobody is watching costs
# nothing to be a minute stale.
BALANCE_TTL = 120
NET_TIMEOUT = 20
# A whole turn through somebody else's door. The CLI road gets ten minutes
# and there is no reason this one should get less.
CALL_TIMEOUT = 600


class Refused(ValueError):
    """Something was asked for that this module will not do, with the reason
    in the message. Never a stack trace at a person."""


CLAUDE_PAUSED = "Paused: Codex-only mode disables Claude/Anthropic. Pending work is retained."
_MODE_LOCK = threading.RLock()
_CLAUDE_ACTIVE = 0


def codex_only() -> bool:
    try:
        return json.loads(CHOICE_PATH.read_text(encoding="utf-8")).get("codex_only", False) is True
    except FileNotFoundError:
        return False
    except (OSError, ValueError, AttributeError):
        # An unreadable setting must not reopen the Claude road.
        return True


def is_claude(spec) -> bool:
    spec = resolve(spec) if isinstance(spec, str) else spec
    words = " ".join(str(spec.get(k) or "") for k in ("service", "key", "id")).lower()
    # Router auto-selection can route to Anthropic without naming it up front.
    router_id = str(spec.get("id") or "").lower().split(":", 1)[0]
    return ("claude" in words or "anthropic" in words
            or (spec.get("service") == "openrouter"
                and (router_id.startswith("openrouter/")
                     or router_id in ("auto", "free", "switchpoint/router"))))


def native_tools_enabled() -> bool:
    """Default on for Codex; an explicit saved off or unreadable config stays off."""
    try:
        return json.loads(CHOICE_PATH.read_text(encoding="utf-8")).get("native_tools", True) is True
    except FileNotFoundError:
        return True
    except (OSError, ValueError, AttributeError):
        return False


def set_native_tools(enabled):
    if not isinstance(enabled, bool):
        raise Refused("Native tools must be true or false")
    if enabled and resolve(chosen())["service"] != "codex":
        raise Refused("Choose Codex subscription chat first.")
    _save_choice(native_tools=enabled)


def paused_reason(model) -> str | None:
    return CLAUDE_PAUSED if codex_only() and is_claude(model) else None


@contextmanager
def model_activity(spec):
    """Serialize mode changes with Claude calls, including an entire worker page."""
    global _CLAUDE_ACTIVE
    claude = is_claude(spec)
    with _MODE_LOCK:
        if claude and codex_only():
            raise Refused(CLAUDE_PAUSED)
        if claude:
            _CLAUDE_ACTIVE += 1
    try:
        yield
    finally:
        if claude:
            with _MODE_LOCK:
                _CLAUDE_ACTIVE -= 1


def claude_operation(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with model_activity("claude_code/claude-opus-5"):
            return fn(*args, **kwargs)
    return guarded


def set_codex_only(enabled):
    if not isinstance(enabled, bool):
        raise Refused("Codex-only mode must be true or false")
    with _MODE_LOCK:
        if enabled and _CLAUDE_ACTIVE:
            raise Refused("Wait for the active Claude operation to finish before enabling Codex-only mode.")
        _save_choice(codex_only=enabled)


def paused_capabilities() -> list:
    if not codex_only():
        return []
    names = ["Delegated workers and their job execution", "Model-backed web search",
             "Claude quota polling and quota senses"]
    if paused_reason(dream_model()):
        names.append("Dreams (saved night model requires Claude)")
    if paused_reason(chosen()):
        names.append("Chat and scheduled turns (saved chat model requires Claude; choose a model in Settings)")
    return [{"capability": name, "status": "paused", "reason": CLAUDE_PAUSED}
            for name in names]


# -- who the assistant can think through -------------------------------------

SERVICES = {
    "codex": {
        "label": "Codex subscription",
        "how": "ChatGPT subscription through the installed Codex app server.",
        "needs_key": False,
        "note": "Run codex login on this computer using your ChatGPT account. "
                "API-key sign-ins are refused. Subscription limits apply; "
                "the API dollar balance does not measure this connection.",
    },
    "claude_code": {
        "label": "Claude Code",
        "how": home.OWNER_NAME + "'s own plan, through the installed `claude` command. "
               "No key and no per-token bill -- the currency is the plan's "
               "five-hour and weekly windows, which are the gauges in the "
               "header.",
        "needs_key": False,
        "note": None,
    },
    "openai": {
        "label": "OpenAI",
        "how": home.NAME + "'s own key, billed per token at OpenAI's own door.",
        "needs_key": True,
        "note": "OpenAI publish no price endpoint, so the figures below are "
                "OpenRouter's live price for the same model. Their balance is "
                "not readable by any API key at all.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "how": "One key into most houses, billed per token out of a credit "
               "balance.",
        "needs_key": True,
        "note": "The only service here that answers with prices, credits and "
                "spend -- so it is also where every other model's price comes "
                "from.",
    },
}

# What `passwords.py` holds, and the one line each gets on the Settings page.
# The last three came across with the rest of the keys and are not offered
# as roads yet; they are kept because throwing away a working key to type it
# again later is nobody's idea of tidy.
KEY_SLOTS = {
    "openai": "OpenAI -- Terra and Sol think through this one.",
    "openrouter": "OpenRouter -- one key, many houses, and the live price list.",
    # Spelled the way it is actually written in the file. It used to be
    # `openai_admin` here, which found nothing and left the room drawing the
    # old reckoning while a perfectly good key sat on the disk -- and worse,
    # `set_key` rewrites this file from these names, so the next key saved at
    # the desk would have deleted it.
    "openai_admin_key": "OpenAI, usage-reading -- optional, and only for the "
                        "spend gauge. An admin key (`sk-admin-...`); an "
                        "ordinary project key cannot read those endpoints.",
    "anthropic": "Anthropic direct -- carried over, no road offered yet.",
    "gemini": "Google -- carried over, no road offered yet.",
    "deepseek": "DeepSeek -- carried over, no road offered yet.",
}

# What the same slot used to be called. A key found under the old name is
# carried across rather than ignored, so nobody has to paste it twice.
OLD_KEY_SLOTS = {"openai_admin_key": "openai_admin"}

# The models offered by name. Anything else can still be typed in, and a
# typed id is looked up in the live catalogue like these are -- this list is
# the shortcut, not the fence.
#
# `id` is what the service is actually asked for. `price_key` is where to
# look the money up, which for the OpenAI road is OpenRouter's entry for the
# same model: their own door has no price list and a made-up number is worse
# than a borrowed one.
MODELS = [
    {"key": "codex/gpt-6-astra", "service": "codex",
     "id": "gpt-6-astra", "label": "Astra (subscription)",
     "about": "OpenAI's most capable model through your ChatGPT subscription. "
              "Requires codex login.",
     "price_key": "openai/gpt-6-astra"},
    {"key": "codex/gpt-6-sol", "service": "codex",
     "id": "gpt-6-sol", "label": "GPT-6 Sol (subscription)",
     "about": "The everyday workhorse through your ChatGPT subscription. "
              "Requires codex login.",
     "price_key": "openai/gpt-6-sol"},
    {"key": "codex/gpt-6-luna", "service": "codex",
     "id": "gpt-6-luna", "label": "GPT-6 Luna (subscription)",
     "about": "Fast and light through your ChatGPT subscription, for easier "
              "work. Requires codex login.",
     "price_key": "openai/gpt-6-luna"},
    {"key": "codex/gpt-5.6-sol", "service": "codex",
     "id": "gpt-5.6-sol", "label": "Sol (subscription)",
     "about": "Sol through your ChatGPT subscription. Requires codex login.",
     "price_key": "openai/gpt-5.6-sol"},
    {"key": "codex/gpt-5.6-terra", "service": "codex",
     "id": "gpt-5.6-terra", "label": "Terra (subscription)",
     "about": "Terra through your ChatGPT subscription. Requires codex login.",
     "price_key": "openai/gpt-5.6-terra"},
    # -- the plan --
    {"key": "claude_code/claude-opus-5", "service": "claude_code",
     "id": "claude-opus-5", "label": "Opus 5",
     "about": "The default mind. Deep and steady, and the model the room "
              "was first built on.",
     "price_key": "anthropic/claude-opus-5"},
    {"key": "claude_code/claude-fable-5-1", "service": "claude_code",
     "id": "claude-fable-5-1", "label": "Fable 5.1",
     "about": "Thinks before it speaks, always, and spends the plan's windows "
              "fastest of anything here.",
     "price_key": "anthropic/claude-fable-5.1"},
    {"key": "claude_code/claude-sonnet-5", "service": "claude_code",
     "id": "claude-sonnet-5", "label": "Sonnet 5",
     "about": "Lighter and quicker than Opus, and a good deal cheaper against "
              "the weekly window.",
     "price_key": "anthropic/claude-sonnet-5"},

    # -- OpenAI's own door --
    {"key": "openai/gpt-5.6-terra", "service": "openai",
     "id": "gpt-5.6-terra", "label": "Terra",
     "about": "OpenAI's broad, grounded one.",
     "price_key": "openai/gpt-5.6-terra"},
    {"key": "openai/gpt-5.6-sol", "service": "openai",
     "id": "gpt-5.6-sol", "label": "Sol",
     "about": "Terra's warmer sibling, priced a little under it on the way out.",
     "price_key": "openai/gpt-5.6-sol"},

    # -- the router. Peers, not the cheap seats: only models in the same
    # class as Terra, Sol, Opus and Fable, so nothing from the mini/nano/lite
    # tier is listed here. --
    {"key": "openrouter/anthropic/claude-opus-5", "service": "openrouter",
     "id": "anthropic/claude-opus-5", "label": "Opus 5 (router)",
     "about": "The same mind as home, reached by the paid road instead of the "
              "plan -- which is what to switch to when the weekly window is "
              "spent mid-conversation.",
     "price_key": "anthropic/claude-opus-5"},
    {"key": "openrouter/anthropic/claude-fable-5.1", "service": "openrouter",
     "id": "anthropic/claude-fable-5.1", "label": "Fable 5.1 (router)",
     "about": "Fable off the plan's windows and onto a bill.",
     "price_key": "anthropic/claude-fable-5.1"},
    {"key": "openrouter/openai/gpt-6-astra", "service": "openrouter",
     "id": "openai/gpt-6-astra", "label": "GPT-6 Astra",
     "about": "OpenAI's newest flagship, which their own door does not offer "
              "here yet.",
     "price_key": "openai/gpt-6-astra"},
    {"key": "openrouter/openai/gpt-5.6-terra", "service": "openrouter",
     "id": "openai/gpt-5.6-terra", "label": "Terra (router)",
     "about": "Terra reached through the router rather than OpenAI directly.",
     "price_key": "openai/gpt-5.6-terra"},
    {"key": "openrouter/google/gemini-3.1-pro-preview", "service": "openrouter",
     "id": "google/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro",
     "about": "Google's capable one, with a million tokens of room.",
     "price_key": "google/gemini-3.1-pro-preview"},
    {"key": "openrouter/x-ai/grok-4.6", "service": "openrouter",
     "id": "x-ai/grok-4.6", "label": "Grok 4.6",
     "about": "xAI's flagship. Blunt where the others hedge.",
     "price_key": "x-ai/grok-4.6"},
    {"key": "openrouter/moonshotai/kimi-k3", "service": "openrouter",
     "id": "moonshotai/kimi-k3", "label": "Kimi K3",
     "about": "Moonshot's big open-weight one, and a very different training "
              "diet from everything above it.",
     "price_key": "moonshotai/kimi-k3"},
    {"key": "openrouter/qwen/qwen3.8-max-0902", "service": "openrouter",
     "id": "qwen/qwen3.8-max-0902", "label": "Qwen 3.8 Max",
     "about": "Alibaba's flagship, a million tokens, and cheap for the class.",
     "price_key": "qwen/qwen3.8-max-0902"},
    {"key": "openrouter/deepseek/deepseek-v4-pro", "service": "openrouter",
     "id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro",
     "about": "The cheapest thing on this page that is still in the class.",
     "price_key": "deepseek/deepseek-v4-pro"},
]

BY_KEY = {m["key"]: m for m in MODELS}

# The model used before this file existed. Changing this changes who answers
# when nobody has chosen, which is a thing to do on purpose.
DEFAULT_KEY = "claude_code/claude-opus-5"

# The old shorthands, from back when the model was a bare CLI alias. A store
# full of rows stamped "opus" still has to resolve to something.
ALIASES = {
    "opus": "claude_code/claude-opus-5",
    "sonnet": "claude_code/claude-sonnet-5",
    "haiku": "claude_code/claude-haiku-4-5",
    "fable": "claude_code/claude-fable-5-1",
    "claude-opus-5": "claude_code/claude-opus-5",
    "claude-sonnet-5": "claude_code/claude-sonnet-5",
    "claude-fable-5-1": "claude_code/claude-fable-5-1",
}


def resolve(key: str) -> dict:
    """A model key, an old alias, or a bare id, turned into the spec to call.

    Never raises and never returns None. Something unrecognised is assumed to
    be a router id, because that is the one service where typing an id we have
    never heard of is a supported thing to do -- and a room that will not
    answer because a string was unfamiliar is worse than one that tries."""
    key = (key or "").strip() or DEFAULT_KEY
    if key in BY_KEY:
        return BY_KEY[key]
    if key in ALIASES and ALIASES[key] in BY_KEY:
        return BY_KEY[ALIASES[key]]
    if key.startswith("codex/"):
        bare = key.split("/", 1)[1]
        return {"key": key, "service": "codex", "id": bare,
                "label": bare + " (subscription)", "about": None,
                "price_key": "openai/" + bare}
    if key.startswith("claude_code/"):
        bare = key.split("/", 1)[1]
        return {"key": key, "service": "claude_code", "id": bare,
                "label": bare, "about": None, "price_key": None}
    if key.startswith("openai/") and key.count("/") == 1:
        bare = key.split("/", 1)[1]
        return {"key": key, "service": "openai", "id": bare, "label": bare,
                "about": None, "price_key": "openai/" + bare}
    if key.startswith("openrouter/"):
        bare = key.split("/", 1)[1]
        return {"key": key, "service": "openrouter", "id": bare,
                "label": bare, "about": None, "price_key": bare}
    # A bare "vendor/model" -- the router's own spelling.
    if "/" in key:
        return {"key": "openrouter/" + key, "service": "openrouter",
                "id": key, "label": key, "about": None, "price_key": key}
    return dict(BY_KEY[DEFAULT_KEY])


def is_cli(key: str) -> bool:
    """Whether this model uses an installed subscription client."""
    return resolve(key)["service"] in ("claude_code", "codex")


# -- the keys -----------------------------------------------------------------

_KEYS = {}
_KEYS_READ = False
# Its own lock, held only across the read below. Never taken while `_LOCK` is
# held, and nothing under it reaches for `_LOCK`, so the two cannot meet.
_KEYS_LOCK = threading.Lock()


def _load_keys():
    """Read `passwords.py` once, under a lock.

    The lock is not decoration. The flag used to be set before the import ran,
    which meant a second thread arriving in that gap was told the keys were
    read and handed an empty dict -- and the room would then say OpenRouter
    had no key, refuse a model that was perfectly reachable, and be right
    again a second later. It showed up the first time the background price
    fetch and a page request raced at boot, which is exactly when a person
    would have seen it."""
    global _KEYS_READ
    if _KEYS_READ:
        return
    with _KEYS_LOCK:
        if _KEYS_READ:
            return
        try:
            from . import passwords
            _KEYS.update({k: (v or "")
                          for k, v in (passwords.KEYS or {}).items()})
            for now_called, was_called in OLD_KEY_SLOTS.items():
                if not (_KEYS.get(now_called) or "").strip():
                    got = (_KEYS.get(was_called) or "").strip()
                    if got:
                        _KEYS[now_called] = got
        except Exception:
            # No file, or a hand-edit that will not parse. Not fatal: it means
            # no service has a key, which the page says plainly. The room still
            # boots -- but it is not remembered as "read", so the next caller
            # tries again rather than being stuck keyless until a restart.
            return
        _KEYS_READ = True


def key_of(service: str) -> str:
    _load_keys()
    return (_KEYS.get(service) or "").strip()


def keys_present() -> dict:
    """Which keys are set, and how each one ends -- never the key itself.

    The last four characters are how a person tells two keys apart when they
    are deciding whether to paste a new one. The rest never leaves this
    process, and the whole thing never leaves this machine."""
    _load_keys()
    out = {}
    for slot, about in KEY_SLOTS.items():
        val = (_KEYS.get(slot) or "").strip()
        out[slot] = {
            "about": about,
            "set": bool(val),
            "tail": ("..." + val[-4:]) if len(val) >= 8 else ("set" if val else ""),
            "chars": len(val),
        }
    return out


PASSWORDS_TEMPLATE = '''"""The assistant's keys. This file is not in the repository and never will be.

Every service the assistant can think through wants a key, and a key in a
tracked file is a key published the next time anything is pushed. So they live
here, in `.gitignore` since before the file first existed, read by
`server/providers.py` and rewritten by the Settings page whenever the owner
changes one at the desk.

Plain Python on purpose: the owner can open it and fix a key with an editor
when the room will not start, which is exactly the moment a clever format is no help.
The Settings page rewrites this whole file when it saves, so a comment added
by hand inside KEYS will not survive that -- the words below come from
`KEY_SLOTS` in providers.py, which is where to change them.

A missing key is not an error. It means that service is not offered, and the
Settings page says which and why.
"""

KEYS = {
%s}


def get(name: str) -> str:
    """One key by service name, or "" when there is none. Never raises: a
    service with no key is a service that is not offered, which the page
    says out loud, rather than a room that will not boot."""
    return (KEYS.get(name) or "").strip()
'''


def set_key(service: str, value) -> dict:
    """Write one key, keeping the others. `value` of "" clears it.

    The file is rewritten whole and the in-memory copy updated with it, so the
    change is live on the next turn rather than the next boot -- a key pasted
    into Settings that did nothing until a restart would look exactly like a
    key that was refused."""
    if service not in KEY_SLOTS:
        raise Refused("there is no key called " + str(service)[:40] + " here")
    val = (value or "").strip()
    # A key pasted out of a web page arrives with whatever was around it.
    # Whitespace inside one is never meaningful and always a paste artefact.
    val = re.sub(r"\s+", "", val)
    _load_keys()
    with _KEYS_LOCK:
        _KEYS[service] = val
        body = []
        # Every slot we know about, and then anything else already in the file.
        # That second half is not tidiness: a slot added to the file by hand
        # (as `openai_admin_key` once was) would otherwise be quietly thrown
        # away by a whole-file rewrite the next time a key is saved at the desk.
        extra = [k for k in _KEYS if k not in KEY_SLOTS
                 and (_KEYS.get(k) or "").strip()]
        for slot, about in list(KEY_SLOTS.items()) + \
                [(k, "added by hand -- kept as found.") for k in sorted(extra)]:
            got = (_KEYS.get(slot) or "").strip()
            body.append("    # " + about + "\n    %r: %r,\n\n" % (slot, got))
        # Written beside and renamed over, so a crash halfway through leaves
        # the old keys whole instead of a truncated file that will not import.
        tmp = PASSWORDS_PATH.with_suffix(".py.writing")
        tmp.write_text(PASSWORDS_TEMPLATE % "".join(body), encoding="utf-8")
        os.replace(tmp, PASSWORDS_PATH)
    _forget_cached(service)
    return keys_present()


# -- the choice ---------------------------------------------------------------

def chosen() -> str:
    """Which model the room answers on. One line of JSON on disk, so it
    survives a restart -- being a different mind after every reboot is not a
    setting anybody wants."""
    try:
        got = json.loads(CHOICE_PATH.read_text(encoding="utf-8"))
        key = (got or {}).get("model")
        if key:
            return str(key)
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return DEFAULT_KEY


def dream_model() -> str:
    """Which model folds the shelf at night, which is deliberately not the
    one the chat is set to.

    A design decision from when the dreamer was built: a dream runs on the
    assistant's full model, not a small one. The pin that enforced it was a
    constant -- fine while there was one house, and wrong the moment there
    were three, because it would have named Opus on a night the chat had been
    on Terra. So it is a setting now, and still a pin: it never follows the
    chat's dropdown, and unset means the default model. Moving it is a thing
    somebody does on purpose, on this page, which is the whole difference
    between a choice and a drift."""
    try:
        got = json.loads(CHOICE_PATH.read_text(encoding="utf-8"))
        key = (got or {}).get("dream_model")
        if key:
            return str(key)
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return DEFAULT_KEY


def _utc_now() -> str:
    """The same clock, in the same shape, as the rows this is compared with."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def credits_note(service: str = None):
    """What the owner has noted is on an account, and when it was noted.

    OpenAI publish no balance at any endpoint -- their billing door answers to
    a browser session only, and every other path 404s. The number is on the
    dashboard and nowhere else. So it is typed once, dated, and the room does
    the only honest thing left: subtracts what has been spent since, and says
    where each half came from. A figure with a date on it and a subtraction
    under it is worth having; a figure with neither is not."""
    try:
        got = json.loads(CHOICE_PATH.read_text(encoding="utf-8")) or {}
        notes = got.get("credits") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        notes = {}
    if not isinstance(notes, dict):
        notes = {}
    return notes.get(service) if service else notes


def set_credits(service: str, usd, at=None) -> dict:
    """Write down what an account had, and when. `usd` of None forgets it."""
    if service not in SERVICES:
        raise Refused("there is no service called " + str(service)[:40] + " here")
    notes = dict(credits_note() or {})
    if usd is None or str(usd).strip() == "":
        notes.pop(service, None)
    else:
        try:
            amount = float(str(usd).replace("$", "").replace(",", "").strip())
        except ValueError:
            raise Refused('"' + str(usd)[:24] + '" is not an amount of money')
        if amount < 0:
            raise Refused("a balance is not a negative number")
        # Stamped on the store's own clock, which is UTC with an offset on
        # it. `spent_since` compares this against a row's `dt` as text, and
        # text comparison between a local stamp and a UTC one is wrong by
        # exactly the offset, and silently.
        notes[service] = {"usd": round(amount, 4), "at": at or _utc_now()}
    _save_choice(credits=notes)
    _forget_cached()
    return notes


def spent_since(service: str, since_iso: str) -> dict:
    """What the assistant's OWN turns have cost through one service since a
    moment, off its own store. Not the account's spend: other programs (the
    voice among them) may spend the same OpenAI key and are not in here, which
    is why whatever uses this has to say so out loud."""
    from datetime import datetime, timezone

    out = {"usd": 0.0, "turns": 0, "read": False}

    def when(text):
        """An ISO stamp as a moment, zone or no zone. A stamp with none is
        read as UTC, because that is the only clock anything here writes."""
        try:
            got = datetime.fromisoformat(str(text))
        except (TypeError, ValueError):
            return None
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)

    since = when(since_iso)
    if since is None:
        return out
    try:
        from . import db
        conn = db.connect()
    except Exception:
        return out
    try:
        rows = conn.execute(
            "SELECT dt, meta FROM rows WHERE by_model LIKE ? AND meta IS NOT"
            " NULL", (service + "/%",))
        for r in rows:
            at = when(r["dt"])
            if at is None or at < since:
                continue
            try:
                m = json.loads(r["meta"])
            except (json.JSONDecodeError, TypeError):
                continue
            got = m.get("turn_cost_usd")
            if got is None:
                got = m.get("cost_usd")
            if got is None:
                continue
            out["usd"] += got
            out["turns"] += 1
        out["read"] = True
    except Exception:
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out["usd"] = round(out["usd"], 6)
    return out


def choose_dream(key) -> dict:
    """Set the night's mind, or clear it back to the default. Same refusal as
    the chat's: a service with no key is not offered."""
    return _write_choice("dream_model", key, allow_none=True)


def _write_choice(field: str, key, allow_none: bool = False) -> dict:
    if paused_reason(key or DEFAULT_KEY):
        raise Refused(CLAUDE_PAUSED)
    if allow_none and not (key or "").strip():
        _save_choice(**{field: None})
        return {field: None, "label": resolve(DEFAULT_KEY)["label"],
                "default": True}
    spec = resolve(key)
    service = spec["service"]
    if SERVICES.get(service, {}).get("needs_key") and not key_of(service):
        raise Refused(
            SERVICES[service]["label"] + " has no key set, so that model "
            "cannot be reached. Paste one into the keys box above first.")
    _save_choice(**{field: spec["key"]})
    return {field: spec["key"], "label": spec["label"], "service": service}


def _save_choice(**fields):
    with _MODE_LOCK:
        _write_choice_file(**fields)


def _write_choice_file(**fields):
    """Rewrite the choice file, keeping whatever else is in it. Two settings
    live here now and a whole-file write that forgot the other one would set
    the night's mind by changing the chat's."""
    try:
        cur = json.loads(CHOICE_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    cur.update(fields)
    cur["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    CHOICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHOICE_PATH.with_suffix(".json.writing")
    tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    os.replace(tmp, CHOICE_PATH)


def choose(key: str) -> dict:
    """Pick the chat model. Refuses a service with no key rather than letting the
    next turn fail with somebody else's error message."""
    return _write_choice("model", key)


# -- the live price list ------------------------------------------------------

_LOCK = threading.Lock()
_CACHE = {"prices": {"at": 0.0, "by_id": None, "error": None, "asking": False}}


def _fetch(url, key=None, headers=None, body=None, timeout=NET_TIMEOUT):
    head = dict(headers or {})
    if key:
        head["Authorization"] = "Bearer " + key
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        head["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=head, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _why(exc) -> str:
    """An exception said as a sentence a person can act on."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            msg = (body.get("error") or {})
            msg = msg.get("message") if isinstance(msg, dict) else msg
        except Exception:
            msg = None
        if exc.code in (401, 403):
            return "the key was refused (" + str(exc.code) + ")" + \
                (": " + str(msg)[:200] if msg else "")
        return "answered " + str(exc.code) + (": " + str(msg)[:200] if msg else "")
    if isinstance(exc, urllib.error.URLError):
        return "could not be reached: " + str(exc.reason)[:120]
    return type(exc).__name__ + ": " + str(exc)[:160]


def _refresh_prices():
    """OpenRouter's whole catalogue, in the background. It is the only price
    list any of this can read, so it is fetched even when the chosen model is
    on another road -- the Settings page prices every model at once."""
    key = key_of("openrouter")
    try:
        got = _fetch(OPENROUTER_BASE + "/models", key=key or None)
        by_id = {}
        for m in got.get("data") or []:
            p = m.get("pricing") or {}

            def num(name):
                try:
                    v = float(p.get(name))
                except (TypeError, ValueError):
                    return None
                return v if v > 0 else 0.0

            by_id[m.get("id")] = {
                "in_per_m": (num("prompt") or 0) * 1e6,
                "out_per_m": (num("completion") or 0) * 1e6,
                # A prompt that repeats itself is most of every turn, so
                # what a *cached* token costs is not a detail here -- it is
                # the bill. Fable reads a cached token at a fortieth of a
                # fresh one; Sol at a tenth. Charging the prompt at the
                # fresh rate every turn would have read high by most of it
                # the moment the cache warmed up.
                "cache_read_per_m": (num("input_cache_read") or 0) * 1e6,
                "cache_write_per_m": (num("input_cache_write") or 0) * 1e6,
                "context": m.get("context_length"),
                "name": m.get("name"),
            }
        with _LOCK:
            _CACHE["prices"].update({"at": time.time(), "by_id": by_id,
                                     "error": None})
    except Exception as exc:
        with _LOCK:
            # The old list is kept. A price from an hour ago beats no price,
            # and the page says when it was read.
            _CACHE["prices"].update({"at": time.time(), "error": _why(exc)})
    finally:
        with _LOCK:
            _CACHE["prices"]["asking"] = False


def prices(block: bool = False, wait: float = 0.0) -> dict:
    """The live price list, or the last one we got.

    Refreshed off the request that noticed it was stale, in a thread, so the
    page never waits on somebody else's server. `block` is for the command
    line, where there is no page to keep responsive.

    `wait` is for the one caller that genuinely cannot proceed without a
    figure: a turn about to write what it cost into the ledger. A missing
    price there is not a blank on a page that fills in a second later, it is
    a null in the spend history forever. So that caller waits a few seconds
    for a table rather than recording nothing."""
    with _LOCK:
        cur = dict(_CACHE["prices"])
        stale = time.time() - cur["at"] > PRICE_TTL
        if stale and not cur["asking"]:
            _CACHE["prices"]["asking"] = True
            go = True
        else:
            go = False
    if go:
        if block:
            _refresh_prices()
        else:
            threading.Thread(target=_refresh_prices, daemon=True).start()
    if wait:
        # Somebody else's fetch may already be in flight -- started by the
        # poll a moment ago. Waiting on it is the same answer as starting our
        # own, and one fewer call on their server.
        until = time.time() + wait
        while time.time() < until:
            with _LOCK:
                if _CACHE["prices"]["by_id"] or not _CACHE["prices"]["asking"]:
                    break
            time.sleep(0.1)
    with _LOCK:
        cur = dict(_CACHE["prices"])
    return {"by_id": cur["by_id"] or {}, "read_at": cur["at"] or None,
            "error": cur["error"]}


def price_of(spec: dict, table=None) -> dict:
    """What one model costs per million tokens, and where the figure came from.

    The `borrowed` flag is the honest part: OpenAI publish no prices, so their
    two models are priced off OpenRouter's entry for the same model. Shown as
    a price, labelled as a borrowing."""
    if spec["service"] == "codex":
        return {"known": False, "borrowed": False, "source": "subscription"}
    table = table if table is not None else prices()["by_id"]
    pk = spec.get("price_key")
    got = table.get(pk) if pk else None
    if not got:
        return {"known": False, "borrowed": False, "source": None}
    return {
        "known": True,
        "in_per_m": round(got["in_per_m"], 4),
        "out_per_m": round(got["out_per_m"], 4),
        # Zero here means "published as free"; missing means "not published".
        # Both are honest and they are not the same, so a service that names
        # no cached rate falls back to the fresh one rather than to free.
        "cache_read_per_m": round(got.get("cache_read_per_m") or 0, 4),
        "cache_write_per_m": round(got.get("cache_write_per_m") or 0, 4),
        "context": got.get("context"),
        # Every price here comes off OpenRouter. For a router model that is
        # the price actually charged; for anything else it is the going rate
        # for the same model somewhere else, which is a different claim.
        "borrowed": spec["service"] != "openrouter",
        "source": "openrouter",
    }


def cost_of(spec: dict, in_tokens: int, out_tokens: int, table=None,
            cached: int = 0, cache_write: int = 0):
    """What a call came to, in dollars, from the live table.

    `in_tokens` is the WHOLE prompt, the way every service reports it, and
    `cached` is the part of it that was served from cache. The two are not
    charged alike -- a cached token costs a tenth of a fresh one on Sol and a
    fortieth on Fable -- so the cached part is taken out of the fresh count
    and billed at its own rate. Getting this wrong does not look like a bug:
    it looks like the assistant being expensive.

    None when there is no price to reckon with. A made-up cost would go into
    the spend ledger and be indistinguishable from a measured one forever."""
    p = price_of(spec, table)
    if not p["known"]:
        return None
    whole = in_tokens or 0
    hit = min(cached or 0, whole)
    # No published cached rate means we do not know it is cheaper, so it is
    # charged as ordinary input -- which reads high rather than low, and is
    # the right way round for a number somebody budgets against.
    hit_rate = p.get("cache_read_per_m") or p["in_per_m"]
    return round((whole - hit) / 1e6 * p["in_per_m"]
                 + hit / 1e6 * hit_rate
                 + (cache_write or 0) / 1e6 * (p.get("cache_write_per_m") or 0)
                 + (out_tokens or 0) / 1e6 * p["out_per_m"], 6)


# -- what is left -------------------------------------------------------------

def _forget_cached(service=None):
    with _LOCK:
        for name in list(_CACHE):
            if name != "prices":
                _CACHE.pop(name)
        if service == "openrouter":
            _CACHE["prices"]["at"] = 0.0
    # The spend counter keeps its own cache, so clearing ours is not enough:
    # a gauge still drawn off the key that was just replaced looks exactly
    # like a paste that did not take.
    if service in (None, "openai", "openai_admin_key"):
        try:
            from . import openai_spend
            openai_spend.forget()
        except Exception:
            pass


def _cached(name, ttl, make, block=True):
    """A value kept for a while, made again when it goes stale.

    `block=False` hands back what is already there and makes the new one in a
    thread. That is what the two-second poll needs: reading a balance means a
    call to somebody else's server, and the header must never wait on one --
    a service having a slow morning would otherwise be a room that stutters.
    Nothing is drawn wrong in the meantime; a balance a minute old is still a
    balance, and the first poll after a boot simply has none yet."""
    with _LOCK:
        got = _CACHE.get(name)
        fresh = got and time.time() - got["at"] < ttl
        if fresh:
            return got["value"]
        if not block:
            if _CACHE.get(name + ":asking"):
                return got["value"] if got else None
            _CACHE[name + ":asking"] = True

    def run():
        try:
            value = make()
        except Exception:
            value = None
        with _LOCK:
            if value is not None:
                _CACHE[name] = {"at": time.time(), "value": value}
            else:
                # Keep the old reading rather than blanking the gauge, but let
                # the next poll try again rather than sitting on a failure.
                _CACHE[name] = {"at": time.time(),
                                "value": (got or {}).get("value")}
            _CACHE.pop(name + ":asking", None)

    if not block:
        threading.Thread(target=run, daemon=True).start()
        return got["value"] if got else None
    value = make()
    with _LOCK:
        _CACHE[name] = {"at": time.time(), "value": value}
    return value


def _openrouter_money():
    key = key_of("openrouter")
    if not key:
        return {"have": False, "why": "no key set"}
    try:
        k = (_fetch(OPENROUTER_BASE + "/key", key=key) or {}).get("data") or {}
        c = (_fetch(OPENROUTER_BASE + "/credits", key=key) or {}).get("data") or {}
    except Exception as exc:
        return {"have": False, "why": _why(exc)}
    total = c.get("total_credits")
    used = c.get("total_usage")
    left = None
    if isinstance(total, (int, float)) and isinstance(used, (int, float)):
        left = round(total - used, 4)
    return {
        "have": True,
        # Read off their own door, like OpenAI's spend is. Both gauges on the
        # page are measurements; only the noted-balance fallback is not.
        "real": True,
        "balance_usd": left,
        "balance_from": "OpenRouter, live",
        "bought_usd": total,
        "spent_ever_usd": round(used, 4) if isinstance(used, (int, float)) else None,
        "today_usd": k.get("usage_daily"),
        "week_usd": k.get("usage_weekly"),
        "month_usd": k.get("usage_monthly"),
        # A key with a spend limit on it has a second, tighter ceiling than
        # the credit balance. Usually null, and worth drawing when it is not.
        "limit_usd": k.get("limit"),
        "limit_left_usd": k.get("limit_remaining"),
    }


def _openai_money():
    """What OpenAI will tell us, which with an admin key is the thing that
    matters: what has actually been spent, against the actual ceiling.

    `openai_spend.py` reads both off their own endpoints with the admin key.
    That figure is a measurement and is marked `real`. Everything below it is
    the old reckoning -- a balance the owner typed off the dashboard, less
    what the assistant's own turns were priced at -- kept only for the
    minutes when the real read is not there, and marked `estimate` wherever
    it is drawn. The two must never be able to look alike on the page: one of
    them can see other programs spending the same key, and the other cannot.

    There is still no credit balance at any endpoint -- their billing door
    answers to a browser session only. That is a separate number and it stays
    missing."""
    from . import openai_spend

    if not key_of("openai"):
        return {"have": False, "why": "no key set"}

    out = {
        "have": False,
        "real": False,
        "balance_usd": None,
        "balance_why": "OpenAI's credit balance is not readable by any API "
                       "key -- their billing endpoints answer to a browser "
                       "session only. The spend and the ceiling are read live; "
                       "the balance is not one of them.",
    }

    # The reckoning first, so it stands in the minutes before the real read
    # lands -- and is plainly labelled an estimate whether it is drawn or not.
    note = credits_note("openai")
    if note:
        mine = spent_since("openai", note.get("at") or "")
        out.update({
            "entered_usd": note.get("usd"),
            "entered_at": note.get("at"),
            "spent_since_usd": round(mine["usd"], 4),
            "spent_since_turns": mine["turns"],
            "estimate_balance_usd": round((note.get("usd") or 0) - mine["usd"], 4),
            "estimate_from": home.NAME + "'s own turns, priced off the "
                             "table -- an estimate, and blind to anything "
                             "else on this key",
        })

    got = openai_spend.now()
    if not got:
        out["why"] = openai_spend.status()["error"] or "nothing read yet"
        return out

    out.update({
        "have": True,
        "real": True,
        "spent_usd": got["spent_usd"],
        "limit_usd": got["limit_usd"],
        "used_fraction": got["used_fraction"],
        "today_usd": got["today_usd"],
        "month_usd": got["spent_usd"] if got["interval"] == "month" else None,
        "interval": got["interval"],
        "enforced": got["enforced"],
        "since": got["since"],
        # When the counter goes back to zero: a figure with no horizon on it
        # is a score, not a budget.
        "resets_at": got.get("resets_at"),
        "resets_in_days": got.get("resets_in_days"),
        "spend_from": "OpenAI, live",
        "why": None,
    })
    return out


def money(block: bool = True) -> dict:
    """Balance and spend, per service, cached so the poll is cheap.

    `block=False` is the poll's version: whatever was last read, with a fresh
    read started behind it. `None` for a service simply means nothing has come
    back yet, which the header draws as nothing rather than as zero."""
    return {
        "openrouter": _cached("m:openrouter", BALANCE_TTL, _openrouter_money,
                              block),
        "openai": _cached("m:openai", BALANCE_TTL, _openai_money, block),
        # The plan's own windows are the gauge on this road, and `limits.py`
        # already reads them into the header. Nothing to add here.
        "claude_code": {"have": False, "why": "the plan's windows are the "
                        "gauge on this road -- they are in the header"},
        "codex": {"have": False, "why": "ChatGPT subscription; usage limits "
                  "are available in Codex. No API dollar balance applies."},
    }


# -- the catalogue, as the page draws it --------------------------------------

def catalogue(wait: float = 0.0) -> dict:
    """Everything the Settings page needs in one object: the services, which
    keys are set, every model with its live price, and what is left to spend.

    `wait` is for the very first read after a boot, when the price table is
    empty and the fetch that fills it has only just been started. Without it
    the page draws fourteen models all saying "no price read" and stays that
    way until somebody presses the button -- which is a page that looks
    broken and is only early. Waiting a couple of seconds once is the whole
    fix; every later read finds the table warm and returns at once."""
    table = prices(wait=wait)
    out_models = []
    for m in MODELS:
        if paused_reason(m):
            continue
        service = m["service"]
        ready = (not SERVICES[service]["needs_key"]) or bool(key_of(service))
        out_models.append({
            "key": m["key"], "service": service, "id": m["id"],
            "label": m["label"], "about": m["about"],
            "price": price_of(m, table["by_id"]),
            "ready": ready,
        })
    return {
        "services": {k: dict(v, has_key=bool(key_of(k)) or not v["needs_key"])
                     for k, v in SERVICES.items()
                     if not (codex_only() and k == "claude_code")},
        "keys": {k: v for k, v in keys_present().items()
                 if not (codex_only() and k == "anthropic")},
        "codex_only": codex_only(),
        "native_tools": native_tools_enabled(),
        "native_tools_profile": "assistant-chat",
        "paused": paused_capabilities(),
        "chat_paused": paused_reason(chosen()),
        "dream_paused": paused_reason(dream_model()),
        "models": out_models,
        "chosen": chosen(),
        "credits": credits_note() or {},
        "dream_model": dream_model(),
        # Whether the night is on its own pin or has been moved on purpose.
        # The page draws the difference, because "the same as the chat" and
        # "deliberately set to the same thing" are not the same setting.
        "dream_pinned": dream_model() == DEFAULT_KEY,
        "default": DEFAULT_KEY,
        "prices_read_at": table["read_at"],
        "prices_error": table["error"],
        "money": money(),
    }


def now() -> dict:
    """The small version, for the header: which model is answering, what that
    costs, and how much is left on that road.

    Cheap enough to ride the two-second poll: the price table is the cached
    one and the balance is whatever was last read, with any refresh happening
    behind it. Nothing here waits on the network."""
    spec = resolve(chosen())
    out = {
        "model": spec["key"],
        "codex_only": codex_only(),
        "paused": paused_capabilities(),
        "label": spec["label"],
        "service": spec["service"],
        "service_label": SERVICES.get(spec["service"], {}).get("label")
                         or spec["service"],
        "price": price_of(spec),
        "left": None,
    }
    if paused_reason(spec):
        out.update(label="Chat paused", service_label="Codex-only mode",
                   price=None, limits=None, limits_error=CLAUDE_PAUSED)
        return out
    if spec["service"] == "codex":
        from . import codex_backend
        subscription = codex_backend.limits_now()
        out["limits"] = subscription["limits"]
        out["credits"] = subscription.get("credits")
        out["limits_error"] = subscription["error"]
    # On a paid road, the money IS the gauge -- the plan's windows measure
    # somebody else's currency. So it rides up to the header beside them,
    # with the same shape: a figure, and a bar that fills as it goes.
    #
    # `reads` says which way round the figure runs, because the two roads
    # count opposite things and a column that silently means "left" on one
    # service and "spent" on the other is a gauge nobody can trust. OpenAI
    # counts UP to a ceiling; OpenRouter counts DOWN from a balance.
    # `real` says whether it was measured or reckoned, and the page must
    # never draw a reckoning without saying so.
    if spec["service"] not in ("claude_code", "codex"):
        m = (money(block=False) or {}).get(spec["service"]) or {}
        if m.get("real") and m.get("spent_usd") is not None:
            out["left"] = {
                "reads": "spent",
                "real": True,
                "spent_usd": m["spent_usd"],
                "of_usd": m.get("limit_usd"),
                "used_fraction": m.get("used_fraction"),
                "today_usd": m.get("today_usd"),
                "interval": m.get("interval"),
                "enforced": m.get("enforced"),
                "resets_at": m.get("resets_at"),
                "resets_in_days": m.get("resets_in_days"),
                "from": m.get("spend_from"),
            }
        elif m.get("balance_usd") is not None:
            started = m.get("bought_usd")
            spent = None
            if started:
                spent = max(0.0, min(1.0, 1.0 - (m["balance_usd"] / started)))
            out["left"] = {
                "reads": "left",
                "real": True,
                "balance_usd": m["balance_usd"],
                "of_usd": started,
                "used_fraction": spent,
                "from": m.get("balance_from"),
                "today_usd": m.get("today_usd"),
            }
        elif m.get("estimate_balance_usd") is not None:
            # The old reckoning, and only when there is nothing measured. It
            # goes up labelled, never as though it were the real counter.
            started = m.get("entered_usd")
            spent = None
            if started:
                spent = max(0.0, min(
                    1.0, 1.0 - (m["estimate_balance_usd"] / started)))
            out["left"] = {
                "reads": "left",
                "real": False,
                "balance_usd": m["estimate_balance_usd"],
                "of_usd": started,
                "used_fraction": spent,
                "from": m.get("estimate_from"),
                "why": m.get("why"),
            }
        elif m.get("why"):
            # Nothing to draw, and the reason is the whole point: a blank
            # gauge with no sentence beside it is what wastes the afternoon.
            out["left"] = {"reads": "none", "real": False, "why": m["why"]}
    return out


# -- the schema, as somebody else's door wants it -----------------------------

def strict_schema(schema: dict) -> dict:
    """The response schema, rewritten for OpenAI's strict structured output.

    Their rule is that every object bans extra properties and lists *all* of
    its properties as required -- so a field that is genuinely optional has to
    be spelled as one that is always present and may be null. That is exactly
    what the schema means anyway; it just says it the other way round. So the
    rewrite is mechanical: seal every object, require everything, and widen
    the type of anything that was not required to include null.

    Nothing is added or removed. The same answers validate."""
    def walk(node):
        if not isinstance(node, dict):
            return node
        out = dict(node)
        if isinstance(out.get("properties"), dict):
            props = {k: walk(v) for k, v in out["properties"].items()}
            was = set(out.get("required") or [])
            for name, sub in props.items():
                if name in was:
                    continue
                # Optional becomes always-present-but-nullable, which is the
                # only shape strict mode has for "may be absent".
                t = sub.get("type")
                if isinstance(t, str) and t != "null":
                    sub["type"] = [t, "null"]
                elif isinstance(t, list) and "null" not in t:
                    sub["type"] = list(t) + ["null"]
            out["properties"] = props
            out["required"] = list(props)
            out["additionalProperties"] = False
        if isinstance(out.get("items"), dict):
            out["items"] = walk(out["items"])
        return out

    return walk(json.loads(json.dumps(schema)))


def denull(payload: dict, schema: dict) -> dict:
    """Undo the widening above, on the way back.

    A null that arrives where the original schema does not allow one means "I did
    not answer this", and the room downstream expects such a field to be
    missing or empty -- not present and None. `setdefault` cannot help,
    because the key *is* present.

    Whether the field was `required` is deliberately not the question. The
    schema requires almost everything, and a strict door should never send a
    null for an array at all -- but OpenRouter fronts a great many houses and
    not all of them honour strict as written. A `null` where the room is about
    to write `for x in answer["essences"]` is a crash, and being ready for it
    costs one branch."""
    props = (schema or {}).get("properties") or {}
    out = dict(payload)
    for name, sub in props.items():
        if out.get(name) is not None:
            continue
        t = sub.get("type")
        types = [t] if isinstance(t, str) else list(t or [])
        if "null" in types:
            continue            # genuinely nullable in the original schema
        if "array" in types:
            out[name] = []      # the room iterates these unguarded
        elif name in out:
            out.pop(name)       # let the room's own default take over
    return out


# -- calling somebody else's door ---------------------------------------------

class TurnBroke(RuntimeError):
    """Raised the same shape `brain.TurnBroke` is, and re-raised as one by the
    caller -- what arrived is carried on it so a half-written answer is still
    kept."""

    def __init__(self, message, raw="", reply=""):
        super().__init__(message)
        self.raw, self.reply = raw, reply


def _images_for_openai(images):
    """Picture blocks, which are Anthropic's shape, in OpenAI's.

    One conversion, in one place. The bytes are the same bytes; only the
    envelope differs, and the order the prompt names them in is kept exactly
    -- a picture that moves is a picture numbered wrong in the prompt."""
    out = []
    for b in images or []:
        src = (b or {}).get("source") or {}
        if src.get("type") != "base64":
            continue
        out.append({"type": "image_url", "image_url": {
            "url": "data:" + (src.get("media_type") or "image/png")
                   + ";base64," + src.get("data", "")}})
    return out


def call(spec: dict, system: str, prompt_json: str, schema: dict,
         on_step=None, on_write=None, expect: str = "reply",
         images=None) -> dict:
    try:
        with model_activity(spec):
            return _call(spec, system, prompt_json, schema, on_step, on_write, expect, images)
    except Refused as exc:
        raise TurnBroke(str(exc)) from exc


def _call(spec: dict, system: str, prompt_json: str, schema: dict,
          on_step=None, on_write=None, expect: str = "reply", images=None) -> dict:
    """One turn through an OpenAI-shaped door, read as it arrives.

    The answer that comes back is the same object `brain.call_claude` returns
    from the plan road: the schema's fields, plus `_meta` with what it cost. The room
    above this does not know or care which door it came through -- which is
    the whole point, and the reason the stamping code has one place to look.

    Streamed, because a turn is thirty seconds and a spinner for thirty
    seconds is what the step lines exist to replace."""
    say = on_step or (lambda *a, **k: None)
    wrote = on_write or (lambda *a, **k: None)
    service = spec["service"]
    if service == "codex":
        from . import codex_backend
        return codex_backend.call(spec, system, prompt_json, schema,
                                  on_step=on_step, on_write=on_write,
                                  expect=expect, images=images)
    key = key_of(service)
    if not key:
        raise TurnBroke(
            SERVICES.get(service, {}).get("label", service) + " has no key "
            "set, so nothing was sent. Settings has the box for it.")

    base = OPENROUTER_BASE if service == "openrouter" else OPENAI_BASE
    content = [{"type": "text", "text": prompt_json}] + _images_for_openai(images)
    body = {
        "model": spec["id"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
        "stream": True,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "assistant_turn", "strict": True,
            "schema": strict_schema(schema)}},
    }
    if service == "openrouter":
        # The router will tell us what the call actually cost in credits,
        # which beats reckoning it off a price table -- but only if asked.
        body["usage"] = {"include": True}
    else:
        body["stream_options"] = {"include_usage": True}

    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + key}
    if service == "openrouter":
        # The router asks callers to name themselves. This is a room, not an
        # app in their directory, so the title alone names it.
        headers["X-Title"] = home.APP_NAME

    # The "asking ..." line belongs to the caller, which knows which round
    # this is; saying it here too printed it twice.
    req = urllib.request.Request(
        base + "/chat/completions", headers=headers,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"))

    raw, usage, cost, finish = "", {}, None, None
    started, last, said_writing = time.time(), 0.0, False
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as r:
            for line in r:
                if time.time() - started > CALL_TIMEOUT:
                    raise TimeoutError("over " + str(CALL_TIMEOUT) + " seconds")
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    ev = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if ev.get("usage"):
                    usage = ev["usage"]
                    if ev["usage"].get("cost") is not None:
                        cost = ev["usage"]["cost"]
                for ch in ev.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content") or ""
                    if piece:
                        if not said_writing:
                            said_writing = True
                            say(home.NAME + " is writing the answer")
                        raw += piece
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                now = time.time()
                if now - last >= 0.25:
                    last = now
                    wrote({"mode": "writing", "thought_chars": 0,
                           "answer_chars": len(raw), "reply": _peek(raw)})
    except urllib.error.HTTPError as exc:
        raise TurnBroke("the call was refused: " + _why(exc), raw=raw,
                        reply=_peek(raw)) from exc
    except Exception as exc:
        raise TurnBroke("the call did not come back: " + _why(exc), raw=raw,
                        reply=_peek(raw)) from exc

    wrote({"mode": "writing", "thought_chars": 0, "answer_chars": len(raw),
           "reply": _peek(raw)})
    if finish == "length":
        # Cut off mid-sentence. Said as what it is: the JSON will not parse
        # and the reason is not a bug in the parsing.
        raise TurnBroke(
            "the answer was cut off at the model's output limit, so it is not "
            "whole JSON. What arrived is kept.", raw=raw, reply=_peek(raw))
    if not raw.strip():
        raise TurnBroke("nothing came back at all -- no words, no error.",
                        raw=raw, reply="")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TurnBroke("what came back would not parse as JSON: "
                        + str(exc)[:200], raw=raw, reply=_peek(raw)) from exc
    if not isinstance(payload, dict) or expect not in payload:
        raise TurnBroke("what came back is not the shape asked for -- no `"
                        + expect + "` in it.", raw=raw, reply=_peek(raw))

    payload = denull(payload, schema)
    say("the answer came back whole")

    got_in = usage.get("prompt_tokens") or 0
    got_out = usage.get("completion_tokens") or 0
    cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
    thinking = ((usage.get("completion_tokens_details") or {})
                .get("reasoning_tokens")) or 0
    # The one place that waits on the price list. What a turn cost is written
    # into the store and read back forever; a null there because the table
    # happened to be cold on the first call after a boot is a hole nothing
    # later can fill.
    table = prices(wait=5.0)["by_id"]
    priced = price_of(spec, table)
    if cost is None:
        # The router hands back what it actually charged, cache and all, so
        # this branch is only for OpenAI's own door -- where the cached share
        # has to be taken off by hand.
        cost = cost_of(spec, got_in, got_out, table, cached=cached)
    payload["_meta"] = {
        "model": spec["id"],
        "model_key": spec["key"],
        "service": service,
        "context_max_tokens": priced.get("context"),
        "input_tokens": got_in or None,
        # Their caching is automatic and reported, not requested. Read tokens
        # are part of the prompt total, like the plan road counts them.
        "cache_read_tokens": cached if got_in else None,
        "cache_write_tokens": 0 if got_in else None,
        "fresh_input_tokens": (got_in - cached) if got_in else None,
        "output_tokens": got_out or None,
        "thinking_tokens": thinking or None,
        "cost_usd": cost,
        "duration_ms": int((time.time() - started) * 1000),
        # No plan windows on a paid road. Empty rather than absent, so the
        # header draws "no gauge here" instead of the last road's numbers.
        "limits": [],
    }
    return payload


def _peek(raw: str) -> str:
    """The reply as it forms, pulled out of half-written JSON. The plan road
    has its own copy of this shape; this one only has to survive a string
    that stops mid-escape."""
    m = re.search(r'"reply"\s*:\s*"', raw)
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
        if not nxt:
            break
        if nxt == "u":
            hexes = raw[i + 2:i + 6]
            if len(hexes) < 4:
                break
            try:
                out.append(chr(int(hexes, 16)))
            except ValueError:
                pass
            i += 6
            continue
        out.append({"n": "\n", "t": "\t", "r": "", "b": "", "f": ""}
                   .get(nxt, nxt))
        i += 2
    return "".join(out)


# -- from the command line ----------------------------------------------------

def _main(argv):
    what = argv[1] if len(argv) > 1 else "status"
    if what == "models":
        table = prices(block=True)["by_id"]
        for m in MODELS:
            p = price_of(m, table)
            money_line = ("$%.2f in / $%.2f out per M" %
                          (p["in_per_m"], p["out_per_m"])) if p["known"] \
                else "no price read"
            print("  %-34s %-13s %s%s" % (
                m["key"], m["label"], money_line,
                "  (borrowed)" if p.get("borrowed") else ""))
        return 0
    print(home.NAME, "is", resolve(chosen())["label"], "--", chosen())
    print()
    print("keys:")
    for slot, info in keys_present().items():
        print("  %-18s %s" % (slot, info["tail"] or "not set"))
    print()
    print("money:")
    for name, m in money().items():
        if not m.get("have"):
            print("  %-12s %s" % (name, m.get("why") or "nothing to read"))
            continue
        bits = []
        if m.get("spent_usd") is not None:
            bits.append("$%.4f spent%s" % (
                m["spent_usd"],
                " of $%.2f" % m["limit_usd"] if m.get("limit_usd") else ""))
        if m.get("balance_usd") is not None:
            bits.append("$%.4f left" % m["balance_usd"])
        for label, k in (("today", "today_usd"), ("month", "month_usd")):
            if m.get(k) is not None:
                bits.append("%s $%.4f" % (label, m[k]))
        if not m.get("real"):
            bits.append("(estimate)")
        print("  %-12s %s" % (name, "   ".join(bits) or "nothing to read"))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
