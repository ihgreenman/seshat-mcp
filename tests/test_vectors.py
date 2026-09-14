"""Embedding, vector search, and hybrid fusion. Spec §6, §6.3, §6.4.

No test here calls Ollama. The embedder is a deterministic fake, because what
needs pinning is the *plumbing* -- prefixes applied on the right side, writes
never blocking, degradation when the vector side is cold -- not the model's
semantics, which a unit test cannot check anyway.
"""

import math
import zlib

import pytest

from seshat.embeddings import (
    DOCUMENT_PREFIX,
    QUERY_PREFIX,
    EmbedderUnavailable,
    document_text,
    embed_documents,
    embed_query,
    l2_norm,
)
from seshat.markdown import strip_markup
from seshat.store import RRF_K, Store, fuse
from seshat.worker import EmbeddingWorker

DIM = 8


class FakeEmbedder:
    """Hashes tokens into a small vector. Deterministic, offline, and records
    exactly what it was asked to embed -- which is how the prefix tests work."""

    model = "fake-embed"
    dim = DIM

    def __init__(self):
        self.seen: list[str] = []
        self.fail = False
        # Exact prefixed-text -> vector overrides, for tests that need to place
        # notes in the vector space deliberately rather than by hash accident.
        self.table: dict[str, list[float]] = {}

    def _vector(self, text: str) -> list[float]:
        # zlib.crc32, not hash(): str hashing is salted per process, so a fake
        # built on it produces different vectors every run and the tests that
        # depend on relative distances pass or fail at random.
        v = [0.0] * DIM
        for token in text.lower().split():
            v[zlib.crc32(token.encode()) % DIM] += 1.0
        norm = l2_norm(v) or 1.0
        return [x / norm for x in v]

    def embed(self, texts, timeout=None):
        if self.fail:
            raise EmbedderUnavailable("pretend ollama is down")
        self.seen.extend(texts)
        return [self.table.get(t, self._vector(t)) for t in texts]


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def vstore(tmp_path, embedder):
    s = Store(tmp_path / "v.db", embedder=embedder)
    if not s.vector_loaded:
        pytest.skip("sqlite-vec unavailable")
    yield s
    s.close()


# ------------------------------------------------------------------ fusion


def test_fuse_matches_the_formula_computed_by_hand():
    """score = sum over rankings of 1/(k + rank), ranks 1-indexed."""
    scores = fuse([["a", "b", "c"], ["c", "a"]], k=60)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["b"] == pytest.approx(1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)


def test_agreement_between_retrievers_outranks_either_alone():
    """The entire reason for running them separately (§6)."""
    scores = fuse([["shared", "fts_only"], ["shared", "vec_only"]])
    assert scores["shared"] > scores["fts_only"]
    assert scores["shared"] > scores["vec_only"]


def test_scores_accumulate_across_rankings_rather_than_taking_the_best():
    """Sum, not maximum -- and the two differ exactly where it matters.

    A note both retrievers rank second beats a note one retriever ranks first:
    2/(k+2) > 1/(k+1). Under `max` the mediocre-but-corroborated note would
    lose, which would make running two retrievers pointless for anything except
    recall. The earlier agreement test passes under either rule; this one does
    not.
    """
    scores = fuse([["top", "corroborated"], ["other", "corroborated"]])
    assert scores["corroborated"] > scores["top"]
    assert scores["corroborated"] == pytest.approx(2 / (RRF_K + 2))


def test_fusion_never_reconciles_score_scales():
    """RRF is rank-based, so a ranking's underlying numbers are irrelevant --
    BM25 (negative, unbounded) and cosine distance (0..2) never have to be
    made commensurable."""
    assert fuse([["a", "b"]]) == fuse([["a", "b"]])
    assert fuse([["a"]])["a"] == pytest.approx(1 / (RRF_K + 1))


def test_fusion_of_one_ranking_is_the_fts_only_shape():
    """Degradation must not change the scale a caller has calibrated against."""
    assert fuse([["a", "b", "c"]]) == {
        "a": 1 / (RRF_K + 1), "b": 1 / (RRF_K + 2), "c": 1 / (RRF_K + 3)
    }


# ------------------------------------------------------- what gets embedded


def test_a_backend_cannot_apply_a_prefix_itself(embedder):
    """Structural, not disciplinary: `embed` receives fully-prefixed text, so
    there is no second place where the convention could be got wrong."""
    embed_documents(embedder, ["a note"])
    embed_query(embedder, "a question")
    assert embedder.seen == [DOCUMENT_PREFIX + "a note", QUERY_PREFIX + "a question"]


