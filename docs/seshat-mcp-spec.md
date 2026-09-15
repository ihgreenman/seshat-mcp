# seshat-mcp — Design Specification

Named for the Egyptian goddess of writing, measurement, and record-keeping.
Distribution name `seshat-mcp`; CLI and MCP server identifier both `seshat`.
Note that `seshat` is taken on crates.io (a Matrix event indexer), which
matters only if the implementation goes to Rust.

**Spec version: 1.2.** See §11 for versioning discipline and history.

**Status:** implementation at increment 2 (software 0.2.0, store schema 3).
1.2 revises §6 against measured retrieval results; all changes are additive
or narrowing-of-claims. No signature removed, no migration required.

**Provenance:** this design emerged from a design conversation between Ian Greenhoe
and Claude (Opus 5), September 2026. Decisions and their rationale are recorded below;
open questions are recorded as open rather than resolved by fiat.

**Copyright © 2026 Ian Greenhoe.** Licensed under the MIT License —
attribution required. The copyright and permission notice must be preserved
in copies and in substantial portions of this document and of any
implementation derived from it.

---

## 1. Purpose

A persistent note store that an LLM assistant can both **query cheaply** and
**write to cheaply**, giving it continuity across sessions.

The unit is a note, not a file. This is not a document indexer and not a
general artifact tracker; notes may *reference* external artifacts by content
hash, but the store does not manage them.

Design targets, in priority order:

1. **Writing is nearly free.** Every required argument on the write path
   reduces the number of notes that get written. Retrieval is where
   complexity is spent.
2. **The store never silently lies.** A reader must always be able to tell
   whether what they are holding is current.
3. **Small enough to read in full.** A store that is never read end-to-end
   cannot be trusted. Growth management is a first-class concern (§8).

---

## 2. Core model

**Append-only with supersession.** Notes are immutable. There is no update
and no delete. Every edit — correcting an error, adding information,
improving a bad description — is expressed by writing a new note that
supersedes one or more old ones.

Consequences worth stating explicitly:

- The history of what was believed, and when, is preserved by construction.
- "I was wrong about X, here is why" is a first-class note rather than a
  deletion.
- Supersession is the *only* mutation mechanism, so there is no second edit
  path to keep consistent with it.

**The structure is a DAG, not a chain.** A note may supersede several notes
(a merge). A note may be superseded by more than one note (a fork — which
happens by accident when you supersede something you had forgotten was
already superseded). "The current version" is therefore a *set* of heads,
not a single note. The store must not assume uniqueness anywhere.

**Every supersession edge carries a `retained` score** in [0, 1]: the
fraction of the old note that survives in the new one.

- `1.0` — the new note adds; the old is still entirely correct
- `~0.5` — the old note was partly right
- `0.0` — full retraction; the old note was wrong

The name is deliberate. It measures survival of the *old* note. Confidence
in the *new* note is a different quantity and is not represented here.

---

## 3. Tool surface

Six tools: five data operations and `help`. This is the whole interface.

### 3.1 `note(desc, text, supersedes=[]) -> id`

Creates a note.

| field | type | notes |
|---|---|---|
| `desc` | string, required | The recognition handle. See §4. |
| `text` | string, required | The body. |
| `supersedes` | list of `{id, retained, why?}`, optional | Default empty. `why` optional; see §5.5. |

Supersession is folded into creation deliberately: a separate second call
is a call that gets forgotten, and the failure mode is a store holding two
contradictory notes with no edge between them. Creation and its edges are
atomic.

Returns the new note's id.

### 3.2 `context(text, since=None, limit=N) -> [(id, desc, score)]`

Primary retrieval. Hybrid search (§6) over the note pool.

- `text` — the query. **May be empty**, in which case the call degrades to
  a recency listing. This is how session-start orientation works without a
  sixth tool.
- `since` — timestamp lower bound, optional.
- `limit` — result cap.

**Returns the fused score with each result.** A bare list implies that
everything in it is relevant, and a reader will act accordingly.

**But the RRF score cannot carry confidence, and must not be presented as
if it does.** `Σ 1/(k + rank)` is derived from position alone: a perfect match
at rank 1 and a worthless single-token match at rank 1 receive *identical*
scores. Measurement confirms it — the fused score is invariant across a 60×
range of `k` (§6.8), which is what a number encoding almost nothing looks
like. Rank-blindness in fusion (§6.7) propagates straight to the output.

So `context` returns three things per result, not one:

| field | meaning |
|---|---|
| `score` | fused RRF value. **Sort key only.** Comparable within a result set, meaningless across them. |
| `vector_similarity` | raw cosine of the best-matching embedding, or null if unembedded. This is the value that can actually be *low*. |
| `matched` | which retrievers contributed — `["fts", "vector"]`, `["vector"]`, etc. |

`vector_similarity` is what makes honest triage possible. Without it, a caller
handed five results when the store contains nothing relevant has no way to
say so.

**`context` searches content; it does not resolve identifiers.** Note ids
appear in neither the FTS columns nor the embedding input, so
`context("olive-canvas-bright-zebra")` can only return notes whose *text*
mentions that id — never the note that has it. This is deliberate (§6.1) and
it is the correct division, but it is silent, so: when a query is shaped like
an id — four hyphenated lowercase words — the response carries a note saying
so and pointing at `read`. Detection is free and requires no index change.

Returns `desc`, never bodies. Bodies are paid for individually via `read`.

**Pool membership is governed by `retained` (§5.1)** — the mechanism that
keeps additive supersession from silently deleting correct information.

### 3.3 `read(id, edge_limit=N, with_sources=False) -> record`

Returns the full record, not just the body:

| field | notes |
|---|---|
| `desc` | |
| `text` | |
| `created_at` | |
| `supersedes` | list of `{id, retained, rationale}`, truncated to `edge_limit` |
| `supersedes_total` | count before truncation |
| `superseded_by` | list of `{id, retained, rationale}`, truncated to `edge_limit` |
| `superseded_by_total` | count before truncation |
| `heads` | set of current head ids reachable from this note |
| `resolved_by_proximity` | present only on a near-miss resolution; see §6.1 |
| `links` | outbound references extracted from this note's text — `(kind, target, label)` (§6.5) |
| `backlinks` | notes whose text references this one — `(id, desc)`, truncated to `edge_limit` |
| `backlinks_total` | count before truncation |
| `sources` | snapshots for this note's links; only when `with_sources` (§6.6) |

