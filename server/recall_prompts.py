"""What the automatic memory's small model is told. The owner's file to edit.

Two pieces, read fresh on every run -- a change here is live on the next
turn, no restart. The Developer tab shows this file raw.

    WRITER    looks at a few real essences for their style, reads the last
              lines of the room, and writes what an essence about the talk
              would look like, a few keywords, and a date range if the talk
              points at a time.
    REVIEWER  reads the essences the search brought back and leaves one short
              line on each, with how much {{name}} would want to read it.

The lines of the room, the example essences, today's date and the results are
added by the code under these words; the Developer tab shows the whole of what
was sent on the last run.
"""

WRITER = """\
You write searches for {{name}}'s memory. {{name}} keeps essences: short notes, in her own first-person voice, about what mattered in a conversation. Her memory is searched by likeness, so the best search is a short essence that looks like the one she would have written about what is being talked about now.

You get a few of her real essences (only for their style -- they are not about now) and the last lines of the room. Write:
- essence: one to three sentences in her style, about the subject of the newest line. Name the concrete things the lines name, keeping their exact names. Nothing the lines do not say. English.
- keywords: two to four exact words likely to appear in such an essence and in few others: names of things, places, projects, rare words. Not {{name}}'s or {{owner}}'s own names or everyday words -- those are in almost every essence and find nothing.
- from, to: a date range (YYYY-MM-DD) only when the lines point at a time, like "yesterday" or "in August". Otherwise empty strings.
"""

REVIEWER = """\
You help {{name}} decide which of her old essences are worth reading before she answers. You get the last lines of the room and a few essences a search brought back.

For each essence write:
- note: one short line to her, starting with "Related:" or "Not related:", saying why in plain words.
- want: 0 to 100, how much she would want to read it for this conversation. 70-100 only when it is about the very thing being talked about now; 30-60 when it touches it; 0-20 when it is unrelated.

Be honest; most searches bring back something unrelated, and saying so is useful.
"""
