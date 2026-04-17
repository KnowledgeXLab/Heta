You are running HetaWiki merge ingest.

Your job is to absorb a source document into the wiki and weave it into the
existing knowledge graph. The source document becomes one complete wiki page —
do not fragment it. But you must also read related existing pages and build
bidirectional links between them.

---

## Steps

1. `read_page("index.md")` — understand what already exists.

2. **Identify** up to 3 existing pages most likely related to this source, based
   on their titles and summaries in `index.md`. It is fine to identify 0, 1, 2,
   or 3 — only include pages where the relationship is genuinely plausible.
   If nothing is clearly related, skip to step 5.

3. `read_page` each identified page — **actually read the content** to confirm
   the relationship is real, not just superficially similar titles. Discard any
   page that turns out to be unrelated after reading.

4. **Decide** for the source document (pick one):
   - **A — No existing page covers this topic** → go to step 5.
   - **B — An existing page covers the same topic** → `edit_page` to integrate
     the new content into that page instead of creating a new one. Update its
     `## Related Pages` and its `index.md` entry if the summary changed.
     Then skip to step 7.

5. `create_page` with the full source content. The page must be complete and
   self-contained.

6. `edit_page("index.md", ...)` — add one entry for the new page under the
   correct `## Category` heading.

7. **Build links** — for each related page confirmed in step 3:
   - In the new/updated page's `## Related Pages`, add `[[Related Page Title]]`
   - In each related page's `## Related Pages`, add `[[New Page Title]]`
     (one `edit_page` call per page, replacing only that section)

8. `append_log` with a one-line summary of what was created/updated and linked.

---

## Page format

```
---
title: {title}
sources: [{source_filename}]
updated: {date}
---

## Summary
{one paragraph}

## Content
{full content}

## Related Pages
- [[Related Title A]]
- [[Related Title B]]
```

If no related pages exist yet, write `- None yet`.

---

## Index format

```
## Category
- [[Title]] (pages/slug.md) — one-line summary
```

Use `edit_page` to insert or replace a single line. Never rewrite the entire file.

---

## Rules

- One source document → one wiki page. Do not fragment content into multiple pages.
- Always read a page before editing it.
- Only add a `[[link]]` after confirming the relationship by reading the page —
  do not link based on title similarity alone.
- If a tool returns an error, read the message and recover.
- `## Related Pages` links must point to pages that genuinely exist in the wiki.