`superseded_by` is not optional. Ids leak forward within a conversation; a
reader will hold an id from many turns earlier and act on it. A `read` that
returns only the body makes stale-but-confident use of a retracted note the
default behaviour.

**Truncation must be visible.** The `_total` counts are not optional: a
reader shown five of twelve supersessions will conclude the note absorbed
five things, which violates priority 2 more cheaply than almost anything else
in the design. The same applies to `backlinks`, where a hub note is exactly
the case that overflows — a truncated list without its count reports a hub as
a leaf.

Expected degree distribution is mild. `superseded_by` is ~0 for active notes
and ~1 otherwise — it exceeds 1 only by accident, when something already
superseded is superseded again. `supersedes` is the merge direction and is
the one that can legitimately grow, since consolidating scattered notes into
one is the recommended response to §8. Truncation order within a list is
unresolved; `retained` descending is the current guess.

**Backlinks are not optional.** Outbound references are visible by reading the
note; inbound ones are invisible without a reverse index, and they are the
higher-value direction — a note six later notes point at is a hub, and that is
undiscoverable by reading any of the seven. Storing reference edges without
surfacing them would leave the connections in the data and the reconstruction
work with the reader, which is the failure this design exists to avoid.

Reference edges stay out of `chain` (§3.5). Mixing "revision of" with "related
to" in one traversal blurs the distinction that makes supersession mean
anything.

`heads` is returned directly to save round trips — without it, walking
A←B←C from A costs two extra calls, and the likely outcome is that the
reader gives up and uses the stale note. It is a set because of §2.

### 3.4 `supersedes(old_id, new_id, retained, why=None) -> null`

Retroactive supersession: recording, later, that an existing note
invalidates an earlier one. Genuinely distinct from the creation-time case
(§3.1) and not reducible to it.

Called on a pair that already has an edge, this records a **re-assessment**
rather than failing (§5.5).

**Must reject edges that would create a cycle.** Creation-time supersession
cannot cycle, since a new note has no descendants. Retroactive supersession
can, and a cycle makes head resolution non-terminating. This is a write-time
rejection, not a checker finding (§7.1).

### 3.5 `chain(id) -> {nodes, edges}`

Returns the ancestor and descendant closure of a note.

- `nodes`: `[(id, desc)]` — same shape as `context`, no bodies
- `edges`: `[(old_id, new_id, retained, rationale)]` — raw scores, current
  assessment only; full assessment history on request

**Does not aggregate `retained` along paths.** See §5.3.

### 3.6 `help() -> {versions, capabilities}`

Reports what this server actually is. Six tools rather than five; justified
because it is not a data operation and because everything else in the surface
is meaningless without knowing which contract it honours.

| field | notes |
|---|---|
| `spec_version` | which revision of *this document* the server implements |
| `software_version` | the implementation's own semver |
| `store_version` | schema version of the open database (§11.2) |
| `capabilities` | see below |

**Capabilities are not optional.** During incremental development a server
legitimately implements the full 1.1 tool surface with no embedder — see the
build order in §11.3 — and reporting a bare `spec_version` would overstate it.
At minimum:

```json
{ "vector": false, "checker": false, "snapshots": false,
  "embedding_model": null, "embedding_backlog": 0,
  "snapshot_status": { "pending": 0, "ok": 0,
                       "unreachable": 0, "gone": 0, "thin": 0 } }
```

`embedding_backlog` is the count of rows missing from `embedding_meta`. A
caller seeing a nonzero backlog knows `context` is currently FTS-weighted and
can say so rather than silently returning worse results.

**Snapshots need a histogram, not a counter, and the asymmetry is the point.**
A backlog counts *pending* work. A fetch that failed permanently has already
left the backlog, so a bare counter reads zero while the data is missing —
which is exactly the silent failure the preservation model exists to prevent.
Data that silently fails preservation is not data that can be relied on.

The three failure states differ in remediation, which is why they are not
collapsed:

- `unreachable` — transient; retryable.
- `gone` — the target was already dead at capture. Permanent, and itself
  information.
- `thin` — extraction yielded implausibly little (§6.6). **Needs human
  attention while manual recovery is still possible**, which is a deadline the
  other two do not have.

Embedding gets only a counter because embedding failures are local,
deterministic, and always recoverable by re-running. Snapshot failures often
are not. Same async shape, opposite permanence.

`help` returns versions and capabilities only — not tool documentation. Tool
descriptions already carry that, and duplicating them costs context on a call
whose whole purpose is cheapness.

---

## 4. On `desc`

`desc` is the most important field in the system. It is the only thing
visible during triage, so every retrieval decision is made on it alone.

It should be written **to be recognised later**, not to summarise. The
difference is large in practice:

- Good: `Bilinear transform loses precision near Nyquist`
- Bad: `notes on filter precision`

Assistants drift toward the second form unless the tool description
explicitly instructs otherwise. The `note` tool's MCP description should
carry this guidance and an example pair.

A bad description is fixed by superseding the note with `retained=1.0` and
a better `desc`.

---

## 5. `retained` semantics

### 5.1 It drives retrieval, not just annotation

This is the load-bearing consequence. In the additive case (`retained=1.0`),
the new note may contain only the addendum and not restate the old note at
all. If `context` excluded the old note anyway, correct information would
be silently lost.

The pool membership rule:

```
retained(A) = 1.0                          if A has no outgoing edges
            = max over A's direct edges     otherwise

A ∈ context pool  ⟺  retained(A) ≥ θ
```

θ is a tuning parameter; no principled basis for an initial value (§9).

Note the scope: **direct edges only, no path composition.** Whether a distant
descendant D reaches A via B or via C does not affect A's membership. The
diamond case resolves without traversal.

### 5.2 Combination across paths is maximum

`retained` is an *existential* claim — does this content survive somewhere —
not a universal one. Maximum is the existential quantifier over edges;
minimum would be the universal.

