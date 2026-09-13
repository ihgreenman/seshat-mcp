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
        args=["-m", "seshat.cli", "--db", str(tmp_path / "s.db"), "serve"],
    )
    return stdio_client(params)


async def test_tool_surface_is_exactly_five_tools(tmp_path):
    """§3: 'Five tools. This is the whole interface.' Every tool costs context
    on every turn, which is why the checker is a CLI subcommand (§7)."""
    async with await session(tmp_path) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
    assert {t.name for t in tools.tools} == {"note", "context", "read", "supersedes", "chain"}


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
        [sys.executable, "-m", "seshat.cli", "--db", str(tmp_path / "s.db"), "serve"],
        input=request + "\n", capture_output=True, text=True, timeout=30,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, "server produced no response at all"
    for line in lines:
        message = json.loads(line)  # raises if anything else reached stdout
        assert message["jsonrpc"] == "2.0"
    assert "seshat store:" in proc.stderr, "logging must go to stderr"
