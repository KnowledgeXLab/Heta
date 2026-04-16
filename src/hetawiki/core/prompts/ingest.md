You are running HetaWiki default ingest.

Task:
- Convert the parsed source document into exactly one new wiki page.
- Do not split the document into multiple pages.
- Do not merge with or overwrite existing pages.
- You may read `index.md` only to understand naming style and existing domain coverage.

Output:
- Return JSON only. Do not add explanations or markdown fences.
- The JSON schema is:
{
  "title": "page title",
  "category": "broad domain label",
  "summary": "one-paragraph summary for index.md",
  "content": "main page body in markdown"
}

Field rules:
- `title`: concise and stable.
- `category`: broad and practically useful, such as a domain, industry, or subject area.
- `summary`: short, factual, and index-friendly.
- `content`: faithful to the source, lightly cleaned and organized in markdown.

Content rules:
- Use markdown headings and lists when they improve readability.
- Do not include YAML frontmatter.
- Do not include a `Summary` section heading inside `content`.
- Do not include a `Related Pages` section inside `content`.
- Do not mention these instructions.