This pairs with §5.3: minimum is the upper Fréchet bound along a path,
maximum combines across paths. Together they are Zadeh's min/max algebra,
which makes the scheme a coherent fuzzy logic rather than two unrelated
heuristics.

Three properties that matter for an append-only store:

- **Order-independent.** Max is associative, commutative, and idempotent, so
  the score does not depend on edge insertion order, and recording the same
  supersession twice is a no-op. In a store where edges cannot be retracted,
  this is worth more than it sounds.
- **Fails toward keeping.** Adding an edge can only raise the maximum, so the
  rule never silently drops a note that some live descendant still depends on.
  Given the asymmetry — a stale note in the pool costs bounded noise and is
  visible via `read`, a dropped live note costs invisible information loss —
  this is the right direction for the error to run.
- **Monotone under edge addition.** Adding edges can only gain membership,
  never lose it. Re-assessment of an existing edge (§5.5) can lower a score
  and drop a note from the pool — deliberately, and with a record.

*Relation to the C++ diamond:* structurally the same fork-and-recombine, but
semantically simpler. There, the question is about identity and layout — one
`A` subobject or two — and `virtual` inheritance is the machinery for
choosing. Here there is only ever one `A` note, so the whole question reduces
to an aggregation rule.

### 5.3 No aggregation along paths

Given A←B with `r₁` and B←C with `r₂`, the fraction of A surviving in C is
**not determined** by `r₁` and `r₂`. It depends on whether the two revisions
cut overlapping or disjoint content, which the store has no evidence about.
The Fréchet bounds are:

```
max(0, r₁ + r₂ - 1)  ≤  r  ≤  min(r₁, r₂)
```

Łukasiewicz t-norm on the left, minimum on the right, product between them
under an independence assumption that is not justified here.

Two hops at 0.5 give `[0, 0.5]` — already too wide to act on. Any single
aggregated number would be false precision, and a reader would treat it as
real. `chain` therefore returns raw edge scores and leaves judgement to the
caller.

### 5.4 The price of maximum: retraction is non-local

If B supersedes A at 1.0, and you later discover A was wrong and write C
superseding A at 0.0, the maximum keeps A in the pool at 1.0 — and correctly
so, because B asserted A's content and B is therefore contaminated too.
Retracting A properly means also superseding B.

This is more work, but it surfaces the dependency rather than letting a
retraction quietly pull the ground out from under live notes that rest on it.
Treat it as correct behaviour and accept the ergonomic cost; §7.1 makes the
required follow-up detectable. If it proves intolerable in practice, the
escape hatch is a hard retraction flag, which is a different thing from a
`retained` score and should not be smuggled into one.

### 5.5 `retained` is itself append-only

A score is a judgment about a relationship, made at the moment the
relationship is created — under the same handicap that makes note salience
unknowable at write time (§8). It will sometimes be wrong, and §5.4
guarantees you revisit old edges whenever a retraction propagates.

Requiring a whole new note to correct a number is absurd. Overwriting the
number in place destroys the audit trail that append-only exists to preserve,
and the score *is* a belief — silently editing it puts a hole in exactly the
record the design is built to keep. So an edge is not `(old, new, retained)`
but `(old, new)` plus a history of assessments, latest wins. Same mechanism
as notes, with latest-wins resolution instead of DAG heads.

Each assessment carries an **embedded rationale**: free text, inline in the
row, explaining why that score. A history of bare numbers is uninterpretable
— `0.8 → 0.3` records that a belief moved but nothing about what moved it,
which defeats most of the point of keeping the history.

The rationale is deliberately *not* a reference into `note`. A note about a
score is meta-content competing in `context` against real findings, which is
exactly the dilution §8 warns about, and it would need its own supersession
chain to stay correct. Embedded, it is immutable with its assessment — also
semantically right, since the rationale belongs to that assessment rather
than to the edge.

It is indexed in FTS but **not** in the vector table: findable when looked
for, never competing with notes in `context`.

(The column is named `rationale` rather than `note` only to avoid collision
with the `note` table in queries.)

No new tool surface: `supersedes(old, new, retained, why=None)` on an
existing pair records a new assessment rather than erroring, and
`note(supersedes=[...])` records the first. `why` is optional — the write
path stays cheap (§1).

Whether this earns its keep is unknown. If re-assessment proves rare, it is
machinery for an event that does not happen, and a hand-written `UPDATE`
would have covered it. It is specified anyway because the schema cost is one
table and a view, and history cannot be retrofitted onto data that has
already been overwritten.

---

## 6. Storage and retrieval

SQLite, single file. Schema sketch:

