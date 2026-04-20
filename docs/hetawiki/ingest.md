# Ingesting Documents

Use Ingest to add documents into the current wiki.

## Modes

- **Add as New Page**: create one new wiki page from the uploaded document.
- **Integrate with Wiki**: merge the uploaded document into the current wiki and update related pages when needed.

## When to Use Each Mode

Use **Add as New Page** when:

- the document is new
- one document should become one page
- existing pages should stay unchanged

Use **Integrate with Wiki** when:

- the document overlaps with existing wiki topics
- existing pages may need updates
- the result should be integrated instead of simply appended

## How to Use It

1. Open the HetaWiki page.
2. Upload a file in the Ingest section.
3. Choose a mode.
4. Submit the task.
5. Wait for the task to finish.
6. Check the result in the page view, index, graph, or query page.

## Notes

- If you are unsure, start with **Add as New Page**.
- Very large documents may be rejected by `default ingest`.
- **Integrate with Wiki** is slower because it updates the wiki more carefully.

## API

Submit an ingest task:

```bash
curl -X POST http://localhost:8000/api/v1/hetawiki/ingest \
  -F "file=@deepseek-r1.pdf" \
  -F "merge=false"
```

Check task status:

```bash
curl http://localhost:8000/api/v1/hetawiki/tasks/abc123
```
