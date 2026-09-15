# Retrieval findings — seshat implementation, spec 1.1

Measured against the increment-2 implementation (software 0.2.0, store schema 3)
on 2026-09-14. Everything below is empirical unless flagged otherwise.

**Summary for the impatient.** Hybrid retrieval earns its place, but not for the
reason the aggregate numbers first suggested — on ordinary conceptual queries it
is a wash against vector-only. It justifies itself almost entirely on **opaque
identifiers**, where the vector side fails in a specific and repeatable way.
Dropping function words from the keyword expansion is a strict improvement. RRF's
`k` is not a tuning knob at this scale. The spec's claim that nomic's task
prefixes matter "measurably" is **still unsupported** after two attempts at
measuring it.

---

## 1. Method, and what is wrong with it

**Corpus.** 40 notes written in the style the store is for — a recognition-handle
`desc` plus a short body — spanning signal processing, numerics, ML pipelines,
retrieval/storage, the note store's own design, Rust/C++/tooling, CV/robotics,
distributed systems, and licensing. A further 6–12 notes were added for the
identifier experiments in §3.

**Queries.** 42 queries with known acceptable answers, tagged by kind:

- `ident` (8) — names something exactly
- `concept` (27) — paraphrase with little or no lexical overlap with the note
- `mixed` (7) — ordinary phrasing, partial overlap

Conceptual queries were deliberately written *not* to reuse the note's wording.
Where a topic had two plausible answers, both are accepted, so a configuration is
not punished for returning a genuinely relevant second note.

**Metrics.** top-1 accuracy, MRR, recall@10. Effect sizes are also reported as
**paired per-query win/loss/tie counts**, which turned out to matter (§4).

**The limitations, stated plainly:**

1. **One author wrote the notes, the queries, and the implementation.** This
   inflates absolute scores for every configuration. It is defensible only for
   *comparing* configurations, which is all it is used for. None of the absolute
   numbers below should be quoted as "seshat achieves 0.97 MRR".
2. **42 queries is small.** A difference of one or two queries is noise. This is
   why §4 exists.
3. **The corpus is topically spread.** Real stores accumulate many near-duplicate
   notes on one subject, which is the regime where retrieval actually gets hard.
   Nothing here measures that.
4. **Relevance is binary and mostly single-target.** No graded judgements.

---

## 2. Results

### 2.1 Single retrievers and hybrid

MRR, overall and split by query kind:

| configuration | top-1 | MRR | R@10 | ident | concept | mixed |
|---|---|---|---|---|---|---|
| FTS only, function words kept | 0.76 | 0.822 | 0.90 | 1.00 | 0.73 | 1.00 |
| FTS only, function words dropped | 0.81 | 0.857 | 0.90 | 1.00 | 0.79 | 1.00 |
| Vector only | 0.95 | 0.963 | 1.00 | 1.00 | 0.94 | 1.00 |
| Hybrid, function words kept | 0.93 | 0.942 | 0.98 | 1.00 | 0.91 | 1.00 |
| **Hybrid, function words dropped** | **0.95** | **0.967** | **1.00** | 1.00 | 0.95 | 1.00 |

Two things stand out.

**Hybrid with noisy keyword input is worse than vector alone** (0.942 vs 0.963).
Adding a retriever actively degraded results. That is §4.1's finding and the
reason the stopword change was made.

**Hybrid barely beats vector-only even after the fix** (0.967 vs 0.963; paired:
1 win, 2 losses, 39 ties). On this query set the vector side is doing nearly all
the work, and the case for running two retrievers is *not* made by these numbers.

### 2.2 RRF `k` is not a tuning knob

| k | 5 | 10 | 30 | 60 | 120 | 300 |
|---|---|---|---|---|---|---|
| MRR | 0.967 | 0.967 | 0.967 | 0.967 | 0.967 | 0.967 |

Identical across a 60× range. With two rankings over a small corpus, `k` only
changes the relative weight of deep ranks against shallow ones, and almost every
answer here is in the top three of at least one ranking. **Recommendation: leave
k = 60 and stop thinking about it.** It is not where retrieval quality lives, and
a future spec revision should not treat it as an open tuning question. This may
change if the corpus grows enough that answers routinely sit at rank 10+.

---

## 3. The finding that actually justifies hybrid

§6 justifies running FTS alongside vectors with: *"Pure vector search is poor at
exact identifiers — function names, paper titles, hash prefixes."*

