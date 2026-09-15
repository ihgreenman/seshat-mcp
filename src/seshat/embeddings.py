"""Embedding backends. Spec §6, §6.3, §6.4.

The task prefixes live here and nowhere else, and a *backend cannot apply them
at all*. §6 is explicit that omitting them, or using the same one on both sides,
degrades retrieval silently -- nothing errors -- so the arrangement is
structural: an Embedder implements only `embed(texts)`, and the two module-level
functions below are the sole path that adds a prefix. There is no parameter to
get wrong and no second implementation to keep in step.

Spec 1.2 WITHDREW the claim that omitting them degrades retrieval "measurably":
two attempts failed to measure it, and the second -- 42 queries -- came out 3
wins to 2 losses paired, which is a coin flip. The prefixes are still applied,
because they are the documented usage and cost nothing, but no one should
expect a measurable difference on a corpus of this size.
"""

from __future__ import annotations

import json
import logging
import math
import struct
import urllib.error
import urllib.request
from typing import Protocol, Sequence

log = logging.getLogger("seshat.embeddings")

DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "
"""nomic's asymmetric task prefixes (§6). Ollama does not add them."""

DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_DIM = 768
DEFAULT_ENDPOINT = "http://localhost:11434"
QUERY_TIMEOUT = 10.0
"""Short: a cold model must not hang an interactive `context` call."""
DOCUMENT_TIMEOUT = 120.0
"""Long: the worker is off the write path and can afford a cold model load."""


class EmbedderUnavailable(Exception):
    """The backend could not be reached. Never fatal -- retrieval degrades."""


class Embedder(Protocol):
    """A backend embeds already-prefixed text. It never sees a task."""

    model: str
    dim: int

    def embed(self, texts: Sequence[str], timeout: float) -> list[list[float]]: ...


def embed_documents(embedder: Embedder, texts: Sequence[str]) -> list[list[float]]:
    """Embed notes for storage. `search_document:` goes on here, only here."""
    return embedder.embed([DOCUMENT_PREFIX + t for t in texts], DOCUMENT_TIMEOUT)


def embed_query(embedder: Embedder, text: str) -> list[float]:
    """Embed a query. `search_query:` goes on here, only here."""
    return embedder.embed([QUERY_PREFIX + text], QUERY_TIMEOUT)[0]


def pack(vector: Sequence[float]) -> bytes:
    """Little-endian float32, the layout vec0 stores."""
    return struct.pack(f"<{len(vector)}f", *vector)


def l2_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def document_text(desc: str, text: str) -> str:
    """What gets embedded: the whole note, markup stripped (§6, §6.4).

    Notes are short, so there is no chunking -- the main simplification
    relative to a document indexer.
    """
    from .markdown import strip_markup

    stripped = strip_markup(text)
    return f"{desc}\n\n{stripped}".strip() if stripped else desc


class OllamaEmbedder:
    """`nomic-embed-text` over Ollama's HTTP API (§6).

    Deliberately stdlib-only: no client library, so the dependency list stays
    `mcp` and `pydantic`.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        dim: int = DEFAULT_DIM,
        endpoint: str = DEFAULT_ENDPOINT,
    ):
        self.model = model
        self.dim = dim
        self.endpoint = endpoint.rstrip("/")

    def _post(self, inputs: list[str], timeout: float) -> list[list[float]]:
        payload = json.dumps({"model": self.model, "input": inputs}).encode()
        request = urllib.request.Request(
            f"{self.endpoint}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            raise EmbedderUnavailable(f"ollama HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:
            raise EmbedderUnavailable(f"{type(exc).__name__}: {exc}") from exc

        vectors = body.get("embeddings")
        if not vectors or len(vectors) != len(inputs):
            raise EmbedderUnavailable(f"ollama returned {len(vectors or [])} of {len(inputs)}")
        for vector in vectors:
            if len(vector) != self.dim:
                # Refuse rather than store garbage: a dimension change is a
                # model migration (§6.3), not something to paper over.
                raise EmbedderUnavailable(
                    f"model {self.model} returned dim {len(vector)}, expected {self.dim}"
                )
        return vectors

    def embed(self, texts: Sequence[str], timeout: float = DOCUMENT_TIMEOUT) -> list[list[float]]:
        return self._post(list(texts), timeout)