def test_documents_and_queries_get_opposite_prefixes(vstore, embedder):
    """§6: the same prefix on both sides degrades retrieval silently. No caller
    chooses -- the two module functions apply their own."""
    vstore.create_note("a finding", "some body text")
    EmbeddingWorker(vstore).drain()
    vstore.context("a question")

    documents = [t for t in embedder.seen if t.startswith(DOCUMENT_PREFIX)]
    queries = [t for t in embedder.seen if t.startswith(QUERY_PREFIX)]
    assert len(documents) == 1 and len(queries) == 1
    assert not any(t.startswith(QUERY_PREFIX) for t in documents)


def test_embedded_text_is_stripped_but_the_index_is_raw(vstore, embedder):
    """§6.4, the one that is easy to get backwards."""
    text = "See [the card](https://ollama.com/library/nomic) for dims.\n\n```\ncurl https://x\n```"
    note_id, _ = vstore.create_note("embedding dims", text)
    EmbeddingWorker(vstore).drain()

    embedded = embedder.seen[0]
    assert "ollama.com" not in embedded, "URLs are noise in the vector"
    assert "curl" not in embedded, "code blocks are the largest noise source"
    assert "the card" in embedded, "anchor text carries the meaning"
    # ...while FTS still finds the raw URL, because that is a real query.
    assert vstore.context("ollama.com")[0].id == note_id


def test_document_text_includes_desc(vstore):
    assert document_text("the handle", "the body") == "the handle\n\nthe body"
    assert document_text("the handle", "```\ncode\n```") == "the handle"


def test_strip_markup_leaves_prose():
    text = "# Heading\n\n- **bold** item\n- `code` item\n\n> quoted\n\n[label](http://x)"
    stripped = strip_markup(text)
    assert "Heading" in stripped and "bold" in stripped and "label" in stripped
    for marker in ("#", "**", "`", ">", "http"):
        assert marker not in stripped, marker


# ----------------------------------------------------------------- the queue


def test_writes_do_not_block_on_embedding(vstore, embedder):
    """§6.3, the single most important implementation constraint: a stopped
    Ollama must not make a write fail."""
    embedder.fail = True
    note_id, _ = vstore.create_note("written while the embedder is down", "body")

    assert vstore.read(note_id).desc == "written while the embedder is down"
    assert vstore.context("embedder")[0].id == note_id, "keyword-findable immediately"
    assert vstore.embedding_backlog() == 1


def test_the_backlog_is_a_query_not_a_table(vstore):
    """A row's absence from embedding_meta means 'needs embedding' (§6.3), which
    is what makes model migration a DELETE plus a drain."""
    for i in range(3):
        vstore.create_note(f"note {i}", "body")
    assert vstore.embedding_backlog() == 3
    assert EmbeddingWorker(vstore).drain() == 3
    assert vstore.embedding_backlog() == 0


def test_changing_model_invalidates_every_embedding(vstore, embedder):
    vstore.create_note("a note", "body")
    EmbeddingWorker(vstore).drain()
    assert vstore.embedding_backlog() == 0

    embedder.model = "some-other-model"
    assert vstore.embedding_backlog() == 1, "model != current means needs embedding"


def test_model_migration_is_a_delete_and_a_drain(vstore):
    for i in range(3):
        vstore.create_note(f"note {i}", "body")
    EmbeddingWorker(vstore).drain()

    assert vstore.clear_embeddings() == 3
    assert vstore.embedding_backlog() == 3
    assert EmbeddingWorker(vstore).drain() == 3
    assert vstore.embedding_backlog() == 0


def test_worker_surfaces_embedder_failure_rather_than_looking_idle(vstore, embedder):
    """'nothing to do' and 'could not do it' must be distinguishable, or the
    backlog reported by help is a lie."""
    vstore.create_note("a note", "body")
    embedder.fail = True
    with pytest.raises(EmbedderUnavailable):
        EmbeddingWorker(vstore).run_once()
    assert vstore.embedding_backlog() == 1


def test_embedding_provenance_is_recorded(vstore):
    note_id, _ = vstore.create_note("a note", "body")
    EmbeddingWorker(vstore).drain()
    row = vstore.db.execute(
        "SELECT model, dim, normalized FROM embedding_meta WHERE note_id = ?", (note_id,)
    ).fetchone()
    assert row["model"] == "fake-embed"
    assert row["dim"] == DIM
    assert row["normalized"] == 1


# ------------------------------------------------------------- degradation


def test_context_degrades_to_fts_when_the_embedder_is_down(vstore, embedder):
    """§6.3: worse, but never wrong, and never an error."""
    note_id, _ = vstore.create_note("Nyquist precision loss", "prewarping fixes it")
    EmbeddingWorker(vstore).drain()

    embedder.fail = True
    hits = vstore.context("nyquist")
    assert [h.id for h in hits] == [note_id]
    assert hits[0].score == pytest.approx(1 / (RRF_K + 1)), "single-ranking shape"


