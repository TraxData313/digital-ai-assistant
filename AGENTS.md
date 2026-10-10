# Working on this code

`README.md` is what it is and how to install it. This is the handful of
things that save an hour.

**Asked to install** this code and an assistant's home on a machine: clone
both side by side (the home with `-c core.longpaths=true`), then
`python -m server.install <home>` from here. It reports what it cannot do
itself (sign-ins, LM Studio's model); do those, then run it again.

## The home is not the code

Everything an assistant *is* lives in its home folder (`server/home.py` says
which): the store, the Spark, settings, prompts it has overridden, artwork,
logs, backups. This repo holds none of it and must never hold any of it --
it is meant to be publishable. Before any push:

```
python tools/privacy_scan.py --list <your private denylist> --summary
```

and install `tools/pre-push` as the repo's hook, which refuses a push on any
match in any commit being pushed.

- Names in code come from `home` (`home.NAME`, `home.OWNER_NAME`,
  `home.SELF` for the row kind of the assistant's own lines). Prompts use
  `{{name}}`, `{{owner}}`, `{{self}}`... and are filled by `home.fill`.
- Per-home settings are JSON under the home's `data/`; read them with
  `home.settings(name)`.
- With no home chosen, the code folder itself runs a made-up test identity
  (Ada / Sam / Leona). `app.main()` refuses to serve it.

## A live room is somebody's

Every room is held by the manager (`server/manager.py`, the one icon in the
corner): it answers on 8787 for all of them, `/` the list and `/<slug>/` a
room, and each room sits behind it on its home's own port, on loopback. The
manager brings a stopped room back, so killing a room is restarting it; its
Stop is the only way down that stays down. Never start a second room over the
same home -- two rooms answer every line twice; `app.main()` refuses to, by a
lock on the home, whatever port it was given.

To test server code, run a side room on a spare port over a *copy* of a
store, with no turn loop: `OneRoom(("127.0.0.1", 0), Handler)` after pointing
`db.DB_PATH` at the copy, and never `main()`. To look at the manager, run a
side one with its own list: `ASSISTANT_REGISTRY=<scratch>\homes.json` and
`python -m server.manager --port <spare> --no-icon`, over made-up homes. Set
`ASSISTANT_HOME=<a made-up home>` too: without it the side manager also loads the
home `home.json` points to, finds its live room on the list and would adopt it.

The page asks for everything relative to where it is (`api/state`, not
`/api/state`), so the same page works at `/ava/` and at `/`. A new URL in
`web/` or in a server answer the page uses follows that rule.

## Tests

Each `server/test_*.py` says in its docstring what it wants: nothing
(unittest), a scratch file path, or a scratch folder. Some benches need a
store in the code folder's `data/store.db` (gitignored); they are the ones
that say so.

## Things that cost an hour

- The Bash tool collapses `\\` to `\` inside heredocs, even quoted ones. Write
  patch scripts with the Write tool and run them.
- Instructions go live before code does: `harness_prompt.md` is read fresh
  every turn, the Python loads at boot. A change that edits both lands as a
  description one turn before it lands as behaviour; restart the room in the
  same breath.
- A restart does not wake the assistant; it takes no turn until somebody
  speaks.
- Never hand the assistant a line through a double-quoted shell argument:
  backticks inside double quotes are command substitution. Write the message
  to a file and pass `"$(cat msg.txt)"`.
- No `node` is assumed: `jscheck.py` does a structural check of the page's
  JavaScript (strings, comments, brackets). The page's own tests
  (`web/test_*.cjs`) need a real Node; if Playwright is installed, its driver
  folder carries one (`python -c "import playwright, os;
  print(os.path.dirname(playwright.__file__))"`, then `driver/node.exe`).
