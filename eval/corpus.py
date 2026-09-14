"""Evaluation corpus and query set for seshat retrieval policy.

Notes are written in the style the store is actually for: findings with a
recognition-handle `desc` and a short body. Queries are written the way someone
would ask months later -- deliberately NOT reusing the note's wording, except
where the query is explicitly an identifier lookup.

Relevance judgements are `accept`: the set of note keys that would genuinely
answer the query. Single-target judgements would understate every config
equally, but they would also punish a config for returning a second, genuinely
relevant note, so overlapping topics list all acceptable answers.

BIAS DISCLOSURE: one author wrote both the notes and the queries, and knows the
implementation. That inflates absolute scores for every configuration. It is
only defensible for *comparing* configurations, which is what this is for.
"""

NOTES = {
    # --- signal processing
    "bilinear": (
        "Bilinear transform compresses the frequency axis",
        "Digital cutoff lands below the analog one. Prewarping with tan(wT/2) restores "
        "the match at exactly one chosen frequency and nowhere else.",
    ),
    "tau": (
        "Monopole decay constant is tau = -1/ln(1 - 1/p)",
        "Approximately p - 1/2 for large peak gain p. They agree within 50/p percent, so "
        "substituting one for the other is invisible at high p and 24% wrong at Q = 0.707.",
    ),
    "peak_gain": (
        "peak is a gain, not a time",
        "Recurring confusion in the filter code. The parameter named peak is a "
        "dimensionless multiplier; the time constant is derived from it, not equal to it.",
    ),
    "butterworth": (
        "Butterworth Q is 0.707 for second order",
        "Maximally flat passband magnitude. Higher Q peaks before rolloff.",
    ),
    "filter_units": (
        "Filter specs belong in Hz and seconds, never samples",
        "Anything expressed in samples bakes the sample rate into the feature definition, "
        "so the same spec means different things at 16k and 48k.",
    ),
    "group_delay": (
        "Linear phase costs latency proportional to filter length",
        "Symmetric FIR has constant group delay of (N-1)/2 samples. IIR cannot be linear "
        "phase causally.",
    ),
    # --- numerics
    "cancellation": (
        "Tail energy computed as total minus cumsum dies to cancellation",
        "Subtracting two nearly equal floating point numbers destroys the significant "
        "digits. Accumulate from the tail instead.",
    ),
    "argmax_allfalse": (
        "argmax on an all-False array returns 0, not an error",
        "A verification script reported index 0 as a detection for every empty case. "
        "Check .any() before trusting the index.",
    ),
    "catastrophic_check": (
        "Two verification scripts were themselves buggy",
        "Both produced plausible wrong numbers. Sanity-check the checker before trusting "
        "what it says about the code under test.",
    ),
    # --- ML / training
    "audio_truncation": (
        "Audio truncated at 8s while targets were not",
        "Corrupted about 85% of the training corpus across a dozen versions. Pursued for a "
        "long time as a modelling problem when it was a data pipeline bug.",
    ),
    "lexicon_variant": (
        "Wrong pronunciation variant defaulted from the lexicon",
        "Silent selection of the first entry rather than the contextually correct one.",
    ),
    "causal_lm_embedding": (
        "A causal LM is not an embedding model",
        "Decoder-only models without contrastive training give anisotropic spaces dominated "
        "by token frequency, so similarity degrades toward lexical overlap.",
    ),
    "check_data_first": (
        "Audit the data path before entertaining an architectural explanation",
        "Two long debugging arcs turned out to be pipeline bugs. Shapes and label alignment "
        "first, theory second.",
    ),
    # --- retrieval / storage
    "rrf": (
        "Reciprocal rank fusion needs no score calibration",
        "It is rank-based, so incommensurable scorers never have to be reconciled. "
        "score = sum of 1/(k + rank).",
    ),
    "bm25_identifiers": (
        "Dense vectors are poor at exact identifiers",
        "Function names, paper titles and hash prefixes are a large share of real queries. "
        "Keyword search handles them far better.",
    ),
    "fts5_external": (
        "FTS5 external content mode needs triggers to stay in sync",
        "content='note' stores no copy, so inserts must be mirrored. Append-only data makes "
        "this a single insert trigger.",
    ),
    "wal_mode": (
        "WAL lets readers proceed during a write transaction",
        "Without it a long write blocks every reader on the same file.",
    ),
    "sqlite_threads": (
        "sqlite3 connections are thread-checked by default",
        "check_same_thread=False plus a lock spanning whole operations, because "
        "resolve-then-insert is only atomic if nothing interleaves.",
    ),
    "vec_bruteforce": (
        "Brute force KNN is fast enough at this corpus size",
        "Avoiding the ANN index keeps the blast radius of an upstream break small.",
    ),
    # --- the note store's own design
    "retained_max": (
        "retained combines by maximum across paths, not minimum",
        "It is an existential claim -- does this content survive somewhere -- so max is the "
        "right quantifier. Fails toward keeping the note.",
    ),
    "retraction_nonlocal": (
        "Retraction is non-local under maximum",
        "If B asserted A's content, retracting A leaves B contaminated, so B must be "
        "superseded too. The checker flags the follow-up you owe.",
    ),
    "four_words": (
        "Four word ids, not three",
        "The risk is a corrupted lookup landing on a real note. At 10k notes three words "
        "gives about 1 in 140; four gives about 1 in 215,000.",
    ),
    "recovery_radius": (
        "Recovery radius is exactly 1 and not a tunable",
        "A code corrects t errors iff minimum distance >= 2t+1. Correcting two needs d >= 5, "
        "but length four caps d at 4 by the Singleton bound.",
    ),
    "desc_recognition": (
        "desc is written to be recognised, not to summarise",
        "It is the only field visible during triage, so every retrieval decision is made on "
        "it alone.",
    ),
    "snapshot_deadline": (
        "Capture at creation makes link preservation once-only",
        "A page not fetched when the note was written may be gone later. Extraction is "
        "re-runnable; an unfetched page is not.",
    ),
    "link_vs_snapshot": (
        "DELETE FROM link is safe, DELETE FROM snapshot is not",
        "They share a primary key and have opposite recoverability. Separate database files "
        "keep the rebuild from reaching the witness.",
    ),
    # --- Rust / C++ / tooling
    "rust_borrow": (
        "Borrow checker rejects holding a reference across a mutation",
        "The fix is usually to narrow the scope of the borrow rather than to clone.",
    ),
    "cpp_diamond": (
        "Virtual inheritance decides identity, not just layout",
        "The diamond question is whether there is one A subobject or two.",
    ),
    "cmake_include": (
        "target_include_directories PUBLIC propagates to dependents",
        "PRIVATE does not, which is why a consumer fails to find the header.",
    ),
    "gtest_fixture": (
        "A gtest fixture is constructed fresh for every test case",
        "State set up in the constructor does not leak between cases, which is usually what "
        "you want and occasionally expensive.",
    ),
    # --- CV / robotics
    "lens_distortion": (
        "Radial and tangential coefficients come from a checkerboard fit",
        "Undistorting with the wrong coefficient order silently warps the image.",
    ),
    "backlash": (
        "Gearbox backlash appears as a deadband in the position loop",
        "Usually compensated feed-forward rather than by raising gain.",
    ),
    "hand_eye": (
        "Hand-eye calibration solves AX = XB",
        "Needs motions with non-parallel rotation axes or the solution is underdetermined.",
    ),
    "exposure_sync": (
        "Rolling shutter skews fast motion",
        "Global shutter or a much shorter exposure is the only real fix; software "
        "correction assumes constant velocity.",
    ),
    # --- distributed / infra
    "idempotent_retry": (
        "A retry without an idempotency key duplicates work",
        "At-least-once delivery means the consumer must deduplicate.",
    ),
    "clock_skew": (
        "Wall clock timestamps are not a total order across machines",
        "Use a logical clock when ordering matters.",
    ),
    "backpressure": (
        "An unbounded queue converts a throughput problem into a memory problem",
        "Bound it and shed load explicitly.",
    ),
    # --- licensing / process
    "copyleft": (
        "Avoid copyleft for anything linked into shipped code",
        "GPL, LGPL, AGPL and MPL are out for inclusion. Using such tools is fine; shipping "
        "their code inside ours is not.",
    ),
    "credit_contributors": (
        "Maintain CREDITS beyond what the license requires",
        "Naming people who helped is an ethical commitment, not boilerplate.",
    ),
    "provenance_bytes": (
        "Files in the provenance archive stay byte-for-byte unmodified",
        "No reformatting, no line ending normalisation. The hash is the point.",
    ),
}

