"""Lean on the poster without Steam and without a model:

    python -m server.test_steam_post

Nothing here opens a socket. The point is every refusal the poster promises,
and the one promise underneath them: a draft is either sent exactly as written or
not sent at all. So the bench checks that a bad draft is REFUSED rather than
repaired -- no truncating a long one, no appending a missing sign-off -- that
a third item never gets as far as the client, that a dead session is loud
rather than quiet, and that no failure anywhere carries the session in its
message.
"""

import sys

from . import steam_post

# The account, the items and the sign-off come from the home's comments.json,
# and a bench has no home -- so a made-up pair, set before GOOD is built.
steam_post.STEAMID64 = "76561190000000000"
steam_post.ITEMS = {"1000000001": "Lantern Mod", "1000000002": "Field Drills"}
steam_post.SIGN_OFF = "(Ada, Sam's Digital Assistant)"
steam_post.POST_URL_TMPL = (
    "https://steamcommunity.com/comment/PublishedFile_Public/post/"
    + steam_post.STEAMID64 + "/{item_id}/")


FAILED = []


def check(name, ok, detail=""):
    mark = "ok " if ok else "FAIL"
    print("  " + mark + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def refuses(name, item_id, text, because=""):
    """The draft is refused, and the client is never opened to do it."""
    opened = []
    real = steam_post._session
    steam_post._session = lambda: opened.append(1) or ("x", "y")
    try:
        steam_post.post(item_id, text)
        check(name, False, "it went through")
    except steam_post.Refused as exc:
        check(name, not opened, "the client was opened anyway")
        if because and because not in str(exc):
            check(name + " (says why)", False, str(exc))
    except Exception as exc:
        check(name, False, type(exc).__name__ + ": " + exc)
    finally:
        steam_post._session = real


GOOD = "Thanks, that helps. " + steam_post.SIGN_OFF


def main():
    print("the refusals")
    refuses("a third item never reaches the client", "1234567890", GOOD,
            "not one of its own")
    refuses("an empty draft", "1000000001", "")
    refuses("whitespace-only", "1000000001", "   \n  ")
    refuses("a draft with no sign-off", "1000000001",
            "Thanks, that helps.", "sign-off")
    refuses("a sign-off that is not last", "1000000001",
            steam_post.SIGN_OFF + " thanks, that helps.")
    refuses("a draft over the ceiling", "1000000001",
            "x" * (steam_post.MAX_CHARS + 1 - len(steam_post.SIGN_OFF))
            + steam_post.SIGN_OFF, "rewritten shorter")
    refuses("padded text, rather than trim it here", "1000000001",
            "  " + GOOD + "  ")

    print("what it does NOT do")
    long_one = ("y" * (steam_post.MAX_CHARS + 50)) + steam_post.SIGN_OFF
    try:
        steam_post.check("1000000001", long_one)
        check("a long draft is refused, not cut", False, "it passed")
    except steam_post.Refused:
        check("a long draft is refused, not cut", len(long_one) > steam_post.MAX_CHARS)
    no_sign = "Thanks, that helps."
    try:
        steam_post.check("1000000001", no_sign)
        check("a sign-off is never appended", False, "it passed")
    except steam_post.Refused:
        check("a sign-off is never appended", not no_sign.endswith(steam_post.SIGN_OFF))

    print("the exact approved text survives")
    both = [i for i in steam_post.ITEMS]
    check("both of his items pass, and only those", len(both) == 2
          and set(both) == {"1000000001", "1000000002"}, both)
    steam_post.check("1000000002", GOOD)
    check("a good draft on his second item passes untouched", True)

    print("a dead session is loud")
    real = steam_post._session

    def dead():
        raise steam_post.NotLoggedIn("his client is not holding a session")
    steam_post._session = dead
    try:
        steam_post.post("1000000001", GOOD)
        check("a dead session raises rather than returning", False, "returned")
    except steam_post.NotLoggedIn as exc:
        check("a dead session raises rather than returning", True)
        check("and says nothing was posted or why not", bool(str(exc)))
    finally:
        steam_post._session = real

    print("nothing of the session in what it says")
    secret = "SESSIONVALUE_" + ("z" * 40)
    steam_post._session = lambda: (secret, secret)
    try:
        steam_post.post("1000000001", GOOD)
        check("a network failure is not silent", False, "returned")
    except steam_post.NotLoggedIn as exc:
        check("a network failure is not silent", True)
        check("and its message holds no part of the session",
              secret not in str(exc) and secret[:20] not in str(exc), str(exc))
    except Exception as exc:
        check("a network failure comes back as NotLoggedIn", False,
              type(exc).__name__)
    finally:
        steam_post._session = real

    print("it never touches the read path")
    src = open(steam_post.__file__, encoding="utf-8").read()
    check("no render URL anywhere in the poster",
          "/render/" not in src)
    check("the debug port is only ever 127.0.0.1",
          src.count("127.0.0.1") >= 2 and "0.0.0.0" not in src)

    if FAILED:
        print(str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("every rule held")


if __name__ == "__main__":
    main()
