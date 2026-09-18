"""End-to-end over the real stdio transport. Spec §3, §10 (local stdio only).

These tests run the server as a subprocess, which is the only way to catch the
failure mode that matters here: anything printed to stdout is a malformed
JSON-RPC frame, and it surfaces looking like an SDK bug rather than a stray
print in a debug path.
"""

import json
import sys

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def payload(result):
    """Tool results come back as content blocks; structured output rides along."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


async def session(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        # Both switches off, always. These tests write notes containing URLs,
        # and the default configuration would fetch them and call Ollama --
        # real outbound traffic from a unit test suite.
        args=["-m", "seshat.cli", "--db", str(tmp_path / "s.db"),
              "--no-snapshots", "--no-embeddings", "serve"],
    )
    return stdio_client(params)


async def test_tool_surface_is_exactly_six_tools(tmp_path):
    """§3 (spec 1.1): five data operations plus `help`. Every tool costs context
    on every turn, which is why the checker stays a CLI subcommand (§7) and why
    `help` had to justify itself (§3.6)."""
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
    assert {t.name for t in tools.tools} == {
        "note", "context", "read", "supersedes", "chain", "help"
    }


async def test_help_reports_capabilities_not_just_a_version(tmp_path):
    """§3.6: a server legitimately implements the full surface with no embedder,
    and a bare spec_version would overstate it."""
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool("note", {"desc": "a note", "text": "body"})
            info = payload(await client.call_tool("help", {}))

    assert info["spec_version"] and info["software_version"]
    assert info["store_version"] >= 2
    caps = info["capabilities"]
    assert caps["vector"] is False, "started with --no-embeddings -- say so"
    assert caps["embedding_model"] is None
    assert caps["embedding_backlog"] == 1, "the note is missing from embedding_meta"
    assert set(caps["snapshot_status"]) >= {"pending", "ok", "unreachable", "gone", "thin"}


async def test_read_surfaces_links_and_backlinks(tmp_path):
    """§3.3: backlinks are the higher-value direction, and undiscoverable by
    reading any of the notes involved."""
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            hub = payload(await client.call_tool("note", {
                "desc": "the anchor finding", "text": "see https://example.com/paper",
            }))["id"]
            for i in range(2):
                await client.call_tool("note", {
                    "desc": f"follow-up {i}", "text": f"builds on {hub}",
                })
            record = payload(await client.call_tool("read", {"id": hub}))

    assert [l["target"] for l in record["links"]] == ["https://example.com/paper"]
    assert record["backlinks_total"] == 2
    assert len(record["backlinks"]) == 2
    assert "sources" not in record, "preserved content is opt-in"


async def test_note_description_carries_the_desc_guidance(tmp_path):
    """§4: assistants drift toward summary-style descs unless the tool
    description says otherwise and shows an example pair."""
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = {t.name: t for t in (await client.list_tools()).tools}

    note_desc = tools["note"].description
    assert "good:" in note_desc and "bad:" in note_desc
    assert "retained" in note_desc and "OLD" in note_desc
    assert "superseded_by" in tools["read"].description, "read must warn about staleness"


async def test_round_trip_through_the_protocol(tmp_path):
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()

            first = payload(await client.call_tool("note", {
                "desc": "Bilinear transform loses precision near Nyquist",
                "text": "prewarping with tan(wT/2) fixes it",
            }))
            note_id = first["id"]
            assert len(note_id.split("-")) == 4

            second = payload(await client.call_tool("note", {
                "desc": "Prewarping is only exact at one frequency",
                "text": "the match is exact at the chosen wc and nowhere else",
                "supersedes": [{"id": note_id, "retained": 1.0, "why": "adds a caveat"}],
            }))

            found = payload(await client.call_tool("context", {"text": "nyquist"}))
            assert found["results"][0]["id"] == note_id
            assert found["results"][0]["score"] > 0

            record = payload(await client.call_tool("read", {"id": note_id}))
            assert record["heads"] == [second["id"]]
            assert record["superseded_by_total"] == 1

            graph = payload(await client.call_tool("chain", {"id": second["id"]}))
            assert {n["id"] for n in graph["nodes"]} == {note_id, second["id"]}

            again = payload(await client.call_tool("supersedes", {
                "old_id": note_id, "new_id": second["id"],
                "retained": 0.4, "why": "on reflection the caveat undercuts the original",
            }))
            assert again["reassessment"] is True
            assert again["previous_retained"] == 1.0

            after = payload(await client.call_tool("context", {"text": "nyquist"}))
            assert note_id not in {r["id"] for r in after["results"]}


async def test_empty_context_orients_at_session_start(tmp_path):
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            for i in range(3):
                await client.call_tool("note", {"desc": f"finding {i}", "text": "body"})
            listing = payload(await client.call_tool("context", {}))
    assert listing["count"] == 3
    assert listing["results"][0]["desc"] == "finding 2", "most recent first"


async def test_errors_come_back_as_tool_errors_not_crashes(tmp_path):
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            await client.call_tool("note", {"desc": "a real note", "text": "body"})
            result = await client.call_tool("read", {"id": "zoo-zoo-zoo-zoo"})
    assert result.is_error
    assert "context()" in result.content[0].text, "point the caller at the fallback (§6.1)"


async def test_a_misnamed_edge_key_fails_rather_than_dropping_the_rationale(tmp_path):
    """The reported defect: `rationale` -- the column name in §5.5's schema --
    passed where the argument is `why` was accepted and silently discarded. The
    edge was written with a null rationale and the caller was told it succeeded,
    which is a priority-2 violation: believing something was recorded when it
    was not. Found only because the caller happened to re-read its own work.
    """
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            first = payload(await client.call_tool(
                "note", {"desc": "original claim", "text": "tau equals p"}
            ))
            result = await client.call_tool("note", {
                "desc": "corrected claim",
                "text": "tau is about p minus a half",
                "supersedes": [
                    {"id": first["id"], "retained": 0.4, "rationale": "sign error"}
                ],
            })
            listing = payload(await client.call_tool("context", {"limit": 50}))

    assert result.is_error, "an unreadable key must not report success"
    message = result.content[0].text
    assert "rationale" in message and "why" in message, "name the confusion: " + message
    assert listing["count"] == 1, "the whole write is refused, not half-applied"


async def test_a_well_formed_edge_still_records_its_rationale(tmp_path):
    """The other half: the rejection above must not be over-eager. Read back
    through `chain`, the way the defect was originally caught."""
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            first = payload(await client.call_tool(
                "note", {"desc": "original claim", "text": "tau equals p"}
            ))
            second = payload(await client.call_tool("note", {
                "desc": "corrected claim",
                "text": "tau is about p minus a half",
                "supersedes": [{"id": first["id"], "retained": 0.4, "why": "sign error"}],
            }))
            chain = payload(await client.call_tool("chain", {"id": second["id"]}))

    rationales = [e.get("rationale") for e in chain["edges"]]
    assert "sign error" in rationales, chain["edges"]


async def test_the_declared_schema_and_the_accepted_keys_agree(tmp_path):
    """Two statements of one rule, from opposite ends: the JSON schema the
    client validates against and the constant the store enforces. Derived
    independently, compared here -- a key added to one and not the other is
    exactly how the silent-drop bug gets reintroduced.
    """
    from seshat.store import EDGE_KEYS

    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = {t.name: t for t in (await client.list_tools()).tools}

    schema = tools["note"].input_schema["properties"]["supersedes"]
    assert "items" not in schema, "the rule must be stated once, not beside the union"
    arrays = [b for b in schema["anyOf"] if b["type"] == "array"]
    assert len(arrays) == 1, "a second, laxer array branch would make the strict one moot"
    items = arrays[0]["items"]
    assert items["additionalProperties"] is False
    assert set(items["properties"]) == set(EDGE_KEYS)
    assert set(items["required"]) == {"id", "retained"}


async def test_concurrent_calls_share_one_connection_safely(tmp_path):
    """Sync tool bodies run in a worker thread pool, so the sqlite connection is
    touched from threads other than the one that opened it.

    This pins `check_same_thread=False`, not the store lock -- sqlite3 is in
    serialized mode (threadsafety 3), so statements alone do not collide. The
    lock buys operation-level atomicity, which test_concurrency.py covers.
    """
    import anyio

    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            results = []

            async def write_one(i):
                results.append(payload(
                    await client.call_tool("note", {"desc": f"finding {i}", "text": "body"})
                ))

            async with anyio.create_task_group() as tg:
                for i in range(20):
                    tg.start_soon(write_one, i)

            listing = payload(await client.call_tool("context", {"limit": 50}))

    assert len({r["id"] for r in results}) == 20, "no lost or duplicated ids"
    assert listing["count"] == 20


def test_stdout_carries_only_json_rpc(tmp_path):
    """The failure this guards: a stray print lands mid-frame and the session
    dies looking like an SDK problem. Checked on the raw pipe, not through a
    client that would have already choked on it."""
    import subprocess

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    })
    proc = subprocess.run(
        [sys.executable, "-m", "seshat.cli", "--db", str(tmp_path / "s.db"),
         "--no-snapshots", "--no-embeddings", "serve"],
        input=request + "\n", capture_output=True, text=True, timeout=30,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, "server produced no response at all"
    for line in lines:
        message = json.loads(line)  # raises if anything else reached stdout
        assert message["jsonrpc"] == "2.0"
    assert "seshat store:" in proc.stderr, "logging must go to stderr"


# ------------------------------------------- §5.6 rationale prefix convention


def test_both_tool_descriptions_document_the_prefix_set():
    """§5.6: "Document the set in `note`'s and `supersedes`' tool descriptions,
    where a writer will see it; a convention documented only here will not be
    followed." Both, because either tool can write a rationale."""
    from seshat.server import NOTE_DESCRIPTION, SUPERSEDES_DESCRIPTION
    from seshat.store import RATIONALE_PREFIXES

    for description in (NOTE_DESCRIPTION, SUPERSEDES_DESCRIPTION):
        for prefix in RATIONALE_PREFIXES:
            assert prefix in description
        assert "closed" in description or "Do not extend" in description


def test_the_prefix_set_matches_the_spec():
    """The set is closed, so the code and the document must agree on what is in
    it. Read out of the spec's own table rather than restated here -- a copy in
    the test would drift with the copy in the code and agree with it anyway.
    """
    import re
    from pathlib import Path

    from seshat.store import RATIONALE_PREFIXES

    spec = (Path(__file__).resolve().parent.parent / "docs" / "seshat-mcp-spec.md").read_text()
    section = spec[spec.index("### 5.6"):]
    section = section[: section.index("\n## ")]
    # The table rows look like: | `scope:` | the old note is correct ... |
    in_spec = tuple(re.findall(r"^\|\s*`([a-z]+:)`\s*\|", section, re.M))
    assert in_spec, "no prefix table found in §5.6"
    assert set(in_spec) == set(RATIONALE_PREFIXES), (
        f"spec lists {sorted(in_spec)}, code declares {sorted(RATIONALE_PREFIXES)}"
    )
