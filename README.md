# seshat-mcp

A persistent note store an LLM assistant can query and write cheaply.
Design and rationale: [`docs/seshat-mcp-spec.md`](docs/seshat-mcp-spec.md).

**Built against spec version 1.3.** The version is declared in the spec header
and recorded as `seshat.SPEC_VERSION`; `help` and `seshat info` report it
alongside the software and store versions, which move independently (§11.1).
`tests/test_spec_version.py` fails if the code ever claims a revision the
document has not reached.

**Status.** The full tool surface is implemented, retrieval is hybrid
(FTS5/BM25 and vector KNN fused with RRF), and snapshots capture, extract and
record what was expected of them. `help` reports what is actually live rather
than what is nominally supported — `vector: false` whenever the vector side is
unavailable, and an `unextracted` count so `thin: 0` cannot read as a clean
bill of health. See [Not yet built](#not-yet-built).

**Schema 4 has no migration path.** It consolidates versions 1–3: spec 1.3
needed a column on a table nothing had populated yet, and a one-time reset was
taken instead of a migration chain. An older store is refused with instructions
and left byte-for-byte untouched; `seshat reset` archives it (renames, never
deletes) so the next run creates a fresh one. **This was the last free schema
change** — once captures exist, every column is a real migration against
witnesses that cannot be regenerated.

## Install

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
```

Python ≥ 3.11. Runtime dependencies: `mcp`, `pydantic`, and optionally
`sqlite-vec` for vector search (`pip install -e '.[vector]'`). Nothing else —
the store is stdlib `sqlite3`, both HTTP clients are stdlib `urllib`, and the
BIP-39 wordlist is vendored.

Vector search additionally wants [Ollama](https://ollama.com) with
`ollama pull nomic-embed-text`. Both are optional at runtime: without either,
retrieval falls back to keyword search and says so.

## Run

```sh
seshat --db ~/notes/seshat.db serve          # MCP server on stdio
seshat --db ~/notes/seshat.db --no-snapshots --no-embeddings serve
```

As an MCP server (`claude mcp add`, or in a config file):

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

Without `--db` or `$SESHAT_DB` the store lands at
`~/.local/share/seshat/seshat.db`, with snapshots beside it at
`seshat.db.snapshots.db`.

## Tool surface

The six tools of §3 — `note`, `context`, `read`, `supersedes`, `chain`, and
`help`. The surface does not grow beyond that: maintenance lives in the CLI,
because every tool costs context on every turn.

```sh
seshat info                 # versions, capabilities, counts, pool size
seshat context "nyquist"    # search; empty query is a recency listing
seshat read <id>
seshat chain <id>
seshat why "polyphase"      # search assessment rationales (§5.5)
seshat misses               # id lookups that failed (§6.1)
seshat snapshots            # capture histogram (§6.6)
seshat fetch                # drain the capture queue now
seshat reindex-links        # rebuild the derived link index (§6.5)
seshat embed                # embed everything outstanding, now
seshat reembed              # discard and rebuild all embeddings (model migration)
seshat extract              # read captured bytes into text (§6.6)
seshat reextract            # re-run extraction over stored bytes
seshat paste <id> <url>     # supply content for a failed capture (§10.2)
seshat reset                # archive an unopenable store and start fresh
seshat review               # local web interface for reading the store (§10)
seshat check                # consistency report for human review (§7)
```

`seshat misses` classifies each failure, which is the point of the table:

| shown as | meaning |
|---|---|
| `corrupted` | a real id one word-edit away; benign, and the row caches the correction |
| `ambiguous` | several real ids equidistant; resolving would have been a guess |
| `FABRICATED` | nothing within distance — the id was invented, not mistyped |

## Snapshots, and the thing to be careful about

Writing a note extracts the URLs in its text and fetches them once, in the
background (§6.6). **This means a write triggers outbound network requests.** It
is on by default because capture is once-only — a link written while capture is
off is unrecoverable later — but `--no-snapshots` or `SESHAT_SNAPSHOTS=0` turns
it off entirely, and the write path never touches the network itself (a test
pins that).

Capture and extraction are separate stages and both run. Extraction happens
immediately after capture, because **the deadline is on detection, not
extraction**: bytes stay re-extractable forever, but a capture that turns out to
be a paywall interstitial or a JS shell can only be repaired while the page is
still live, and that cannot be known until something reads it.

Extraction is stdlib-only — no lxml, no build toolchain. It records a version
(`fetch:stdlib-html/1`), so adopting `trafilatura` later is `seshat reextract`
over stored bytes rather than a migration. Non-text media (PDFs, images) is
refused rather than decoded into mojibake that would look like a successful
extraction, and surfaces as `thin` for a human to paste.

**Each capture records what was expected of it** (§6.9). The markdown anchor
text states the expectation in the act of citing —
`[texas population data](url)` — and is copied into the snapshot, not read back
from the derived `link` table, so a later re-extraction cannot leave an old
capture judged against new intent. A degenerate label ("here", "this article")
falls back to the note's own desc, and which of the two was used is recorded,
because a miss means different things depending on it.

The check is lexical and **never sets `status`**. A statistical table can
contain none of the expected words and still be a perfect capture, so an unmet
expectation is a review candidate — it appears in `seshat check` as a `review`
row and in `read(with_sources=True)` as `expectation_met: false`.

Provenance is three-valued: `fetch` for the worker, `manual` for pasted content,
`browser` for a future extension. `seshat paste` supplies content for a failed
capture without seshat ever handling a credential — you are already
authenticated and already looking at the page.

> **The one genuinely dangerous line in this design.** `link` and `snapshot`
> share a `(note_id, target)` primary key and have opposite recoverability:
> `DELETE FROM link` is a safe rebuild that completes in seconds, and
> `DELETE FROM snapshot` is unrecoverable data loss. They live in **separate
> database files** so that the rebuild cannot reach the snapshot table even if
> someone edits the statement. Two tests assert the separation structurally —
> the notes database has no `snapshot` table and the snapshot database has no
> `link` table. Do not consolidate them.

## Consistency checking

`seshat check` runs §7's checks and prints a report for human review. It never
mutates, never auto-creates edges, never auto-fixes — and that is **enforced,
not promised**: every connection it opens is `mode=ro`, so a stray write is a
SQLite error rather than a silent correction to someone's belief history.

All ten structural checks of §7.1 plus both semantic checks of §7.2. The
highest-value one is *orphaned retraction*: A is retracted, but some descendant
asserted A's content at high `retained` and is itself un-superseded, so it now
rests on something known to be wrong. That is the mechanical consequence of
combining by maximum, and without the check §5.4's non-locality is a trap rather
than a feature.

The report ends with **where to start reading** (§7.3) — notes ranked by how
many findings implicate them, weighted by severity. §8 argues the store must
periodically be read in full; this says where to begin.

`--no-semantic` skips the embedding comparisons, `--similarity` sets the
suggested-link threshold (open question §9.5 — the default 0.90 is a guess),
`--json` emits findings as data, and `--fail-on error` exits non-zero so it can
run in a cron job.

A clean report says "No findings" and explicitly **does not** claim the store is
consistent: §7.1 validates structure, not truth, and §7.2 only generates
candidates.

## Review interface

```sh
seshat --db ~/notes/seshat.db review     # http://127.0.0.1:8765
```

**Reading the store is the point** (§10.1). §8 concedes that periodic full
reading is the only growth mitigation with a mechanism behind it, and nobody
does that against a SQLite CLI. The home page offers the two handles the spec
names: **open loops** (oldest notes nothing has superseded) and **most cited**
(inbound references — the one retroactive salience measure the design has).
Failure triage is the secondary function, not the reason it exists.

Three things are enforced rather than intended:

- **The notes database is opened read-only.** §10.3 requires that the UI never
  permit editing a note and warns the absence will look like an oversight. A
  connection that physically cannot write is not an affordance anyone relaxes by
  accident — and the note page says why there is no edit button, rather than
  leaving it a puzzle. Snapshot repair uses a separate writable connection to
  the separate snapshot file.
- **127.0.0.1 only.** Binding anything else raises; remote transport is out of
  scope (§12) and would demand an auth model this tool has no business owning.
- **POSTs are origin-checked and token-guarded.** "localhost is safe" stops
  being true the moment a browser is a client — any page you visit can post to
  127.0.0.1. §10.2 names this for the future extension; it applies the instant a
  POST endpoint exists, which is now.

### One interpretation worth knowing about

§10.3 says "a snapshot that already holds content is immutable", and the paywall
case sits exactly on that line: an interstitial *is* content, and a truthful
record of what the URL served an anonymous fetcher. Refusing the paste would
break §10.2's main use; allowing a blind overwrite would destroy evidence.

The rule implemented: **the captured bytes are what is immutable.** A machine
extraction is a derived reading of those bytes and can always be regenerated, so
a human may replace it — `raw` is never touched, and `extraction='manual'`
records who read it. A pasted reading is never replaced afterwards, because
pasted text cannot be regenerated from anything. Both witnesses survive, which
is what the section is protecting. Flagged here because it is an interpretation,
not the spec's letter.

## Retrieval

FTS5/BM25 and vector KNN run independently and fuse with reciprocal rank
fusion, `score = Σ 1/(k + rankᵢ)` with k=60 (§6). RRF is rank-based, so BM25
scores and cosine distances never have to be made commensurable — there is no
invented calibration anywhere in the path. `k` is **fixed, not a knob**:
measured invariant from 5 to 300.

Sum, not maximum: a note both retrievers rank second beats a note one ranks
first, which is the entire reason for running two of them.

Each result carries three values and they are not interchangeable (§3.2):

| field | what it is |
|---|---|
| `score` | fused rank value. **Sort key only.** A perfect match and a worthless one-token match both score ~0.016 at rank 1 |
| `vector_similarity` | raw cosine, or null if unembedded / vector side down. The number that can actually be low |
| `matched` | which retrievers contributed — `["fts"]`, `["vector"]`, or both |

On an empty query all three are **null, never zero** (§3.2): no ranking was
fused and no query vector exists, so they are undefined rather than low. Zero
would read as "nothing matched" when nothing was asked.

Measured against 20 queries the store cannot answer: `score` gives no signal at
all (identical values appear in answerable and unanswerable sets), while
`vector_similarity` medians separate 0.699 against 0.517 and an unanswerable
query's top hit is usually vector-only. Both are triage inputs, not thresholds
to hard-code — the ranges overlap. See `docs/retrieval-findings.md` §6b.

**`context` searches content, not identifiers.** A note's id is in neither the
FTS columns nor the embedding, so passing an id returns notes that *mention* it,
never the note that has it. Since that is silent, an id-shaped query gets a note
in the response pointing at `read`.

**Degradation is a first-class path, not an error** (§6.3). No `sqlite-vec`, no
Ollama, a cold model, or simply nothing embedded yet all produce the FTS
ranking alone — same score shape, so a caller's triage stays calibrated — and
`help` reports `vector: false` so the degradation is visible rather than
silently worse. A failed query embedding also puts the vector side on a 30s
cooldown, without which every subsequent `context` would pay the timeout while
Ollama is down.

Embedding is asynchronous and off the write path (§6.3): `note()` commits and
returns, a worker catches up. A note is keyword-findable immediately and
semantically findable seconds later. **Unembedded is a legitimate state**, and
the backlog is a query — a row absent from `embedding_meta`, or carrying a
different `model`, needs embedding — which is what makes model migration a
`DELETE` plus a drain rather than a schema change.

The nomic task prefixes (`search_document:` / `search_query:`) are applied by
two module-level functions and **a backend cannot apply them at all**: an
`Embedder` implements only `embed(texts)` and receives fully-prefixed text.
§6 warns the convention is easy to get wrong in exactly one code path, so there
is exactly one code path.

## Implementation notes

Things a reader of the spec would want to know about this build. Spec sections
in brackets.

- **Pool membership is a `LEFT JOIN` with `COALESCE(MAX(...), 1.0)`** [§5.1].
  An inner join type-checks, runs, and silently drops every never-superseded
  note — i.e. most of the store — while `context` keeps returning the revised
  ones and looks fine.
- **`retained` combines by maximum across paths** [§5.2], tested by shuffling a
  fixed edge set through six insertion orders and asserting identical pool
  membership — associativity, commutativity and idempotence in one test.
- **No aggregation along paths** [§5.3]. `chain` returns raw per-edge scores.
- **Re-assessment is append-only** [§5.5], latest-wins via a view.
- **Truncation is always visible** [§3.3], including `backlinks_total` — a hub
  note is exactly the case that overflows.
- **Cycles are rejected at write time** [§3.4] by reachability check.
- **Extraction never fails a write** [§6.4]. Malformed markdown yields fewer
  links, never an error; fenced code blocks are skipped so a URL in a sample
  is not mistaken for a reference.
- **Capture never fails a write** [§1, §6.3]. The note commits first, captures
  queue after, and a broken snapshot store degrades to "no witness".
- **The capture queue is durable**, so a crash between writing a note and
  fetching its links does not lose a capture that can never be taken again.
- **Snapshots are immutable** [§6.6]. A re-queued capture cannot overwrite an
  earlier witness; two notes citing one target get two rows, and comparing them
  is the drift test.
- **Errors carry their guidance to the model**: a store error becomes an MCP
  `ToolError`, so `NoteNotFound` arrives as "use `context()` to find it by what
  it was about" rather than a generic crash.
- **Logging is pinned to stderr**, with a test that reads the raw pipe and
  parses every line as JSON-RPC.
- **One connection plus one lock.** Sync tool bodies run in a worker thread
  pool, so the connection is opened `check_same_thread=False`; the lock spans
  whole operations, because `resolve` → cycle-check → `INSERT` is only atomic
  if nothing interleaves.

### Places this build decides something the spec left open

1. **FTS queries are OR, not AND** — recall with BM25 ranking, rather than a
   conjunctive filter that returns nothing on a five-word question. Tokens are
   quoted, so no caller string reaches FTS5's query syntax. Function words are
   dropped from the expansion, for a reason found the hard way: see below.
2. **`near_miss` distinguishes ambiguity from fabrication** within the spec's
   columns: `candidate` NULL with a non-NULL `distance` means several real ids
   were equidistant; both NULL means nothing was in range.
3. **`raw` is an extra table in the snapshot database**, holding the bytes the
   spec's `snapshot` row does not have a column for. It is the hedge that makes
   extraction re-runnable, and a future extractor fills the derived columns
   without rewriting the witness.
4. **A migrated store has no `created_under_spec`** — it was created before the
   `meta` table existed, and inventing a value would be worse than a null.

`θ` defaults to 0.8 [§9.2] — a starting point with no principled basis,
tunable with `--theta`.

### One defect found by running it, and the fix

Live hybrid retrieval on a five-note corpus ranked *"Catastrophic cancellation
in tail energy"* first for the query **"combining two rankings"**, above the
note actually about rank fusion — which the vector side had correctly ranked
first.

The cause is an interaction, not a bug in any one part. `OR` expansion lets a
single token retrieve a document, the only matching token was **"two"**, and RRF
is rank-blind: it cannot see that the keyword match was worthless, so a spurious
rank-1 hit contributed exactly as much as a good one, and corroboration across
the two retrievers then promoted the wrong note above the right one.

The fix is at the source — function words are dropped from the FTS expansion,
keeping them only when a query consists of nothing else (so "one two three"
still finds the note that says it). All four probe queries rank correctly after
it. Worth stating plainly: this was found on a corpus of five notes, so the
*frequency* of the failure is unmeasured. The mechanism, though, is general —
function word plus OR plus rank-blind fusion — and does not depend on corpus
size.

### One spec claim this build could not verify

§6 (through spec 1.1) said omitting nomic's `search_document:` / `search_query:` prefixes, or using
the same one on both sides, "degrades retrieval measurably and silently". A
probe against the live model (15 notes, 12 queries, four prefix schemes) could
not detect any difference — all four schemes scored within noise of each other.
That is a **null result, not a refutation**: twelve queries cannot resolve an
effect this size. The prefixes are the documented usage and cost nothing, so
they are applied as specified — but spec 1.2 withdrew the word "measurably",
and a second attempt at 42 queries came out 3 wins to 2 losses paired, which is
a coin flip.

## Tests

```sh
.venv/bin/python -m pytest
```

252 tests, no network access — the embedder and the fetcher are both injected,
and the server tests run the subprocess with `--no-snapshots --no-embeddings`
so nothing in the suite reaches Ollama or the web. Expected values are derived independently of the
implementation (`tests/reference.py` re-derives pool membership, latest-wins
resolution, heads and Levenshtein distance from the spec text and imports
nothing from `seshat`), and the load-bearing assertions have been checked by
mutating the implementation to confirm they turn red — including the pool join,
the max/min rule, truncation counts, silent id resolution, the store lock,
stdout discipline, snapshot immutability, write-path isolation, the task
prefixes, strip-vs-index, vector degradation, and sum-vs-max fusion.

## Not yet built

- **The browser extension** [§10.2] — increment 6. Eliminates the capture
  deadline entirely: the browser supplies content at write time, so the gap
  between reading a page and fetching it is zero.
- **Growth management** [§8] — unsolved in the spec, deliberately not guessed
  at here.

## License

The spec is © 2026 Ian Greenhoe, MIT. See `CREDITS.md` for third-party material.
