"""Retrieval policy evaluation for seshat.

Compares FTS-only, vector-only and hybrid configurations over a 40-note corpus
and 42 queries with known answers. Embeds once per prefix scheme and caches, so
re-running is cheap.

Metrics, all @10 unless stated:
  top1    fraction of queries whose first result is an accepted answer
  MRR     mean of 1/(rank of first accepted answer), 0 if not in top 10
  recall  fraction of queries with an accepted answer anywhere in top 10

Harness self-checks run first: every note must be retrievable by its own desc,
and every query's accepted keys must exist. A metric computed over a corpus that
did not load correctly is worse than no metric.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from corpus import NOTES, QUERIES  # noqa: E402

sys.path.insert(0, "/Users/greenman/src/seshat-mcp/src")

from seshat.embeddings import (  # noqa: E402
    DOCUMENT_PREFIX,
    QUERY_PREFIX,
    OllamaEmbedder,
    document_text,
    l2_norm,
)
from seshat.store import RRF_K, Store, fts_query, fuse  # noqa: E402

CACHE = pathlib.Path(__file__).parent / "embed_cache.json"
DB = pathlib.Path(__file__).parent / "eval.db"


# ------------------------------------------------------------------ embedding

def load_cache() -> dict:
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def embed_all(texts: list[str], cache: dict, embedder: OllamaEmbedder) -> None:
    missing = [t for t in texts if t not in cache]
    for i in range(0, len(missing), 32):
        batch = missing[i : i + 32]
        for text, vector in zip(batch, embedder.embed(batch)):
            cache[text] = vector
    if missing:
        CACHE.write_text(json.dumps(cache))


def cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b)) / ((l2_norm(a) * l2_norm(b)) or 1.0)


# --------------------------------------------------------------------- rankings

def vector_ranking(query: str, cache: dict, doc_prefix: str, query_prefix: str,
                   keys: list[str], doc_texts: dict[str, str], limit: int) -> list[str]:
    qv = cache[query_prefix + query]
    scored = sorted(
        ((cosine(qv, cache[doc_prefix + doc_texts[k]]), k) for k in keys), reverse=True
    )
    return [k for _, k in scored[:limit]]


def fts_ranking(store: Store, ids: dict, query: str, limit: int, stopwords: bool) -> list[str]:
    import seshat.store as module

    saved = module.STOPWORDS
    if not stopwords:
        module.STOPWORDS = frozenset()
    try:
        expression = fts_query(query)
        if expression is None:
            return []
        rows = store._fts_ranking(expression, None, limit)
    finally:
        module.STOPWORDS = saved
    return [ids[r] for r in rows]


# ---------------------------------------------------------------------- metrics

def score(rankings: dict[str, list[str]], subset=None) -> dict[str, float]:
    top1, rr, recall, n = 0, [], 0, 0
    for (query, accept, kind) in QUERIES:
        if subset and kind != subset:
            continue
        n += 1
        ranked = rankings[query]
        if ranked and ranked[0] in accept:
            top1 += 1
        found = next((i for i, k in enumerate(ranked, 1) if k in accept), None)
        rr.append(1.0 / found if found else 0.0)
        recall += bool(found)
    return {
        "n": n,
        "top1": top1 / n,
        "mrr": statistics.mean(rr),
        "recall": recall / n,
    }


def report(label: str, rankings: dict[str, list[str]]) -> None:
    overall = score(rankings)
    line = f"{label:<34} top1 {overall['top1']:.2f}  MRR {overall['mrr']:.3f}  R@10 {overall['recall']:.2f}"
    for kind in ("ident", "concept", "mixed"):
        s = score(rankings, kind)
        line += f"   {kind[:4]} {s['mrr']:.2f}"
    print(line)


def main() -> None:
    keys = list(NOTES)
    doc_texts = {k: document_text(*NOTES[k]) for k in keys}

    print(f"corpus: {len(keys)} notes, {len(QUERIES)} queries")
    for _, accept, _ in QUERIES:
        missing = accept - set(keys)
        assert not missing, f"query references unknown notes: {missing}"

    if DB.exists():
        DB.unlink()
    store = Store(DB, embedder=None)
    ids: dict[str, str] = {}
    for key in keys:
        note_id, _ = store.create_note(*NOTES[key])
        ids[note_id] = key

    # Harness self-check: a corpus that did not index is not worth measuring.
    unfindable = [
        k for k in keys
        if k not in fts_ranking(store, ids, NOTES[k][0], 5, True)
    ]
    assert not unfindable, f"notes not retrievable by their own desc: {unfindable}"
    print("harness check: every note retrievable by its own desc\n")

    embedder = OllamaEmbedder()
    cache = load_cache()
    schemes = {
        "correct": (DOCUMENT_PREFIX, QUERY_PREFIX),
        "both-doc": (DOCUMENT_PREFIX, DOCUMENT_PREFIX),
        "none": ("", ""),
    }
    wanted = []
    for dp, qp in schemes.values():
        wanted += [dp + doc_texts[k] for k in keys]
        wanted += [qp + q for q, _, _ in QUERIES]
    embed_all(sorted(set(wanted)), cache, embedder)
    print(f"embeddings cached: {len(cache)}\n")

    dp, qp = schemes["correct"]
    vec = {q: vector_ranking(q, cache, dp, qp, keys, doc_texts, 10) for q, _, _ in QUERIES}
    fts_on = {q: fts_ranking(store, ids, q, 10, True) for q, _, _ in QUERIES}
    fts_off = {q: fts_ranking(store, ids, q, 10, False) for q, _, _ in QUERIES}

    def hybrid(fts, k=RRF_K):
        out = {}
        for query, _, _ in QUERIES:
            scores = fuse([fts[query], vec[query]], k=k)
            out[query] = [k_ for k_, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:10]
        return out

    print("=" * 96)
    print("SINGLE RETRIEVERS" + " " * 33 + "overall" + " " * 24 + "by query kind (MRR)")
    print("=" * 96)
    report("FTS only, stopwords kept", fts_off)
    report("FTS only, stopwords dropped", fts_on)
    report("Vector only", vec)

    print("\n" + "=" * 96)
    print("HYBRID (RRF)")
    print("=" * 96)
    report("Hybrid, stopwords kept", hybrid(fts_off))
    report("Hybrid, stopwords dropped", hybrid(fts_on))

    print("\n" + "=" * 96)
    print("RRF k SENSITIVITY (stopwords dropped)")
    print("=" * 96)
    for k in (5, 10, 30, 60, 120, 300):
        report(f"Hybrid, k = {k}", hybrid(fts_on, k))

    print("\n" + "=" * 96)
    print("NOMIC TASK PREFIXES (vector only)")
    print("=" * 96)
    for name, (d, q) in schemes.items():
        ranking = {qq: vector_ranking(qq, cache, d, q, keys, doc_texts, 10)
                   for qq, _, _ in QUERIES}
        report(f"prefixes: {name}", ranking)

    print("\n" + "=" * 96)
    print("QUERIES WHERE HYBRID BEATS OR LOSES TO BOTH SINGLES")
    print("=" * 96)
    hyb = hybrid(fts_on)

    def rank_of(ranking, accept):
        return next((i for i, k in enumerate(ranking, 1) if k in accept), None)

    for query, accept, kind in QUERIES:
        h, f, v = (rank_of(hyb[query], accept), rank_of(fts_on[query], accept),
                   rank_of(vec[query], accept))
        singles = [r for r in (f, v) if r]
        best_single = min(singles) if singles else None
        if h and best_single and h < best_single:
            print(f"  WIN   h={h} f={f} v={v}  {query!r}")
        elif (h or 99) > (best_single or 99):
            print(f"  LOSS  h={h} f={f} v={v}  {query!r}")
    store.close()


if __name__ == "__main__":
    main()
