# seshat-mcp

A persistent note store an LLM assistant can query and write cheaply, so it has
continuity across sessions.

The unit is a note, not a file. Notes are **append-only**: there is no update and
no delete, and every correction is a new note that supersedes an old one, which
means the history of what you believed and when is preserved by construction.

Built against spec 1.3 — [`docs/seshat-mcp-spec.md`](docs/seshat-mcp-spec.md).

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[vector]'
ollama pull nomic-embed-text        # optional; enables semantic search
```

Python ≥ 3.11. Two required dependencies (`mcp`, `pydantic`); everything else is
stdlib. `sqlite-vec` and Ollama are both optional at runtime — without them
retrieval falls back to keyword search and `help` says so.

## Use it from Claude Code

```sh
claude mcp add --scope user seshat \
  -e SESHAT_DB=$HOME/notes/seshat.db \
  -- /path/to/.venv/bin/seshat serve
```

Or in a config file:

```json
{
  "mcpServers": {
    "seshat": {
      "command": "/path/to/.venv/bin/seshat",
      "args": ["serve"],
      "env": { "SESHAT_DB": "~/notes/seshat.db" }
    }
  }
}
```

Without `SESHAT_DB` the store lands at `~/.local/share/seshat/seshat.db`.

## Tools

Six, and the surface does not grow: every tool costs context on every turn.

| tool | what it does |
|---|---|
| `note(desc, text, supersedes=[])` | write a note; `supersedes` records what it replaces and how much of it survives |
| `context(text, since, limit)` | search. Empty query is a recency listing — the session-start orientation call |
| `read(id, edge_limit, with_sources)` | one note in full, with its edges, links, backlinks and preserved sources |
| `supersedes(old, new, retained, why)` | record after the fact that one note supersedes another |
| `chain(id)` | how a belief got here and what became of it |
| `help()` | which spec revision this is and which capabilities are actually live |

## Command line

```sh
seshat review               # local web interface — start here for reading
seshat info                 # versions, capabilities, counts
seshat context "nyquist"    # search
seshat read <id>
seshat check                # consistency report for human review
seshat embed                # embed anything outstanding
seshat extract              # read captured pages into text
seshat paste <id> <url>     # supply content for a capture that failed
seshat token                # the browser extension's API token
seshat reset                # archive an unopenable store and start fresh
```

`seshat review` is the one to reach for first: it's a local, read-only browser
over notes, chains and captured sources, plus a queue of captures that need a
human. `--help` lists the rest.

It binds `127.0.0.1` and has **no authentication** — the bind address is the
only access control it has, so it is loopback-only and there is no switch that
changes that:

```sh
seshat review --host ::1      # fine: still loopback
seshat review --host 0.0.0.0  # refused, before anything opens
```

`--host` picks among loopback addresses and nothing more. If you want to reach
it from another machine, put a reverse proxy in front — it can authenticate and
terminate TLS, which this interface cannot.

## Four things that will surprise you

1. **There is no delete, and no edit.** Correct a note by writing a new one that
   supersedes it. The review interface has no edit button on purpose.
2. **Writing a note fetches the URLs in it.** Once, in the background, so you
   still have the page later when it has moved or died. That means a write makes
   outbound requests — `--no-snapshots` turns it off, at the cost of never being
   able to capture those links again. Only public addresses are fetched:
   loopback, link-local, private and reserved ranges are refused, re-checked
   after every redirect, with no flag to allow them. A note is not a way to make
   your machine fetch things inside your network. If you genuinely want a
   private page preserved, paste it in triage.
3. **`context` searches content, not identifiers.** A note's id is in no index,
   so searching one finds notes that *mention* it, never the note itself. Use
   `read` for that; it recovers from a single mistyped word.
4. **The `score` on a result is a sort key, not a confidence.** It comes from
   rank alone, so a perfect match and a worthless one both score ~0.016 at rank
   1. `vector_similarity` is the number that can actually be low.

## Browser extension

`extension/` captures the page you're reading as evidence for a note, and
repairs captures that failed — useful for anything behind a login, where a
background fetch gets a paywall and your browser doesn't. See
[`extension/README.md`](extension/README.md).

## Tests

```sh
.venv/bin/python -m pytest
```

277 tests, no network access: the embedder and the fetcher are both injected.
Expected values are derived independently of the implementation
(`tests/reference.py` re-derives the load-bearing rules from the spec text and
imports nothing from `seshat`), and the assertions that matter have been checked
by mutating the implementation to confirm they turn red.

## Further reading

| | |
|---|---|
| [`docs/seshat-mcp-spec.md`](docs/seshat-mcp-spec.md) | the design and its rationale — the authority for everything here |
| [`docs/implementation-notes.md`](docs/implementation-notes.md) | what this build decides, interprets, and got wrong once |
| [`docs/retrieval-findings.md`](docs/retrieval-findings.md) | measured retrieval results, including two claims that did not survive measurement |
| [`eval/`](eval/) | the harness those numbers came from |

## License

© 2026 Ian Greenhoe, MIT. See [`CREDITS.md`](CREDITS.md) for third-party material.
