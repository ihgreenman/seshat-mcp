"""Can retrieval distinguish note ids that differ by one word?

Suggested by Chat against §3 of docs/retrieval-findings.md. The identifier
experiment there used opaque hex and version strings, where the vector side
collapsed near-misses to a single point. Note ids are different in kind: BIP-39
words are *native tokens*, which is exactly why §6.1 chose them. So the question
is open -- do they survive embedding as distinguishable, or collapse like hashes?

Two framings, because they answer different questions:

  (a) three notes whose OWN ids differ by one word. Query the id.
      Asks: can `context` find a note by its identifier at all?

  (b) three notes that CITE those ids in their text. Query the id.
      Asks: the true analogue of the hash test -- when an id appears as
      content, can retrieval tell near-identical ids apart?
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

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
DB = pathlib.Path(__file__).parent / "noteids.db"

# Three ids one word-edit apart. Real BIP-39 words throughout -- note that the
# spec's own example, bright-otter-canvas-fig, is not (see findings §7).
TARGETS = {
    "olive-canvas-bright-zebra": (
        "Resampler drops a sample at block boundaries",
        "The phase accumulator resets per block instead of carrying over.",
    ),
    "olive-canvas-bright-yellow": (  # differs in word 4
        "Window function applied twice in the overlap-add path",
        "Once in the analysis stage and again before the inverse transform.",
    ),
    "olive-canvas-brisk-zebra": (  # differs in word 3
        "Sample rate conversion ratio is computed with integer division",
        "48000/44100 truncates to 1, so the resampler becomes a no-op.",
    ),
}

CITERS = {
    "cite_a": ("Confirmed the block boundary bug on hardware",
               "Reproduced what olive-canvas-bright-zebra describes, on the DSP target."),
    "cite_b": ("Overlap-add fix verified against the reference",
               "The double windowing in olive-canvas-bright-yellow is gone after the patch."),
    "cite_c": ("Integer division bug also affects the decimator",
               "Same root cause as olive-canvas-brisk-zebra, different call site."),
}
CITE_FOR = {
    "olive-canvas-bright-zebra": "cite_a",
    "olive-canvas-bright-yellow": "cite_b",
    "olive-canvas-brisk-zebra": "cite_c",
}


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / ((l2_norm(a) * l2_norm(b)) or 1.0)


def main() -> None:
    if DB.exists():
        DB.unlink()
    store = Store(DB, embedder=None)

    keys, doc, ids = [], {}, {}
    for key, (desc, text) in NOTES.items():
        note_id, _ = store.create_note(desc, text)
        ids[note_id] = key
        keys.append(key)
        doc[key] = document_text(desc, text)

    # Chosen ids have to be inserted directly; minting is random by design.
    for note_id, (desc, text) in TARGETS.items():
        store.db.execute(
            "INSERT INTO note(id, desc, text, created_at) VALUES (?,?,?,?)",
            (note_id, desc, text, "2026-02-01T00:00:00.000000+00:00"),
        )
        ids[note_id] = note_id
        keys.append(note_id)
        doc[note_id] = document_text(desc, text)
    store.db.commit()

    for key, (desc, text) in CITERS.items():
        note_id, _ = store.create_note(desc, text)
        ids[note_id] = key
        keys.append(key)
        doc[key] = document_text(desc, text)

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    embedder = OllamaEmbedder()
    want = [DOCUMENT_PREFIX + doc[k] for k in keys] + [QUERY_PREFIX + t for t in TARGETS]
    missing = sorted({t for t in want if t not in cache})
    for i in range(0, len(missing), 32):
        batch = missing[i : i + 32]
        for text, vector in zip(batch, embedder.embed(batch)):
            cache[text] = vector
    if missing:
        CACHE.write_text(json.dumps(cache))

    print(f"corpus: {len(keys)} notes "
          f"({len(TARGETS)} with ids one word apart, {len(CITERS)} citing them)\n")

    def rankings(query: str):
        expression = fts_query(query)
        fts = [ids[r] for r in store._fts_ranking(expression, None, 10)] if expression else []
        qv = cache[QUERY_PREFIX + query]
        vec = [k for _, k in sorted(
            ((cosine(qv, cache[DOCUMENT_PREFIX + doc[k]]), k) for k in keys), reverse=True
        )][:10]
        hyb = [k for k, _ in sorted(fuse([fts, vec], k=RRF_K).items(), key=lambda kv: -kv[1])][:10]
        return fts, vec, hyb

    def rank(ranking, target):
        return next((i for i, k in enumerate(ranking, 1) if k == target), None)

    fmt = lambda v: str(v) if v else "--"  # noqa: E731

    print("(a) querying an id, looking for THE NOTE THAT HAS THAT ID")
    print(f"    {'query':<30} {'FTS':>5} {'VEC':>5} {'HYB':>5}")
    print("    " + "-" * 52)
    for note_id in TARGETS:
        fts, vec, hyb = rankings(note_id)
        print(f"    {note_id:<30} {fmt(rank(fts, note_id)):>5} "
              f"{fmt(rank(vec, note_id)):>5} {fmt(rank(hyb, note_id)):>5}")

    print("\n(b) querying an id, looking for THE NOTE THAT CITES IT")
    print(f"    {'query':<30} {'FTS':>5} {'VEC':>5} {'HYB':>5}   vector's top hit")
    print("    " + "-" * 76)
    rr = {"fts": [], "vec": [], "hyb": []}
    for note_id in TARGETS:
        target = CITE_FOR[note_id]
        fts, vec, hyb = rankings(note_id)
        r = {"fts": rank(fts, target), "vec": rank(vec, target), "hyb": rank(hyb, target)}
        for name, value in r.items():
            rr[name].append(1.0 / value if value else 0.0)
        top = vec[0]
        flag = "" if top == target else "   <- wrong"
        print(f"    {note_id:<30} {fmt(r['fts']):>5} {fmt(r['vec']):>5} {fmt(r['hyb']):>5}   "
              f"{top}{flag}")
    print("    " + "-" * 76)
    print(f"    {'MRR':<30} {statistics.mean(rr['fts']):>5.3f} "
          f"{statistics.mean(rr['vec']):>5.3f} {statistics.mean(rr['hyb']):>5.3f}")

    print("\n(c) pairwise cosine between the three citing notes")
    for a in CITERS:
        row = "    " + f"{a:<10}"
        for b in CITERS:
            row += f" {cosine(cache[DOCUMENT_PREFIX + doc[a]], cache[DOCUMENT_PREFIX + doc[b]]):.3f}"
        print(row)
    store.close()


if __name__ == "__main__":
    main()
