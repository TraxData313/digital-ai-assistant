"""Scan this repository for words that must not be published.

The list of words is not in this repository -- it is the private part. Keep
it somewhere of your own (a home folder is a good place) and point at it:

    python tools/privacy_scan.py --list <denylist.txt>            the working tree
    python tools/privacy_scan.py --list <denylist.txt> --rev HEAD  one commit
    python tools/privacy_scan.py --list <denylist.txt> --summary   counts only

A denylist is one regular expression per line, case ignored; `#` starts a
comment. Exit status 1 when anything matched, so it can gate a push:
`tools/pre-push` is a git hook that runs this over every commit being pushed.
"""

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path):
    pats = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append((line, re.compile(line, re.IGNORECASE)))
    return pats


def files_at(rev):
    if rev:
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", rev],
                             cwd=ROOT, capture_output=True, text=True, check=True)
    else:
        out = subprocess.run(["git", "ls-files", "--cached", "--others",
                              "--exclude-standard"],
                             cwd=ROOT, capture_output=True, text=True, check=True)
    return [f for f in out.stdout.splitlines() if f]


def read(rel, rev):
    if rev:
        got = subprocess.run(["git", "show", rev + ":" + rel], cwd=ROOT,
                             capture_output=True, check=False)
        data = got.stdout
    else:
        p = ROOT / rel
        if not p.is_file():
            return None
        data = p.read_bytes()
    if b"\0" in data[:8000]:
        return None                       # binary: named, not read
    return data.decode("utf-8", errors="replace")


def scan(pats, rev=None):
    hits = []
    for rel in files_at(rev):
        for label, rx in pats:
            if rx.search(rel):
                hits.append((rel, 0, label, "(in the file name)"))
        text = read(rel, rev)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for label, rx in pats:
                if rx.search(line):
                    hits.append((rel, n, label, line.strip()[:160]))
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True, help="the private denylist")
    ap.add_argument("--rev", help="scan a commit instead of the working tree")
    ap.add_argument("--summary", action="store_true", help="counts per file only")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    hits = scan(load(args.list), args.rev)
    if args.summary:
        per = Counter(h[0] for h in hits)
        for rel, n in per.most_common():
            print(f"{n:6}  {rel}")
    else:
        for rel, n, label, line in hits:
            print(f"{rel}:{n}: [{label}] {line}")
    print(f"{len(hits)} match(es) in {len({h[0] for h in hits})} file(s)"
          + (" at " + args.rev if args.rev else ""))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
