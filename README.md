# digital-ai-assistant

A personal assistant that does not start over. It keeps one long working set,
folds what it lets go of into essences it can search and reach back through,
keeps its own Spark (the text it reads as itself), starts and follows Codex
and Claude Code sessions to do work, watches for things worth waking for, and folds its day at night. It runs as a
small local web room on Windows, thinking through Claude Code on your plan,
OpenAI, or OpenRouter.

The code is shared. The assistant is not: everything that makes one assistant
*that* one lives in its **home**, a folder of its own that is also its own
private git repository. One install runs one home; a second machine can run a
different assistant from the same code, and neither can see the other.

## Install

Needs Python 3.11+ on Windows, and for the default mind the `claude` command
(Claude Code) signed in. For the nightly off-site backup, the GitHub CLI `gh`
signed in.

```
git clone <this repo>
cd digital-ai-assistant
pip install -r requirements.txt
```

## Make an assistant, or pick one

Point setup at a folder. An empty folder -- or a freshly created empty repo --
becomes a new assistant; a folder that already holds one (it has
`identity.json`) is simply chosen.

```
python -m server.setup C:\path\to\ava
```

It asks what the assistant is called, whom it works for, which port it answers
on and which time zone its schedules mean, then writes:

```
<home>/
  identity.json   name, owner, people, port, time zone
  data/spark.md   a starter Spark -- rewrite it before the first turn
  data/*.json     settings; everything household-specific starts off
  .gitignore      the store, backups, logs and keys stay out of git
```

Add a private remote to the home and push it, so the nightly backup has
somewhere to go:

```
cd C:\path\to\ava
git remote add origin <a private repo>
git push -u origin HEAD
```

## Run it

```
pythonw start.pyw --open     # the Digital Assistant Manager: one icon for every assistant
python -m server.manager     # the same, in a console
.\make_shortcut.ps1 -Startup # a Desktop shortcut, and start at login
```

The manager answers on `http://localhost:8787`: `/` lists every assistant,
and each one's room is at `/<its name>/` -- `http://localhost:8787/ava/`. One
address, so a phone on the owner's tailnet needs to know only the one. Each
room still runs as its own process with its own store, on its home's own port
on loopback, where nothing but the manager can reach it.

`python -m server.app` still runs one room on its own, saying everything in a
console. It refuses to start without a home chosen, and refuses to run twice
over the same home.

## More than one assistant on a machine

The manager's page, and the name at the top of every room, list every
assistant this install knows. From there:

- **Open** walks into its room.
- **Stop** puts it down and keeps it down -- across a restart of the manager
  and of the machine -- until **Start**. A stopped assistant does not answer,
  dream or run its jobs, and anything it had sent out working stops with it.
- **Restart** is its own restart, done for it.
- **Detach** stops it and has the manager forget it and let go of its folder,
  which is left exactly as it is: move it anywhere (another drive, a synced
  folder), keep it, or delete it yourself. The install's own home -- the one
  `home.json` points at -- is not detached from here.
- **Attach an assistant** takes a folder that already holds one (it has
  `identity.json`) -- one detached and moved, or made elsewhere -- lists it and
  starts it. If its port is taken here it is given another, and says so.
- **+ New assistant** asks for a name and a folder, makes the home there,
  starts it, and opens it. It thinks through the same model as the first
  assistant until its own Settings say otherwise, and works for the same
  person. Write its Spark (`data\spark.md` in its folder) before talking to it
  much.

The icon in the corner does the same from its menu, and a room that falls
over is brought back, unless it keeps falling (then it stays down and says
why). Everything each room says goes to its home's `logs/`; the manager's own
story to `logs/` beside the code.

The list is `homes.json` beside the code: which folders, which are stopped,
and the manager's port (`"port"`, 8787 by default). It is per machine and
never committed. `python -m server.homes` prints it.

### Who gets in

