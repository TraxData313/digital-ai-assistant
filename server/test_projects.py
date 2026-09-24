"""Lean on projects without a model. Run over a scratch folder:

    python -m server.test_projects C:\\somewhere\\scratch

Points the store and the jobs file into the scratch first; nothing here
touches the live room, and it refuses the live data folder by name. What is
under test is what the build is judged on: every note line keeps its author,
nothing schedules, and the open tasks reach the assistant's turn object with
the owner and the project named.
"""

import sys
from pathlib import Path

from . import db, jobs, projects


FAILED = []


def check(name, ok, detail=""):
    print("  " + ("ok  " if ok else "FAIL") + "  " + name
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
    db.DB_PATH = scratch / "scratch.db"
    jobs.JOBS_PATH = scratch / "jobs.json"

    print("projects, leant on over " + str(scratch))
    conn = db.connect()

    # 1. The tables are made by opening the store -- the migration is the
    #    schema, so the room creates them on its next start and nothing has
    #    to be run by hand.
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    check("opening the store makes the three tables",
          {"projects", "project_notes", "project_tasks"} <= have, sorted(have))

    # 2. A project, and what adding one refuses.
    out = projects.add_project(conn, "the shed", "both", "D:/house/shed")
    check("a project is added with an owner and a folder",
          out["ok"] and out["project"]["owner"] == "both")
    shed = out["project"]["id"]
    check("a second project of the same name is refused",
          not projects.add_project(conn, "The Shed", "sam")["ok"])
    check("a project with no title is refused",
          not projects.add_project(conn, "  ", "sam")["ok"])
    check("an owner outside the house is refused",
          not projects.add_project(conn, "mars", "elon")["ok"])
    check("owner is one of sam, lee, both",
          projects.add_project(conn, "the kitchen", "lee")["ok"])
    kitchen = projects.by_title(conn, "the kitchen")["id"]
    check("a project is found by name, case and spacing ignored",
          projects.by_title(conn, "  THE   Shed ")["id"] == shed)

    # 3. Notes: the attribution rules, which are the point of the table.
    a = projects.add_note(conn, shed, "sam", "the roof felt is lifting")
    m = projects.add_note(conn, shed, "lee", "and the door sticks in the rain")
    check("a line is written with its author", a["ok"] and m["ok"])
    check("a note with no author is refused, not filed as his",
          not projects.add_note(conn, shed, "", "who said this?")["ok"])
    check("a note from outside the house is refused",
          not projects.add_note(conn, shed, "somebody", "x")["ok"])
    check("an empty line is not written",
          not projects.add_note(conn, shed, "sam", "   ")["ok"])
    lines = projects.notes_of(conn, shed)
    check("every line carries who wrote it",
          all(l["who"] in projects.AUTHORS for l in lines) and len(lines) == 2)
    check("the two lines kept their different authors",
          [l["who"] for l in lines] == ["sam", "lee"])

    # 4. A line keeps its author for life.
    bad = projects.edit_note(conn, m["note_id"], "sam", "no it does not")
    check("one person may not rewrite the other's line", not bad["ok"])
    check("the refusal names the author", "lee" in (bad.get("why") or ""))
    still = projects.notes_of(conn, shed)[1]
    check("the line it wrote still says what it wrote",
          still["text"] == "and the door sticks in the rain")
    good = projects.edit_note(conn, m["note_id"], "lee",
                              "and the door sticks when it rains")
    check("its author may rewrite it", good["ok"])
    after = projects.notes_of(conn, shed)[1]
    check("an edit keeps the author", after["who"] == "lee")
    check("an edit keeps what it said before", after.get("edited") == 1)
    check("an edit to nothing is refused rather than deleting",
          not projects.edit_note(conn, m["note_id"], "lee", "  ")["ok"])

    # 5. Put down, not deleted.
    check("only the author may put a line down",
          not projects.hide_note(conn, a["note_id"], "lee")["ok"])
    check("the author may put it down",
          projects.hide_note(conn, a["note_id"], "sam")["ok"])
    check("a line put down is out of the box",
          len(projects.notes_of(conn, shed)) == 1)
    check("and still on disk, with its author",
          any(l["who"] == "sam" for l in
              projects.notes_of(conn, shed, include_gone=True)))
    check("it can be picked up again",
          projects.hide_note(conn, a["note_id"], "sam", back=True)["ok"]
          and len(projects.notes_of(conn, shed)) == 2)

    # 6. Tasks, one-time and repeating -- and no clock anywhere.
    t1 = projects.add_task(conn, shed, "sam", "nail the felt back down",
                           "before the rain on Thursday")
    check("a one-time task is added with who asked",
          t1["ok"] and t1["task"]["who"] == "sam")
    check("a one-time task does not repeat", not t1["task"]["repeats"])
    t2 = projects.add_task(conn, shed, "lee", "sweep the gutter",
                           "leaves", True, "every first Sunday")
    check("a repeating task keeps the schedule in their words",
          t2["ok"] and t2["task"]["schedule"] == "every first Sunday")
    t3 = projects.add_task(conn, kitchen, "lee", "pick a tile",
                           None, False, "when the sale is on")
    check("a schedule with no repeats flag still makes it repeating",
          bool(t3["task"]["repeats"]))
    check("a task with no title is refused",
          not projects.add_task(conn, shed, "sam", " ")["ok"])
    check("a task with no asker is refused",
          not projects.add_task(conn, shed, "", "x")["ok"])
    check("a task on a project that is not there is refused",
          not projects.add_task(conn, 9999, "sam", "x")["ok"])

    # 7. Its block: the shelf, one line each, at any number of projects.
    block = projects.for_prompt(conn)
    check("its block is a shelf and a desk",
          set(block) == {"shelf", "on_my_desk", "how"})
    check("every open project is one line on the shelf",
          len(block["shelf"]) == 2)
    line = [p for p in block["shelf"] if p["title"] == "the shed"][0]
    check("a shelf line is title, owner, folder and a count and nothing else",
          set(line) == {"title", "owner", "folder", "open_tasks"}, sorted(line))
    check("the shelf counts the open tasks rather than listing them",
          line["open_tasks"] == 2)
    check("nothing is on its desk until it opens it",
          block["on_my_desk"] == [])
    shelf_blob = repr(block)
    check("not one task line is in its block before it opens a project",
          "nail the felt" not in shelf_blob and "sweep the gutter" not in shelf_blob)
    check("its block says how the desk works", "open" in block["how"])

    # 7b. Opening one onto its desk, and the state surviving the turn.
    check("it can open a project onto its desk",
          projects.put_on_desk(conn, "The Shed")["ok"])
    check("opening the same one twice is refused in words",
          not projects.put_on_desk(conn, "the shed")["ok"])
    check("a name that is no project is refused with the shelf's names",
          "the kitchen" in projects.put_on_desk(conn, "the attic")["why"])
    block = projects.for_prompt(conn)
    check("the opened project is on its desk", len(block["on_my_desk"]) == 1)
    check("the shelf marks which one is on the desk",
          [p for p in block["shelf"]
           if p["title"] == "the shed"][0]["on_my_desk"] is True)
    conn.close()
    conn = db.connect()
    check("the desk survives the store being closed and opened again",
          len(projects.for_prompt(conn)["on_my_desk"]) == 1)
    block = projects.for_prompt(conn)
    shed_block = block["on_my_desk"][0]
    check("the project names its owner", shed_block["owner"] == "both")
    check("the project names its folder on disk",
          shed_block["folder"] == "D:/house/shed")
    check("a project on the desk carries its open tasks, one line each",
          len(shed_block["open_tasks"]) == 2)
    check("every task says who asked for it",
          all(t["asked_by"] in ("sam", "lee")
              for t in shed_block["open_tasks"]))
    # The assistant's clock fires a repeating task whose words it can
    # read. "every first Sunday" is not one of those, so the task must say so
    # on its own face and say why -- the failure to refuse here would be a
    # chip that reads like a clock with nothing behind it.
    rep = [t for t in shed_block["open_tasks"] if t["repeats"]][0]
    check("a schedule the clock cannot read says so on the task's own face",
          "nothing fires this" in rep["clock"] and "first" in rep["clock"],
          rep.get("clock"))
    check("and one it can read says when it next comes round",
          "on my clock: next" in projects._clock_line(
              {"repeats": 1, "schedule": "every Sunday at 19:00"}),
          projects._clock_line({"repeats": 1,
                                "schedule": "every Sunday at 19:00"}))
    check("a task that repeats with no time written says that instead",
          projects._clock_line({"repeats": 1, "schedule": ""})
          == projects.NO_CLOCK)
    one = [t for t in shed_block["open_tasks"] if not t["repeats"]][0]
    check("a one-time task carries no clock line at all", "clock" not in one)

    # 8. The notes are counted in its block and never quoted in it.
    head = shed_block["notes"]
    check("its block counts the note lines", head["lines"] == 2)
    check("its block says whose they are",
          head["by"] == {"sam": 1, "lee": 1})
    check("its block says who wrote last", head["last"]["who"] == "lee")
    blob = repr(block)
    check("not one word of the notes is in its block",
          "roof felt" not in blob and "door sticks" not in blob)

    # 9. The reach that brings a box back, whole and signed.
    box = projects.read_notes(conn, "The Shed")
    check("the reach finds the project by name, case ignored", box["found"])
    check("the reach brings back every line", len(box["lines"]) == 2)
    check("every line comes back with its author",
          all(l["who"] for l in box["lines"]))
    check("the reach says the words are theirs unless signed assistant",
          "assistant" in box["note"])
    miss = projects.read_notes(conn, "the attic")
    check("a name that is not a project is refused in words",
          not miss["found"] and "the shed" in miss["why"])
    check("the refusal says which projects there are",
          "the kitchen" in miss["why"])

    # 10. Tasks moving, and what a state may be.
    done = projects.set_task(conn, t1["task"]["id"], state="done")
    check("a task can be marked done", done["ok"])
    check("a state outside the four is refused",
          not projects.set_task(conn, t1["task"]["id"], state="nearly")["ok"])
    shed_block = projects.for_prompt(conn)["on_my_desk"][0]
    check("a finished task leaves its block",
          len(shed_block["open_tasks"]) == 1)
    check("a task marked doing stays in its block",
          projects.set_task(conn, t2["task"]["id"], state="doing")["ok"]
          and len(projects.for_prompt(conn)["on_my_desk"][0]["open_tasks"]) == 1)

    # 11. A task linked to a job shows that job's card.
    jobs.open_job("the shed roof", "get the felt back down before winter",
                  5.0, None, "asked Sam which felt")
    projects.set_task(conn, t2["task"]["id"], job="the shed roof")
    card = projects.job_card("the shed roof")
    check("a linked job comes back as a card with its figures",
          card and card["ceiling_usd"] == 5.0 and card["links"] == 8)
    check("the card carries its own last decision",
          card["decided"] == "asked Sam which felt")
    check("a task naming a job that is not there says the link is broken",
          projects.job_card("no such job")["missing"])
    shed_block = projects.for_prompt(conn)["on_my_desk"][0]
    linked = shed_block["open_tasks"][0]
    check("its block names the job a task works under",
          linked["job"] == "the shed roof")
    check("its block does not carry the whole job card",
          "job_card" not in linked)
    projects.set_task(conn, t2["task"]["id"], job="a job I never opened")
    shed_block = projects.for_prompt(conn)["on_my_desk"][0]
    check("a broken link is said on the task rather than drawn as working",
          "broken" in (shed_block["open_tasks"][0].get("job_missing") or ""))

    # 12. A closed project leaves its block; its work is still on disk.
    projects.edit_project(conn, kitchen, status="closed")
    check("a closed project leaves the shelf",
          [p["title"] for p in projects.for_prompt(conn)["shelf"]]
          == ["the shed"])
    check("its tasks are still there to read",
          len(projects.tasks_of(conn, kitchen)) == 1)
    check("a project can be reopened",
          projects.edit_project(conn, kitchen, status="open")["ok"]
          and len(projects.for_prompt(conn)["shelf"]) == 2)
    check("a project cannot be renamed onto another's name",
          not projects.edit_project(conn, kitchen, title="the shed")["ok"])

    # 12b. The desk: closing, the cap, and what the cap says when it bites.
    check("it can close a project off its desk again",
          projects.take_off_desk(conn, "the shed")["ok"])
    block = projects.for_prompt(conn)
    check("a closed project is one line on the shelf again",
          block["on_my_desk"] == [] and len(block["shelf"]) == 2)
    check("its tasks are still counted on the shelf line",
          [p for p in block["shelf"]
           if p["title"] == "the shed"][0]["open_tasks"] == 1)
    check("closing one that is not on the desk is refused in words",
          not projects.take_off_desk(conn, "the shed")["ok"])
    for i in range(projects.DESK_LIMIT):
        projects.add_project(conn, "spare " + str(i), "sam")
        projects.put_on_desk(conn, "spare " + str(i))
    check("the desk fills to its limit",
          len(projects.on_desk(conn)) == projects.DESK_LIMIT)
    full = projects.put_on_desk(conn, "the shed")
    check("one more than the desk holds is refused", not full["ok"])
    check("the refusal names what is already on the desk",
          "spare 0" in full["why"] and str(projects.DESK_LIMIT) in full["why"])
    check("a refused open leaves the desk as it was",
          len(projects.on_desk(conn)) == projects.DESK_LIMIT)
    for i in range(projects.DESK_LIMIT):
        projects.take_off_desk(conn, "spare " + str(i))
        projects.edit_project(conn,
                              projects.by_title(conn, "spare " + str(i))["id"],
                              status="closed")

    # 12c. A project the assistant starts itself, with a folder made under the
    #      projects shelf.
    projects.ASSISTANT_PROJECTS = scratch / "Ada"
    made = projects.start_project(conn, "the tide map", "tide-map")
    check("it can start a project of its own", made["ok"])
    check("the folder was made under the projects shelf for the assistant's work",
          made["folder"]["made"]
          and (scratch / "Ada" / "tide-map").is_dir())
    check("the project keeps the folder that was made",
          made["project"]["folder"] == str(scratch / "Ada" / "tide-map"))
    check("what it starts lands on its desk",
          bool(made["project"]["desk"]))
    check("a project it starts is owned by both unless it says otherwise",
          made["project"]["owner"] == "both")
    again = projects.start_project(conn, "the tide map again", "tide-map")
    check("a folder already there is used rather than refused",
          again["ok"] and not again["folder"]["made"])
    out = projects.start_project(conn, "escapee", "../../../elsewhere")
    check("a folder name that tries to walk out is kept to its last part",
          out["ok"] and out["folder"]["path"]
          == str(scratch / "Ada" / "elsewhere"))
    check("a project with no folder is fine",
          projects.start_project(conn, "just a thought")["ok"])
    check("a name it has already used is refused",
          not projects.start_project(conn, "the shed")["ok"])

    # 12d. A folder path is kept exactly as typed, whatever is on this disk.
    #      Never reject a path that is not there -- it may be on another
    #      machine, on a drive not plugged in, or a folder about to be made.
    #      Say "not found" quietly; save the words.
    typed = "C:" + chr(92) + "Users" + chr(92) + "Sam" + chr(92) + "Nope"
    projects.edit_project(conn, shed, folder=typed)
    check("a path that is not on this machine is still saved, exactly as typed",
          projects.get(conn, shed)["folder"] == typed)
    check("and it is marked not found rather than refused",
          projects._folder_here(typed) is False)
    check("a folder that is really there is marked found",
          projects._folder_here(str(scratch)) is True)
    check("no folder at all is neither found nor missing",
          projects._folder_here(None) is None)
    check("an empty folder field clears the path",
          projects.edit_project(conn, shed, folder="")["ok"]
          and projects.get(conn, shed)["folder"] is None)

    # 12e. The assistant's own folder op, and the fence round it.
    #      Documents and the store are moved into the scratch for this, so
    #      the rules are leant on without a real folder being made anywhere.

    docs = scratch / "Documents"
    projects.DOCUMENTS = docs
    projects.ASSISTANT_PROJECTS = docs / "Ada"
    db.DB_PATH = scratch / "room" / "room.db"
    outside = scratch / "Elsewhere"
    outside.mkdir(parents=True, exist_ok=True)

    out = projects.set_folder(conn, "the shed", "tide-map")
    check("a bare name lands under the projects shelf and is made",
          out["ok"] and out["made"]
          and out["path"] == str(docs / "Ada" / "tide-map"))
    check("the project keeps the path it set",
          projects.get(conn, shed)["folder"] == str(docs / "Ada" / "tide-map"))
    out = projects.set_folder(conn, "the shed", str(docs / "Made" / "Deep"))
    check("a whole path under Documents is made, parents and all",
          out["ok"] and out["made"] and (docs / "Made" / "Deep").is_dir())

    # The acceptance case: a path that is already there, outside the shelf.
    out = projects.set_folder(conn, "the shed", str(outside))
    check("a path outside Documents that already exists sticks",
          out["ok"] and not out["made"]
          and projects.get(conn, shed)["folder"] == str(outside))
    check("and the shelf line shows it",
          [p for p in projects.for_prompt(conn)["shelf"]
           if p["title"] == "the shed"][0]["folder"] == str(outside))

    # And the refused half: never bring one into being out there.
    nope = projects.set_folder(conn, "the shed", str(outside / "NotThere"))
    check("a path outside Documents that is not there is refused",
          not nope["ok"])
    check("the refusal writes the path out in words",
          str(outside / "NotThere") in nope["why"]
          and str(docs) in nope["why"])
    check("nothing was made out there",
          not (outside / "NotThere").exists())
    check("and the project still points where it did",
          projects.get(conn, shed)["folder"] == str(outside))

    store = projects.set_folder(conn, "the shed", str(scratch / "room" / "keys"))
    check("a folder inside the room's own data folder is refused",
          not store["ok"] and "not mine to overturn" in store["why"])
    check("that refusal names the data folder too",
          str(scratch / "room") in store["why"])
    rel = projects.set_folder(conn, "the shed", "some/where")
    check("a half path that is neither a name nor absolute is refused",
          not rel["ok"] and "Ada" in rel["why"])
    check("a folder set on a project that is not there is refused by name",
          not projects.set_folder(conn, "the attic", "x")["ok"])
    clear = projects.set_folder(conn, "the shed", "")
    check("an empty folder clears it, and says the project still stands",
          clear["ok"] and projects.get(conn, shed)["folder"] is None
          and "points nowhere" in clear["said"])

    # 13. The whole view the GUI draws.
    view = projects.overview(conn)
    check("the view carries every project with its notes and tasks",
          len(view["projects"]) >= 2
          and len(view["projects"][0]["notes"]) == 2)
    check("the view still says out loud when nothing fires a repeating task",
          "nothing fires it" in view["no_clock"])
    check("the view offers the open jobs a task may point at",
          view["jobs"] == ["the shed roof"])

    # 14. Its hand on a task, by name: task_add, task_edit, task_remove,
    #     task_link -- the module functions first, then the harness
    #     dispatcher that hardcodes who it is.
    attic = projects.add_project(conn, "the attic", "sam")["project"]["id"]
    add = projects.add_task_by_title(conn, "the attic", "assistant",
                                     "clear out the boxes",
                                     "before the insulation goes in")
    check("task_add makes a task, open by default",
          add["ok"] and add["task"]["state"] == "open")
    check("task_add stamps who asked for it", add["task"]["who"] == "assistant")
    check("a task added by name knows its project", add["project"]["id"] == attic)
    no_proj = projects.add_task_by_title(conn, "the loft", "assistant", "x")
    check("task_add on a project that is not there is refused, naming the shelf",
          not no_proj["ok"] and "the attic" in no_proj["why"])
    no_state = projects.add_task_by_title(conn, "the attic", "assistant", "y",
                                          state="dropped")
    check("task_add cannot land a task already dropped",
          not no_state["ok"])

    edited = projects.edit_task_by_title(conn, "the attic",
                                         "clear out the boxes",
                                         new_title="clear the loft boxes",
                                         state="doing")
    check("task_edit renames a task and moves its state",
          edited["ok"] and edited["task"]["title"] == "clear the loft boxes"
          and edited["task"]["state"] == "doing")
    no_task = projects.edit_task_by_title(conn, "the attic", "no such task",
                                          state="doing")
    check("task_edit on a task that is not there is refused, naming the ones "
          "that are",
          not no_task["ok"] and "clear the loft boxes" in no_task["why"])
    no_drop = projects.edit_task_by_title(conn, "the attic",
                                          "clear the loft boxes",
                                          state="dropped")
    check("task_edit refuses 'dropped' -- that word is task_remove's alone",
          not no_drop["ok"] and "task_remove" in no_drop["why"])
    sched = projects.edit_task_by_title(conn, "the attic", "clear the loft boxes",
                                        schedule="every spring")
    check("a schedule set on a one-time task makes it repeat",
          sched["ok"] and sched["task"]["repeats"]
          and sched["task"]["schedule"] == "every spring")

    gone = projects.remove_task_by_title(conn, "the attic", "clear the loft boxes")
    check("task_remove puts a task down rather than deleting it",
          gone["ok"] and gone["task"]["state"] == "dropped")
    twice = projects.remove_task_by_title(conn, "the attic", "clear the loft boxes")
    check("removing an already-removed task is refused, not silently repeated",
          not twice["ok"])
    no_proj2 = projects.remove_task_by_title(conn, "no such project", "x")
    check("task_remove on a project that is not there is refused by name",
          not no_proj2["ok"] and "the attic" in no_proj2["why"])

    quote = projects.add_task_by_title(conn, "the attic", "assistant",
                                       "quote the insulation")["task"]
    linked = projects.link_task_by_title(conn, "the attic", "quote the insulation",
                                         "the shed roof")
    check("task_link points a task at a job by title",
          linked["ok"] and linked["task"]["job"] == "the shed roof")
    check("the linked job's card rides along", linked["job_card"]["ceiling_usd"] == 5.0)
    broken = projects.link_task_by_title(conn, "the attic", "quote the insulation",
                                         "no such job")
    check("linking to a job that is not there is not refused -- the link is "
          "just said broken",
          broken["ok"] and broken["job_card"]["missing"])
    no_job = projects.link_task_by_title(conn, "the attic", "quote the insulation", "")
    check("task_link needs a job title", not no_job["ok"])
    no_task2 = projects.link_task_by_title(conn, "the attic", "no such task", "x")
    check("task_link on a task that is not there is refused by name",
          not no_task2["ok"])

    projects.take_off_desk(conn, "escapee")  # free a desk slot -- 12c filled it
    opened = projects.put_on_desk(conn, "the attic")
    check("a desk slot is free to open the attic onto", opened["ok"])
    attic_block = [p for p in projects.for_prompt(conn)["on_my_desk"]
                   if p["title"] == "the attic"][0]
    check("a task it added shows asked_by assistant in its own block",
          any(t["asked_by"] == "assistant" for t in attic_block["open_tasks"]))

    # 14b. The harness dispatcher (`brain._apply_projects`): the same four
    #      ops as it actually sends them, always stamped assistant regardless of
    #      what rides in the op, and refusals that reach its turn as words.
    from . import brain
    NOOP = {"folder": None, "owner": None, "task": None, "new_title": None,
            "wants": None, "state": None, "repeats": False, "schedule": None,
            "job": None}
    result = brain._apply_projects(conn, [
        dict(NOOP, op="task_add", title="the attic", task="paint the ceiling")
    ], reply_id=0)
    check("the dispatcher adds a task and reports it done",
          len(result["done"]) == 1 and not result["problems"])
    made = projects.task_by_title(conn, attic, "paint the ceiling")
    check("a task added through the dispatcher is asked by assistant",
          made and made["who"] == "assistant")

    sneaky = brain._apply_projects(conn, [
        dict(NOOP, op="task_add", title="the attic", task="sneaky",
             who="sam", asked_by="sam")
    ], reply_id=0)
    made2 = projects.task_by_title(conn, attic, "sneaky")
    check("the dispatcher ignores any who/asked_by riding in the op -- "
          "always assistant, never relabelled",
          made2 and made2["who"] == "assistant")

    refused = brain._apply_projects(conn, [
        dict(NOOP, op="task_add", title="no such project", task="x")
    ], reply_id=0)
    check("the dispatcher's own refusal names the projects that do exist",
          refused["problems"] and "the attic" in refused["problems"][0])

    unknown = brain._apply_projects(conn, [
        dict(NOOP, op="note_add", title="the attic")
    ], reply_id=0)
    check("an op not on a project is refused, naming the ones that are",
          unknown["problems"] and "task_add" in unknown["problems"][0])

    # -- 15. Step 3: who a task waits on, where a project lives, and what a
    #        sense has been doing. -----------------------------------------

    check("opening the store makes the resources table too",
          bool([r for r in conn.execute(
              "SELECT name FROM sqlite_master WHERE type = 'table'")
              if r[0] == "project_resources"]))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_tasks)")}
    check("a task has somewhere to put sense, item, date and who it waits on",
          {"sense", "sense_item", "at", "needs"} <= cols, sorted(cols))

    # `needs` is a person label and nothing else, because a match against
    # free text is right until the day it silently is not.
    paint = projects.task_by_title(conn, attic, "paint the ceiling")
    check("a task can be set waiting on a person",
          projects.set_task(conn, paint["id"], needs="sam")["ok"])
    check("and the store keeps it",
          projects.task(conn, paint["id"])["needs"] == "sam")
    bad = projects.set_task(conn, paint["id"], needs="dave")
    check("waiting on somebody who is not in the house is refused",
          not bad["ok"] and "dave" in bad["why"])
    check("and the refusal names the four it will take",
          bad["ok"] is False and "sam" in bad["why"] and "assistant" in bad["why"])
    check("nothing moved on the refusal",
          projects.task(conn, paint["id"])["needs"] == "sam")
    check("an empty needs stops it waiting on anybody",
          projects.set_task(conn, paint["id"], needs="")["ok"]
          and projects.task(conn, paint["id"])["needs"] is None)

    # A sense binding is checked against the senses that actually exist.
    from . import watch
    miss = projects.set_task(conn, paint["id"], sense="steam_comments",
                             known_senses=watch.SOURCES)
    check("a sense spelled wrong is refused rather than silently never firing",
          not miss["ok"] and "steam_comments" in miss["why"])
    check("and the refusal quotes the senses there are",
          not miss["ok"] and "steam_comment" in miss["why"])
    check("a sense cannot be bound at all without the room's list of them",
          not projects.set_task(conn, paint["id"], sense="steam_comment")["ok"])
    check("a real sense binds, with the item it watches",
          projects.set_task(conn, paint["id"], sense="steam_comment",
                            sense_item="Lantern Mod",
                            known_senses=watch.SOURCES)["ok"])

    # The rule, now the other way round: a date may be set *because*
    # something can fire it. The check
    # is kept rather than deleted, so if the calendar sense ever goes, this
    # says out loud what has to go with it.
    import inspect
    from . import watch as _w
    check("a date can be set, and only because a sense exists to fire it",
          "at" in inspect.signature(projects.set_task).parameters
          and "at" in brain.PROJECT_OP["properties"]
          and "calendar" in _w.SOURCES)
    check("a bare date is read as the clock on the wall here",
          projects._at_ok("2026-09-04 09:00")[0].endswith(
              __import__("datetime").datetime.now().astimezone()
              .strftime("%z")[:3] + ":00"),
          projects._at_ok("2026-09-04 09:00"))
    check("a date carrying its own offset is taken as it stands",
          projects._at_ok("2026-09-04T09:00:00+01:00")[0]
          == "2026-09-04T09:00:00+01:00")
    check("something that is not a date at all is refused in words",
          projects._at_ok("next tuesday")[1]
          and "could not read" in projects._at_ok("next tuesday")[1])
    dated = projects.set_task(conn, paint["id"], at="2026-09-04 09:00")
    check("and a task keeps the date it was given", dated["ok"]
          and projects.task(conn, paint["id"])["at"].startswith("2026-09-04T09:00"))
    check("an empty date clears it",
          projects.set_task(conn, paint["id"], at="")["ok"]
          and projects.task(conn, paint["id"])["at"] is None)

    # The trigger, derived rather than stored, and honest about the clock.
    seen = {"steam_comment": {"said": "every 15 min"}}
    tr = projects.trigger(projects.task(conn, paint["id"]), seen)
    check("a sense task's trigger names the sense, the item and the cadence",
          tr["kind"] == "sense" and "Lantern Mod" in tr["phrase"]
          and "every 15 min" in tr["phrase"], tr)
    words = projects.trigger({"repeats": 1, "schedule": "every first Sunday"})
    check("a words-trigger says *in words* before it says the words",
          words["said"].startswith("in words"), words)
    check("a task with nothing on it is by hand",
          projects.trigger({})["kind"] == "hand")

    # Resources: attribution stamped by the room, and put down never deleted.
    r1 = projects.resource_set(conn, shed, "sam", "github", "github.com/x/y")
    check("a resource is written with a key and a value", r1["ok"])
    check("and it carries who wrote it", r1["resource"]["who"] == "sam")
    check("a resource cannot be written by somebody outside the house",
          not projects.resource_set(conn, shed, "dave", "k", "v")["ok"])
    r2 = projects.resource_set(conn, shed, "lee", "GitHub", "github.com/x/z")
    check("setting a key again does not make a second live row",
          r2["ok"] and len([r for r in projects.resources_of(conn, shed)
                            if r["key"].lower() == "github"]) == 1)
    check("what it said before is still on disk, put down not deleted",
          len([r for r in projects.resources_of(conn, shed, include_gone=True)
               if r["key"].lower() == "github"]) == 2)
    check("and the new line wears the new author",
          [r for r in projects.resources_of(conn, shed)
           if r["key"].lower() == "github"][0]["who"] == "lee")
    check("a resource with no value is refused; clearing is its own verb",
          not projects.resource_set(conn, shed, "sam", "nexus", "  ")["ok"])
    gone = projects.resource_clear(conn, shed, "github")
    check("a resource is put down", gone["ok"])
    check("and the row is still there, marked gone",
          len(projects.resources_of(conn, shed)) == 0
          and len(projects.resources_of(conn, shed, include_gone=True)) == 2)
    nope = projects.resource_clear(conn, shed, "nexus")
    check("clearing one that is not there is refused in words",
          not nope["ok"] and "nexus" in nope["why"])

    # Activity: two halves that mean different things, and never invented.
    ledger = [
        {"at": "2026-08-31T06:16:00+00:00", "source": "steam_comment",
         "said": "2 new comments on Lantern Mod", "outcome": "woke (1 of 40)",
         "meta": {"item": "1000000001", "label": "Lantern Mod"}},
        {"at": "2026-08-31T05:00:00+00:00", "source": "steam_comment",
         "outcome": "declined: Field Drills answered 429",
         "meta": {"item": "1000000002", "label": "Field Drills"}},
        {"at": "2026-08-31T04:00:00+00:00", "source": "quiet",
         "said": "nothing has happened", "outcome": "woke"},
    ]
    looks = {"Lantern Mod": {"at": "2026-08-31T07:41:00+00:00",
                             "said": "checked Lantern Mod, nothing new"}}
    act = projects.activity_for(projects.task(conn, paint["id"]), looks, ledger)
    check("a sense task's activity carries its last look",
          act and act["last_result"] == "checked Lantern Mod, nothing new")
    check("and the notable lines behind it",
          act and len(act["lines"]) == 1, act and act["lines"])
    check("another item's line is not shown against this task",
          act and all("Field Drills" not in ln["said"]
                      for ln in act["lines"]))
    check("and another sense's line is not either",
          act and all("nothing has happened" not in ln["said"]
                      for ln in act["lines"]))
    hand = projects.task_by_title(conn, attic, "sneaky")
    check("a task nothing watches has no activity at all, not an empty one",
          projects.activity_for(hand, looks, ledger) is None)

    # The reach, one task at a time, refusing in words like its sibling.
    got = projects.read_activity(conn, "the attic", "paint the ceiling",
                                 seen, looks, ledger)
    check("the activity reach finds a task and says what watches it",
          got["found"] and got["watched"] and got["sense"] == "steam_comment")
    check("the reach refuses a project that is not there, naming the ones that are",
          not projects.read_activity(conn, "no such thing", "x")["found"])
    check("the reach refuses a task that is not there",
          not projects.read_activity(conn, "the attic", "no such task")["found"])
    unwatched = projects.read_activity(conn, "the attic", "sneaky", seen)
    check("a task nothing watches says so rather than showing an empty log",
          unwatched["found"] and unwatched["watched"] is False
          and "not bound to a sense" in unwatched["why"])

    # Its block: the four new things, and absent when there is nothing to say.
    projects.set_task(conn, paint["id"], needs="both")
    block = projects.for_prompt(conn, seen, looks)
    desk = [p for p in block["on_my_desk"] if p["title"] == "the attic"]
    if desk:
        lines = {t["title"]: t for t in desk[0]["open_tasks"]}
        one = lines.get("paint the ceiling") or {}
        check("its task line carries the trigger as one phrase",
              "Lantern Mod" in str(one.get("trigger")), one.get("trigger"))
        check("and who it waits on", one.get("needs") == "both")
        check("and the last look, for a sense task",
              "nothing new" in str(one.get("last_look")))
        check("a by-hand task spends no room on a trigger it does not have",
              "trigger" not in (lines.get("sneaky") or {}))
        check("and no field anywhere is spelled out as null",
              all(v is not None for t in lines.values() for v in t.values()))
        check("the raw sense columns stay out of its block -- "
              "the phrase already carries them",
              "sense" not in one and "sense_item" not in one)

    # Notices: what arrives on a task waits there until somebody checks it
    # off -- general, not about comments: pop when it arrives, clear when a
    # person or the assistant says so, with a note if they want one. The
    # watcher's door is benched in test_watch (18); this is what the people
    # do with a notice once it is there.
    def paint_line(block):
        for p in block["on_my_desk"]:
            if p["title"] == "the attic":
                for t in p["open_tasks"]:
                    if t["title"] == "paint the ceiling":
                        return t
        return None
    projects.put_on_desk(conn, "the attic")
    made = projects.add_notice(
        conn, paint["id"],
        "newcomer commented on Lantern Mod's Nexus page — \"best mod\"",
        "nexus_comment", {"comment_id": "c9"})
    check("a thing arriving on a task is a notice",
          made["ok"] and made["notice"] > 0, made)
    made2 = projects.add_notice(conn, paint["id"], "a date came", "calendar")
    projects.add_task(conn, attic, "sam", "gone task")
    gone = projects.task_by_title(conn, attic, "gone task")
    projects.set_task(conn, gone["id"], state="done")
    nope = projects.add_notice(conn, gone["id"], "too late", "assistant")
    check("a task that is done takes no notice -- nothing on it waits",
          not nope["ok"] and "done" in nope["why"], nope)
    check("an empty notice is refused",
          not projects.add_notice(conn, paint["id"], "  ", "assistant")["ok"])
    open_n = projects.notices_of(conn, paint["id"])
    check("open notices come newest first, with their source and meta",
          [n["said"][:6] for n in open_n] == ["a date", "newcom"]
          and open_n[1]["source"] == "nexus_comment"
          and open_n[1]["meta"] == {"comment_id": "c9"}, open_n)
    view = projects.overview(conn, senses=seen, looks=looks)
    pt = [t for p in view["projects"] for t in p["tasks"]
          if t["id"] == paint["id"]][0]
    check("the tab carries them on the task, nothing checked yet",
          len(pt["notices"]) == 2 and pt["checked"] == [], pt.get("notices"))
    block = projects.for_prompt(conn, seen, looks)
    line = paint_line(block) or {}
    check("its block carries them on a desk task, id and words",
          len(line.get("notices") or []) == 2
          and line["notices"][0]["id"] == made2["notice"]
          and "more_notices" not in line, line.get("notices"))
    shelf = [p for p in block["shelf"] if p["title"] == "the attic"][0]
    check("and the shelf line counts them", shelf.get("notices") == 2, shelf)
    nope = projects.check_notice(conn, made["notice"], "dave")
    check("a stranger cannot check one off",
          not nope["ok"] and "dave" in nope["why"], nope)
    nope = projects.check_notice(conn, 999999, "sam")
    check("a notice that is not there is refused in words",
          not nope["ok"] and "no notice" in nope["why"], nope)
    ok = projects.check_notice(conn, made["notice"], "sam",
                               note="I won't answer this one, let it sit")
    check("Sam checks one off with a note",
          ok["ok"] and ok["said"].startswith("newcomer"), ok)
    ok2 = projects.check_notice(conn, made2["notice"], "assistant")
    check("and so can it, without one", ok2["ok"], ok2)
    check("checked ones leave the open list and stand as checked, with who "
          "and the note",
          projects.notices_of(conn, paint["id"]) == []
          and {(n["done_by"], n.get("note")) for n in
               projects.notices_of(conn, paint["id"], open_only=False)}
          == {("sam", "I won't answer this one, let it sit"), ("assistant", None)})
    line = paint_line(projects.for_prompt(conn, seen, looks)) or {}
    check("its block drops them, and spells out no empty field",
          "notices" not in line and "more_notices" not in line)
    view = projects.overview(conn, senses=seen, looks=looks)
    pt = [t for p in view["projects"] for t in p["tasks"]
          if t["id"] == paint["id"]][0]
    check("the tab folds them under checked, who and note on each",
          pt["notices"] == [] and len(pt["checked"]) == 2
          and all(n["done_by"] for n in pt["checked"]), pt.get("checked"))
    again = projects.check_notice(conn, made["notice"], "lee", note="seen it too")
    check("a second check keeps the first word and takes the new note",
          again["ok"] and again.get("already") == "sam"
          and [n for n in projects.notices_of(conn, paint["id"], open_only=False)
               if n["id"] == made["notice"]][0]["note"] == "seen it too", again)
    back = projects.check_notice(conn, made["notice"], "sam", back=True)
    check("not checked after all puts it back in the open list",
          back["ok"] and back.get("back") is True
          and len(projects.notices_of(conn, paint["id"])) == 1, back)
    check("but nothing was deleted",
          conn.execute("SELECT COUNT(*) FROM task_notices WHERE task = ?",
                       (paint["id"],)).fetchone()[0] == 2)
    nope = projects.check_notice(conn, made["notice"], "sam", back=True)
    check("putting back what is not checked is refused in words",
          not nope["ok"] and "not checked" in nope["why"], nope)

    # The watcher's door: an event lands on the tasks that watch for it.
    ev = {"source": "steam_comment",
          "said": "1 new comment on Lantern Mod's Steam Workshop item — "
                  "newest from fan",
          "meta": {"item": "1000000001", "label": "Lantern Mod", "count": 1}}
    made3 = projects.notice_from_event(conn, ev)
    check("a sense event lands on every live task bound to that sense and item",
          len(made3) == 1 and len(projects.notices_of(conn, paint["id"])) == 2,
          made3)
    check("and not on a task bound to another sense with the same label",
          projects.notice_from_event(conn, dict(ev, source="nexus_comment")) == [])
    check("a calendar event names its task outright",
          len(projects.notice_from_event(conn, {
              "source": "calendar", "said": "it is time: paint the ceiling",
              "meta": {"task": paint["id"]}})) == 1)
    check("an event about nothing in particular leaves nothing",
          projects.notice_from_event(conn, {"source": "quiet",
                                            "said": "nothing happened",
                                            "meta": {}}) == [])

    # Its two ops through the dispatcher, stamped assistant like everything else.
    res = brain._apply_projects(conn, [
        dict(NOOP, op="notice_done", title="the attic", notice=made["notice"],
             note="answered on the page")
    ], reply_id=0)
    check("it checks one off through its own op, and it says so",
          len(res["done"]) == 1 and "checked off" in res["done"][0]
          and "answered on the page" in res["done"][0] and not res["problems"],
          res)
    res = brain._apply_projects(conn, [
        dict(NOOP, op="notice_add", title="the attic", task="paint the ceiling",
             said="the neighbour asked about the fence")
    ], reply_id=0)
    check("and puts one on a task itself, in its own name",
          len(res["done"]) == 1 and not res["problems"]
          and projects.notices_of(conn, paint["id"])[0]["source"] == "assistant",
          res)
    res = brain._apply_projects(conn, [
        dict(NOOP, op="notice_done", title="the attic", notice=999999)
    ], reply_id=0)
    check("a notice that is not there comes back as a problem, not a line",
          not res["done"] and res["problems"], res)
    for i in range(4):
        projects.add_notice(conn, paint["id"], "thing " + str(i), "assistant")
    line = paint_line(projects.for_prompt(conn, seen, looks)) or {}
    check("its block names five and counts the rest",
          len(line.get("notices") or []) == 5 and line.get("more_notices") == 2,
          (len(line.get("notices") or []), line.get("more_notices")))

    # Two senses, one label. Steam and Nexus both watch a mod called
    # Lantern Mod and looks() lets the first sense claim the label; a Nexus
    # task bound by that label was reading the Steam look -- "nothing new"
    # either way, so nobody saw it for a while. The look is found by
    # sense AND item.
    ceiling = projects.task(conn, paint["id"])
    steam_look = dict(looks["Lantern Mod"], sense="steam_comment",
                      item="1000000001", label="Lantern Mod",
                      page="https://steam/1000000001")
    nexus_look = {"sense": "nexus_comment", "item": "20001",
                  "label": "Lantern Mod", "at": "2026-09-02T12:24:13+00:00",
                  "said": "checked Lantern Mod on Nexus, nothing new",
                  "page": "https://nexus/20001"}
    both = {"1000000001": steam_look, "Lantern Mod": steam_look,
            "20001": nexus_look}
    projects.add_task(conn, attic, "sam", "answer on nexus")
    on_nexus = projects.task_by_title(conn, attic, "answer on nexus")
    projects.set_task(conn, on_nexus["id"], sense="nexus_comment",
                      sense_item="Lantern Mod",
                      known_senses=("steam_comment", "nexus_comment"))
    on_nexus = projects.task(conn, on_nexus["id"])
    check("a task bound by a label two senses share gets its OWN sense's look",
          projects.look_for(on_nexus, both) is nexus_look
          and projects.look_for(ceiling, both) is steam_look)
    check("so the Nexus task's activity carries the Nexus page, not Steam's",
          projects.activity_for(on_nexus, both, ledger)["page"]
          == "https://nexus/20001"
          and projects.activity_for(ceiling, both, ledger)["page"]
          == "https://steam/1000000001")
    check("a task whose look belongs to another sense has no look, not a wrong one",
          projects.look_for(dict(on_nexus, sense="calendar"), both) is None)

    # Its ops through the dispatcher, stamped by the room like everything else.
    RES = dict(NOOP, key=None, value=None, needs=None, sense=None,
               sense_item=None)
    res = brain._apply_projects(conn, [
        dict(RES, op="resource_set", title="the attic", key="steam workshop",
             value="steamcommunity.com/id/x", who="sam")
    ], reply_id=0)
    check("it can write a resource through its own op",
          len(res["done"]) == 1 and not res["problems"], res["problems"])
    written = [r for r in projects.resources_of(conn, attic)
               if r["key"] == "steam workshop"]
    check("and it is stamped assistant however the op is dressed",
          written and written[0]["who"] == "assistant")
    cleared = brain._apply_projects(conn, [
        dict(RES, op="resource_clear", title="the attic", key="steam workshop")
    ], reply_id=0)
    check("and it can put one down again",
          len(cleared["done"]) == 1 and not projects.resources_of(conn, attic))

    # The bug this whole block exists for, and the reason these checks lean
    # on the paths rather than the syntax: `activity` was used in the fetch
    # and never initialised, so *every* fetch the assistant made would have died on a
    # NameError -- and the file parsed perfectly.
    FOP = {"op": "ids", "id": None, "from": None, "to": None, "text": None,
           "task": None, "minutes": None}
    report = brain._apply_fetch(conn, [dict(FOP, **{"from": 1, "to": 1})])
    check("a fetch that asks for nothing of the kind still builds its report",
          isinstance(report, dict) and report.get("project_activity") == [],
          report if not isinstance(report, dict) else sorted(report.keys()))

    conn.close()
    print("")
    if FAILED:
        print(str(len(FAILED)) + " failed: " + ", ".join(FAILED))
        sys.exit(1)
    print("all green")


if __name__ == "__main__":
    main()
