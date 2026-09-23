"""Lean on the specialist roles without sending anybody. Run over a scratch
folder:

    python -m server.test_specialists C:\\somewhere\\scratch

Nothing here dispatches, spends, or touches the live room. The rules under
test come from the phase 3 design: a specialist is a base role wearing a
standing brief -- same tools, same caps, same veto by construction -- and
each specialty's standing word is its own, so a first page proves WHICH
brief a hand was given.
"""

import json
import sys
from pathlib import Path

from . import worker


FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok " if ok else "FAIL") + "  " + name
          + (("  -- " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def main():
    if len(sys.argv) < 2:
        print("give me a scratch folder; I will not run over the live data")
        sys.exit(2)
    scratch = Path(sys.argv[1]).resolve()
    live = Path(__file__).resolve().parent.parent / "data"
    if scratch in (live, live.parent):
        print("that is the live room; give me a scratch folder")
        sys.exit(2)
    scratch.mkdir(parents=True, exist_ok=True)

    print("specialists, leant on over " + str(scratch))

    # 1. The roles are there, and the specialties know themselves.
    check("builder and researcher are roles",
          "builder" in worker.ROLES and "researcher" in worker.ROLES)
    check("the specialties name exactly the two of them",
          sorted(worker.SPECIALTIES) == ["builder", "researcher"])
    check("angel and reader are not specialties",
          not worker.ROLES["angel"].get("specialty")
          and not worker.ROLES["reader"].get("specialty"))

    # 2. A specialty is its base in everything but the brief and the word.
    b, a = worker.ROLES["builder"], worker.ROLES["angel"]
    check("the builder runs in the angel's shape",
          b["permission_mode"] == "bypassPermissions"
          and b["where"] == "worktree" and b["sizes"] is a["sizes"]
          and b["stall"] == a["stall"] and b["must_have"] == a["must_have"])
    r, rd = worker.ROLES["researcher"], worker.ROLES["reader"]
    check("the researcher runs in the reader's shape",
          r["permission_mode"] == "dontAsk" and r["where"] == "root"
          and r["sizes"] is rd["sizes"] and sorted(r["tools"]) == sorted(rd["tools"]))

    # 3. Every brief file is on disk, and every briefed role's word is in
    # its own composed text -- a word the text does not carry is one the
    # hand cannot know.
    for role in ("angel", "builder", "researcher"):
        shape = worker.ROLES[role]
        missing = [str(p) for p in shape["brief"] if not Path(p).is_file()]
        check(role + "'s brief files are on disk", not missing, missing)
        check(role + "'s word is in its brief",
              worker._brief_word(role) == shape["word"],
              worker._brief_word(role))
    check("the reader has no brief and no word",
          not worker.ROLES["reader"]["brief"]
          and worker._brief_word("reader") == "")
    words = [worker.ROLES[r]["word"] for r in ("angel", "builder", "researcher")]
    check("the three words are three different words",
          len(set(words)) == 3, words)

    # 4. The builder's composed text carries BOTH crafts, base first.
    text = worker._brief_text("builder")
    check("the builder holds the angel's brief and its own",
          "lantern" in text and "plumbline" in text)
    check("base comes before craft",
          text.find("lantern") < text.find("plumbline"))

    # 5. Money looks up the role's own table -- a builder page reserves at
    # angel prices, a researcher page at reader prices.
    check("a builder page is priced from the angel table",
          worker.page_cap_usd("builder", "medium")
          == worker.ANGEL_SIZES["medium"]["max_budget_usd"])
    check("a researcher page is priced from the reader table",
          worker.page_cap_usd("researcher", "small")
          == worker.SIZES["small"]["max_budget_usd"])
    check("a role there is not falls back to the default table",
          worker.page_cap_usd("wizard", "small")
          == worker.SIZES["small"]["max_budget_usd"])

    # 6. The veto is keyed on the shape: a builder's settings carry the
    # PreToolUse hook exactly like an angel's, and a read-shaped role's
    # carry none. This is the check that would have caught a builder
    # running unfenced.
    for role, armed in (("angel", True), ("builder", True),
                        ("reader", False), ("researcher", False)):
        where = scratch / ("settings-" + role)
        where.mkdir(parents=True, exist_ok=True)
        path = worker._settings_file(where, "echo hook", role)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        has = "PreToolUse" in cfg.get("hooks", {})
        check(role + (" carries the veto" if armed else " carries no veto"),
              has == armed)

    # 7. What brain.py keys the name rule on: hands with hands are kept and
    # talked to again, so they must be named -- builder included.
    check("the builder is a hand with hands (must be named)",
          worker.ROLES["builder"]["where"] == "worktree")
    check("the researcher is not (an errand may be nameless)",
          worker.ROLES["researcher"]["where"] != "worktree")

    # 8. The terms it reads price every role, specialties included.
    named = worker.ROLES.keys()
    check("all four roles are in the shapes its terms price",
          all(x in named for x in ("reader", "angel", "builder", "researcher")))

    # 9. The bench carve-out, added after the third identical refusal: a
    # plain server.test_* run on an outside scratch passes, and everything
    # else still stops exactly as before. The veto reads its root from the
    # environment, so it is pointed at this checkout for the duration.
    import os
    from . import worker_hook
    os.environ["ASSISTANT_ROOT"] = str(Path(__file__).resolve().parent.parent)
    os.environ["ASSISTANT_HAND_CWD"] = str(scratch)

    def bash(c):
        return worker_hook.veto({"tool_name": "Bash",
                                 "tool_input": {"command": c}})

    check("a bench on an outside scratch passes",
          bash('python -m server.test_watch "C:\\somewhere\\scratch"') == ("", ""))
    check("python.exe and /tmp pass in the exact form",
          bash("python.exe -m server.test_jobs /tmp/x") == ("", ""))
    check("any flag in front of -m still asks, even -X",
          bash("python -X utf8 -m server.test_jobs C:/tmp/x")[0] != "")
    check("the -c smuggle is refused",
          bash("python -c__import__('os') -m server.test_watch /tmp/x")[0] != "")
    check("a bench pointed inside this repo still asks",
          bash("python -m server.test_watch "
               + os.environ["ASSISTANT_ROOT"] + "\\data\\x")[0] != "")
    os.environ["ASSISTANT_HAND_CWD"] = os.environ["ASSISTANT_ROOT"] + "\\data\\worktrees\\wt"
    check("a relative scratch from a worktree resolves and still asks",
          bash("python -m server.test_watch scratch")[0] != "")
    os.environ["ASSISTANT_HAND_CWD"] = str(scratch)
    check("a relative scratch from an outside folder passes",
          bash("python -m server.test_watch scratch") == ("", ""))
    kept_root = os.environ["ASSISTANT_ROOT"]
    os.environ["ASSISTANT_ROOT"] = ""
    check("no root, no carve-out -- it fails closed",
          bash("python -m server.test_watch C:/tmp/x")[0] != "")
    os.environ["ASSISTANT_ROOT"] = kept_root
    check("a chained bench still asks if what rides with it is dangerous",
          bash("rm -rf C:/tmp/x && python -m server.test_watch C:/tmp/x")[0]
          != "")
    check("a bench with no scratch still asks",
          bash("python -m server.test_watch")[0] != "")
    check("walking up still asks",
          bash("python -m server.test_watch ..\\up")[0] != "")
    check("the rest of server.* still asks",
          bash("python -m server.backup C:/tmp/x")[0] != "")
    check("a flag after the module still asks",
          bash("python -m server.test_watch --force C:/tmp/x")[0] != "")
    check("the store is still refused by name",
          bash("type data\\store.db")[0] != "")

    # 9b. What once defeated six of these carve-outs in one night:
    # a `cd "..." && ` prefix, or a trailing `2>&1`, `| tail -80`,
    # `; echo "exit:$?"`, defeated the anchor and the underlying rule fired
    # instead of the carve-out. These are real strings of that shape, from
    # the refusals ledger -- now judged by segment, and now passing on their
    # own, with nothing else about the veto widened.
    check("a leading cd \"...\" && no longer defeats the bench carve-out",
          bash('cd "' + str(scratch) + '" && python -m server.test_watch '
               '/tmp/watch-scratch-steam 2>&1 | tail -80') == ("", ""))
    check("a bare bench with a trailing redirect and pipe now passes",
          bash("python -m server.test_watch /tmp/watch-scratch-verify "
               "2>&1 | tail -80") == ("", ""))
    check("chained tee/echo/grep bookkeeping after the bench now passes",
          bash('python -m server.test_watch /tmp/watch-scratch-verify 2>&1 '
               '| tee /tmp/watch-bench-out.txt | tail -1; '
               'echo "---counts---"; '
               'grep -c "^  ok" /tmp/watch-bench-out.txt; '
               'grep -c "FAIL" /tmp/watch-bench-out.txt') == ("", ""))
    check("a run-line quoted inside a commit message no longer trips the "
          "module rule",
          bash('git commit -m "A sixth sense: steam_comment\n\n'
               'Bench: python -m server.test_watch <scratch> -- every rule '
               'held."') == ("", ""))
    check("the same quoting trick on a non-git command still refuses -- "
          "the mask is scoped to git commit's own -m/--message",
          bash('bash -c "rm -rf /tmp/x"')[0] != "")

    # 9c. What once defeated the carve-out five times in one day: an
    # ordinary multi-line script, one statement per line
    # and no `;` in sight, which `veto` flattened to single spaces before
    # ever splitting -- so a real newline, exactly as much a separator in
    # Bash as `;` is, never became a segment boundary at all, and the
    # whole script stayed one unsplittable blob the anchored bench shape
    # could never match. This is a real string of that shape, from the
    # refusals ledger, and the shape the door names: every path
    # argument absolute and outside this repo and outside data/, nothing
    # else about the refusal narrowed.
    check("the bench line on its own, isolated by real newlines from a "
          "leading cd and a compiled-first echo, now passes",
          bash('cd "' + str(scratch) + '"\n'
               'python -m py_compile server/test_watch.py && echo "OK: '
               'compiles"\n'
               'python -m server.test_watch /tmp/ada_nexus_scratch 2>&1 '
               '| tail -40') == ("", ""))
    check("the real multi-line string, rm -rf and all, still refuses -- a "
          "newline beside the bench carries the same segment along for "
          "judgement as a `;` or `&&` would, and rm -rf is never clean",
          bash('cd "' + str(scratch) + '"\n'
               'python -m py_compile server/test_watch.py && echo "OK: '
               'compiles"\n'
               'rm -rf /tmp/ada_nexus_scratch\n'
               'python -m server.test_watch /tmp/ada_nexus_scratch 2>&1 '
               '| tail -40')[0] != "")
    os.environ["ASSISTANT_HAND_CWD"] = (
        os.environ["ASSISTANT_ROOT"] + "\\data\\worktrees\\wt")
    check("a relative scratch path on its own newline-separated line "
          "still asks -- the hand's cwd is a worktree, so a bare name "
          "resolves inside the repo",
          bash('cd "' + str(scratch) + '"\n'
               'python -m server.test_watch scratch-rel')[0] != "")
    os.environ["ASSISTANT_HAND_CWD"] = str(scratch)
    check("a path inside the repo on its own newline-separated line "
          "still asks",
          bash('echo "starting"\n'
               'python -m server.test_watch "' + os.environ["ASSISTANT_ROOT"]
               + '\\x"')[0] != "")
    check("a path under data/ on its own newline-separated line still "
          "asks",
          bash('echo "starting"\n'
               'python -m server.test_watch "' + os.environ["ASSISTANT_ROOT"]
               + '\\data\\x"')[0] != "")
    check("an allowed module run with && rm -rf appended still refuses",
          bash("python -m server.test_watch /tmp/watch-scratch-verify "
               "&& rm -rf /tmp/watch-scratch-verify")[0] != "")
    check("an allowed module run with a newline then rm -rf appended "
          "still refuses",
          bash("python -m server.test_watch /tmp/watch-scratch-verify\n"
               "rm -rf /tmp/watch-scratch-verify")[0] != "")

    # 10. Branch deletion: an angel may take its own
    # already-merged branch down, and nobody else's. `git branch` itself is
    # asked, not assumed, so this needs a real ref pointing at a commit
    # origin/main already has -- and it is cleaned up whichever way the
    # check comes out.
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=os.environ["ASSISTANT_ROOT"],
                              capture_output=True, text=True)

    canary = "ada/veto-canary-" + str(os.getpid())
    git("branch", canary, "origin/main")
    try:
        check("a merged ada/ branch may be -D'd",
              bash("git branch -D " + canary) == ("", ""))
    finally:
        git("branch", "-D", canary)
    check("a name outside ada/ still refused",
          bash("git branch -D not-ada/foo")[0] != "")
    check("an ada/ branch that does not exist still refused",
          bash("git branch -D ada/no-such-canary")[0] != "")
    check("a chained branch delete still refused if what rides with it is "
          "dangerous",
          bash("rm -rf C:/tmp/x && git branch -D ada/no-such-canary")[0]
          != "")
    canary2 = "ada/veto-canary2-" + str(os.getpid())
    git("branch", canary2, "origin/main")
    try:
        check("a real refused string (trailing ; echo) now "
              "passes",
              bash("git branch -D " + canary2 + ' 2>&1; echo "exit:$?"')
              == ("", ""))
    finally:
        git("branch", "-D", canary2)
    check("lowercase -d (git's own safe delete) is untouched",
          bash("git branch -d some-branch")[0] == "")

    # 11. Worktree removal: a hand may take its own
    # worktree down and nobody else's -- previously ungated entirely.
    os.environ["ASSISTANT_HAND_CWD"] = (
        os.environ["ASSISTANT_ROOT"] + "/data/worktrees/veto-canary-wt")
    own = os.environ["ASSISTANT_HAND_CWD"]
    check("a hand may remove its own worktree folder",
          bash("git worktree remove " + own) == ("", ""))
    check("'.' resolves to that same own worktree",
          bash("git worktree remove .") == ("", ""))
    check("someone else's worktree stays refused",
          bash("git worktree remove "
               + os.environ["ASSISTANT_ROOT"]
               + "/data/worktrees/someone-elses") != ("", ""))
    check("--force is refused -- no flags at all on this form",
          bash("git worktree remove --force " + own)[0] != "")
    check("a chained worktree remove stays refused if what rides with it "
          "is dangerous",
          bash("rm -rf C:/tmp/x && git worktree remove " + own)[0] != "")
    check("a real refused string ('.' plus trailing ; echo) "
          "now passes",
          bash('git worktree remove . 2>&1; echo "exit:$?"') == ("", ""))
    check("a real refused string (own path plus trailing "
          "; echo) now passes",
          bash("git worktree remove " + own + ' 2>&1; echo "exit:$?"')
          == ("", ""))
    check("cd'ing to root then naming the worktree relative to root still "
          "refuses -- the cd's destination is not trusted for resolving it",
          bash('cd "' + os.environ["ASSISTANT_ROOT"] + '" && git worktree remove '
               '"data/worktrees/veto-canary-wt"')[0] != "")
    os.environ["ASSISTANT_HAND_CWD"] = str(scratch)
    check("removing its own cwd is still refused when that cwd is not "
          "under data/worktrees at all",
          bash("git worktree remove " + str(scratch))[0] != "")
    os.environ["ASSISTANT_HAND_CWD"] = own

    if FAILED:
        print(str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("every rule held")


if __name__ == "__main__":
    main()