```sql
-- Store identity. Written at creation, bumped only on migration. §11.2
CREATE TABLE meta (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);
-- rows: store_version, created_under_spec, created_at

CREATE TABLE note (
  id          TEXT PRIMARY KEY,   -- four BIP-39 words, hyphenated. §6.1
  desc        TEXT NOT NULL,
  text        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX note_created ON note(created_at);   -- ids are not time-sortable

-- An edge is (old_id, new_id); its score is the latest assessment. §5.5
CREATE TABLE assessment (
  old_id       TEXT NOT NULL REFERENCES note(id),
  new_id       TEXT NOT NULL REFERENCES note(id),
  retained     REAL NOT NULL CHECK (retained BETWEEN 0 AND 1),
  rationale    TEXT,            -- why this score; embedded, not a note ref
  asserted_at  TEXT NOT NULL,
  PRIMARY KEY (old_id, new_id, asserted_at)
);

CREATE VIEW supersession AS
  SELECT old_id, new_id, retained, rationale, asserted_at
  FROM assessment a
  WHERE asserted_at = (
    SELECT MAX(asserted_at) FROM assessment b
    WHERE b.old_id = a.old_id AND b.new_id = a.new_id
  );

CREATE VIRTUAL TABLE note_fts USING fts5(desc, text, content='note');

CREATE VIRTUAL TABLE note_vec USING vec0(
  note_id TEXT PRIMARY KEY,
  embedding float[768]              -- nomic-embed-text; see §6.2
);

-- Embedding provenance. A row's absence, or model != current, means
-- "needs embedding". Model migration is a DELETE plus a worker drain. §6.3
CREATE TABLE embedding_meta (
  note_id     TEXT PRIMARY KEY REFERENCES note(id),
  model       TEXT NOT NULL,
  dim         INTEGER NOT NULL,
  normalized  INTEGER NOT NULL,
  embedded_at TEXT NOT NULL
);

-- Derived index over note text. Rebuildable, disposable. §6.5
CREATE TABLE link (
  note_id  TEXT NOT NULL REFERENCES note(id),
  kind     TEXT NOT NULL,         -- 'url' | 'hash' | 'note'
  target   TEXT NOT NULL,
  label    TEXT,                  -- markdown anchor text, if any
  PRIMARY KEY (note_id, kind, target)
);

-- A witness: what the target said when this note was written. Immutable.
-- Same key as `link`, opposite recoverability. NEVER dropped by an
-- extraction rebuild. Lives in a separate database file. §6.6
CREATE TABLE snapshot (
  note_id       TEXT NOT NULL,
  target        TEXT NOT NULL,
  captured_at   TEXT NOT NULL,
  status        TEXT NOT NULL,    -- 'ok' | 'gone' | 'unreachable' | 'thin'
  title         TEXT,
  text          TEXT,             -- extracted, truncated to the cap
  full_hash     TEXT,             -- over the COMPLETE extraction, not `text`
  full_length   INTEGER,
  raw_length    INTEGER,
  extraction    TEXT,             -- extractor name/version
  PRIMARY KEY (note_id, target)
);

-- Lookups that missed. §6.1
CREATE TABLE near_miss (
  requested    TEXT NOT NULL,
  candidate    TEXT REFERENCES note(id),  -- NULL = nothing within distance
  distance     INTEGER,
  first_seen   TEXT NOT NULL,
  hit_count    INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (requested)
);
```

**Embedding:** whole note (`desc` + `text` concatenated). Notes are short,
so chunking is unnecessary — this is the main simplification relative to a
document indexer.

**Model:** `nomic-embed-text` via Ollama (768-dim, Apache-2.0). Chosen on
ecosystem grounds — by far the most-pulled Ollama embedding model, so
integration problems are already answered somewhere. Alternatives:
`qwen3-embedding` (Matryoshka truncation, so dimensions can change later
without re-embedding), `bge-m3` (MIT).

**A causal LM is not an embedding model.** Ollama will return a vector for
any model, including reasoning models, by pooling hidden states. Decoder-only
models without contrastive training produce anisotropic spaces dominated by
token frequency, so semantic similarity degrades toward lexical overlap —
which FTS5 already does better. Only pull from Ollama's embedding library.

**nomic requires asymmetric task prefixes.** `search_document:` when storing,
`search_query:` when querying. Ollama does not add them. Qwen3 is likewise
instruction-aware on the query side only. Keep the prefixes in one place; they
are easy to get wrong in exactly one code path.

This is the documented usage and costs nothing, which is why it is specified.
**The size of the effect is unmeasured.** Two attempts to detect it found
nothing defensible: at 40 notes and 42 queries, correct-versus-both-document
shows +0.043 MRR but 3 wins against 2 losses paired, which is a coin flip.
Earlier revisions of this document claimed the difference was "measurable";
that claim is withdrawn. If it matters it will show at larger scale or in the
near-duplicate regime (§8), neither of which has been tested.

(nomic's Ollama card lists 2,048 context against a native 8,192; set
`num_ctx` if that ever matters. It does not, for notes.)

**Hybrid retrieval:** run FTS5/BM25 and vector KNN independently, fuse with
reciprocal rank fusion:

```
score = Σᵢ 1 / (k + rankᵢ),  k ≈ 60
```

`k = 60` is **not a tuning parameter.** Measured, MRR is identical across
k ∈ [5, 300] — a 60× range. With two rankings over a store of this size, `k`
only reweights deep ranks against shallow ones, and nearly every answer sits
in the top three of at least one ranking. Leave it alone; retrieval quality
does not live there. Revisit only if answers routinely appear at rank 10+.

RRF is rank-based, so incommensurable score scales never have to be
reconciled. That freedom has a price, paid in §6.7.

### 6.1 Identifiers

Ids are **four BIP-39 English words, hyphenated** (`bright-otter-canvas-fig`).

Rationale is LLM-specific: random base32 fragments into junk tokens and gets
reproduced unreliably; word sequences are native tokens and are reproduced
near-perfectly. BIP-39 is preferred over the EFF diceware lists because every
word is uniquely determined by its first four letters, visually confusable
pairs are already removed, and there is no attribution requirement.

**Why four and not three.** The risk is not insert collision but a *corrupted
lookup landing on a real note*. The one-word-substitution neighbourhood has
size `k × 2047`. At 10,000 notes:

| words | space | neighbours | P(corruption hits a real note) |
|---|---|---|---|
| 3 | 2048³ ≈ 8.6e9 | 6,141 | ≈ 1 in 140 |
| 4 | 2048⁴ ≈ 1.76e13 | 8,188 | ≈ 1 in 215,000 |

1 in 140 is unacceptable for an operation performed constantly. Four words
buys three orders of magnitude.

**Lookup is lenient, resolution is never silent.** Accept case-insensitively,
accept four-character prefixes per word, accept any separator. On a miss, run
an edit-distance search and return the nearest candidate **flagged** —
`resolved_by_proximity: true`, with both requested and actual ids. Never
substitute silently; that is priority 2.

**Recovery radius is exactly 1. Not a tunable.** A code corrects `t` errors
iff its minimum distance `d ≥ 2t + 1`. Correcting two requires `d ≥ 5`, but
the code has length 4, so `d ≤ 4` by the Singleton bound. Two-error correction
is not merely risky here — it is impossible for a length-4 code over any
alphabet.

The counting agrees. The radius-2 ball is `C(4,2)·2047² ≈ 2.5e7`, which at
10,000 notes gives roughly a 1-in-70 chance of landing on a real but wrong
note — exactly the margin that the third-to-fourth word bought back.

Use **word-level Levenshtein**, not Hamming: deletion (three words instead of
four) is a likelier generation error than substitution, and the lookup must
treat 3- and 5-word inputs as distance-1 candidates rather than rejecting them
as malformed. Comparable ball size, so it is free.

