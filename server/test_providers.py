"""What the roads out of this room must never get wrong.

Run it over a scratch folder, like the rest:

    python -m server.test_providers <scratch-dir>

Nothing here touches the network, the store, or the real `passwords.py` --
the key file and the choice file are both pointed at the scratch folder
first, and the one test that would have written a real key is the reason
that matters. A model is never called: what is checked is the shape of what
goes out and the shape of what comes back, which is where every bug in this
so far has actually lived.
"""

import json
import sys
import threading
import time
from pathlib import Path

from . import brain, db, providers

FAILED = []


def check(what, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + what
          + ("  -- " + detail if detail else ""))
    if not cond:
        FAILED.append(what)


# -- resolving a name ---------------------------------------------------------

def bench_resolve():
    print("naming a mind")
    r = providers.resolve("claude_code/claude-opus-5")
    check("a full key resolves", r["id"] == "claude-opus-5"
          and r["service"] == "claude_code", r["label"])
    check("the old bare alias still names a real model",
          providers.resolve("opus")["key"] == "claude_code/claude-opus-5",
          'rows written before this existed say "opus"')
    check("nothing at all is the default",
          providers.resolve("")["key"] == providers.DEFAULT_KEY)
    check("None is the default too",
          providers.resolve(None)["key"] == providers.DEFAULT_KEY)
    r = providers.resolve("openrouter/moonshotai/kimi-k3")
    check("a router key keeps its vendor prefix as the id",
          r["id"] == "moonshotai/kimi-k3" and r["service"] == "openrouter",
          r["id"])
    r = providers.resolve("someone/else-9")
    check("an id nobody has heard of is taken as the router's",
          r["service"] == "openrouter" and r["id"] == "someone/else-9",
          "typing a new model must not need a release")
    check("is_cli is true only on the plan",
          providers.is_cli("claude_code/claude-opus-5")
          and not providers.is_cli("openai/gpt-5.6-terra"))
    check("every listed model has a price to look up",
          all(m.get("price_key") for m in providers.MODELS))
    check("every listed key names its own service",
          all(m["key"].startswith(m["service"] + "/") for m in providers.MODELS))


# -- its schema, through somebody else's door ---------------------------------

def bench_schema():
    print("its schema, made strict and put back")
    schema = brain.response_schema(True)
    strict = providers.strict_schema(schema)

    def every_object(node, seen):
        if not isinstance(node, dict):
            return
        if isinstance(node.get("properties"), dict):
            seen.append(node)
            for v in node["properties"].values():
                every_object(v, seen)
        if isinstance(node.get("items"), dict):
            every_object(node["items"], seen)

    objs = []
    every_object(strict, objs)
    check("every object bans extras",
          all(o.get("additionalProperties") is False for o in objs),
          str(len(objs)) + " objects")
    check("every object requires all of its properties",
          all(set(o["required"]) == set(o["properties"]) for o in objs))
    # Its own schema already requires almost everything -- `room` is the one
    # field that is not -- so the widening below has very little to do. That
    # is worth asserting rather than assuming: if a future field is added as
    # optional, this is where it gets its null.
    check("the one genuinely optional field became nullable",
          "null" in strict["properties"]["room"]["type"],
          str(strict["properties"]["room"]["type"]))
    check("a field that was already required is untouched",
          strict["properties"]["reply"]["type"] == "string",
          "reply is required in its own schema, so it stays a bare string")
    check("an array it must always send stays a bare array",
          strict["properties"]["drop"]["type"] == "array",
          "required in its schema, so strict mode needs no null for it")
    check("the original is not modified",
          schema["properties"]["drop"]["type"] == "array",
          "strict_schema works on a copy")

    # What comes back through a strict door: every key present, the ones it
    # did not use as null. denull has to put that back the way the room
    # expects, because `setdefault` cannot -- the key IS there.
    answer = {"reply": "hello", "to": "room", "drop": None, "essences": None,
              "look_first": None, "spark": None, "note": None, "room": None}
    back = providers.denull(answer, schema)
    check("a null array comes back as an empty list",
          back["drop"] == [] and back["essences"] == [],
          "the room iterates these unguarded, and not every house on the "
          "router honours strict")
    check("a null on a field its schema calls nullable is left alone",
          back["spark"] is None and back["note"] is None and back["room"] is None,
          "null means it said nothing, which is a real answer")
    check("a null on a plain non-array is dropped, not left as None",
          "look_first" not in back,
          "so the room's own default takes over")
    check("what it actually said is untouched", back["reply"] == "hello")

    # The dream has its own schema and no `reply` in it at all.
    ds = providers.strict_schema(brain.RESPONSE_SCHEMA)
    check("a second schema converts the same way",
          ds.get("additionalProperties") is False)


