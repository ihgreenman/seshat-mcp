# Implementation notes

Detail that a reader of the spec would want about *this build* — decisions,
interpretations, and the places running it taught us something. The README is
the front door; this is the part you read before changing the code.

Spec section references are to [`seshat-mcp-spec.md`](seshat-mcp-spec.md).

## Status

All six increments of §11.3 are implemented, against spec 1.3 (software 0.3.0,
store schema 4, snapshot schema 3).

**Schema 4 has no migration path.** It consolidates versions 1–3: spec 1.3 needed
a column on a table nothing had populated yet, and a one-time reset was taken
instead of a migration chain. An older store is refused with instructions and
left byte-for-byte untouched; `seshat reset` archives it (renames, never deletes)
so the next run creates a fresh one. **That was the last free schema change** —
once captures exist, every column is a real migration against witnesses that
cannot be regenerated.

The one narrow exception is the snapshot database, which takes additive
migrations: creating a new table rewrites no row and touches no witness, which
is a different and much safer class of change than altering `snapshot` itself.

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
`browser` for extension captures. `seshat paste` supplies content for a failed
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
- **It uses the same retrieval path as everything else.** The interface builds
  its stores with the configured embedder, so search here is hybrid rather than
  keyword-only — and when the vector side cannot answer, the search page says
  so rather than quietly returning worse results. The process also runs its own
  embedding worker, because an extension capture writes a note *here*, not in
  the MCP server, and would otherwise sit unembedded until something else ran.
- **POSTs are origin-checked and token-guarded.** "localhost is safe" stops
  being true the moment a browser is a client — any page you visit can post to
  127.0.0.1. §10.2 names this for the extension; it applies the instant a
  POST endpoint exists.

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

## Browser extension

`extension/` is a Manifest V3 extension implementing §10.2's two jobs: **capture**
(read a page, write the note there and then, DOM attached) and **repair** (open
the page you are already authenticated on, click once). See
[`../extension/README.md`](../extension/README.md) to install it.

**It removes the capture deadline rather than reducing friction.** §6.6's
deadline exists because of the gap between reading a page and fetching it
afterwards; through the extension that gap is zero, so nothing is queued, `thin`
is decidable immediately, and the recovery window never opens.

It writes a note with evidence attached — a second front door to `note()`, not a
new model — and then **stops**. There is no "and what do you think?" prompt,
because a capture and an analysis are deliberately separate notes (§6.10). The
popup asks *what the page told you*, not what the page is.

Enable/disable with `seshat review --no-capture-api`; `seshat token` prints the
shared secret. The endpoints require a bearer token **and** a recognised origin,
so an ordinary web page is refused even holding the token — "localhost is safe"
stops being true the moment a browser is a client.

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
to hard-code — the ranges overlap. See [`retrieval-findings.md`](retrieval-findings.md) §6b.

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

## Correctness details

Spec sections in brackets.

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

### A second defect found by running it: a silently dropped key

Reported from real use by another assistant writing into the store. It passed
`rationale` — the *column* name from §5.5's schema — where the argument is
`why`. Because the `supersedes` entries were typed `dict[str, Any]`, the schema
said `additionalProperties: true`, the key was accepted, and
`edge.get("why")` simply never looked at it. The edge was written with a null
rationale and the call reported success.

That is a priority-1/2 failure rather than a missing feature: the caller was
told its reasoning had been recorded and it had not been. It was caught only
because that caller re-read its own work with `chain`, which is not a habit
anything can rely on.

The spec sets this trap itself — §5.5's parenthetical explains that the column
is named `rationale` only to avoid colliding with the `note` table, so a reader
of the schema has every reason to reach for that word.

The fix is in the store rather than the tool, because the MCP surface is not
the only caller: an unknown key in a `supersedes` entry is now an error naming
the confusion ("`rationale` (did you mean `why`?)"), the required keys report
themselves by name instead of raising a bare `KeyError`, and validation runs
over the whole list before anything is resolved or written — a note carrying
half its requested edges would be the same lie one table over. The tool schema
declares `additionalProperties: false` to match, and a test compares the
schema's property set against the store's constant so the two cannot drift.

Generalising past the one key: the rule is that **ignoring an input is never a
silent success**. Anywhere a caller-supplied structure is read field by field,
the fields not read have to be an error.

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

## Not yet built

All six increments of §11.3 are implemented. What remains is not code:

- **Growth management** [§8] — unsolved in the spec, and deliberately not
  guessed at here. Inbound reference count and open-loop listing are surfaced
  in the review interface as the two handles the spec names; automated pruning
  waits on real usage.
- **θ** [§9.2] and **the suggested-link similarity threshold** [§9.5] — both
  wait on the near-duplicate regime, which §9.5 says not to synthesise. The
  store is running; the real thing arrives on its own.
- **A better extractor** — the stdlib one leaks boilerplate on sites that use
  `div`s rather than semantic elements. It records a version, so switching to
  `trafilatura` is `seshat reextract` over stored bytes, not a migration.