**Past radius 1, fall back to `context`, not to a wider search.** Two
surviving words carry ~22 bits, but a caller who mangled an id almost
certainly still knows what the note was *about* — far more information. Switch
from symbol-addressing to content-addressing rather than pushing a channel
that has already failed.

**Id resolution is lexical only. Never consult vector similarity.** Word ids
are opaque identifiers in the sense of §6.7: each BIP-39 word is a real token
carrying real meaning, so an id embeds as roughly the average of four
concepts, and two ids differing in one word land almost on top of each other.
Measured, vector-only retrieval asked for one id returns the note holding a
one-word-different id — the same collapse hex digests show, partial rather
than total. Native tokens help; they do not rescue it.

The consequence is specific: if near-miss recovery ever consulted embedding
similarity, it would confidently return **a different real note** in place of
the clean not-found that this section's sparsity argument was built to
guarantee. "Ids are words, and words embed fine" is a plausible and wrong
inference for an implementer to make, which is why it is written down here.

**The `near_miss` table** records every miss. Its value is that it separates
two failure modes that otherwise look identical:

- `candidate IS NOT NULL` — a real id was corrupted. Benign, and the row
  doubles as a cache so the same corruption skips the distance scan next time.
- `candidate IS NULL` — nothing within distance. The id was *fabricated*,
  not mistyped. This is the alarming case, and the one worth watching.

Recurring corruptions with high `hit_count` are empirical evidence of
confusable word pairs, which feeds back into the generation blocklist. LLM
substitutions are phonetically and semantically biased rather than uniform
(`canvas → canyon`, not `canvas → zebra`), so the effective radius-1 ball is
much smaller than 8,188 and the table maps the real error channel rather than
the theoretical one.

Rows are never promoted to silent redirects. A cached candidate still returns
flagged.

### 6.2 Ids are not time-sortable

ULIDs would have let `context("", since=...)` ride the primary key. Word ids
do not, so `created_at` carries its own index. Small, known cost.

### 6.3 Writes must not block on embedding

The single most important implementation constraint, and it follows directly
from priority 1. If `note()` embeds synchronously, then a cold model load or
a stopped Ollama daemon makes the write *fail* — and readily-written notes
are the entire value proposition.

Write the row, commit, return the id, enqueue the embedding. FTS5 is a
synchronous trigger with no external dependency, so a note is keyword-findable
immediately and semantically findable seconds later. **Unembedded is a
legitimate state, not an error**, and `context` must degrade to FTS-only
rather than failing when the vector side is cold.

This also makes model migration free: clear `embedding_meta`, let the worker
drain.

### 6.4 Note text is markdown by convention

Notes are authored in markdown, and CommonMark is the reference dialect for
extraction (§6.5). **This is a convention, not a contract.** The store never
parses a note to decide whether to accept it — a write that fails over an
unbalanced bracket would violate priority 1 outright. Non-conforming text is
stored verbatim and is never an error.

Two consequences for the pipeline:

- **Embed stripped, index raw.** Markdown syntax contributes tokens that are
  semantic noise — URL fragments especially — so strip markup before
  embedding. FTS5 indexes the raw text, because searching for a
  half-remembered URL is a real query. Easy to get backwards.
- **`desc` is plain text.** It is a single-line recognition handle shown in
  list context (§4); formatting there is noise rather than structure.

### 6.5 External references

**Link text is canonical; the `link` table is a derived index.**

The rejected alternative is a `links=[...]` argument on `note()`. It fails the
same way a separate `supersedes` call would: when an author has a URL they put
it in the prose, because that is where it belongs in a sentence. The argument
would be populated inconsistently, producing a partial index that looks
authoritative — worse than none — and it adds weight to the write path against
priority 1.

It is also the branch that is genuinely hard to reverse. Links living *only*
in a table, later wanted inline, would require rewriting note text, which
immutability forbids. You would end up superseding notes for data-migration
reasons, polluting the supersession graph with edges recording no change in
belief.

Derived extraction has none of that. Changing the extraction rules is a
re-run, not a migration. It is also the third instance of a shape already
committed to twice: FTS5 in `content='note'` mode, and `embedding_meta`, are
both mutable derived data over immutable rows.

**Extraction rules**, in decreasing confidence:

- **URLs** — markdown `[label](url)` preferred so the anchor text is captured;
  bare URLs also extracted.
- **Content hashes** — §12 already has notes referencing external artifacts by
  hash. This makes that queryable.
- **Internal references** — a bare four-word BIP-39 id in note text is a link
  to another note. The pattern is distinctive enough to detect without any
  markup, so no `[[wiki]]` extension is needed. Semantically an associative
  reference, *not* supersession; the model otherwise has no place for one.
- **Bare filesystem paths** — not extracted. Too many false positives, and
  machine-local anyway.

**Skip fenced code blocks.** A URL in a code sample is an example, not a
reference. This is the direct payoff of §6.4.

Link checking pulls `SELECT DISTINCT target FROM link`, so per-note
duplication costs nothing at read. Duplication would only hurt on write-back,
which is why mutable state lives in `source` (§6.6) rather than here.

### 6.6 Snapshots

A snapshot records **what the target said when a given note was written**. It
is keyed `(note_id, target)` and is immutable, exactly like the note it
belongs to.

**Why per-note and not per-target.** A per-target row would track *current
state*, and it would have been the only thing in this design that did —
everything else is historical by construction, which is the reason the store
is append-only at all. Keyed per-note-at-time-of-writing, a snapshot becomes a
witness to the state of the world at the moment a belief formed. Note and
snapshot seal together; superseding the note leaves its witness intact, which
is correct, because the old belief was formed against the old page.

**Drift becomes a query, not a job.** Two notes citing the same target a year
apart yield two snapshots, and comparing them *is* the drift test — over your
own reading history rather than against the live web. There is consequently
**no link checker**: the content is already held, and if the target
disappears, it disappears. Rot stops being a threat model.

This also retires the network concern. Fetching happens once, at capture, and
never again. There is no recurring outbound traffic and no ongoing disclosure
of what is being read. Public archive submission would publish the URL and
remains opt-in per note, off by default.

