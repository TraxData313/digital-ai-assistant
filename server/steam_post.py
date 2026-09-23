"""The assistant's one hand on the Steam Workshop: posting a reply it has
written and the owner has said yes to, on one of the items the home's settings
list.

Why the session comes from the running client: a pasted cookie cannot hold --
a `steamLoginSecure` is a day-long JWT, so any copy of one put into a file is
stale by tomorrow. The owner's always-on Steam client keeps a live community
session as an ordinary part of being logged in, and with an empty
`.cef-enable-remote-debugging` file in its folder it opens a Chrome DevTools
port on 127.0.0.1:8080 and nowhere else. This module asks that port for the
session at the moment of the post, uses it once, and forgets it. Nothing is
stored, nothing expires, and there is no password anywhere in this room.

The conditions on this code, built in rather than remembered:

* One post per explicit call. No queue, no loop, no retry that re-posts. A
  failure fails and says so.
* The text arrives complete. Length and sign-off are CHECKED and a bad draft
  is REFUSED -- never truncated, never appended to. The text that was approved
  must be exactly the text posted, so this code is forbidden from shaping it
  on the way out.
* Only the items the home's settings list. Any other is refused before
  anything is opened.
* The read path (`watch.py`'s render URL) stays unauthenticated forever. This
  module never touches it, so a dead session can never blind the sense.
* A dead session is loud. A post never fails quietly.
* Nothing of the session appears in a log, an exception, a return value, or
  anything printed. It lives in a local for the length of one request.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
from . import home

# The owner's own account on that site, and the only items the assistant may
# ever speak on, both from the home's settings. Not a default to be overridden
# by a caller -- an allow-list, checked before the client is so much as opened.
_COMMENTS = home.settings("comments.json")
STEAMID64 = str(_COMMENTS.get("steam_id64") or "")
ITEMS = {str(i.get("id")): str(i.get("label"))
         for i in (_COMMENTS.get("steam_items") or ()) if i.get("id")}

# The sign-off is the whole reason a comment of the assistant's may go out at
# all: it marks the comment as written by the owner's assistant, never by the
# owner. Verbatim, and it must be the last thing in the comment.
# The home's settings hold it. No sign-off, no post: an empty one refuses
# every draft.
SIGN_OFF = str(_COMMENTS.get("sign_off") or "")

# The assistant's own ceiling. A draft that has to be cut was a draft that
# was showing off.
MAX_CHARS = 1000

DEBUG_PORT = 8080
POST_URL_TMPL = ("https://steamcommunity.com/comment/PublishedFile_Public"
                 "/post/" + STEAMID64 + "/{item_id}/")
ITEM_PAGE_TMPL = ("https://steamcommunity.com/"
                  "sharedfiles/filedetails/?id={item_id}")
TIMEOUT_S = 20


class Refused(Exception):
    """The draft was wrong and nothing was sent. This code's rule, not Steam's."""


class NotLoggedIn(Exception):
    """The client is not holding a community session. Loud on purpose: a post
    that cannot happen must never look like one that did."""


# --------------------------------------------------------------------------
# The smallest websocket that can carry CDP. This room has no dependencies and
# is not about to grow one for a handshake and two frame headers.
# --------------------------------------------------------------------------

class _Ws:

    def __init__(self, url, timeout=TIMEOUT_S):
        if not url.startswith("ws://127.0.0.1:"):
            raise NotLoggedIn("the debug port answered with a socket that is"
                              " not on this machine; refusing to open it")
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET /" + path + " HTTP/1.1\r\n"
            "Host: " + hostport + "\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: " + key + "\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise NotLoggedIn("the Steam client closed the debug socket"
                                  " during the handshake")
            head += chunk
        head, _, self.buf = head.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise NotLoggedIn("the debug port refused the upgrade -- is Steam"
                              " running with .cef-enable-remote-debugging?")
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if accept not in head:
            raise NotLoggedIn("the debug port gave a bad handshake accept")
        self._id = 0

    def _exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise NotLoggedIn("the Steam client closed the debug socket")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode, payload):
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < (1 << 16):
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _frame(self):
        b0, b1 = self._exact(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        if b1 & 0x80:                      # server frames are never masked
            self._exact(4)
        return b0 & 0x80, b0 & 0x0F, self._exact(n)

    def call(self, method, params=None):
        self._id += 1
        mine = self._id
        self._send(0x1, json.dumps(
            {"id": mine, "method": method, "params": params or {}}).encode())
        while True:
            data, kind = b"", None
            while True:
                fin, op, payload = self._frame()
                if op == 0x9:                       # ping -> pong
                    self._send(0xA, payload)
                    continue
                if op == 0x8:
                    raise NotLoggedIn("the Steam client closed the debug"
                                      " socket mid-question")
                if op in (0x1, 0x2):
                    kind = op
                data += payload
                if fin:
                    break
            if kind is None:
                continue
            got = json.loads(data.decode("utf-8"))
            if got.get("id") != mine:
                continue                            # an event, not my answer
            if "error" in got:
                raise NotLoggedIn("the Steam client refused " + method)
            return got.get("result") or {}

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _session():
    """The live community session, straight from the owner's running client.

    Returns the two cookie values and nothing else. The caller keeps them in a
    local for one request; they are never written down. Every failure here is
    `NotLoggedIn`, and no failure carries a value in its message.
    """
    try:
        version = json.load(urllib.request.urlopen(
            "http://127.0.0.1:" + str(DEBUG_PORT) + "/json/version",
            timeout=TIMEOUT_S))
    except Exception:
        raise NotLoggedIn(
            "nothing is answering the Steam debug port on 127.0.0.1:"
            + str(DEBUG_PORT) + " -- Steam is closed, or the"
            " .cef-enable-remote-debugging file is gone from its folder and it"
            " has been restarted since")
    ws = _Ws(version["webSocketDebuggerUrl"])
    try:
        jar = ws.call("Storage.getCookies").get("cookies") or []
    finally:
        ws.close()
    found = {}
    for c in jar:
        if not (c.get("domain") or "").endswith("steamcommunity.com"):
            continue
        if c.get("name") in ("steamLoginSecure", "sessionid"):
            found[c["name"]] = c.get("value") or ""
    missing = [n for n in ("steamLoginSecure", "sessionid")
               if not found.get(n)]
    if missing:
        raise NotLoggedIn(
            "the Steam client is running but is not holding a community"
            " session (missing: " + ", ".join(missing) + ") -- the account is"
            " signed out, or the Community tab has not been opened since"
            " signing in. Nothing was posted.")
    return found["steamLoginSecure"], found["sessionid"]


def check(item_id: str, text: str) -> None:
    """The refusals, in the order that costs least. Raises `Refused`
    with the reason, or returns and says nothing. Never alters `text` -- that
    is the whole point of it being a check and not a tidy-up."""
    if item_id not in ITEMS:
        raise Refused("item " + str(item_id) + " is not one of its own; it may"
                      " only speak on " + ", ".join(
                          i + " (" + n + ")" for i, n in sorted(ITEMS.items())))
    if not text or not text.strip():
        raise Refused("the draft is empty")
    if text != text.strip():
        raise Refused("the draft has whitespace at one end; send the exact"
                      " text, not a version this code would have to trim")
    if not SIGN_OFF or not text.endswith(SIGN_OFF):
        raise Refused("the draft does not end with the sign-off, " + SIGN_OFF
                      + " -- it is not appended here, it is written into the"
                      " draft or the draft does not go")
    if len(text) > MAX_CHARS:
        raise Refused("the draft is " + str(len(text)) + " characters, over"
                      " the ceiling of " + str(MAX_CHARS) + "; it is not cut"
                      " here, it is rewritten shorter")


def post(item_id: str, text: str) -> dict:
    """One comment, once. Checked first, sent second, and never retried.

    Returns what happened, with nothing of the session in it. Raises `Refused`
    if the draft breaks one of the rules above and `NotLoggedIn` if the
    client is not holding a session -- in both cases nothing was sent.
    """
    item_id = str(item_id)
    check(item_id, text)
    login, sessionid = _session()
    body = urllib.parse.urlencode({
        "comment": text,
        "count": "10",
        "sessionid": sessionid,
    }).encode()
    req = urllib.request.Request(
        POST_URL_TMPL.format(item_id=item_id), data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://steamcommunity.com",
            "Referer": ITEM_PAGE_TMPL.format(item_id=item_id),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Valve"
                          " Steam Client",
            "Cookie": ("steamLoginSecure=" + login
                       + "; sessionid=" + sessionid),
        })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            answer = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # `exc` knows the url and the status and nothing of the headers sent,
        # but it is never let out whole -- only these two facts are.
        raise NotLoggedIn("Steam answered " + str(exc.code) + " to the post on "
                          + ITEMS[item_id] + "; nothing was posted")
    except Exception as exc:
        raise NotLoggedIn("the post on " + ITEMS[item_id] + " did not reach"
                          " Steam (" + type(exc).__name__ + "); nothing was"
                          " posted")
    if not answer.get("success"):
        raise NotLoggedIn(
            "Steam refused the comment on " + ITEMS[item_id] + ": "
            + str(answer.get("error") or "no reason given")
            + "; nothing was posted")
    return {
        "posted": True,
        "item": item_id,
        "label": ITEMS[item_id],
        "chars": len(text),
        "page": ITEM_PAGE_TMPL.format(item_id=item_id),
    }


def main():
    """`python -m server.steam_post <item_id> <path-to-utf8-file>`

    The text comes from a file, never from the command line: a comment carries
    quotes, em dashes and newlines, and a shell is the wrong place to keep any
    of them intact. It is read exactly as written and sent exactly as read.
    """
    import sys
    if len(sys.argv) != 3:
        print(main.__doc__.strip().splitlines()[0])
        print("items: " + ", ".join(i + " (" + n + ")"
                                    for i, n in sorted(ITEMS.items())))
        raise SystemExit(2)
    item_id, path = sys.argv[1], sys.argv[2]
    with open(path, encoding="utf-8") as fh:
        text = fh.read().strip()
    try:
        done = post(item_id, text)
    except (Refused, NotLoggedIn) as exc:
        print(type(exc).__name__ + ": " + str(exc))
        raise SystemExit(1)
    print("posted " + str(done["chars"]) + " characters on "
          + done["label"] + " -- " + done["page"])


if __name__ == "__main__":
    main()
