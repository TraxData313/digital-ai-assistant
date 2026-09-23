"""A crude structural read of app.js -- not a parser, and it does not pretend
to be one. There is no JS engine on this machine, so what can be checked
cheaply is checked: that every string, template literal, comment and bracket
closes. That is the exact class of breakage that took this page down before
(an apostrophe inside a single-quoted string), and it is the class a patch
script is most likely to introduce."""

import sys

src = open(sys.argv[1], encoding="utf-8").read()
i, n = 0, len(src)
stack = []          # brackets, with the line they opened on
line = 1
prev = ""           # last significant character, for telling regex from divide
bad = []

while i < n:
    c = src[i]
    if c == "\n":
        line += 1
        i += 1
        continue
    if c in " \t\r":
        i += 1
        continue
    # comments
    if c == "/" and i + 1 < n and src[i + 1] == "/":
        while i < n and src[i] != "\n":
            i += 1
        continue
    if c == "/" and i + 1 < n and src[i + 1] == "*":
        end = src.find("*/", i + 2)
        if end < 0:
            bad.append("line " + str(line) + ": block comment never closes")
            break
        line += src.count("\n", i, end)
        i = end + 2
        continue
    # a regex literal, when a slash cannot be division
    if c == "/" and prev in "(=,:[!&|?{};+*%~^<>" + "":
        j, closed, inclass = i + 1, False, False
        while j < n:
            if src[j] == "\\":
                j += 2
                continue
            if src[j] == "\n":
                break
            # A slash inside [...] is a literal slash, not the end of the
            # pattern. There is such a regex in this very file, and reading
            # it as closed is what made this check cry wolf the first time.
            if src[j] == "[":
                inclass = True
            elif src[j] == "]":
                inclass = False
            elif src[j] == "/" and not inclass:
                closed = True
                break
            j += 1
        if closed:
            i = j + 1
            prev = "/"
            continue
    # strings and template literals
    if c in "\"'`":
        quote, j = c, i + 1
        depth = 0
        while j < n:
            d = src[j]
            if d == "\\":
                j += 2
                continue
            if d == "\n":
                line += 1
                if quote != "`":
                    bad.append("line " + str(line - 1) + ": a "
                               + quote + " string runs off the end of its line")
                    break
                j += 1
                continue
            if quote == "`" and d == "$" and j + 1 < n and src[j + 1] == "{":
                depth += 1
                j += 2
                continue
            if quote == "`" and d == "{" and depth:
                depth += 1
                j += 1
                continue
            if quote == "`" and d == "}" and depth:
                depth -= 1
                j += 1
                continue
            if d == quote and not depth:
                break
            j += 1
        else:
            bad.append("line " + str(line) + ": a " + quote + " never closes")
            break
        i = j + 1
        prev = quote
        continue
    if c in "([{":
        stack.append((c, line))
        i += 1
        prev = c
        continue
    if c in ")]}":
        want = {")": "(", "]": "[", "}": "{"}[c]
        if not stack:
            bad.append("line " + str(line) + ": a stray " + c)
            break
        got, at = stack.pop()
        if got != want:
            bad.append("line " + str(line) + ": " + c + " closes a " + got
                       + " opened on line " + str(at))
            break
        i += 1
        prev = c
        continue
    prev = c
    i += 1

if stack:
    bad.append(str(len(stack)) + " bracket(s) never close: "
               + ", ".join(g + " on line " + str(a) for g, a in stack[:5]))

print(sys.argv[1] + ": " + str(line) + " lines read")
if bad:
    for b in bad:
        print("  BAD  " + b)
    sys.exit(1)
print("  ok   every string, comment and bracket closes")