**`link` and `snapshot` share a key and stay separate anyway.** `link` is
regenerable from note text in seconds; a snapshot is gone forever if dropped.

> **The extraction rebuild must never touch `snapshot`.** "Drop and
> re-extract" is the natural way to regenerate a derived index, and applying it
> to the wrong table destroys the only data in the system that cannot be
> recovered. This is the sharpest edge in the design and it is a two-line
> mistake. Keeping the tables structurally separate is what keeps
> `DELETE FROM link` safe.

**Mechanics:**

- **Asynchronous, per §6.3.** `note()` never blocks on a fetch.
- **A separate queue from embedding.** Both are async, but they fail
  differently: embedding is local, fast, and fails atomically; fetching is
  network-bound, slow, and hangs. One stalled request must not block the
  embedding backlog.
- **Hash the full extraction, store the truncated copy.** A 64k cap covers
  articles comfortably; long PDFs, API references, and long comment threads do
  not fit. `full_hash` is computed over the complete extraction and
  `full_length` records what was cut, so truncation is visible and
  cross-snapshot comparison still works when both copies were trimmed. Storing
  raw HTML would become the document corpus §12 excludes; stripping the
  advertising, tracking, and decoration leaves something far smaller than the
  page it came from.
- **Separate database file.** Snapshots live outside the notes store, which
  stays small and portable.
- **A target already dead at capture** is information: record `status='gone'`
  rather than retrying indefinitely.

**Capture-once makes extraction failure permanent.** This is the real cost of
the model. Under periodic re-checking a bad parse could be redone; here, if
the extractor chokes on a JS-rendered page or a paywall interstitial, the
mangled text is what exists forever — and the page may be gone. Record
`extraction` and `raw_length`, and flag low-yield results at capture time: a
200 response yielding 200 characters is a failed parse, not a short article,
and it should surface as `status='thin'` **while manual recovery is still
possible**.

**Verification is adjacency, not a pass.** "Does the extracted text actually
support what the note claims" is a semantic judgement, not a structural check,
and belongs nowhere in §7. Make it answerable on demand instead:
`read(id, with_sources=False)`, opt-in because preserved text would otherwise
wreck every return.

### 6.7 Why hybrid, precisely

The general case does not justify it. On a topically spread corpus, hybrid
beats vector-only by 0.967 against 0.963 MRR — paired, 1 win against 2 losses
over 42 queries. On ordinary conceptual retrieval the vector side does nearly
all the work and the second retriever is a wash.

**The justification is opaque identifiers, and there it is decisive.** Given
notes whose identifiers differ by one character, vector-only scores 8/12 exact
top-1; hybrid scores 12/12, because FTS supplies the exact match at rank 1 and
RRF cannot lose a rank-1-in-one-ranking result.

The failure mode is what matters, not the score. Asked about three different
hex digests, the vector side returns **the same wrong note** for all three. It
has collapsed them to a single point and picks whichever note's surrounding
prose it prefers. A caller asking about one artifact is confidently handed the
note about another — silently, repeatably, with no signal that anything went
wrong. Given §12 has notes referencing artifacts by content hash, and given
that acting on the wrong artifact is exactly the class of error this design
exists to prevent, that is a sufficient argument on its own.

**The category is narrower than "exact identifiers."** Word-like identifiers
embed fine — `SQLITE_LOCKED`, `vec_distance_l2`, `check_same_thread` all
resolve correctly, because subword tokens carry real signal. The failing
category is *identifiers whose distinguishing part is not lexical*: hex
digests, version numbers, UUIDs, line numbers. Note ids are a partial case
(§6.1): native tokens make the collapse incomplete rather than absent.

### 6.8 Fusion cannot filter; the retrievers must

RRF is **rank-blind by design.** It sees that a note is rank 1 in the keyword
ranking and cannot see that the match was worthless. A junk rank-1 hit
contributes exactly what a perfect one does.

This is not hypothetical. Expanding free text to `"combining" OR "two" OR
"rankings"` lets any single token retrieve a document alone; the query
*combining two rankings* matched a note on catastrophic cancellation via the
word "two", entered the keyword ranking at rank 1, and was promoted **above**
the note the vector side had correctly ranked first. Three individually
reasonable decisions — OR expansion, rank-blind fusion, no minimum evidence —
composing into a wrong answer.

**Any precision control must live inside a retriever, before fusion.** RRF's
benefit is freedom from *cross-retriever* score calibration; a within-retriever
evidence gate does not touch that, because a BM25 score is never compared to a
cosine. The two are fully compatible.

The concrete measure taken: function words are dropped from FTS expansion
unless the query is nothing else, so "one two three" still finds the note that
says it. Paired over 42 queries: 3 wins, 0 losses, 39 ties. Strictly dominant,
which is the standard a change at this sample size has to meet.

### 6.9 Other

**License note:** `sqlite-vec` is MIT OR Apache-2.0 and pre-v1 (v0.1.7,
March 2026; expect breaking changes). At this corpus size, brute-force KNN
is fast enough that the ANN index work can be ignored entirely, which keeps
the blast radius of an upstream break small. `fastembed` is Apache-2.0.
`jina-embeddings-v3` is CC-BY-NC and should be avoided despite its
benchmark position.

---

## 7. Consistency checking

An offline batch operation over the whole store. Produces a **report for
human review**; it never mutates, never auto-creates edges, never auto-fixes.

**Deliberately not an MCP tool.** It is periodic maintenance, not something
needed mid-conversation, and every tool in the surface costs context on every
turn. Ship it as a CLI subcommand.

### 7.1 Structural checks

Exact, cheap, no embeddings required. These are the checks that actually
validate `retained`.