The first attempt to test this **found nothing**: on the 8 `ident` queries above,
vector-only scored a perfect 1.00 MRR. That result was misleading, and worth
recording as a methodological trap: those identifiers were *pronounceable*
(`check_same_thread`, `Singleton bound`, `AX = XB`) — English-adjacent tokens a
subword model represents fine. A second attempt added six notes with genuinely
opaque identifiers, and still found nothing, because each identifier was unique
in the corpus: any retriever that gave it non-zero weight won.

The discriminating experiment adds **near-miss distractors** — several notes whose
identifiers differ by one character:

| query | FTS rank | Vector rank | Hybrid rank | vector's actual top hit |
|---|---|---|---|---|
| `4f2a9c81` | 1 | 2 | 1 | **hash_b** (wrong) |
| `4f2a9d81` | 1 | 1 | 1 | hash_b |
| `4e2a9c81` | 1 | 2 | 1 | **hash_b** (wrong) |
| `v0.1.9` | 1 | 2 | 1 | **ver_b** (wrong) |
| `v0.1.7` | 1 | 1 | 1 | ver_b |
| `v0.2.1` | 1 | 2 | 1 | **ver_b** (wrong) |
| `SQLITE_BUSY` | 1 | 1 | 1 | err_a |
| `SQLITE_LOCKED` | 1 | 1 | 1 | err_b |
| `SQLITE_CORRUPT` | 1 | 1 | 1 | err_c |
| `vec_distance_cosine` | 1 | 1 | 1 | fn_a |
| `vec_distance_l2` | 1 | 1 | 1 | fn_b |
| `vec_quantize_binary` | 1 | 1 | 1 | fn_c |
| **MRR** | **1.000** | **0.833** | **1.000** | |
| **exact top-1** | **12/12** | **8/12** | **12/12** | |

The failure is not random — it is **systematic and invisible**. For all three
hash queries the vector side returns *the same note*, `hash_b`, and for all three
version queries it returns *the same note*, `ver_b`. The embedding cannot
distinguish `4f2a9c81` from `4f2a9d81` from `4e2a9c81` at all; it has collapsed
them to one point and picks whichever note's surrounding prose it likes best. A
caller asking about the good corpus gets confidently handed the note about the
truncated one.

Note the split: **word-like identifiers are fine** (`SQLITE_LOCKED`,
`vec_distance_l2` — all correct), because subword tokens carry real signal.
**Opaque strings are not** (hex digests, version numbers). The spec's claim is
correct but its scope is narrower than the wording implies — it is not "exact
identifiers", it is "identifiers whose distinguishing part is not lexical".

Hybrid recovers full precision: 12/12, because FTS contributes the exact match at
rank 1 and RRF cannot lose a rank-1-in-one-ranking result.

**This, and not general quality, is the argument for hybrid retrieval.** Given the
store explicitly holds notes referencing artifacts by content hash (§12), and
that retracting the wrong artifact is exactly the kind of error the design exists
to prevent, it is a good argument — but it should be stated for what it is.

---

## 4. A defect found by running it, and a lesson about aggregate metrics

### 4.1 Function words plus OR plus rank-blind fusion

On a five-note corpus, the query **"combining two rankings"** returned a note on
*catastrophic cancellation* first — above the note about rank fusion, which the
vector side had correctly ranked #1.

The mechanism is an interaction of three individually reasonable decisions:

1. Free text is expanded to `"combining" OR "two" OR "rankings"`, so **any single
   token can retrieve a document on its own**.