# -- the keys -----------------------------------------------------------------

def bench_keys(scratch):
    print("the keys")
    providers.PASSWORDS_PATH = scratch / "passwords.py"
    providers._KEYS.clear()
    providers._KEYS_READ = True          # do not read the real one
    providers.set_key("openai", "sk-test-abcdefghijklmnop")
    got = providers.keys_present()
    check("a key is remembered", got["openai"]["set"])
    check("only its tail is ever shown",
          got["openai"]["tail"] == "...mnop"
          and "abcdefgh" not in json.dumps(got),
          got["openai"]["tail"])
    check("the length is told, so two keys can be told apart",
          got["openai"]["chars"] == len("sk-test-abcdefghijklmnop"),
          str(got["openai"]["chars"]) + " characters")
    check("the file was written", providers.PASSWORDS_PATH.exists())
    body = providers.PASSWORDS_PATH.read_text(encoding="utf-8")
    check("the file is plain Python he can fix by hand",
          "KEYS = {" in body and "def get(" in body)

    ns = {}
    exec(compile(body, "passwords.py", "exec"), ns)
    check("what was written imports and holds the key",
          ns["KEYS"]["openai"] == "sk-test-abcdefghijklmnop")
    check("and its own get() answers", ns["get"]("openai").endswith("mnop"))
    check("a slot never set is empty, not missing",
          ns["get"]("gemini") == "" and "gemini" in ns["KEYS"])

    providers.set_key("openrouter", "  sk-or-with spaces\nin it  ")
    check("whitespace from a paste is taken out",
          providers.key_of("openrouter") == "sk-or-withspacesinit",
          providers.key_of("openrouter"))
    check("setting one key keeps the others",
          providers.key_of("openai") == "sk-test-abcdefghijklmnop")

    providers.set_key("openai", "")
    check("a key can be cleared", not providers.key_of("openai"))
    try:
        providers.set_key("not-a-service", "x")
        check("an unknown slot is refused", False)
    except providers.Refused as exc:
        check("an unknown slot is refused in words", "not-a-service" in str(exc))