| check | condition | severity |
|---|---|---|
| Cycle | supersession graph contains a cycle | error — should be unreachable given §3.4 |
| Self-supersession | `old_id == new_id` | error |
| No-op supersession | `retained = 1.0` and both `desc` and `text` identical | warning |
| Conflicting fork | a note's outgoing edges span θ (e.g. 0.9 and 0.1) | review |
| Orphaned retraction | A is retracted, but some descendant asserting A at high `retained` is itself un-superseded | review — §5.4, the follow-up you owe |
| Dead justification | A is in the pool only by virtue of an edge to B, and B is itself retracted | review — specific to max |
| Double retraction | A retracted by B, B retracted by C | review — is A back? context-dependent by nature; flagging is the whole job |
| Thin snapshot | a note's snapshot has `status='thin'` | review — **time-sensitive**; the target may still be live (§6.6) |
| Missing snapshot | a `link` row of kind `url` with no corresponding `snapshot` row and no pending fetch | error — preservation failed silently |
| Dangling internal link | a `link` of kind `note` whose target id does not exist | review — likely a fabricated or corrupted id (§6.1) |

*Orphaned retraction* is the highest-value check in the list: it is the
mechanical consequence of choosing max, and without it §5.4's non-locality
is a trap rather than a feature.

### 7.2 Semantic checks

Embedding-based, heuristic, and strictly **candidate generation** — a human
decides.

**Suggested links.** Pairs with high cosine similarity and no path between
them in the DAG are candidate missing supersessions. Expect this to be noisy:
topical relatedness is not supersession, and most candidates will be rejected.
Start the threshold high and tune down.

**The check that does not work.** It is tempting to validate `retained`
against text similarity, on the intuition that more retained content means
more overlap. That intuition is wrong in both directions:

- `retained = 1.0`, additive: the new note may be *only* the addendum and
  share almost nothing with the old. Low similarity, entirely legitimate.
- `retained = 0.0`, retraction: "I was wrong about X because Y" generally
  restates X. High similarity, entirely legitimate.

Similarity is close to orthogonal to `retained`, so a checker built on that
correlation would emit mostly false positives and train you to ignore it.
`retained` validation belongs in §7.1.

One narrow exception is decidable: identical or near-identical `text` with a
changed `desc` is a description fix, reliably classifiable, and relevant to
open question §9.3.

### 7.3 Relation to growth management

Checker output doubles as a reading guide. §8 argues the store must
periodically be read in full; the report says where to start.

---

## 8. Growth management

**Unsolved.** Stated here so it is not mistaken for an oversight.

Every system of this kind fills with true-but-useless notes until retrieval
stops being worth the tokens. No reliable fix is known to the authors of
this document. Observations that seem robust:

- Salience is not knowable at write time. Whether a note matters depends on
  what happens afterward. The store must support retroactive revaluation —
  which supersession with `retained = 0.0` partly provides.
- **Inbound reference count is a retroactive salience measure**, and the only
  one the design currently has. It accrues from what was actually used later
  rather than from a judgement at write time, which is exactly the property
  the previous point says is needed. It does not solve pruning — a note may
  matter enormously and be referenced by nothing — but it is a real signal,
  already present in the `link` table, and worth looking at before inventing
  anything more elaborate.
- The only mitigation with a clear mechanism is keeping the store small
  enough to be read in full, periodically.
- An open loop is a note nothing has superseded. A query for old,
  un-superseded notes is therefore the natural pruning handle, and is
  expressible with `context("", since=...)` plus head filtering.

Recommend deferring any automated pruning until real usage data exists.

**This is also the unmeasured regime for retrieval.** Every retrieval number
in §6 comes from a topically spread corpus, where almost any retriever
succeeds. A store full of true-but-useless near-duplicate notes is where
retrieval actually gets hard, and where §9.2 and §9.5 would become answerable.
Both problems wait on the same accumulated data.

---

## 9. Open questions

1. **`extends` vs `supersedes`.** The additive case (`retained = 1.0`) has
   genuinely different semantics — "these go together" rather than "prefer
   the newer." It is currently one mechanism with a threshold. Two named
   relations is a defensible alternative. Not obvious; flagged as a real fork.

2. **θ, the pool-membership threshold.** No principled basis for an initial
   value. Start around 0.8 and tune against real data.

3. **`I was wrong` vs `I said that badly`.** Both produce a supersession
   edge; they mean opposite things about the history of belief. Currently
   distinguishable only by reading both notes, or by the narrow textual test
   in §7.2, and by §5.5's assessment history. If it starts mattering — e.g. for asking the store where thinking
   actually changed — the fix is one optional boolean, not a schema change.

4. **Optional `kind` field.** An earlier design had four kinds (finding,
   failure, thread, decision). Threads collapse into the supersession model
   for free; failures and decisions are findings with different retrieval
   priority. Deferred — add a single optional argument if and when the need
   appears in practice.

   The strongest argument for eventually adding it: a `check_failures(topic)`
   call made *before* proposing an approach is defensive retrieval, and it is
   plausibly the highest-value query in the system. It can be approximated by
   convention in `desc` first.

5. **Similarity threshold for suggested links.** Unknown, and unanswerable
   from current data — it needs the near-duplicate regime (§8), which no
   corpus measured so far has. Do not synthesise that corpus: notes written to
   order measure the author's idea of accumulation rather than accumulation.
   The store is running; the real thing arrives on its own, and the harness
   caches embeddings so re-running it later is nearly free.

6. **Behaviour when nothing relevant exists.** Every query measured so far has
   an answer in the corpus, so nothing tests the response to a query the store
   cannot serve. That is where an over-eager keyword side does the most damage,
   and where `vector_similarity` (§3.2) has to earn its place. Cheap to add to
   the existing harness.

7. **Near-duplicate bodies.** A description-fix supersession duplicates the
   body verbatim, costing a re-embed and storing the text twice. Negligible
   at note size; worth revisiting only if chains of near-duplicates
   accumulate.

---

## 10. Review interface

A local web UI, served by a subcommand of the same binary. Not an MCP tool
surface — it adds nothing to §3 — and not part of the first three increments
(§11.3). Increment 4.

**Localhost only.** Binds 127.0.0.1, no authentication, no remote access.
Making it network-reachable is out of scope (§12); it would demand an auth
model this tool has no business owning.

### 10.1 Primary function: reading the store

Priority 3 holds that the store must stay small enough to be read in full, and
§8 concedes that periodic full reading is the only growth mitigation with a
mechanism behind it. Nobody does that against a SQLite CLI. A browse view over
notes, supersession chains, and snapshots is what makes the one unsolved
problem in this design tractable in practice. Failure triage (§10.2) is the
secondary function, not the reason the interface exists.