2. The only token matching anything was **"two"** (in "…when the two are nearly
   equal").
3. **RRF is rank-blind by design.** It sees "this note is rank 1 in the keyword
   ranking" and cannot see that the match was worthless. A junk rank-1 hit
   contributes exactly as much as a perfect one.

Corroboration across two retrievers then promoted the wrong note above the right
one. Fusion working as specified, fed garbage by the query expansion.

**Fix applied:** function words are dropped from the FTS expansion, unless the
query consists of nothing else (so "one two three" still finds the note that says
it). Paired over 42 queries: **3 wins, 0 losses, 39 ties, ΔMRR +0.024.** Strictly
dominant — it never made a query worse. That is a strong enough result to act on
even at this sample size, because the evidence is one-sided rather than marginal.

The general form is worth stating for the spec: **any retriever that can return a
result at rank 1 on weak evidence gets a full vote under RRF.** Rank-based fusion
buys freedom from score calibration and pays for it by discarding the information
that would let it discount a bad match. Filtering has to happen before fusion,
because fusion cannot do it.

### 4.2 The prefix claim, and why aggregate deltas are not enough

§6: *"Omitting them, or using the same prefix on both sides, degrades retrieval
measurably and silently."*

Two measurements now:

| attempt | corpus | queries | result |
|---|---|---|---|
| first | 15 notes | 12 | no detectable difference |
| second | 40 notes | 42 | see below |

Vector-only MRR by prefix scheme:

| scheme | top-1 | MRR |
|---|---|---|
| correct (`search_document:` / `search_query:`) | 0.95 | 0.963 |
| both sides `search_document:` | 0.88 | 0.920 |
| no prefixes | 0.93 | 0.944 |

At a glance this *supports* the spec: correct beats both-document by +0.043 MRR
and by 3 queries of top-1. I nearly wrote it up that way.

The paired comparison says otherwise:

| comparison | wins | losses | ties |
|---|---|---|---|
| correct vs both-document | 3 | **2** | 37 |
| correct vs none | 2 | 0 | 40 |

Three wins against two losses is a coin flip. The MRR gap comes from a handful of
small rank shifts in both directions, and aggregating them hid the fact that the
"losing" configuration won on two queries outright. Compare the stopword result —
3 wins, **zero** losses — which is what a real effect looks like at this sample
size.

**Conclusion: the prefix claim remains unverified after two attempts.** The
prefixes are kept, because they are the documented usage and cost nothing, but
the word "measurably" is not currently supported by anything measured here. If it
matters, it needs either a much larger query set or a corpus of near-duplicate
notes where asymmetry would plausibly bite.

The methodological point generalises: **an aggregate metric difference of this
size is not evidence.** Report paired outcomes.

---

## 5. What changed in the implementation

- Function words dropped from FTS query expansion (§4.1). One function,
  `seshat.store.fts_query`, with the reasoning in a docstring and a regression
  test carrying the original failing query.
- Nothing else. `k` stays 60, prefixes stay as specified, fusion stays sum-of-RRF.

## 6. Questions this raises for the spec

1. **Should §6 narrow its identifier claim?** "Exact identifiers" is too broad —
   word-like identifiers embed fine. The real category is *identifiers whose
   distinguishing part is not lexical*: hashes, version numbers, UUIDs, line
   numbers. That sharpening also tells a reader when hybrid matters.
2. **Should `k` move from "≈ 60" to "60, not a tuning parameter"?** It is
   currently phrased as if it were a knob. It is not, at this scale.
3. **Is the "measurably" in the prefix paragraph defensible?** Suggest softening
   to "the documented usage; the size of the effect at small corpus scale is
   unmeasured", unless there is a source for it.
4. **§9.5 (similarity threshold for suggested links) is still unanswerable** from
   this data. It needs the near-duplicate regime, which this corpus does not have.
5. **Does anything filter before fusion?** §4.1's general form suggests the spec
   might want a sentence on it: RRF cannot discount a weak match, so any
   precision control has to live in the retrievers. Currently implicit.
6. **The regime that was not measured at all** is the one §8 is about — a store
   full of true-but-useless near-duplicate notes. Every number here comes from a
   topically spread corpus where almost any retriever succeeds. If retrieval
   policy is going to be set on evidence, that is the experiment worth building
   next, and it needs real accumulated notes rather than written-to-order ones.

## 7. Unrelated, but found in the same pass

**The spec's own example identifier is not a valid BIP-39 id.** §6.1 illustrates
the scheme with `bright-otter-canvas-fig`, but `otter` and `fig` are not in the
BIP-39 English wordlist (`bright` and `canvas` are). Anything that validates ids
properly will refuse it.

This is harmless in the document — it is an illustration, not test data — but it
cost real debugging time here: the dangling-internal-link checker looked broken
when it was correctly declining to treat a non-identifier as an identifier. A
replacement made of real words, e.g. `olive-canvas-bright-zebra`, would remove
the trap for the next implementer. Pinned in `tests/test_links.py` so it cannot
be rediscovered a third time.

---

*Evaluation harness: `scratchpad/corpus.py`, `evaluate.py`, `identifiers.py`.
Embeddings cached, so re-running is cheap and does not re-hit Ollama.
Model: `nomic-embed-text` (768-dim, L2-normalised by Ollama) via local Ollama.*