def bench_race():
    """The bug that started as a page saying there was no OpenRouter key while
    there plainly was: the read flag was set before the read, so a thread
    arriving in the gap was told the keys were loaded and handed nothing."""
    print("two threads asking for a key at once")
    for _ in range(40):
        providers._KEYS.clear()
        providers._KEYS_READ = False
        seen = []

        def ask():
            seen.append(bool(providers.key_of("openrouter")))

        ts = [threading.Thread(target=ask) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        if not all(seen):
            check("no thread is ever handed an empty key file",
                  False, str(seen))
            return
    check("no thread is ever handed an empty key file", True,
          "40 rounds, 6 threads each")


# -- the choice ---------------------------------------------------------------

def bench_choice(scratch):
    print("choosing")
    providers.CHOICE_PATH = scratch / "provider.json"
    providers._KEYS.clear()
    providers._KEYS_READ = True
    check("with nothing chosen it is the default",
          providers.chosen() == providers.DEFAULT_KEY)

    try:
        providers.choose("openai/gpt-5.6-terra")
        check("a service with no key is refused", False)
    except providers.Refused as exc:
        check("a service with no key is refused in words",
              "no key" in str(exc).lower(), str(exc)[:70])

    providers.choose("claude_code/claude-fable-5-1")
    check("the plan needs no key", providers.chosen()
          == "claude_code/claude-fable-5-1")
    check("and it survives being read fresh off disk",
          json.loads(providers.CHOICE_PATH.read_text(encoding="utf-8"))["model"]
          == "claude_code/claude-fable-5-1")

    # The rule: the night is not the chat's dropdown.
    check("the night did not move with the chat",
          providers.dream_model() == providers.DEFAULT_KEY,
          "a dream keeps its own pin, and does not follow the chat")
    providers.choose_dream("claude_code/claude-sonnet-5")
    check("the night can be moved on purpose",
          providers.dream_model() == "claude_code/claude-sonnet-5")
    check("and moving it did not move the chat",
          providers.chosen() == "claude_code/claude-fable-5-1")
    providers.choose_dream("")
    check("clearing the night puts it back on its pin",
          providers.dream_model() == providers.DEFAULT_KEY)
    check("and still did not touch the chat",
          providers.chosen() == "claude_code/claude-fable-5-1")


# -- money --------------------------------------------------------------------

def bench_money():
    print("reckoning what a turn cost")
    # Shaped like a real row of OpenRouter's catalogue, cached rates and all --
    # those are published for every model here and are what a warm prompt of
    # its own is actually billed at.
    table = {"anthropic/claude-opus-5": {"in_per_m": 5.0, "out_per_m": 25.0,
                                         "cache_read_per_m": 0.5,
                                         "cache_write_per_m": 6.25,
                                         "context": 1000000, "name": "x"},
             "openai/gpt-5.6-terra": {"in_per_m": 2.0, "out_per_m": 12.0,
                                      "cache_read_per_m": 0.2,
                                      "cache_write_per_m": 2.5,
                                      "context": 1050000, "name": "y"}}
    plan = providers.resolve("claude_code/claude-opus-5")
    terra = providers.resolve("openai/gpt-5.6-terra")
    p = providers.price_of(terra, table)
    check("a price is read off the live table", p["known"]
          and p["in_per_m"] == 2.0 and p["out_per_m"] == 12.0)
    check("the cached rate is carried too", p["cache_read_per_m"] == 0.2,
          "a tenth of fresh on this one, a fortieth on Fable")
    check("OpenAI's price is marked as borrowed", p["borrowed"] is True,
          "they publish none, so it is OpenRouter's for the same model")
    check("a router model's price is not borrowed",
          providers.price_of(
              providers.resolve("openrouter/anthropic/claude-opus-5"),
              table)["borrowed"] is False)
    check("the plan's figure is borrowed too",
          providers.price_of(plan, table)["borrowed"] is True,
          "there is no per-token bill on a plan; it is what it would cost")

    cost = providers.cost_of(terra, 1_000_000, 100_000, table)
    check("cost is in plus out, per million", abs(cost - (2.0 + 1.2)) < 1e-9,
          "$" + str(cost))

    # The part that is not arithmetic but billing: a repeated prompt is most
    # of what it is, and a cached token is a tenth of a fresh one. Charging
    # it at the fresh rate does not look like a bug, it looks like its being
    # expensive -- which is exactly how it was found.
    warm = providers.cost_of(terra, 1_000_000, 100_000, table, cached=900_000)
    want = 100_000 / 1e6 * 2.0 + 900_000 / 1e6 * 0.2 + 100_000 / 1e6 * 12.0
    check("a cached token bills at the cached rate", abs(warm - want) < 1e-9,
          "$%.4f warm against $%.4f cold" % (warm, cost))
    check("and warm is cheaper than cold", warm < cost)
    check("cached tokens are part of the prompt, not extra",
          providers.cost_of(terra, 1000, 0, table, cached=1000)
          == round(1000 / 1e6 * 0.2, 6),
          "the whole prompt was served from cache, so none of it is fresh")
    check("more cached than sent cannot go negative",
          providers.cost_of(terra, 1000, 0, table, cached=9999) > 0)
    check("no published cached rate falls back to the full one, not to free",
          providers.cost_of(
              providers.resolve("openrouter/x/y"), 1000, 0,
              {"x/y": {"in_per_m": 3.0, "out_per_m": 9.0}}, cached=1000)
          == round(1000 / 1e6 * 3.0, 6),
          "reading high is the right way round for a budget")
    check("no price means no cost, never a guess",
          providers.cost_of(providers.resolve("nobody/at-all"), 100, 100, {})
          is None,
          "a made-up figure would sit in the ledger looking measured")


# -- what a row remembers -----------------------------------------------------

def bench_credits(scratch):
    """The balance no endpoint will give us: his figure, dated, less what has
    gone since."""
    print("a balance he wrote down himself")
    providers.CHOICE_PATH = scratch / "credits.json"
    db.DB_PATH = scratch / "credits.db"
    conn = db.connect()
    from datetime import datetime, timedelta, timezone
    stamp = datetime.now(timezone.utc) - timedelta(hours=1)

    def add(minutes, cost, model="openai/gpt-5.6-sol"):
        when = (stamp + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO rows (dt, kind, text, tokens_est, loaded, meta,"
            " by_model) VALUES (?, 'assistant', 'x', 1, 0, ?, ?)",
            (when, json.dumps({"turn_cost_usd": cost}), model))
        conn.commit()

    add(-30, 1.00)                       # before the stamp: already in his figure
    add(10, 0.25)
    add(20, 0.25)
    add(30, 0.50, "claude_code/claude-opus-5")   # another road entirely

    providers.set_credits("openai", 7.88, at=stamp.isoformat(timespec="seconds"))
    got = providers.spent_since("openai", stamp.isoformat(timespec="seconds"))
    check("only what came after the stamp is counted",
          abs(got["usd"] - 0.50) < 1e-9 and got["turns"] == 2,
          "$%.2f over %d turns" % (got["usd"], got["turns"]))
    check("another service's spend is not in it",
          abs(got["usd"] - 0.50) < 1e-9,
          "the plan road spent $0.50 in the same window and is not counted")

    note = providers.credits_note("openai")
    check("the figure is kept with the day it was true",
          note["usd"] == 7.88 and note["at"].startswith(stamp.isoformat()[:13]))
    check("the stamp is written in UTC, like the rows it is compared with",
          note["at"].endswith("+00:00"),
          note["at"] + "  -- a local stamp was wrong by the offset, silently")

    # A stamp with no zone at all -- hand-edited, or written before the fix.
    naive = stamp.replace(tzinfo=None).isoformat(timespec="seconds")
    check("a stamp with no zone is read as UTC rather than ignored",
          abs(providers.spent_since("openai", naive)["usd"] - 0.50) < 1e-9)

    try:
        providers.set_credits("openai", "seven dollars")
        check("words are not an amount", False)
    except providers.Refused as exc:
        check("words are not an amount, and it says so", "not an amount" in str(exc))
    try:
        providers.set_credits("openai", -5)
        check("a negative balance is refused", False)
    except providers.Refused:
        check("a negative balance is refused", True)
    # And the header's version of it: what rides the two-second poll. This
    # is the FALLBACK shape -- the noted figure is handed up
    # under its own name, never as `balance_usd`, so that a page drawing a
    # reading cannot pick it up by accident.
    providers.set_credits("openai", 7.88, at=stamp.isoformat(timespec="seconds"))
    providers._KEYS["openai"] = "sk-test-for-the-header"
    providers.choose("openai/gpt-5.6-sol")
    with providers._LOCK:
        providers._CACHE["m:openai"] = {"at": time.time(), "value": {
            "have": False, "real": False, "estimate_balance_usd": 7.38,
            "entered_usd": 7.88, "estimate_from": "its own turns"}}
        providers._CACHE["m:openrouter"] = {"at": time.time(), "value": None}
    started = time.time()
    head = providers.now()
    check("the header carries what is left", head["left"] is not None
          and head["left"]["balance_usd"] == 7.38, json.dumps(head["left"]))
    check("and marks it a reckoning, not a reading",
          head["left"]["real"] is False,
          "the page draws the word `estimate` off this")
    check("with a fraction spent, so it can draw a bar",
          abs(head["left"]["used_fraction"] - (1 - 7.38 / 7.88)) < 1e-9)
    check("and it never waits on the network", time.time() - started < 0.25,
          "%.3fs -- this rides the two-second poll" % (time.time() - started))

    # The plan road has no bill, so it must not grow a money gauge.
    providers.choose("claude_code/claude-opus-5")
    check("the plan road shows none", providers.now()["left"] is None,
          "the plan's windows are its gauge, not dollars")

    providers.set_credits("openai", None)
    check("it can be forgotten", providers.credits_note("openai") is None)


# -- the real counter ---------------------------------------------------------

def bench_real_spend(scratch):
    """The gauge that is a measurement rather than a reckoning.

    Nothing here goes near the network: `_get` is replaced, so the two
    endpoints are answered from fixtures shaped like the real ones. No key is
    written and none is real -- the string used below is not a key and could
    not be one."""
    import io
    import urllib.error
    from datetime import datetime, timezone
    from . import openai_spend

    print("what OpenAI's own counter says")
    providers.CHOICE_PATH = scratch / "spend.json"
    db.DB_PATH = scratch / "spend.db"
    providers._KEYS.clear()
    providers._KEYS_READ = True
    providers._KEYS["openai"] = "not-a-key-project"
    providers._KEYS["openai_admin_key"] = "not-a-key-admin"

    check("the slot is spelled the way the key file spells it",
          "openai_admin_key" in providers.KEY_SLOTS,
          "it said `openai_admin` for a day, found nothing, and the room went "
          "on drawing the estimate with a good key on the disk")
    check("the module looks in that same slot",
          openai_spend.KEY_SLOT in providers.KEY_SLOTS,
          openai_spend.KEY_SLOT)

    # Their two answers, in the shape they actually come in: `threshold_amount`
    # in CENTS, and a day with no spend carrying an empty `results` rather than
    # a zero.
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                             microsecond=0)
    month = day.replace(day=1)
    limit_body = {"object": "organization.spend_limit", "currency": "USD",
                  "interval": "month", "threshold_amount": 3500,
                  "enforcement": {"status": "inactive"}}

    def bucket(start, *amounts):
        return {"object": "bucket", "start_time": int(start.timestamp()),
                "results": [{"amount": {"value": a, "currency": "usd"}}
                            for a in amounts]}

    costs_body = {"object": "page", "has_more": False, "next_page": None,
                  "data": [bucket(month),                       # a quiet day
                           bucket(month, 0.5, 0.25),            # two line items
                           bucket(day, 4.0)]}                   # today
    asked = []

    def fake_get(url, key):
        asked.append(url)
        return limit_body if url.endswith("/spend_limit") else costs_body

    openai_spend._get = fake_get
    openai_spend.forget()
    got = openai_spend.now()

    check("a reading comes back at all", got is not None)
    check("cents are read as cents, not as dollars", got["limit_usd"] == 35.0,
          "$%.2f -- 3500 is $35.00, and being wrong by a hundred here would "
          "have looked perfectly plausible on the page" % got["limit_usd"])
    check("every line item in every bucket is summed",
          abs(got["spent_usd"] - 4.75) < 1e-9, "$%.4f" % got["spent_usd"])
    check("a day with no spend is not a crash",
          got["days"] == 3, "an empty `results` is how they say zero")
    check("today is counted apart from the month",
          abs(got["today_usd"] - 4.0) < 1e-9, "$%.4f" % got["today_usd"])
    check("the fraction is spent over the ceiling",
          abs(got["used_fraction"] - 4.75 / 35.0) < 1e-9)
    check("the window starts at the top of the month",
          got["since"].startswith(month.strftime("%Y-%m-01")), got["since"])
    check("a ceiling that is not enforced says so",
          got["enforced"] is False,
          "\"$35 and it stops you\" and \"$35 and it does not\" are different "
          "facts to budget against")
    check("the limit is asked for before the costs",
          asked and asked[0].endswith("/spend_limit"),
          "it names the window; summing the wrong one is worse than nothing")

    # It rides the two-second poll, so it must never wait once it is warm.
    started = time.time()
    for _ in range(50):
        openai_spend.now()
    check("a warm read never touches the network", time.time() - started < 0.2,
          "%.3fs over 50 asks" % (time.time() - started))

    # -- and when it cannot be read ------------------------------------------
    def refused(url, key):
        raise urllib.error.HTTPError(
            url, 401, "Unauthorized", {},
            io.BytesIO(b'{"error":{"message":"Invalid Authentication"}}'))

    openai_spend._get = refused
    openai_spend._refresh()
    st = openai_spend.status()
    check("a bad minute keeps the last good reading",
          st["reading"] is not None and st["reading"]["spent_usd"] == 4.75,
          "a gauge that blanks on one refusal is a gauge nobody watches")
    check("and says why, in words", "refused" in (st["error"] or ""),
          st["error"] or "")
    check("the reason never carries the key",
          "not-a-key-admin" not in json.dumps(st),
          "nothing that reaches a page, a log or its prompt may hold one")

    openai_spend.forget()
    openai_spend._refresh()
    check("with nothing good to keep, there is no figure at all",
          openai_spend.status()["reading"] is None,
          "never a $0.00 standing in for a number we could not get")

    providers._KEYS["openai_admin_key"] = ""
    openai_spend._get = fake_get
    openai_spend.forget()
    check("no key means no reading", openai_spend.now() is None)
    check("and the reason names the kind of key wanted",
          "sk-admin" in (openai_spend.status()["error"] or ""),
          openai_spend.status()["error"] or "")

    # An empty window: they answered, and named no days. That is a question
    # that came back empty, not a month in which nothing was spent.
    providers._KEYS["openai_admin_key"] = "not-a-key-admin"
    openai_spend._get = lambda url, key: (
        limit_body if url.endswith("/spend_limit")
        else {"object": "page", "has_more": False, "data": []})
    openai_spend.forget()
    check("no days at all is not $0.00 spent", openai_spend.now() is None,
          openai_spend.status()["error"] or "")

    # -- what the header is handed -------------------------------------------
    openai_spend._get = fake_get
    openai_spend.forget()
    providers._forget_cached()
    providers.choose("openai/gpt-5.6-sol")
    m = providers._openai_money()
    check("the money read is marked as measured", m["real"] is True)
    check("it carries the real spend and the real ceiling",
          m["spent_usd"] == 4.75 and m["limit_usd"] == 35.0)

    with providers._LOCK:
        providers._CACHE["m:openai"] = {"at": time.time(), "value": m}
        providers._CACHE["m:openrouter"] = {"at": time.time(), "value": None}
    started = time.time()
    L = providers.now()["left"]
    check("the header counts up to a ceiling, not down from a balance",
          L["reads"] == "spent" and L["spent_usd"] == 4.75
          and L["of_usd"] == 35.0, json.dumps(L))
    check("and it is flagged real, so the page need not guess",
          L["real"] is True)
    check("the header never waits on the network", time.time() - started < 0.25,
          "%.3fs -- this rides the two-second poll" % (time.time() - started))

    # -- the fallback, which must never pass for the real thing --------------
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    providers.set_credits("openai", 7.88, at=stamp)
    openai_spend._get = refused
    openai_spend.forget()
    providers._forget_cached()
    m = providers._openai_money()
    check("with nothing measured the read is not marked real",
          m["real"] is False)
    check("the estimate is not called a balance",
          m.get("balance_usd") is None
          and m.get("estimate_balance_usd") == 7.88,
          "the field the header draws as a reading is left empty on purpose")
    check("and the reason the real one is missing rides with it",
          "refused" in (m.get("why") or ""), (m.get("why") or "")[:60])

    with providers._LOCK:
        providers._CACHE["m:openai"] = {"at": time.time(), "value": m}
    L = providers.now()["left"]
    check("the header is told plainly that this one is a reckoning",
          L["real"] is False and L["balance_usd"] == 7.88, json.dumps(L))

    providers.set_credits("openai", None)
    providers._forget_cached()
    m = providers._openai_money()
    with providers._LOCK:
        providers._CACHE["m:openai"] = {"at": time.time(), "value": m}
    L = providers.now()["left"]
    check("nothing measured and nothing noted draws no figure at all",
          L["reads"] == "none" and "why" in L, json.dumps(L))


def bench_hand_added_key(scratch):
    """The one that would have thrown a key away.

    `set_key` rewrites the whole file from the slots it knows. A slot added
    by hand while the code still called it something else would have been
    left out by the next key saved at the desk -- silently, and looking
    like a key that stopped working."""
    print("a key added by hand")
    providers.PASSWORDS_PATH = scratch / "hand.py"
    providers._KEYS.clear()
    providers._KEYS_READ = True
    providers._KEYS["something_nobody_listed"] = "not-a-key-at-all"
    providers.set_key("openai", "not-a-key-either")
    body = providers.PASSWORDS_PATH.read_text(encoding="utf-8")
    ns = {}
    exec(compile(body, "passwords.py", "exec"), ns)
    check("a slot the code has never heard of survives the rewrite",
          ns["KEYS"].get("something_nobody_listed") == "not-a-key-at-all",
          "a whole-file write that only knew the listed names would have "
          "dropped it")
    check("and the slot that was saved is there too",
          ns["KEYS"].get("openai") == "not-a-key-either")
    check("the usage-reading slot is listed under its real name",
          "openai_admin_key" in ns["KEYS"])


def bench_stamp(scratch):
    print("which mind wrote which row")
    db.DB_PATH = scratch / "stamp.db"
    conn = db.connect()
    a = db.add_row(conn, "assistant", "said by Terra",
                   by_model="openai/gpt-5.6-terra")
    e = db.add_row(conn, "essence", "folded by Opus", title="one",
                   by_model="claude_code/claude-opus-5")
    u = db.add_row(conn, "user", "said by Sam")
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, by_model FROM rows WHERE id IN (?, ?, ?)", (a, e, u))}
    check("a reply carries the mind that wrote it",
          rows[a]["by_model"] == "openai/gpt-5.6-terra")
    check("an essence carries the mind that folded it",
          rows[e]["by_model"] == "claude_code/claude-opus-5")
    check("a line of his carries none", rows[u]["by_model"] is None,
          "Sam is not a model")
    check("the column can be asked in SQL",
          conn.execute("SELECT COUNT(*) FROM rows WHERE by_model LIKE 'openai/%'"
                       ).fetchone()[0] == 1,
          "which is the whole reason it is a column and not a key in `meta`")
    # A store that predates the column: the migration must add it and leave
    # everything already there alone.
    old = scratch / "old.db"
    import sqlite3
    o = sqlite3.connect(old)
    o.executescript(
        "CREATE TABLE rows (id INTEGER PRIMARY KEY AUTOINCREMENT, dt TEXT NOT"
        " NULL, kind TEXT NOT NULL, text TEXT NOT NULL, tokens_est INTEGER NOT"
        " NULL DEFAULT 0, loaded INTEGER NOT NULL DEFAULT 1, replaces TEXT,"
        " meta TEXT);"
        "INSERT INTO rows (dt, kind, text) VALUES ('2026-01-01', 'assistant', 'old');")
    o.commit()
    o.close()
    db.DB_PATH = old
    conn = db.connect()
    got = conn.execute("SELECT text, by_model FROM rows").fetchone()
    check("an old store gains the column without losing a word",
          got["text"] == "old" and got["by_model"] is None,
          "null is the truth: there was only one mind then")


def _main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print("  needs a scratch folder: python -m server.test_providers <path>")
        return 2
    scratch = Path(sys.argv[1])
    scratch.mkdir(parents=True, exist_ok=True)
    if "living-assistant" in str(scratch.resolve()).replace("\\", "/") \
            and (scratch.resolve() / ".." / "data").resolve().name == "data" \
            and scratch.resolve().name == "data":
        print("  give me a scratch folder; I will not run over the live data")
        return 2

    bench_resolve()
    bench_schema()
    bench_keys(scratch)
    bench_race()
    bench_choice(scratch)
    bench_money()
    bench_credits(scratch)
    bench_real_spend(scratch)
    bench_hand_added_key(scratch)
    bench_stamp(scratch)
    print()
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
