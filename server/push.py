"""The line to the owner's phone. One short text, when the room notices something.

This is the other half of the watcher's loop: the assistant notices, the owner
hears. It sends one line to wherever `data/push.json` points -- an ntfy topic
or a Telegram bot, configured by the owner and off until they do. The file can
hold a token, so `files.py` refuses it to the assistant and its hands by name,
the same as the angel key: the loop belongs to the assistant, the wire to the
owner.

    {"kind": "ntfy", "url": "https://ntfy.sh/your-topic", "title": "Assistant"}
    {"kind": "telegram", "token": "12345:abc...", "chat_id": "6789..."}

Failure never raises and never blocks a waking -- a phone that cannot be
reached is written down in the ledger and the waking goes ahead. The push is
the echo of the event, not the event.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from . import home

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = home.DATA / "push.json"
TIMEOUT = 6


def _config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cfg if isinstance(cfg, dict) else None


def status() -> str:
    """One word for the prompt: what the wire is, never what is on it."""
    cfg = _config()
    if not cfg:
        return "off"
    kind = str(cfg.get("kind") or "")
    if kind == "ntfy" and cfg.get("url"):
        return "ntfy"
    if kind == "telegram" and cfg.get("token") and cfg.get("chat_id"):
        return "telegram"
    return "off (badly configured)"


def send(text: str):
    """One line out. Returns None when it went, else one plain reason.

    No retries: a waking must not stand in a retry loop, and the ledger
    keeps the miss. The next waking is the next chance."""
    cfg = _config()
    if not cfg:
        return "no push configured"
    text = (text or "").strip()
    if not text:
        return "nothing to say"
    kind = str(cfg.get("kind") or "")
    try:
        if kind == "ntfy" and cfg.get("url"):
            req = urllib.request.Request(
                str(cfg["url"]),
                data=text.encode("utf-8"),
                headers={"Title": str(cfg.get("title") or home.NAME),
                         "User-Agent": "digital-ai-assistant"},
                method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT):
                return None
        if kind == "telegram" and cfg.get("token") and cfg.get("chat_id"):
            req = urllib.request.Request(
                "https://api.telegram.org/bot" + str(cfg["token"])
                + "/sendMessage",
                data=urllib.parse.urlencode(
                    {"chat_id": str(cfg["chat_id"]), "text": text}
                ).encode("utf-8"),
                headers={"User-Agent": "digital-ai-assistant"},
                method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT):
                return None
        return "push.json names no wire we know"
    except urllib.error.HTTPError as exc:
        return "the wire answered " + str(exc.code)
    except Exception as exc:      # network, timeout -- the ledger's problem
        return type(exc).__name__ + ": " + str(exc)[:120]
