# seshat-mcp — Design Specification

Named for the Egyptian goddess of writing, measurement, and record-keeping.
Distribution name `seshat-mcp`; CLI and MCP server identifier both `seshat`.
Note that `seshat` is taken on crates.io (a Matrix event indexer), which
matters only if the implementation goes to Rust.

**Spec version:** 1.0. Every revision gets an explicit version number here, and
the implementation records the version it was built against (`seshat info`).
Version skew — code citing one version while the document has moved several
ahead — is the failure mode this line exists to prevent.

**Status:** design complete, unimplemented. Nothing here has been built or tested.

**Provenance:** this design emerged from a design conversation between Ian Greenhoe
and Claude (Opus 5), September 2026. Decisions and their rationale are recorded below;
open questions are recorded as open rather than resolved by fiat.

**License intent:** MIT.

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

Five tools. This is the whole interface.

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
everything in it is relevant, and a reader will act accordingly. The score
is what makes honest triage possible.

Returns `desc`, never bodies. Bodies are paid for individually via `read`.

**Pool membership is governed by `retained` (§5.1)** — the mechanism that
keeps additive supersession from silently deleting correct information.

### 3.3 `read(id, edge_limit=N) -> record`

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

`superseded_by` is not optional. Ids leak forward within a conversation; a
reader will hold an id from many turns earlier and act on it. A `read` that
returns only the body makes stale-but-confident use of a retracted note the
default behaviour.

**Truncation must be visible.** The `_total` counts are not optional: a
reader shown five of twelve supersessions will conclude the note absorbed
five things, which violates priority 2 more cheaply than almost anything else
in the design.

Expected degree distribution is mild. `superseded_by` is ~0 for active notes
and ~1 otherwise — it exceeds 1 only by accident, when something already
superseded is superseded again. `supersedes` is the merge direction and is
the one that can legitimately grow, since consolidating scattered notes into
one is the recommended response to §8. Truncation order within a list is
unresolved; `retained` descending is the current guess.

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
`search_query:` when querying. Ollama does not add them. Omitting them, or
using the same prefix on both sides, degrades retrieval measurably and
silently — nothing errors. Qwen3 is likewise instruction-aware on the query
side only. Keep the prefixes in one place; they are easy to get wrong in
exactly one code path.

(nomic's Ollama card lists 2,048 context against a native 8,192; set
`num_ctx` if that ever matters. It does not, for notes.)

**Hybrid retrieval:** run FTS5/BM25 and vector KNN independently, fuse with
reciprocal rank fusion:

```
score = Σᵢ 1 / (k + rankᵢ),  k ≈ 60
```

RRF is rank-based, so incommensurable score scales never have to be
reconciled. Pure vector search is poor at exact identifiers — function
names, paper titles, hash prefixes — which is a large share of real queries
against a store like this.

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

### 6.4 Other

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
- The only mitigation with a clear mechanism is keeping the store small
  enough to be read in full, periodically.
- An open loop is a note nothing has superseded. A query for old,
  un-superseded notes is therefore the natural pruning handle, and is
  expressible with `context("", since=...)` plus head filtering.

Recommend deferring any automated pruning until real usage data exists.

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

5. **Similarity threshold for suggested links.** Unknown. Depends on the
   embedding model and on how much false-positive tolerance the review
   workflow has.

6. **Near-duplicate bodies.** A description-fix supersession duplicates the
   body verbatim, costing a re-embed and storing the text twice. Negligible
   at note size; worth revisiting only if chains of near-duplicates
   accumulate.

---

## 10. Explicitly out of scope

- Document/corpus indexing. Notes reference external artifacts by hash;
  managing those artifacts is a separate system that may never be needed.
- Multi-user access, auth, remote transport. Local stdio, single user.
- Automatic note extraction from conversation. Writes are explicit.
