# Querying the Wiki

Use the Query page to ask questions about the current wiki. The system answers from the wiki and shows the relevant source pages.

## Good Use Cases

- quickly understand a topic
- compare related concepts
- continue reading from the source pages
- turn a good answer into a reusable synthesis page

## How to Use It

1. Open the Query page.
2. Enter your question.
3. Wait for the answer.
4. Check the source page chips below the answer.
5. Open a source page or ask a follow-up question if needed.

## Better Questions

- ask about one main topic at a time
- mention concrete concepts or models
- ask for comparison when you want synthesis

If the information is not in the wiki yet, ingest the document first.

## Archived

`archived` means the answer was saved back into the wiki as a new synthesis page.

## For Developers

Submit a query:

```bash
curl -X POST http://localhost:8000/api/v1/hetawiki/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What is GRPO?"}'
```

Check task status:

```bash
curl http://localhost:8000/api/v1/hetawiki/tasks/abc123
```
