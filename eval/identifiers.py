"""Identifier stress test.

§6 justifies hybrid retrieval on one claim: "Pure vector search is poor at exact
identifiers -- function names, paper titles, hash prefixes -- which is a large
share of real queries against a store like this."

The main evaluation did not reproduce that: vectors scored 1.00 MRR on its
'ident' queries. But those identifiers were *pronounceable* ("Singleton bound",
"AX = XB", "check_same_thread") -- English-adjacent tokens a subword model can
represent. The claim is really about opaque strings, which is what this tests:
hashes, version numbers, error codes, UUID fragments, note ids.

If vectors handle these too, hybrid is doing less work than the spec assumes.
If they fail, hybrid earns its place on exactly the queries FTS exists for.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, "/Users/greenman/src/seshat-mcp/src")

from corpus import NOTES  # noqa: E402
from seshat.embeddings import (  # noqa: E402
    DOCUMENT_PREFIX,
    QUERY_PREFIX,
    OllamaEmbedder,
    document_text,
    l2_norm,
)
from seshat.store import RRF_K, Store, fts_query, fuse  # noqa: E402

CACHE = pathlib.Path(__file__).parent / "embed_cache.json"
DB = pathlib.Path(__file__).parent / "ident.db"

OPAQUE_NOTES = {
    "artifact_hash": (
        "Training corpus v3 archived under sha256 4f2a9c81",
        "Full digest 4f2a9c81b7e35d0a6c14f8e27b9d3a5061cc8e4f2b7a19d06e53c8b4a7f21d9e. "
        "The truncated corpus is the one before it and must not be reused.",
    ),
    "sqlite_vec_version": (
        "sqlite-vec v0.1.9 changed the KNN syntax",
        "Pre-v1 and expect breaking changes. Brute force avoids the ANN index entirely.",
    ),
    "error_code": (
        "SQLITE_BUSY means another writer holds the lock",
        "Error code 5. busy_timeout turns it into a wait rather than an immediate failure.",
    ),
    "fn_name": (
        "vec_distance_cosine works on a stored embedding column",
        "That is what makes a filtered brute force join possible instead of a MATCH query.",
    ),
    "paper": (
        "Cormack, Clarke and Buettcher 2009 introduced reciprocal rank fusion",
        "The k = 60 constant comes from that paper and was never tuned for this corpus.",
    ),
    "note_ref": (
        "Store identity lives in the meta table",
        "Keys are store_version, created_under_spec and created_at.",
    ),
}

OPAQUE_QUERIES = [
    ("4f2a9c81", {"artifact_hash"}),
    ("sha256 4f2a9c81b7e35d0a", {"artifact_hash"}),
    ("v0.1.9", {"sqlite_vec_version"}),
    ("SQLITE_BUSY", {"error_code"}),
    ("error code 5", {"error_code"}),
    ("vec_distance_cosine", {"fn_name"}),
    ("Cormack Clarke Buettcher", {"paper"}),
    ("created_under_spec", {"note_ref"}),
]


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / ((l2_norm(a) * l2_norm(b)) or 1.0)


def main() -> None:
    all_notes = dict(NOTES)
    all_notes.update(OPAQUE_NOTES)
    keys = list(all_notes)
    doc_texts = {k: document_text(*all_notes[k]) for k in keys}

    if DB.exists():
        DB.unlink()
    store = Store(DB, embedder=None)
    ids = {}
    for key in keys:
        note_id, _ = store.create_note(*all_notes[key])
        ids[note_id] = key

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    embedder = OllamaEmbedder()
    wanted = [DOCUMENT_PREFIX + doc_texts[k] for k in keys]
    wanted += [QUERY_PREFIX + q for q, _ in OPAQUE_QUERIES]
    missing = sorted({t for t in wanted if t not in cache})
    for i in range(0, len(missing), 32):
        batch = missing[i : i + 32]
        for text, vector in zip(batch, embedder.embed(batch)):
            cache[text] = vector
    if missing:
        CACHE.write_text(json.dumps(cache))

    print(f"corpus: {len(keys)} notes ({len(OPAQUE_NOTES)} with opaque identifiers)")
    print(f"queries: {len(OPAQUE_QUERIES)}, all exact-identifier lookups\n")
    print(f"{'query':<34} {'FTS':>6} {'VEC':>6} {'HYB':>6}   target")
    print("-" * 86)

    rr = {"fts": [], "vec": [], "hyb": []}
    for query, accept in OPAQUE_QUERIES:
        expression = fts_query(query)
        fts = [ids[r] for r in store._fts_ranking(expression, None, 10)] if expression else []
        qv = cache[QUERY_PREFIX + query]
        vec = [k for _, k in sorted(
            ((cosine(qv, cache[DOCUMENT_PREFIX + doc_texts[k]]), k) for k in keys), reverse=True
        )][:10]
        scores = fuse([fts, vec], k=RRF_K)
        hyb = [k for k, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:10]

        def rank(ranking):
            return next((i for i, k in enumerate(ranking, 1) if k in accept), None)

        r = {"fts": rank(fts), "vec": rank(vec), "hyb": rank(hyb)}
        for name, value in r.items():
            rr[name].append(1.0 / value if value else 0.0)
        fmt = lambda v: str(v) if v else "--"  # noqa: E731
        print(f"{query:<34} {fmt(r['fts']):>6} {fmt(r['vec']):>6} {fmt(r['hyb']):>6}   "
              f"{list(accept)[0]}")

    print("-" * 86)
    print(f"{'MRR':<34} {statistics.mean(rr['fts']):>6.3f} {statistics.mean(rr['vec']):>6.3f} "
          f"{statistics.mean(rr['hyb']):>6.3f}")
    print(f"{'top-1':<34} {sum(x == 1 for x in rr['fts']):>6} "
          f"{sum(x == 1 for x in rr['vec']):>6} {sum(x == 1 for x in rr['hyb']):>6}"
          f"   of {len(OPAQUE_QUERIES)}")
    store.close()


if __name__ == "__main__":
    main()
