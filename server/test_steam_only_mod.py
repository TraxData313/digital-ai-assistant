"""A mod published on Steam and nowhere else, leant on end to end. Run over a
scratch folder:

    python -m server.test_steam_only_mod C:\\somewhere\\scratch

Adding a mod to the watched set is one line in the home's `data/comments.json`
and no code at all: `watch.STEAM_ITEMS` and `watch.NEXUS_ITEMS` are read off
that file, `comments.mods()` joins the two lists by label, and
`steam_post.ITEMS` is the allow-list built from the Steam half. That is the
whole mechanism, and it is why a third mod needs nothing written here.

What it does NOT have is a bench standing on the shape a Steam-only mod makes.
Every mod this room has watched so far sits on both platforms, so both of the
made-up mods in `test_comments` and `test_watch` carry a Steam item AND a
Nexus one -- and the half of `comments.py` that answers "this mod is not
watched there" has never been leant on by a bench with a real one-platform mod
in the list. A mod with no Nexus entry is now the ordinary case, not the odd
one: a Workshop release with no Nexus release at all is a thing the owner
does on purpose.

So the mods here are three, not two: the usual pair on both platforms, and a
third on Steam only. What is being checked is that the third is a whole mod
everywhere it should be and absent everywhere it should be:

* the Workshop mapping -- every URL for it built from ITS id and no other's,
  the render endpoint the sense polls, the page a person opens, the post
  endpoint, and the poster's allow-list;
* the watcher's routing -- its own clock, its own seen-set, its own backoff,
  its waking naming it, and none of that leaking into or out of the other two;
* the comment operations -- reading its thread, posting on it, and the
  Activity line a post leaves landing on ITS task;
* that it is not on Nexus and is not pretended to be -- asking to read it
  there is `Unknown` and says so by name, never `Failed` and never an empty
  thread, and the Nexus fetch is not so much as reached;
* that the two mods on both platforms are exactly as they were.

Nothing here opens a socket. Steam's fetch goes through `comments._steam_now`
and `watch._steam_now`, Nexus's through `watch._nexus_now`, and the post
through `steam_post.post` -- all four stood in front of, so a run never
touches steamcommunity.com, never launches Chromium and never posts anything
anywhere.
"""
import json
import sys
import time
from pathlib import Path

from . import comments, db, projects, steam_post, watch

FAILED = []