# (query, accepted note keys, kind)
#   'ident'   -- the query names something exactly; keyword search should shine
#   'concept' -- paraphrase with little or no lexical overlap; vectors should shine
#   'mixed'   -- ordinary phrasing, some overlap
QUERIES = [
    ("why does my filter cutoff end up in the wrong place", {"bilinear"}, "concept"),
    ("relationship between peak gain and time constant", {"tau", "peak_gain"}, "concept"),
    ("tan(wT/2)", {"bilinear"}, "ident"),
    ("is peak a duration", {"peak_gain", "tau"}, "concept"),
    ("maximally flat passband", {"butterworth"}, "mixed"),
    ("should I specify the corner in samples or hertz", {"filter_units"}, "concept"),
    ("cost of symmetric FIR", {"group_delay"}, "mixed"),
    ("subtracting two close numbers loses digits", {"cancellation"}, "concept"),
    ("numpy returned index zero for an empty detection", {"argmax_allfalse"}, "concept"),
    ("my test harness itself had a bug", {"catastrophic_check"}, "concept"),
    ("most of my training data was silently misaligned", {"audio_truncation"}, "concept"),
    ("the lexicon picked the wrong entry", {"lexicon_variant"}, "mixed"),
    ("can I use a reasoning model to make embeddings", {"causal_lm_embedding"}, "concept"),
    ("results are off, where should I look first", {"check_data_first"}, "concept"),
    ("combining two rankings", {"rrf"}, "concept"),
    ("reciprocal rank fusion", {"rrf"}, "ident"),
    ("keyword search beats vectors for function names", {"bm25_identifiers"}, "mixed"),
    ("content='note'", {"fts5_external"}, "ident"),
    ("concurrent reads while writing to the database", {"wal_mode"}, "concept"),
    ("check_same_thread", {"sqlite_threads"}, "ident"),
    ("do I need an approximate nearest neighbour index", {"vec_bruteforce"}, "concept"),
    ("why max and not min for surviving content", {"retained_max"}, "mixed"),
    ("retracting a note that others depend on", {"retraction_nonlocal"}, "concept"),
    ("how many words should an identifier have", {"four_words"}, "concept"),
    ("Singleton bound", {"recovery_radius"}, "ident"),
    ("how should I phrase the handle for a note", {"desc_recognition"}, "concept"),
    ("what happens if the page disappears later", {"snapshot_deadline"}, "concept"),
    ("which table is safe to drop and rebuild", {"link_vs_snapshot"}, "concept"),
    ("holding a reference while mutating", {"rust_borrow"}, "mixed"),
    ("one A subobject or two", {"cpp_diamond"}, "ident"),
    ("consumer cannot find my header", {"cmake_include"}, "concept"),
    ("does state leak between test cases", {"gtest_fixture"}, "concept"),
    ("undistortion looks warped", {"lens_distortion"}, "concept"),
    ("deadband in the position loop", {"backlash"}, "ident"),
    ("AX = XB", {"hand_eye"}, "ident"),
    ("fast moving objects look slanted", {"exposure_sync"}, "concept"),
    ("the consumer processed the same message twice", {"idempotent_retry"}, "concept"),
    ("ordering events across machines", {"clock_skew"}, "concept"),
    ("my queue ate all the memory", {"backpressure"}, "concept"),
    ("can I depend on this GPL library", {"copyleft"}, "concept"),
    ("do I need to credit someone who helped", {"credit_contributors"}, "concept"),
    ("can I reformat the archived files", {"provenance_bytes"}, "concept"),
]