def test_context_works_with_nothing_embedded_at_all(vstore):
    note_id, _ = vstore.create_note("Nyquist precision loss", "prewarping fixes it")
    assert vstore.embedding_backlog() == 1
    assert [h.id for h in vstore.context("nyquist")] == [note_id]


def test_a_store_without_vector_support_still_retrieves(tmp_path, embedder, monkeypatch):
    """sqlite-vec is optional and pre-v1; its absence costs quality, not
    function (§6.7)."""
    import seshat.vectors as module

    monkeypatch.setattr(module, "load_extension", lambda db: False)
    store = Store(tmp_path / "novec.db", embedder=embedder)
    assert store.vector_loaded is False

    note_id, _ = store.create_note("Nyquist precision loss", "prewarping")
    assert [h.id for h in store.context("nyquist")] == [note_id]
    assert EmbeddingWorker(store).run_once() == 0, "nowhere to put a vector"
    store.close()


def test_a_failed_query_puts_the_vector_side_on_cooldown(vstore, embedder):
    """Without this, every context call pays the embedder timeout while Ollama
    is down -- graceful degradation that is unusable in practice."""
    vstore.create_note("a note", "body")
    EmbeddingWorker(vstore).drain()

    embedder.fail = True
    vstore.context("anything")
    before = len(embedder.seen)
    vstore.context("anything else")
    assert len(embedder.seen) == before, "second query must not retry the embedder"
    assert vstore.vector_ready is False


# ---------------------------------------------------------------- searching


def test_vector_search_respects_the_pool(vstore):
    """A retracted note must not consume one of the limit slots (§5.1)."""
    old, _ = vstore.create_note("retracted claim", "wrong about everything")
    new, _ = vstore.create_note("correction", "the right answer", [{"id": old, "retained": 0.0}])
    EmbeddingWorker(vstore).drain()

    import seshat.vectors as module

    found = module.search(vstore.db, embed_query(vstore.embedder, "claim"), vstore.theta, 10)
    assert old not in found
    assert new in found


def test_vector_search_respects_since(vstore):
    vstore.create_note("early note", "body")
    second, _ = vstore.create_note("late note", "body")
    EmbeddingWorker(vstore).drain()
    later = vstore.db.execute("SELECT created_at FROM note WHERE id = ?", (second,)).fetchone()[0]

    import seshat.vectors as module

    found = module.search(vstore.db, embed_query(vstore.embedder, "note"), vstore.theta, 10, later)
    assert found == [second]


def test_the_vector_side_finds_what_keywords_cannot(vstore, embedder):
    """The whole reason for running two retrievers: a note sharing no keyword
    with the query is unreachable by FTS alone."""
    synonym, _ = vstore.create_note("frequency warping", "the axis is compressed")
    EmbeddingWorker(vstore).drain()

    # Place the query right next to that note in the vector space, and nowhere
    # near any keyword it contains.
    target = embedder.table[DOCUMENT_PREFIX + document_text(
        "frequency warping", "the axis is compressed")] = [1.0] + [0.0] * (DIM - 1)
    vstore.clear_embeddings()
    EmbeddingWorker(vstore).drain()
    embedder.table[QUERY_PREFIX + "prewarping"] = target

    fts_only = vstore._fts_ranking("\"prewarping\"", None, 10)
    assert synonym not in fts_only, "no keyword overlap, so FTS cannot reach it"
    assert synonym in [h.id for h in vstore.context("prewarping")], "but the vector side can"


def test_agreement_scores_above_a_single_match(vstore, embedder):
    """A note both retrievers return outranks one only FTS returns."""
    both, _ = vstore.create_note("alpha beta", "alpha beta")
    fts_only, _ = vstore.create_note("alpha gamma", "alpha gamma")
    EmbeddingWorker(vstore).drain()
    embedder.table[QUERY_PREFIX + "alpha beta"] = embedder._vector(
        DOCUMENT_PREFIX + document_text("alpha beta", "alpha beta")
    )

    hits = {h.id: h.score for h in vstore.context("alpha beta")}
    assert hits[both] > hits[fts_only]


def test_vectors_are_unit_length_as_stored(vstore):
    vstore.create_note("a note", "body")
    EmbeddingWorker(vstore).drain()
    row = vstore.db.execute("SELECT embedding FROM note_vec").fetchone()
    import struct

    values = struct.unpack(f"<{DIM}f", row[0])
    assert math.isclose(l2_norm(values), 1.0, rel_tol=1e-5)
