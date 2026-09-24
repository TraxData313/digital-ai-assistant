"""Lean on the pocket door. No model, no live store, and never the live port:

    python -m server.test_pocket C:\\somewhere\\scratch

Two kinds of check, and the difference matters when reading the result.

The **end-to-end** ones run a real `OneRoom` on a scratch port and make real
HTTP requests, so the status codes below are the ones the handler actually
returned -- 200, 403, 302 -- through `do_GET` and `do_POST` as they are written,
not through a copy of the logic. What they cannot do is arrive from a real
100.x address: a bench cannot forge a source IP. So `Handler._peer` is replaced
for the length of the run by one that reports whatever address the test is
pretending to be. That is the one seam, and it is named here rather than buried:
everything downstream of `client_address` is under test, and the socket's own
reading of `client_address` is not.

The **key** ones touch `people` directly: minted once and kept, kept across what
a restart looks like from the key's side, and a name that cannot walk out of its
own folder.
"""

import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import app, db, files, limits, notes, people


FAILED = []
PRETEND = {"addr": "127.0.0.1"}


def check(name, ok, detail=""):
    mark = "ok  " if ok else "FAIL"
    print("  " + mark + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def _pretend_peer(self):
    """The one seam. See the docstring: a bench cannot come from 100.x, so the
    address the handler reads is the address the test is pretending to be."""
    import ipaddress
    try:
        return ipaddress.ip_address(PRETEND["addr"])
    except ValueError:
        return None


def ask(port, path, cookie=None, method="GET", body=None, follow=False):
    """One request, and what came back: (status, headers, body-text)."""
    url = "http://127.0.0.1:" + str(port) + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    if data:
        req.add_header("Content-Type", "application/json")

    class NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = (urllib.request.build_opener() if follow
              else urllib.request.build_opener(NoFollow))
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def main():
    if len(sys.argv) < 2:
        print("  needs a scratch folder: python -m server.test_pocket <path>")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    root = Path(__file__).resolve().parent.parent
    if root == scratch or root in scratch.parents:
        print("  that scratch is inside the repo. Give me one outside it.")
        sys.exit(2)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

    # Every key this bench makes lands in the scratch, never in data/people.
    people.TOKEN_DIR = scratch / "people"
    # And every line and every note: the door checks below post real lines
    # through /api/send, which write a row and then build a whole state, so
    # the store and the notes folder are the scratch's own. The two caches a
    # state build would otherwise fill by asking the CLI -- the plan's
    # limits, and whether a hand can be sent -- are pre-filled, so the bench
    # stays offline and quick.
    db.DB_PATH = scratch / "store.db"
    notes.NOTES_DIR = scratch / "notes"
    limits._CACHE.update({"at": time.time() + 3600, "limits": {}, "error": None,
                          "asking": False})

    # -- the keys themselves ---------------------------------------------
    first = people.mint("sam")
    check("a key is 64 hex characters", len(first) == 64, len(first))
    check("reading it back gives the same key",
          people.read_token("sam") == first)
    # What a restart looks like from the key's side: the process forgets
    # everything and mints again. Anything cached in memory is dropped first,
    # so this is the disk answering and not a leftover.
    again = people.mint("sam")
    check("minting again after a restart keeps the same key", again == first)
    check("and a third time still", people.mint("sam") == first)

    other = people.mint("someone-else")
    check("a second person gets a different key", other != first)
    check("the room knows whose key is whose",
          people.whose(first) == "sam" and people.whose(other) == "someone-else",
          (people.whose(first), people.whose(other)))
    check("a key nobody has belongs to nobody", people.whose("f" * 64) == "")
    check("an empty key belongs to nobody", people.whose("") == "")
    check("reminting changes it", people.remint("someone-else") != other)

    for bad in ("../sam", "an/ton", "", "SAM!", "a" * 40):
        try:
            people.mint(bad)
            check("a bad name is refused: " + repr(bad), False, "it was allowed")
        except people.BadName:
            check("a bad name is refused: " + repr(bad), True)
    people.mint("sam")  # left standing for the door checks below

    check("100.88.10.20 is inside the tailnet", people.in_tailnet("100.88.10.20"))
    check("100.64.0.0 is inside", people.in_tailnet("100.64.0.0"))
    check("100.127.255.255 is inside", people.in_tailnet("100.127.255.255"))
    check("100.128.0.1 is outside", not people.in_tailnet("100.128.0.1"))
    check("100.63.255.255 is outside", not people.in_tailnet("100.63.255.255"))
    check("192.168.100.45 is outside", not people.in_tailnet("192.168.100.45"))
    check("a v6 address is outside", not people.in_tailnet("fd7a:115c:a1e0::1"))
    check("nonsense is outside", not people.in_tailnet("not-an-address"))

    # -- the key is refused to the assistant -----------------------------
    check("its file tool is never given a .token",
          files.refused_for("sam.token") is not None)
    check("nor one under any other name",
          files.refused_for("LEE.TOKEN") is not None)
    check("an ordinary file is still its own to read",
          files.refused_for("notes.md") is None)

    # -- the door, end to end --------------------------------------------
    # Port 0: the operating system picks a free one and tells us which. The
    # live room is on 8787 and a bench must never be able to land on it, not even by being
    # run twice at once -- so the number is not ours to choose at all.
    app.Handler._peer = _pretend_peer
    room = app.OneRoom(("127.0.0.1", 0), app.Handler)
    port = room.server_address[1]
    print("  (the bench room is on " + str(port) + ", chosen by the OS)")
    threading.Thread(target=room.serve_forever, daemon=True).start()
    try:
        PRETEND["addr"] = "127.0.0.1"
        code, _, _ = ask(port, "/")
        check("loopback with no key gets the room", code == 200, code)
        code, _, _ = ask(port, "/style.css")
        check("loopback gets the static files too", code == 200, code)

        # The manifest is what makes it install to a home screen instead of
        # sitting there as a bookmark, and a phone will not read one that
        # arrives as a stream of bytes -- so the type is checked, not just the
        # status. The icons the manifest names are checked for the same
        # reason: a manifest pointing at a 404 installs a blank tile.
        code, head, text = ask(port, "/manifest.webmanifest")
        check("the manifest is served", code == 200, code)
        # The room puts "; charset=utf-8" on every type it sends, so the
        # check is on the front of the header, not on the whole of it.
        ctype = (head.get("Content-Type") or "").split(";")[0].strip()
        check("and as a manifest, not as bytes",
              ctype == "application/manifest+json", head.get("Content-Type"))
        try:
            icons = [i["src"] for i in json.loads(text)["icons"]]
        except Exception as e:
            icons = []
            check("the manifest is json with icons in it", False, e)
        check("it names three icons", len(icons) == 3, icons)
        for src in icons:
            code, head, _ = ask(port, src)
            ctype = (head.get("Content-Type") or "").split(";")[0].strip()
            check("the icon " + src + " is there, as a png",
                  code == 200 and ctype == "image/png",
                  str(code) + " " + str(head.get("Content-Type")))

        PRETEND["addr"] = "100.88.10.20"
        code, _, _ = ask(port, "/")
        check("tailnet with no key at all is shut out", code == 403, code)

        code, head, body = ask(port, "/?k=" + first)
        check("tailnet with a good key is let in", code == 302, code)
        check("and is sent to the bare address, key out of the bar",
              head.get("Location") == "/", head.get("Location"))
        cookie = head.get("Set-Cookie") or ""
        check("with the key kept in a cookie", first in cookie)
        check("the cookie is SameSite=Lax and long-lived",
              "SameSite=Lax" in cookie and "Max-Age=31536000" in cookie, cookie)
        check("and the redirect carries no body", body == "", repr(body))

        jar = "ada_who=" + first
        code, _, _ = ask(port, "/", cookie=jar)
        check("the bare address works from then on", code == 200, code)

        code, _, _ = ask(port, "/?k=" + "0" * 64)
        check("tailnet with a bad key is shut out", code == 403, code)
        code, _, body = ask(port, "/", cookie="ada_who=" + "0" * 64)
        check("a bad cookie is shut out too", code == 403, code)
        check("and the 403 says nothing at all", body == "", repr(body))

        PRETEND["addr"] = "192.168.100.45"
        code, _, _ = ask(port, "/", cookie=jar)
        check("outside the tailnet, a good key is still shut out",
              code == 403, code)
        PRETEND["addr"] = "8.8.8.8"
        code, _, _ = ask(port, "/", cookie=jar)
        check("and from the open internet, likewise", code == 403, code)

        # /api/send is the one that writes into its room, so it is checked by
        # name rather than trusted to the same gate as the rest.
        PRETEND["addr"] = "8.8.8.8"
        code, _, _ = ask(port, "/api/send", method="POST", body={"text": "hi"})
        check("nothing outside can post a line as him", code == 403, code)
        PRETEND["addr"] = "100.88.10.20"
        code, _, _ = ask(port, "/api/send", method="POST", body={"text": "hi"})
        check("nor a tailnet peer without a key", code == 403, code)

        # The pairing line, which carries a key: loopback only, always.
        PRETEND["addr"] = "100.88.10.20"
        code, _, _ = ask(port, "/api/pair", cookie=jar)
        check("a paired phone is not shown anybody's key", code == 403, code)
        PRETEND["addr"] = "127.0.0.1"
        code, _, body = ask(port, "/api/pair")
        got = json.loads(body) if code == 200 else {}
        check("this machine is shown how to pair one", code == 200, code)
        check("and the line it is shown carries the key",
              bool(got.get("url")) or bool(got.get("template")),
              "neither a url nor a template")

        # -- where a line came from ------------------------------------------
        # A line said at the desk keeps its old bytes: no label at all. One
        # said over the tailnet on the owner's key is labelled as the owner's
        # phone, and Leona's on that key is signed hers, through the owner's
        # door. Read off the scratch store, not off the answer, because the
        # row is what the assistant will be handed.
        def newest_meta():
            c = db.connect()
            try:
                row = c.execute("SELECT meta FROM rows WHERE kind = 'user' "
                                "ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                c.close()
            if row is None:
                return "no row at all"
            m = row["meta"]
            if isinstance(m, str):
                try:
                    m = json.loads(m)
                except json.JSONDecodeError:
                    pass
            return m

        PRETEND["addr"] = "127.0.0.1"
        code, _, _ = ask(port, "/api/send", method="POST",
                         body={"text": "hi from the desk", "who": "sam"})
        check("a line from the desk goes in", code == 200, code)
        check("and carries no label at all -- absence means Sam, at the desk",
              newest_meta() in (None, {}), newest_meta())
        ask(port, "/api/send", method="POST",
            body={"text": "hers, at the desk", "who": "lee"})
        m = newest_meta()
        check("Leona at the desk is signed, through the desk",
              isinstance(m, dict) and m.get("who") == "lee"
              and m.get("via") == "desktop", m)

        # A phone is a tailnet peer that is NOT this machine. The address
        # above could be this machine's own tailnet address, which the room
        # rightly calls the desk -- so the road uses another.
        mine = people.my_tailnet_address()
        phone = "100.101.102.103" if mine != "100.101.102.103" else "100.104.105.106"
        PRETEND["addr"] = phone
        code, _, _ = ask(port, "/api/send", cookie=jar, method="POST",
                         body={"text": "hi from the road", "who": "sam"})
        check("his line over the tailnet goes in on his key", code == 200, code)
        m = newest_meta()
        check("and is labelled as his phone",
              isinstance(m, dict) and m.get("who") == "sam"
              and m.get("via") == "sam's phone", m)
        ask(port, "/api/send", cookie=jar, method="POST",
            body={"text": "hers, on his phone", "who": "lee"})
        m = newest_meta()
        check("Leona on his phone is signed hers, through his door",
              isinstance(m, dict) and m.get("who") == "lee"
              and m.get("via") == "sam's phone", m)
        ask(port, "/api/send", cookie=jar, method="POST",
            body={"text": "unsigned, from the road"})
        m = newest_meta()
        check("an unsigned line over the tailnet is his, from his phone",
              isinstance(m, dict) and m.get("who") == "sam"
              and m.get("via") == "sam's phone", m)

        if mine:
            PRETEND["addr"] = mine
            code, _, _ = ask(port, "/api/send", cookie=jar, method="POST",
                             body={"text": "from this machine's own tailnet "
                                           "address", "who": "sam"})
            check("this machine's own tailnet address is the desk",
                  code == 200 and newest_meta() in (None, {}),
                  (code, newest_meta()))
        else:
            print("  (Tailscale is not up here, so the own-address check "
                  "is skipped)")

        # -- the notes door ---------------------------------------------------
        PRETEND["addr"] = "8.8.8.8"
        code, _, _ = ask(port, "/api/notes", method="POST",
                         body={"who": "sam", "text": "x"})
        check("nothing outside can write his notes", code == 403, code)
        PRETEND["addr"] = "100.88.10.20"
        code, _, _ = ask(port, "/api/notes", method="POST",
                         body={"who": "sam", "text": "x"})
        check("nor a tailnet peer without a key", code == 403, code)
        line = "the boiler service is on the 14th"
        code, _, body = ask(port, "/api/notes", cookie=jar, method="POST",
                            body={"who": "sam", "text": line})
        got = json.loads(body) if code == 200 else {}
        check("his phone can write his notes", code == 200, (code, body[:120]))
        check("and they land in the notes folder",
              (scratch / "notes" / "sam.md").is_file()
              and notes.read("sam") == line, notes.read("sam"))
        check("the answer is the notes as they stand",
              (got.get("sam") or {}).get("text") == line, got)
        code, _, body = ask(port, "/api/notes", cookie=jar, method="POST",
                            body={"who": "stranger", "text": "x"})
        check("a name outside the house is refused in words",
              code == 400 and "house" in body, (code, body[:120]))
        code, _, body = ask(port, "/api/notes", cookie=jar, method="POST",
                            body={"who": "sam",
                                  "text": "z" * (notes.MAX_CHARS + 1)})
        check("over the wall is refused in words",
              code == 400 and str(notes.MAX_CHARS) in body, (code, body[:120]))
        check("and his notes still stand", notes.read("sam") == line)
        PRETEND["addr"] = "127.0.0.1"
        code, _, body = ask(port, "/api/state")
        st = json.loads(body) if code == 200 else {}
        check("the state carries the notes for the box",
              ((st.get("notes") or {}).get("sam") or {}).get("text") == line,
              (code, sorted(st.keys())[:12]))
        check("and its contract mirrors the block it reads",
              (st.get("contract") or {}).get("notes")
              == "Sammy's free notes: " + line,
              (st.get("contract") or {}).get("notes"))
    finally:
        room.shutdown()
        room.server_close()

    shutil.rmtree(scratch, ignore_errors=True)
    if FAILED:
        print("\n  " + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("\n  every rule held")


if __name__ == "__main__":
    main()
