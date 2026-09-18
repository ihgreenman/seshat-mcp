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
    # Tolerant of where the bold markers fall: 1.0 wrote "**Spec version:** 1.0",
    # 1.1 writes "**Spec version: 1.1.**". The version is the payload; the
    # markdown around it is not worth failing a build over.
    match = re.search(
        r"^\*\*Spec version:\*{0,2}\s*([0-9]+(?:\.[0-9]+)*)", SPEC.read_text(), re.M
    )
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


def test_the_extension_tracks_the_package_version():
    """The extension ships alongside the server and speaks its API, so the two
    versions track. Nothing enforced that -- they were hand-edited in separate
    files and had already drifted to 0.1.0 against 0.3.0, which is how the
    USER_AGENT came to claim 0.1 as well.

    Pinned rather than derived: a manifest cannot import Python, so the only
    options are a check here or a build step, and a check is cheaper.
    """
    import json
    from pathlib import Path

    from seshat import __version__

    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "extension" / "manifest.json").read_text()
    )
    assert manifest["version"] == __version__, (
        f"extension is {manifest['version']}, package is {__version__}"
    )


def test_the_project_url_is_stated_once_and_agrees_everywhere():
    """Three places name it -- the packaging metadata, the extension manifest,
    and the User-Agent every capture sends. A placeholder in the last of those
    is what the review found, so they are checked against each other."""
    import json
    from pathlib import Path

    from seshat.snapshots import PROJECT_URL

    root = Path(__file__).resolve().parent.parent
    assert PROJECT_URL in (root / "pyproject.toml").read_text()
    manifest = json.loads((root / "extension" / "manifest.json").read_text())
    assert manifest["homepage_url"] == PROJECT_URL


def test_the_readme_states_the_implemented_spec_revision():
    """The README is the landing page, so its version claim is the first thing
    a reader trusts -- and it had drifted to 1.3 while the code moved to 1.4.

    Checked against SPEC_VERSION rather than against the document, so it tracks
    what is BUILT rather than what is written: a spec revision landing before
    the code is normal, and the README should follow the code.
    """
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    stated = re.search(r"Built against spec ([0-9]+(?:\.[0-9]+)*)", readme)
    assert stated, "the README must say which spec revision this build implements"
    assert stated.group(1) == SPEC_VERSION, (
        f"README says spec {stated.group(1)}, code implements {SPEC_VERSION}"
    )