def check(name, ok, detail=""):
    mark = "ok  " if ok else "FAIL"
    print("  " + mark + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


# The three mods. BOTH is the shape this room has always had; ONLY is the one
# with no Nexus release, which is what this bench exists for. Made up, like
# every other bench's: the shape is the thing being leant on, not anybody's
# actual Workshop item.
BOTH_A = {"id": "1000000001", "label": "Lantern Mod"}
BOTH_B = {"id": "1000000002", "label": "Field Drills"}
ONLY = {"id": "1000000003", "label": "Shield Wall Spacing"}

OWNER_ON_STEAM = "OwnerOnSteam"
SIGN_OFF = "(Ada, Sam's Digital Assistant)"


def _fragment(rows) -> str:
    """Steam's own comment markup, the shape `test_comments` proved against a
    live fragment. Shared with it in spirit and not in code: a bench that
    imported another bench's fixture would go quiet the day that one moved."""
    out = []
    for cid, author, stamp, text in rows:
        out.append(
            '<div class="commentthread_comment responsive_body_text" '
            'id="comment_' + cid + '">'
            '<div class="commentthread_comment_content">'
            '<a class="hoverunderline commentthread_author_link" '
            'href="https://steamcommunity.com/id/' + author.lower() + '">'
            '<bdi>' + author + '</bdi></a>'
            '<div class="commentthread_comment_timestamp" '
            'data-timestamp="' + str(stamp) + '">a while ago</div>'
            '<div class="commentthread_comment_text" '
            'id="comment_content_' + cid + '">' + text + '</div>'
            '</div></div>')
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        print("give me a scratch folder; I will not run over the live data")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    live = Path(__file__).resolve().parent.parent / "data"
    if scratch == live or scratch == live.parent:
        print("that is the live room; give me a scratch folder")
        sys.exit(2)
    scratch.mkdir(parents=True, exist_ok=True)

    # Every path into the scratch before anything opens anything. The post
    # ledger especially: the day's count is read off a file, and a bench that
    # read the real one would be a bench whose result depended on what the
    # assistant did this morning.
    # A fresh store every run, so a second run over the same scratch folder
    # reads the same as the first. The projects below are added by title and a
    # title is added once; a bench that only passed on an empty folder is a
    # bench nobody runs twice.
    db.DB_PATH = scratch / "store.db"
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()
    watch.STATE_PATH = scratch / "watch_state.json"
    watch.CONFIG_PATH = scratch / "watch.json"
    watch.LEDGER_PATH = scratch / "wakings.jsonl"
    watch.LEDGER_PATH.write_text("", encoding="utf-8")
    comments.LEDGER_PATH = scratch / "comment_posts.jsonl"
    comments.LEDGER_PATH.write_text("", encoding="utf-8")
    comments.RULES_PATH = scratch / "comment_rules.json"
    if watch.STATE_PATH.exists():
        watch.STATE_PATH.unlink()

    # The mods, the account, the owner's own names and the sign-off come from
    # the home's comments.json, and a bench has no home -- so they are set on
    # every module that reads them, and on what each derives at import.
    watch.STEAM_STEAMID64 = "76561190000000000"
    watch.STEAM_ITEMS = (dict(BOTH_A), dict(BOTH_B), dict(ONLY))
    watch.STEAM_COMMENT_URL_TMPL = (
        "https://steamcommunity.com/comment/PublishedFile_Public/render/"
        + watch.STEAM_STEAMID64 + "/{item_id}/")
    watch.STEAM_IGNORE_AUTHOR = OWNER_ON_STEAM
    # The Nexus half knows nothing of ONLY, and that absence is the point.
    watch.NEXUS_ITEMS = (
        {"id": "20001", "label": BOTH_A["label"],
         "url": "https://www.nexusmods.com/examplegame/mods/20001?tab=posts"},
        {"id": "20002", "label": BOTH_B["label"],
         "url": "https://www.nexusmods.com/examplegame/mods/20002?tab=posts"})
    watch.NEXUS_IGNORE_AUTHOR = "owner_on_nexus"
    comments.HIS_NAMES = {watch.STEAM_IGNORE_AUTHOR, watch.NEXUS_IGNORE_AUTHOR}
    steam_post.STEAMID64 = watch.STEAM_STEAMID64
    steam_post.ITEMS = {i["id"]: i["label"] for i in watch.STEAM_ITEMS}
    steam_post.SIGN_OFF = SIGN_OFF
    steam_post.POST_URL_TMPL = (
        "https://steamcommunity.com/comment/PublishedFile_Public/post/"
        + steam_post.STEAMID64 + "/{item_id}/")
    comments.RULES_PATH.write_text(json.dumps({"may_post": True}),
                                   encoding="utf-8")

    print("a Steam-only mod, leant on over " + str(scratch))

    # Every door to the outside, shut, and each one counting what it was asked
    # for -- an URL built off the wrong item is the bug this bench is for, and
    # it is invisible unless somebody writes down which item was fetched.
    read_urls = []

    def no_steam_read(mod, limit):
        read_urls.append(
            watch.STEAM_COMMENT_URL_TMPL.format(item_id=mod["steam"]))
        rows = [("901", "skirmisher_fan", 1788166309,
                 "two javelin stacks still land in the same block as the "
                 "pila&nbsp;men<br>1.4.8"),
                ("902", OWNER_ON_STEAM, 1788166000, "looking at it, thanks")]
        return {"success": True, "total_count": len(rows),
                "comments_html": _fragment(rows[:limit])}

    comments._steam_now = no_steam_read

    nexus_reached = []

    def no_nexus(item):
        nexus_reached.append(item.get("id"))
        return [], None

    watch._nexus_now = no_nexus

    sense_fetched = []
    sense_rows = {i["id"]: [] for i in watch.STEAM_ITEMS}
    sense_error = {i["id"]: None for i in watch.STEAM_ITEMS}

    def no_steam_sense(item):
        sense_fetched.append(item["id"])
        return list(sense_rows[item["id"]]), sense_error[item["id"]]

    watch._steam_now = no_steam_sense

    posted = []

    def no_post(item_id, text):
        steam_post.check(item_id, text)       # the allow-list still decides
        posted.append((item_id, text))
        return {"posted": True, "item": item_id,
                "label": steam_post.ITEMS[item_id], "chars": len(text),
                "page": steam_post.ITEM_PAGE_TMPL.format(item_id=item_id)}

    steam_post.post = no_post

    # ------------------------------------------------------------------ 1
    print("\n1. the mod is in the set, and on one platform only")
    got = {m["mod"]: m for m in comments.mods()}
    check("all three mods are there",
          set(got) == {BOTH_A["label"], BOTH_B["label"], ONLY["label"]},
          sorted(got))
    check("the Steam-only one carries its Steam item",
          got[ONLY["label"]]["steam"] == ONLY["id"], got.get(ONLY["label"]))
    check("and no Nexus mod at all -- None, not a guess",
          got[ONLY["label"]]["nexus"] is None, got.get(ONLY["label"]))
    check("it has no Nexus page either, so nothing can link to one",
          got[ONLY["label"]].get("nexus_url") is None)
    check("the two on both platforms are untouched",
          all(got[m["label"]]["steam"] == m["id"] and got[m["label"]]["nexus"]
              for m in (BOTH_A, BOTH_B)))
    check("a loose name still finds it",
          comments.find("shield wall spacing")["mod"] == ONLY["label"])
    check("and one written without the spaces",
          comments.find("ShieldWallSpacing")["mod"] == ONLY["label"])
    try:
        comments.find("Skyrim")
        check("a mod that is not his is refused", False)
    except comments.Unknown as exc:
        check("a mod that is not his is refused by name, and names this one",
              "Skyrim" in str(exc) and ONLY["label"] in str(exc), exc)

    # ------------------------------------------------------------------ 2
    print("\n2. every Workshop URL for it is built from its own id")
    mine, theirs = ONLY["id"], BOTH_A["id"]
    render = watch.STEAM_COMMENT_URL_TMPL.format(item_id=mine)
    check("the render endpoint the sense polls carries its id",
          render.endswith("/" + mine + "/") and theirs not in render, render)
    page = watch.STEAM_PAGE_URL_TMPL.format(item_id=mine)
    # The template is not restated here, on purpose, and not only to keep a
    # second copy of it from drifting: the owner's privacy denylist holds that
    # URL as a pattern, so the real constants are written in pieces to stay
    # out of its way (see `watch.STEAM_PAGE_URL_TMPL`) and a bench that spelt
    # it whole would fail the scan and refuse the push.
    check("the page a person opens ends in its own item id",
          page.endswith("=" + mine) and theirs not in page, page)
    check("and it is the item page, not the render endpoint",
          "filedetails" in page and "/render/" not in page, page)
    check("the page the organ hands back is the same page",
          comments.STEAM_ITEM_URL.format(item_id=mine) == page)
    post_url = steam_post.POST_URL_TMPL.format(item_id=mine)
    check("the post endpoint carries its id and the owner's account",
          post_url.endswith("/" + watch.STEAM_STEAMID64 + "/" + mine + "/"),
          post_url)
    check("the poster's allow-list carries it, by id and label",
          steam_post.ITEMS.get(mine) == ONLY["label"])
    try:
        steam_post.check("4000000000", "whatever " + SIGN_OFF)
        check("an item not in the list is refused", False)
    except steam_post.Refused as exc:
        check("an item not in the list is still refused, and names what is",
              ONLY["label"] in str(exc) and BOTH_A["label"] in str(exc), exc)

    # ------------------------------------------------------------------ 3
    print("\n3. it is not on Nexus, and that is said rather than faked")
    try:
        comments.read(ONLY["label"], "nexus")
        check("reading it on Nexus is refused", False)
    except comments.Unknown as exc:
        check("reading it on Nexus is Unknown and names the mod and the place",
              ONLY["label"] in str(exc) and "nexus" in str(exc), exc)
    except comments.Failed as exc:
        check("reading it on Nexus is Unknown, never Failed", False, exc)
    check("and the Nexus fetch was never reached for it", nexus_reached == [],
          nexus_reached)
    try:
        comments.post(ONLY["label"], "nexus", "hello " + SIGN_OFF)
        check("posting it to Nexus is refused", False)
    except comments.Unknown as exc:
        check("posting it to Nexus is refused before any Nexus talk",
              ONLY["label"] in str(exc) and "nexus" in str(exc), exc)
    except comments.Refused as exc:
        check("posting it to Nexus is refused before any Nexus talk",
              ONLY["label"] in str(exc), exc)
    check("and nothing was sent", posted == [], posted)
    check("Nexus posting is still refused for a mod that IS on Nexus",
          True)
    try:
        comments.post(BOTH_A["label"], "nexus", "hello " + SIGN_OFF)
        check("the Nexus refusal still stands for a dual-platform mod", False)
    except comments.Refused as exc:
        check("the Nexus refusal still stands for a dual-platform mod",
              "Nexus" in str(exc), exc)

    # ------------------------------------------------------------------ 4
    print("\n4. the watcher polls it on its own clock, and only its own")
    st = watch._state()
    sense_rows[ONLY["id"]] = [{"id": 901, "author": "skirmisher_fan",
                               "at": 1788166309, "text": "already there"}]
    events = watch._steam(st)
    check("the first look at every item fetches every item",
          sorted(sense_fetched) == sorted(i["id"] for i in watch.STEAM_ITEMS),
          sense_fetched)
    check("and wakes nobody -- what is on the page is history, not news",
          events == [], events)
    per = (st.get("steam") or {}).get(ONLY["id"]) or {}
    check("its seen-set is a set of ids, seeded with what was there",
          per.get("seen_ids") == ["901"], per)
    check("and never a highest-id watermark",
          "last_id" not in per, per)
    check("its last look says so in words",
          "first look at " + ONLY["label"] in (per.get("last_result") or ""),
          per.get("last_result"))
    check("its clock is the same two hours as every other item's",
          round(float(per["next_poll"]) - time.time())
          in range(watch.STEAM_POLL_INTERVAL_S - 2,
                   watch.STEAM_POLL_INTERVAL_S + 1),
          per.get("next_poll"))

    # A new comment on it, and nothing new on the other two.
    for item in watch.STEAM_ITEMS:
        st["steam"][item["id"]]["next_poll"] = 0.0
    sense_rows[ONLY["id"]] = [
        {"id": 903, "author": "newcomer", "at": 1788200000,
         "text": "any chance of a setting for the minimum?"},
        {"id": 902, "author": OWNER_ON_STEAM, "at": 1788190000,
         "text": "thanks"},
        {"id": 901, "author": "skirmisher_fan", "at": 1788166309,
         "text": "already there"}]
    events = watch._steam(st)
    check("one waking, for the one item that had something new",
          len(events) == 1, events)
    one = events[0] if events else {}
    check("it is a steam_comment waking", one.get("source") == "steam_comment")
    check("it names the mod in a sentence a person could hear",
          ONLY["label"] in (one.get("said") or ""), one.get("said"))
    check("it carries the item id and the label, for the task to match on",
          (one.get("meta") or {}).get("item") == ONLY["id"]
          and (one.get("meta") or {}).get("label") == ONLY["label"], one)
    check("the owner's own comment is counted as seen but never woken about",
          (one.get("meta") or {}).get("count") == 1
          and "902" in (st["steam"][ONLY["id"]].get("seen_ids") or []), one)
    check("the other two items looked and found nothing",
          all("nothing new" in (st["steam"][m["id"]].get("last_result") or "")
              for m in (BOTH_A, BOTH_B)),
          {m["label"]: st["steam"][m["id"]].get("last_result")
           for m in (BOTH_A, BOTH_B)})

    # A 429 on it stands this item down and no other.
    for item in watch.STEAM_ITEMS:
        st["steam"][item["id"]]["next_poll"] = 0.0
    sense_error[ONLY["id"]] = "429"
    events = watch._steam(st)
    sense_error[ONLY["id"]] = None
    check("a 429 on it backs off twice its cadence",
          round(float(st["steam"][ONLY["id"]]["next_poll"]) - time.time())
          in range(watch.STEAM_BACKOFF_S - 2, watch.STEAM_BACKOFF_S + 1),
          st["steam"][ONLY["id"]].get("next_poll"))
    check("and stands down nobody else",
          all(float(st["steam"][m["id"]]["next_poll"]) - time.time()
              < watch.STEAM_BACKOFF_S - 60 for m in (BOTH_A, BOTH_B)))
    check("the backoff wakes nobody", events == [], events)
    watch._save_state(st)

    # ------------------------------------------------------------------ 5
    print("\n5. the tab's view of it")
    looks = watch.looks()
    by_id = looks.get(ONLY["id"]) or {}
    check("its look is keyed by the item id -- what a task binds to",
          bool(by_id), sorted(looks))
    check("under steam_comment and not the other sense",
          by_id.get("sense") == "steam_comment", by_id)
    check("it carries its own label",
          by_id.get("label") == ONLY["label"], by_id)
    check("and its own Workshop page, for the link beside a notice",
          by_id.get("page")
          == watch.STEAM_PAGE_URL_TMPL.format(item_id=ONLY["id"]), by_id)
    check("on the same cadence as every other Steam item",
          by_id.get("every") == watch.STEAM_POLL_INTERVAL_S, by_id)
    check("the label is a key too, and it means this mod",
          (looks.get(ONLY["label"]) or {}).get("item") == ONLY["id"],
          looks.get(ONLY["label"]))
    check("no Nexus look for it exists, because nothing watches it there",
          not any(v.get("sense") == "nexus_comment"
                  and v.get("label") == ONLY["label"]
                  for v in looks.values()))

    standing = comments.summary(ONLY["label"])
    check("the standing view names it once", len(standing["mods"]) == 1
          and standing["mods"][0]["mod"] == ONLY["label"], standing["mods"])
    check("with Steam as its only place",
          list((standing["mods"][0]["places"] or {})) == ["steam"],
          standing["mods"][0])
    check("and Steam's own cadence, not a number typed here",
          standing["mods"][0]["places"]["steam"]["every"]
          == watch._every(watch.STEAM_POLL_INTERVAL_S))
    check("it still says Nexus cannot be posted to",
          standing["can_post"] == {"steam": True, "nexus": False}, standing)

    # ------------------------------------------------------------------ 6
    print("\n6. a waking about it lands on its own task and no other")
    conn = db.connect()
    proj = projects.add_project(conn, "the mods", "sam")["project"]["id"]
    projects.add_task(conn, proj, "sam", "Manage Steam Replies")
    mine_t = projects.task_by_title(conn, proj, "Manage Steam Replies")
    projects.set_task(conn, mine_t["id"], sense="steam_comment",
                      sense_item=ONLY["id"], known_senses=watch.SOURCES)
    projects.add_task(conn, proj, "sam", "Manage Lantern Replies")
    other_t = projects.task_by_title(conn, proj, "Manage Lantern Replies")
    projects.set_task(conn, other_t["id"], sense="steam_comment",
                      sense_item=BOTH_A["id"], known_senses=watch.SOURCES)
    projects.notice_from_event(conn, dict(one, said=one.get("said")))
    got_mine = projects.notices_of(conn, mine_t["id"])
    check("one notice on the task bound to its id",
          len(got_mine) == 1 and ONLY["label"] in got_mine[0]["said"],
          got_mine)
    check("and none on the other mod's task",
          projects.notices_of(conn, other_t["id"]) == [])
    check("the notice names the sense it came from",
          got_mine and got_mine[0]["source"] == "steam_comment", got_mine)
    look = projects.look_for(dict(mine_t, sense="steam_comment",
                                  sense_item=ONLY["id"]), looks)
    check("the task's page reads its own item's last look",
          (look or {}).get("item") == ONLY["id"], look)

    # The label binds too -- it is what `comments.py` and the prompt speak in.
    projects.add_task(conn, proj, "sam", "Manage Replies By Name")
    byname = projects.task_by_title(conn, proj, "Manage Replies By Name")
    projects.set_task(conn, byname["id"], sense="steam_comment",
                      sense_item=ONLY["label"], known_senses=watch.SOURCES)
    projects.notice_from_event(conn, dict(one))
    check("a task bound to its label gets the notice too",
          len(projects.notices_of(conn, byname["id"])) == 1,
          projects.notices_of(conn, byname["id"]))
    check("and the other mod's task still has none",
          projects.notices_of(conn, other_t["id"]) == [])

    # ------------------------------------------------------------------ 7
    print("\n7. reading its thread and answering on it")
    read_urls.clear()
    thread = comments.read(ONLY["label"], "steam", limit=10)
    check("the read went to its own render endpoint and no other",
          read_urls == [watch.STEAM_COMMENT_URL_TMPL.format(
              item_id=ONLY["id"])], read_urls)
    check("it comes back as this mod, on Steam",
          thread["mod"] == ONLY["label"] and thread["where"] == "steam")
    check("and links its own Workshop page",
          thread["page"]
          == comments.STEAM_ITEM_URL.format(item_id=ONLY["id"]), thread["page"])
    check("a stranger's comment is there, whole, line breaks kept",
          thread["comments"]
          and thread["comments"][0]["author"] == "skirmisher_fan"
          and "\n1.4.8" in thread["comments"][0]["text"],
          thread["comments"][:1])
    check("the owner's own reply is dropped and counted",
          thread.get("his_own_hidden") == 1 and all(
              c["author"] != OWNER_ON_STEAM for c in thread["comments"]),
          thread)
    check("every comment comes back fenced as borrowed words",
          all(c.get("quoted") for c in thread["comments"]))
    check("and the fence names this mod's page",
          all(ONLY["label"] in c["quoted"] for c in thread["comments"]))
    kept = comments.read(ONLY["label"], "steam", limit=10, mine=True)
    check("asking for the owner's own keeps them",
          any(c["author"] == OWNER_ON_STEAM for c in kept["comments"]))

    draft = ("Two stacks should outrank a single pilum -- that is what the "
             "tiers do. I will pass the minimum-setting idea on. " + SIGN_OFF)
    out = comments.post(ONLY["label"], "steam", draft)
    check("the post went to its own item", posted == [(ONLY["id"], draft)],
          posted)
    check("it comes back as this mod on Steam",
          out["mod"] == ONLY["label"] and out["where"] == "steam", out)
    check("the draft went exactly as written, nothing trimmed or appended",
          out["chars"] == len(draft))
    ledger = [json.loads(l) for l in
              comments.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    check("the post is written down with its words",
          any(l.get("posted") and l.get("mod") == ONLY["label"]
              and l.get("text") == draft for l in ledger), ledger)
    acts = [json.loads(l) for l in
            watch.LEDGER_PATH.read_text(encoding="utf-8").splitlines()]
    acts = [a for a in acts if "posted a reply" in (a.get("outcome") or "")]
    check("and leaves one Activity line for the tab", len(acts) == 1, acts)
    check("the Activity line carries its item and label, so it lands right",
          acts and (acts[0].get("meta") or {}).get("item") == ONLY["id"]
          and (acts[0]["meta"]).get("label") == ONLY["label"]
          and acts[0]["source"] == "steam_comment", acts)
    try:
        comments.post(ONLY["label"], "steam", "no sign-off here")
        check("a draft without the sign-off is refused", False)
    except comments.Refused as exc:
        check("a draft without the sign-off is refused, not repaired",
              SIGN_OFF in str(exc), exc)
    check("and it really did not send", len(posted) == 1, posted)

    # ------------------------------------------------------------------ 8
    print("\n8. the mods on both platforms, exactly as they were")
    read_urls.clear()
    both = comments.read(BOTH_A["label"], "steam", limit=10)
    check("the dual-platform mod still reads on Steam, from its own item",
          read_urls == [watch.STEAM_COMMENT_URL_TMPL.format(
              item_id=BOTH_A["id"])], read_urls)
    check("and comes back as itself", both["mod"] == BOTH_A["label"])
    nexus_reached.clear()
    on_nexus = comments.read(BOTH_B["label"], "nexus", limit=10)
    check("and still reads on Nexus, through the sense's own fetch",
          nexus_reached == ["20002"], nexus_reached)
    check("coming back as itself on Nexus",
          on_nexus["mod"] == BOTH_B["label"]
          and on_nexus["where"] == "nexus", on_nexus)
    check("the Nexus set is still the two it always was, and not three",
          len(watch.NEXUS_ITEMS) == 2
          and ONLY["label"] not in [i["label"] for i in watch.NEXUS_ITEMS],
          watch.NEXUS_ITEMS)
    check("the poster speaks on three items now and on nothing else",
          set(steam_post.ITEMS) == {BOTH_A["id"], BOTH_B["id"], ONLY["id"]},
          sorted(steam_post.ITEMS))
    check("the day's ceiling is still across both platforms together",
          comments.POSTS_PER_DAY == 5 and
          comments.summary()["posts_left_today"]
          == comments.POSTS_PER_DAY - 1, comments.summary())
    check("and the standing view now names all three",
          len(comments.summary()["mods"]) == 3)

    conn.close()
    if FAILED:
        print("\n" + str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("\nevery rule held")


if __name__ == "__main__":
    main()