The manager keeps the room's own two walls: a knock must come from this
machine or from the owner's tailnet, and carry the key of somebody paired
with one of the assistants. It tells the room behind it who really knocked,
with a secret it hands each room when it starts it; a room refuses those
headers from anyone else. Somebody paired with one assistant is let into
another only if they are paired there too. Starting, stopping and making
assistants is for the owner, from the desk or their phone; the folder dialog
opens at the desk only.

## Its face

Tapping the portrait opens the portrait and the room's background. A picture
uploaded there is kept in the home's `artwork/portraits/` and the face is
drawn from it: the round portrait (in the room and in the manager's list),
the icons a phone keeps, and the browser tab's icon. The
face the home had before is kept file for file in
`artwork/portraits/original/`, and choosing it gives exactly that back.

## Its sessions

The Sessions tab shows every Codex task and Claude Code session on the
machine: which ones the assistant follows, what each last said, what waits
for you, and what the assistant did with them lately. Two switches at the top
give it Codex tasks and Claude Code sessions; switched off, that kind is gone
from its turns entirely -- no operation, no instructions, no wakings.

- **Codex tasks** are ordinary tasks in the Codex desktop app, worked through
  one existing task of yours as the controller (paste its id in the tab).
- **Claude Code sessions** are ordinary Claude Code background sessions
  (`claude --bg`), started with the model, effort and permission mode it
  picks, and with Remote Control on so they appear in your Claude apps and on
  claude.ai/code. `claude attach <id>` opens one in a terminal. They need the
  standalone Claude Code signed in once: `claude auth login`.

The assistant follows what it starts and is woken when a followed session
finishes a turn, waits for you, or ends. A message sent to a session mid-turn
is delivered when the turn ends.

## Its notebook

The assistant keeps a few short notes of its own in front of it every turn,
about whatever it decides is worth keeping. It can add a note, remove one, or
vote one up or down; there is no edit, so a note that needs changing is
removed and written again. Settings shows the notes as a table -- sort by id,
votes, turns kept or size, and open a note to read it whole. The book has a
cap in tokens (5,000 to start), the owner's to move there: one note may take
it 5% past the cap, and past it adding locks until notes are removed or the
cap is raised. Removed notes stay in the store for the backup, and the
assistant has no way to read them back.

## Backups

- Every local copy lives in the home's `all_backups/`, which git never sees:
  stamped zips from the backup button, the latest zip under one name, and a
  copy of the store at each edge of every night.
- Once a night, after the dream, the latest zip is uploaded as a **release
  asset** on the home's own repository (`server/offsite.py`), read back, and
  kept on a rolling rule: seven nights, then one a week for twelve weeks.
  Release assets never enter git's object store, so the repo does not grow.
- The upload refuses if the repository is not private, every time, and says
  so on a task rather than changing anything.

## What a home can change

Everything in `server/*.md` and `server/recall_prompts.py` can be overridden
by a file of the same name in the home's `prompts/`. The code's copies name nobody; `{{name}}`, `{{owner}}` and
friends are filled from `identity.json`. Pictures work the same way from the
home's `artwork/` (`icon.ico`, `icon-down.ico`, `portrait.png`,
`wallpaper.png`, `wallpapers/`, `icon-192.png`, ...). Comment senses for Steam
Workshop or Nexus Mods items read their items from `data/comments.json`.

## Tests

The benches run from the code folder with a made-up identity and never touch
a home -- a process started as `server.test_*` ignores `home.json`, and so
does everything it starts, unless `ASSISTANT_HOME` names a home on purpose:

```
python -m server.test_projects %TEMP%\scratch.db
python -m server.test_voice_backend
```

Each file says in its docstring whether it wants no argument, a scratch file
or a scratch folder.

## Keeping private things out of this repo

`tools/privacy_scan.py` scans the working tree or any commit against a
denylist you keep somewhere private. `tools/pre-push` is a git hook that runs
it over every commit being pushed and refuses the push on any match:

```
cp tools/pre-push .git/hooks/pre-push
git config privacy.denylist C:\path\to\your\denylist.txt
```
