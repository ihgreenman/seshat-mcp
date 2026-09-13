"""Guard against spec/implementation version skew.

Tooling citing one version of a reference while the document has moved several
ahead is a known, expensive failure mode. The point of these tests is that the
skew is *visible*, not that it is zero: a spec revision landing before the code
catches up is normal, and must not turn the suite red. Code claiming to
implement a version the document has never reached is not normal.
"""

import re
from pathlib import Path

import pytest

from seshat import SPEC_VERSION

SPEC = Path(__file__).resolve().parent.parent / "docs" / "seshat-mcp-spec.md"


def parse(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


@pytest.fixture(scope="module")
def doc_version() -> str:
    match = re.search(r"^\*\*Spec version:\*\*\s*([0-9]+(?:\.[0-9]+)*)", SPEC.read_text(), re.M)
    assert match, "the spec must declare its version in the header"
    return match.group(1)


def test_implementation_never_claims_a_future_spec(doc_version):
    assert parse(SPEC_VERSION) <= parse(doc_version), (
        f"code claims spec {SPEC_VERSION} but the document only reaches {doc_version}"
    )


def test_skew_is_reported_not_hidden(doc_version):
    """Informational: a lagging implementation is a fact to surface, not a failure."""
    if parse(SPEC_VERSION) < parse(doc_version):
        pytest.skip(
            f"implementation is at spec {SPEC_VERSION}, document is at {doc_version} "
            f"-- features from the newer revision are not built yet"
        )
    assert SPEC_VERSION == doc_version