### 10.2 Failure triage

Surfaces what `help`'s `snapshot_status` histogram counts and §7.1's checker
rows locate. Permitted repairs:

- **Paste content** for `thin`, `unreachable`, or auth-blocked targets.
- **Retry** a transient `unreachable`.
- **Acknowledge** a `gone` target, so it stops appearing as actionable.

**Credentials are not the answer; content is.** The obvious design — let the
user supply credentials so the fetcher can get past a paywall or a login — is
the wrong shape. It would make seshat a credential store, demanding encryption
at rest, key management, and rotation, with plaintext in an MCP subprocess
config. The user is already authenticated and already looking at the page. A
paste box clears the whole paywall/SSO/SPA class of failures and seshat never
handles a secret.

A browser extension is the better long-term shape for this — one click on the
page already open and already authenticated, capturing the DOM directly. Same
principle as the paste box with the friction removed, and it handles SPAs and
paywalls structurally. Well past increment 4.

Pasted snapshots record `extraction='manual'`. They are witnesses, but
human-mediated ones, and the distinction must survive in the data rather than
becoming invisible.

### 10.3 No edit affordance

**The UI must not permit editing a note.** It will look exactly like a CMS,
the absence will feel like an oversight, and adding it destroys the
append-only guarantee that §5 and the entire audit trail rest on. Corrections
go through `note(supersedes=...)` like everything else. Stated here explicitly
so it is not added later by someone reasonable.

Snapshot repair is not an exception: a snapshot with no content is being
filled, not rewritten, and a snapshot that already holds content is immutable
like the note it witnesses.

### 10.4 Irreducible failure

Some targets cannot be preserved at all: dead before capture, DRM-protected,
interaction-dependent, or purely audiovisual with no text. The correct
response is to record the failure durably and **stop retrying**.

Such a note remains valid. It cites a source nobody can check, which is a
property of the snapshot and weaker evidence — not a defect in the note, and
never grounds for superseding it.

### 10.5 Concurrency

The MCP server, the async workers, and this interface are separate processes
against one SQLite file. WAL mode and `busy_timeout` are not optional once the
UI exists.

---

## 11. Versioning

### 11.1 Three independent versions

| version | what it describes | changes when |
|---|---|---|
| `spec_version` | the contract in this document | semantics or surface change |
| `software_version` | the implementation | any release, ordinary semver |
| `store_version` | the on-disk schema | a migration is required |

They are deliberately not coupled. Software 0.3.1 may implement spec 1.1
against a store at schema 2. A spec revision that only adds an optional
argument needs no migration and leaves `store_version` alone.

### 11.2 Spec version discipline

- **Major** — a change that breaks existing callers or existing stores.
  Removing an argument, changing a return shape, requiring a migration.
- **Minor** — additive. A new optional argument, a new return field, a new
  tool. Old callers keep working.
- **Patch** — clarification with no behavioural consequence.

**Bump on semantic change, not only on signature change.** This is the rule
that is easy to get wrong. Switching §5.2's combination rule from maximum to
minimum, altering θ, or widening §6.1's recovery radius changes nothing about
any signature and changes everything about what the server returns. Those are
major revisions. A version that only moves when arguments move will
misrepresent the server while looking correct.

**Keep the number in one place.** The document header is authoritative; the
implementation holds a `SPEC_VERSION` constant and a test asserts the two
match. Without that they drift, usually within one release.

### 11.3 Build order

Not part of the contract, but the versions above only make sense against it.

1. **FTS only.** Notes, assessments, the five data tools, `help`, FTS5. No
   embedder, no async worker, no Ollama dependency. Complete and usable.
   Reports `vector: false`.
2. **Vector.** Embedding worker, `embedding_meta`, hybrid fusion. §6.3's
   async discipline matters from here on.
3. **Checker.** §7, as a subcommand sharing the DAG traversal code.
4. **Review interface.** §10. Snapshots may land here or in increment 2
   depending on how early link capture matters.

### 11.4 History

- **1.0** — initial handoff to implementation. Core model, five tools,
  `retained` semantics with maximum combination, storage, consistency
  checking, identifiers, growth management.
- **1.1** — versioning discipline (§11), `help` tool (§3.6), `meta` table,
  capability reporting, markdown convention (§6.4), derived `link` table
  (§6.5), per-note immutable `snapshot` table capturing link content at write
  time (§6.6), `with_sources` on `read`, snapshot status histogram in `help`
  and the corresponding checker rows, local review interface (§10), `links`
  and `backlinks` on `read`.
- **1.2** — first revision informed by measurement, against increment 2.
  `context` returns `vector_similarity` and `matched` alongside `score`, which
  is demoted to a sort key (§3.2); identifier-shaped queries get a pointer to
  `read`. Id resolution is stated lexical-only (§6.1). `k` fixed at 60 and
  declared not a tuning parameter. The prefix "measurable" claim withdrawn as
  unsupported. New §6.7 narrowing the hybrid justification to non-lexical
  identifiers, new §6.8 on pre-fusion filtering. Open question on `k` removed;
  §9.2 (θ) and §9.5 (link similarity) remain, both waiting on the
  near-duplicate regime; §9.6 added for the no-answer case.

---

## 12. Explicitly out of scope

- Document/corpus indexing. Notes reference external artifacts by hash and
  URL, and §6.5 indexes those references and their liveness — but fetching,
  storing, or searching the artifacts themselves is a separate system that
  may never be needed.
- Multi-user access, auth, remote transport. Local stdio, single user. The
  review interface (§10) is localhost-only for the same reason.
- Credential storage of any kind. §10.2 resolves authenticated targets by
  accepting content, never secrets.
- Automatic note extraction from conversation. Writes are explicit.
- Reference management. The resemblance to Zotero is real but the hierarchy is
  inverted: there, sources are the spine and notes hang off them; here, notes
  are the spine and snapshots are witnesses to them. Collections, tags,
  citation styles, BibTeX export, and PDF annotation belong to a tool that
  already exists and does them better. The belief history in §5 is what this
  design is for, and it is the thing a reference manager does not have.
