"""Does the store's output let a caller tell there is no answer? Spec §9.6.

Every query measured so far has an answer in the corpus, so nothing tested the
response to a query the store cannot serve. That is where an over-eager keyword
side does the most damage, and where `vector_similarity` (§3.2) has to earn its
place: `score` cannot express "nothing relevant" because it is derived from rank
alone, so rank 1 of a useless list scores exactly what rank 1 of a good list does.

The test: 20 answerable queries against the 40-note corpus, and 20 queries about
subjects the corpus contains nothing on. If `vector_similarity` separates them,
a caller can honestly say "the store has nothing on this". If it does not, §3.2's
new field does not do the job it was added for.
"""

from __future__ import annotations

import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from corpus import NOTES, QUERIES  # noqa: E402
from seshat.embeddings import DEFAULT_MODEL, OllamaEmbedder  # noqa: E402
from seshat.store import Store  # noqa: E402
from seshat.worker import EmbeddingWorker  # noqa: E402

DB = pathlib.Path(__file__).parent / "noanswer.db"

# Subjects the corpus says nothing about. Deliberately plausible-sounding
# technical questions rather than nonsense -- "asdfgh" would be too easy, and
# the realistic failure is a reasonable question about something not recorded.
ABSENT = [
    "how do I get a mortgage pre-approval",
    "best way to proof sourdough in a cold kitchen",
    "kubernetes ingress returns 502 after a rolling update",
    "why is my TypeScript discriminated union not narrowing",
    "what dosage of amoxicillin for a sinus infection",
    "how to register a trademark in the EU",
    "tuning the suspension on a mountain bike fork",
    "postgres logical replication slot filling the disk",
    "what is the melting point of tungsten carbide",
    "how do I claim mileage on a tax return",
    "training a puppy to stop pulling on the lead",
    "CSS grid column collapsing on mobile safari",
    "which telescope eyepiece for planetary viewing",
    "how long to marinate beef for bulgogi",
    "rust axum middleware ordering with tower layers",
    "visa requirements for a working holiday in Japan",
    "why does my espresso shot channel",
    "setting up wireguard between two NAT'd networks",
    "what year did the Hubble deep field release",
    "how to replace a dishwasher drain pump",
]


def main() -> None:
    if DB.exists():
        DB.unlink()
    store = Store(DB, embedder=OllamaEmbedder(model=DEFAULT_MODEL))
    if not store.vector_loaded:
        raise SystemExit("sqlite-vec unavailable")
    for desc, text in NOTES.values():
        store.create_note(desc, text)
    embedded = EmbeddingWorker(store).drain()
    print(f"corpus: {len(NOTES)} notes, {embedded} embedded\n")

    answerable = [q for q, _, _ in QUERIES][:20]

    def probe(queries, label):
        rows = []
        for query in queries:
            hits = store.context(query, limit=5)
            top = hits[0] if hits else None
            rows.append({
                "query": query,
                "n": len(hits),
                "top_sim": top.vector_similarity if top else None,
                "top_matched": ",".join(top.matched) if top else "",
                "top_score": top.score if top else None,
            })
        sims = [r["top_sim"] for r in rows if r["top_sim"] is not None]
        print(f"--- {label} ({len(rows)} queries) ---")
        print(f"    results returned: {sum(1 for r in rows if r['n'])}/{len(rows)}")
        print(f"    top vector_similarity: min {min(sims):.3f}  "
              f"median {statistics.median(sims):.3f}  max {max(sims):.3f}")
        print(f"    top score: {{{', '.join(sorted({f'{r['top_score']:.5f}' for r in rows if r['top_score']}))}}}")
        return rows, sims

    good_rows, good = probe(answerable, "ANSWERABLE")
    print()
    bad_rows, bad = probe(ABSENT, "NOT IN THE STORE")

    print("\n--- separation ---")
    print(f"    worst answerable similarity : {min(good):.3f}")
    print(f"    best absent similarity      : {max(bad):.3f}")
    overlap = min(good) <= max(bad)
    print(f"    {'OVERLAP -- no clean threshold' if overlap else 'CLEAN SEPARATION'}")

    best = None
    for candidate in [x / 100 for x in range(30, 80)]:
        tp = sum(1 for s in good if s >= candidate)
        tn = sum(1 for s in bad if s < candidate)
        acc = (tp + tn) / (len(good) + len(bad))
        if best is None or acc > best[1]:
            best = (candidate, acc, tp, tn)
    threshold, accuracy, tp, tn = best
    print(f"    best threshold {threshold:.2f}: {accuracy:.0%} correct "
          f"({tp}/{len(good)} answerable kept, {tn}/{len(bad)} absent rejected)")

    print("\n--- absent queries that still returned something ---")
    for row in sorted(bad_rows, key=lambda r: -(r["top_sim"] or 0))[:6]:
        print(f"    cos {row['top_sim']:.3f}  [{row['top_matched']:<11}]  {row['query']}")
    store.close()


if __name__ == "__main__":
    main()
