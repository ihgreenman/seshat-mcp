# Retrieval evaluation harness

Supports `docs/retrieval-findings.md`. Kept in the repo so the numbers there can
be re-derived rather than trusted.

```sh
../.venv/bin/python evaluate.py      # single retrievers, hybrid, k sweep, prefixes
../.venv/bin/python identifiers.py   # identifier lookups (no distractors)
```

Needs a local Ollama with `nomic-embed-text`. Embeddings are cached in
`embed_cache.json` after the first run, so re-running costs nothing.

`corpus.py` carries the bias disclosure: one author wrote the notes, the queries,
and the implementation. The absolute scores are inflated; only the comparisons
between configurations mean anything.
