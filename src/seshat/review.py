"""The local review interface. Spec §10.

**Reading the store is the primary function** (§10.1). Priority 3 holds that the
store must stay small enough to be read in full, and §8 concedes that periodic
full reading is the only growth mitigation with a mechanism behind it. Nobody
does that against a SQLite CLI. Failure triage (§10.2) is secondary -- it is not
the reason this exists.

Three structural decisions, each enforced rather than intended:

* **The notes database is opened read-only.** §10.3 requires that the UI never
  permit editing a note, and warns the absence will feel like an oversight to
  someone reasonable. A connection that physically cannot write is not an
  affordance anyone can relax by accident. Snapshot repair goes through a
  separate, writable connection to a separate file.
* **Loopback only, with no override** (§10, §12). The reader has no
  authentication of any kind -- only the extension API carries a token -- so the
  bind address is the entire access-control story. `--host` chooses among
  loopback addresses and nothing more: a non-loopback one is refused before a
  socket exists, and no flag or environment variable relaxes that. Off-box
  access belongs to a reverse proxy, which can authenticate and terminate TLS;
  this interface cannot, and giving it a switch that pretends otherwise would
  put a personal note store one typo away from an open one.
* **Origin-checked, token-guarded POSTs.** The moment a browser can reach an
  interface, "localhost is safe" stops being true: any page you visit can post
  to it. §10.2 names this for the extension, and it applies the instant a POST
  endpoint exists -- which is now.

Stdlib only, server-rendered, no JavaScript and no external assets.
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import secrets
import socket
import threading
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import SPEC_VERSION, __version__
from .checker import Checker, reading_guide
from .embeddings import DEFAULT_MODEL, OllamaEmbedder
from .snapshots import SnapshotStore, SnapshotWorker, snapshot_path_for
from .store import Store
from .worker import EmbeddingWorker

log = logging.getLogger("seshat.review")

STYLE = """
:root { --bg:#fbfbfa; --fg:#1b1b1a; --dim:#6b6b66; --line:#dcdcd6;
        --accent:#7a4b2a; --warn:#8a5a00; --bad:#8a2222; --card:#fff; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16161a; --fg:#e6e6e2; --dim:#9a9a92; --line:#2e2e34;
          --accent:#d0a070; --warn:#d8a13c; --bad:#e08a7a; --card:#1e1e24; }
}
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.6 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif; }
header { border-bottom:1px solid var(--line); padding:14px 24px; display:flex;
  gap:20px; align-items:baseline; flex-wrap:wrap; position:sticky; top:0;
  background:var(--bg); z-index:5 }
header b { font-size:17px; letter-spacing:.02em }
a { color:var(--accent); text-decoration:none } a:hover { text-decoration:underline }
main { max-width:900px; margin:0 auto; padding:24px }
h1 { font-size:21px; margin:0 0 4px } h2 { font-size:15px; margin:28px 0 10px;
  text-transform:uppercase; letter-spacing:.08em; color:var(--dim) }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; margin:10px 0 }
.dim { color:var(--dim) } .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:13px } .bad { color:var(--bad) } .warn { color:var(--warn) }
.pill { display:inline-block; border:1px solid var(--line); border-radius:999px;
  padding:1px 9px; font-size:12px; color:var(--dim); margin-right:6px }
.grid { display:flex; gap:10px; flex-wrap:wrap }
.stat { border:1px solid var(--line); border-radius:8px; padding:10px 14px;
  min-width:104px; background:var(--card) }
.stat b { display:block; font-size:22px; font-weight:600 }
pre { white-space:pre-wrap; word-wrap:break-word; background:var(--card);
  border:1px solid var(--line); border-radius:8px; padding:14px; overflow-x:auto }
table { border-collapse:collapse; width:100% } td,th { text-align:left;
  padding:6px 10px 6px 0; border-bottom:1px solid var(--line); vertical-align:top }
th { color:var(--dim); font-weight:500; font-size:13px }
textarea { width:100%; min-height:150px; font-family:ui-monospace,monospace;
  font-size:13px; padding:10px; border:1px solid var(--line); border-radius:6px;
  background:var(--bg); color:var(--fg) }
button { font:inherit; padding:6px 14px; border:1px solid var(--line);
  border-radius:6px; background:var(--card); color:var(--fg); cursor:pointer }
button:hover { border-color:var(--accent) }
form.inline { display:inline } input[type=search] { font:inherit; padding:6px 10px;
  border:1px solid var(--line); border-radius:6px; background:var(--card);
  color:var(--fg); min-width:260px }
.note { border-left:3px solid var(--line); padding-left:12px; margin:14px 0 }
"""

E = html.escape


def _h(value) -> str:
    return E(str(value)) if value is not None else ""


def token_path(db_path: Path) -> Path:
    return Path(str(db_path) + ".token")


def load_token(db_path: Path) -> str:
    """The shared secret between this interface and the browser extension.

    Persisted, unlike the per-process form token: an extension that had to be
    re-paired every restart would be re-paired carelessly. Written 0600 -- it is
    the only thing standing between any page you visit and your note store.
    """
    path = token_path(db_path)
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token


class Review:
    """Everything the handler needs, built once and shared across requests."""

    def __init__(
        self,
        db_path: Path,
        snapshots: bool = True,
        capture_api: bool = True,
        embedder=None,
    ):
        self.db_path = Path(db_path)
        # The embedder is not optional for quality: without it this interface
        # searches by keyword only, which is a quietly worse answer rather than
        # a visible failure -- the exact thing §3.6 exists to prevent.
        self.store = Store(self.db_path, read_only=True, embedder=embedder)
        self.snapshots = SnapshotStore(snapshot_path_for(self.db_path)) if snapshots else None
        # Regenerated per process: a form from a previous run is not a form.
        self.token = secrets.token_urlsafe(24)
        # Persistent, for the extension, which cannot re-pair on every restart.
        self.api_token = load_token(self.db_path) if capture_api else None

        # §10.3 forbids EDITING a note; §6.10 makes the extension "a second
        # front door to note()", which is creation. So the UI keeps its
        # read-only connection and creation gets its own, used by exactly one
        # endpoint. No code path anywhere can update an existing note.
        self.writer: Store | None = None
        if capture_api:
            self.writer = Store(self.db_path, snapshots=self.snapshots, embedder=embedder)

    def close(self) -> None:
        self.store.close()
        if self.writer is not None:
            self.writer.close()
        if self.snapshots is not None:
            self.snapshots.close()

    # ------------------------------------------------------------- queries

    def counts(self) -> dict:
        db = self.store.db
        return {
            "notes": db.execute("SELECT COUNT(*) FROM note").fetchone()[0],
            "heads": db.execute(
                "SELECT COUNT(*) FROM note WHERE id NOT IN (SELECT old_id FROM supersession)"
            ).fetchone()[0],
            "edges": db.execute("SELECT COUNT(*) FROM supersession").fetchone()[0],
            "links": db.execute("SELECT COUNT(*) FROM link").fetchone()[0],
        }

    def notes(self, mode: str = "all", limit: int = 200) -> list:
        db = self.store.db
        if mode == "heads":
            sql = """SELECT n.id, n.desc, n.created_at FROM note n
                     WHERE n.id NOT IN (SELECT old_id FROM supersession)
                     ORDER BY n.created_at DESC LIMIT ?"""
        elif mode == "open":
            # §8's pruning handle: old and un-superseded. The spec says it is
            # expressible with context("", since=...) plus head filtering; this
            # is that query, made clickable.
            sql = """SELECT n.id, n.desc, n.created_at FROM note n
                     WHERE n.id NOT IN (SELECT old_id FROM supersession)
                     ORDER BY n.created_at ASC LIMIT ?"""
        elif mode == "cited":
            # §8: inbound reference count is the only retroactive salience
            # measure the design has, and it is already in the link table.
            sql = """SELECT n.id, n.desc, n.created_at FROM note n
                     JOIN link l ON l.target = n.id AND l.kind = 'note'
                     GROUP BY n.id ORDER BY COUNT(*) DESC, n.created_at DESC LIMIT ?"""
        else:
            sql = "SELECT id, desc, created_at FROM note ORDER BY created_at DESC LIMIT ?"
        return db.execute(sql, (limit,)).fetchall()

    def sources_for(self, note_id: str) -> list:
        return self.snapshots.sources_for(note_id) if self.snapshots else []


# ----------------------------------------------------------------- rendering


def page(title: str, body: str, review: Review) -> bytes:
    histogram = review.snapshots.histogram() if review.snapshots else {}
    pending = histogram.get("thin", 0) + histogram.get("unreachable", 0)
    badge = f' <span class="pill bad">{pending} to triage</span>' if pending else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(title)} · seshat</title><style>{STYLE}</style></head><body>
<header><b><a href="/">seshat</a></b>
  <a href="/notes">notes</a><a href="/notes?mode=heads">heads</a>
  <a href="/notes?mode=open">open loops</a><a href="/notes?mode=cited">most cited</a>
  <a href="/triage">triage{badge}</a><a href="/check">check</a>
  <form class="inline" action="/search" method="get">
    <input type="search" name="q" placeholder="search notes" aria-label="search">
  </form>
</header><main>{body}</main></body></html>""".encode()


def note_link(note_id: str, desc: str) -> str:
    return f'<a href="/note/{E(note_id)}"><span class="mono">{E(note_id)}</span></a> {E(desc)}'


def render_home(review: Review) -> str:
    counts = review.counts()
    histogram = review.snapshots.histogram() if review.snapshots else {}
    stats = "".join(
        f'<div class="stat"><b>{v}</b><span class="dim">{k}</span></div>'
        for k, v in counts.items()
    )
    snaps = "".join(
        f'<div class="stat"><b>{v}</b><span class="dim">{k}</span></div>'
        for k, v in histogram.items()
    )
    recent = "".join(
        f'<div class="note">{note_link(r["id"], r["desc"])}'
        f'<div class="dim mono">{r["created_at"][:16].replace("T", " ")}</div></div>'
        for r in review.notes(limit=8)
    )
    return f"""<h1>seshat</h1>
<p class="dim">Read the store. Everything here is read-only except snapshot repair.</p>
<h2>store</h2><div class="grid">{stats}</div>
{f'<h2>snapshots</h2><div class="grid">{snaps}</div>' if snaps else ''}
<h2>recent</h2>{recent}
<h2>reading the store</h2>
<div class="card"><p>§8: the only growth mitigation with a mechanism behind it is
keeping the store small enough to read in full, periodically. Useful entry points:</p>
<ul>
<li><a href="/notes?mode=open">Open loops</a> — oldest notes nothing has superseded.</li>
<li><a href="/notes?mode=cited">Most cited</a> — inbound references, the one
retroactive salience measure the design has.</li>
<li><a href="/check">Consistency report</a> — its output doubles as a reading guide.</li>
</ul></div>"""


def render_notes(review: Review, mode: str) -> str:
    labels = {
        "all": ("All notes", "Newest first."),
        "heads": ("Heads", "Notes nothing has superseded — the current state of belief."),
        "open": ("Open loops", "Oldest un-superseded notes. §8's pruning handle: an open "
                 "loop is a note nothing has superseded, and age is the other half."),
        "cited": ("Most cited", "Ranked by inbound references from other notes' text. "
                  "It accrues from what was actually used later, which is the property "
                  "§8 says retroactive salience needs. A note may matter enormously and "
                  "be cited by nothing."),
    }
    title, blurb = labels.get(mode, labels["all"])
    rows = "".join(
        f'<div class="note">{note_link(r["id"], r["desc"])}'
        f'<div class="dim mono">{r["created_at"][:16].replace("T", " ")}</div></div>'
        for r in review.notes(mode)
    )
    return f'<h1>{E(title)}</h1><p class="dim">{E(blurb)}</p>{rows or "<p class=dim>Nothing yet.</p>"}'


def render_note(review: Review, note_id: str) -> str:
    record = review.store.read(note_id, edge_limit=50, with_sources=bool(review.snapshots))
    data = asdict(record)

    def edges(items, label):
        if not items:
            return ""
        rows = "".join(
            f"<tr><td class=mono>{r['retained']:.2f}</td><td>{note_link(r['id'], '')}</td>"
            f"<td class=dim>{_h(r['rationale'] or '')}</td></tr>"
            for r in items
        )
        return f"<h2>{label}</h2><table>{rows}</table>"

    heads = ", ".join(note_link(h, "") for h in data["heads"])
    stale = ""
    if data["superseded_by"]:
        stale = ('<div class="card"><b class="warn">This note has been superseded.</b> '
                 f'Current head(s): {heads}</div>')

    links = "".join(
        f'<tr><td class=pill>{_h(l["kind"])}</td><td class=mono>{_h(l["target"])}</td>'
        f'<td class=dim>{_h(l["label"] or "")}</td></tr>'
        for l in data["links"]
    )
    backlinks = "".join(f'<div class="note">{note_link(b["id"], b["desc"])}</div>'
                        for b in data["backlinks"])

    sources = ""
    for s in data.get("sources") or []:
        met = s.get("expectation_met")
        flag = ("" if met is None else
                ' <span class="pill">expectation met</span>' if met else
                ' <span class="pill bad">expectation not met</span>')
        status = s["status"]
        colour = "bad" if status in ("thin", "gone", "unreachable") else "dim"
        body = (s.get("text") or "")[:3000]
        sources += (
            f'<div class="card"><div class="mono">{_h(s["target"])}</div>'
            f'<div><span class="pill {colour}">{_h(status)}</span>'
            f'<span class="pill">{_h(s.get("provenance") or "unread")}</span>{flag}</div>'
            f'<div class="dim">expected: {_h(s.get("expectation") or "—")} '
            f'({_h(s.get("expectation_source") or "—")})</div>'
            + (f'<div class="dim mono">redirected to {_h(s["final_url"])}</div>'
               if s.get("final_url") else "")
            + (f"<pre>{E(body)}</pre>" if body else
               '<p class="dim">No content extracted.</p>')
            + '<a href="/triage">repair in triage</a></div>'
        )

    return f"""<h1>{E(record.desc)}</h1>
<p class="mono dim">{E(record.id)} · {E(record.created_at[:16].replace("T", " "))}</p>
{stale}
<pre>{E(record.text)}</pre>
<div class="card dim">Notes are immutable (§2). To correct this, write a new note
that supersedes it — through the <span class=mono>note</span> tool with
<span class=mono>supersedes</span>. There is deliberately no edit button:
adding one would destroy the append-only guarantee the whole audit trail rests on.</div>
{edges(data["supersedes"], f"supersedes ({data['supersedes_total']})")}
{edges(data["superseded_by"], f"superseded by ({data['superseded_by_total']})")}
{f'<h2>links</h2><table>{links}</table>' if links else ''}
{f'<h2>backlinks ({data["backlinks_total"]})</h2>{backlinks}' if backlinks else ''}
{f'<h2>sources</h2>{sources}' if sources else ''}
<p><a href="/chain/{E(record.id)}">view chain</a></p>"""


def render_chain(review: Review, note_id: str) -> str:
    graph = review.store.chain(note_id)
    nodes = {n["id"]: n["desc"] for n in graph["nodes"]}
    rows = "".join(
        f"<tr><td>{note_link(e['old_id'], nodes.get(e['old_id'], ''))}</td>"
        f"<td class=mono>→ {e['retained']:.2f} →</td>"
        f"<td>{note_link(e['new_id'], nodes.get(e['new_id'], ''))}</td>"
        f"<td class=dim>{_h(e['rationale'] or '')}</td></tr>"
        for e in graph["edges"]
    )
    return f"""<h1>Chain</h1>
<p class="dim">Ancestors and descendants of <span class=mono>{E(note_id)}</span>.
Scores are per-edge and are <b>not</b> aggregated along paths: two hops at 0.5
bound the surviving fraction only to [0, 0.5], and any single number would be
false precision.</p>
<table>{rows or '<tr><td class=dim>No supersession edges.</td></tr>'}</table>"""


def render_triage(review: Review) -> str:
    if review.snapshots is None:
        return "<h1>Triage</h1><p class=dim>Snapshot capture is disabled.</p>"
    failures = review.snapshots.failures()
    unmet = review.snapshots.unmet_expectations()
    descs = dict(review.store.db.execute("SELECT id, desc FROM note"))
    token = review.token

    def card(row, kind):
        note_id, target = row["note_id"], row["target"]
        status = row.get("status", "ok")
        why = {
            "thin": "Extraction yielded implausibly little — a paywall, a JS shell, or a "
                    "cookie wall. <b>Time-sensitive:</b> repairable only while the page is live.",
            "unreachable": "The fetch failed in a way that might succeed later.",
            "gone": "The target was already dead at capture. That is information, not a "
                    "defect — the note remains valid, it simply cites a source nobody can check.",
        }.get(status, "The capture succeeded but does not contain what the citation sought. "
                      "A candidate for review, not a verdict: a statistical table may contain "
                      "none of the expected words and still be perfect.")
        actions = (
            f'<form class="inline" method="post" action="/retry">'
            f'<input type=hidden name=token value="{E(token)}">'
            f'<input type=hidden name=note_id value="{E(note_id)}">'
            f'<input type=hidden name=target value="{E(target)}">'
            f"<button>retry</button></form> "
            if status == "unreachable" else ""
        )
        actions += (
            f'<form class="inline" method="post" action="/acknowledge">'
            f'<input type=hidden name=token value="{E(token)}">'
            f'<input type=hidden name=note_id value="{E(note_id)}">'
            f'<input type=hidden name=target value="{E(target)}">'
            f"<button>acknowledge unpreservable</button></form>"
        )
        return f"""<div class="card">
<div><span class="pill bad">{E(status if kind == 'failure' else 'expectation')}</span>
<span class="mono">{E(target)}</span></div>
<div class="dim">{why}</div>
<div>{note_link(note_id, descs.get(note_id, ''))}</div>
<div class="dim">expected: {_h(row.get("expectation") or "—")}</div>
<details><summary>paste content</summary>
<form method="post" action="/paste">
  <input type=hidden name=token value="{E(token)}">
  <input type=hidden name=note_id value="{E(note_id)}">
  <input type=hidden name=target value="{E(target)}">
  <p class="dim">Open the page, select all, paste here. seshat never handles a
  credential — you are already authenticated and already looking at it. Recorded
  with <span class=mono>extraction=manual</span>, because a human-mediated
  witness is still a witness and the distinction has to survive in the data.
  The bytes the fetcher received are kept either way.</p>
  <textarea name=text placeholder="paste the page text"></textarea>
  <p><button>save as manual capture</button></p>
</form></details>
<div>{actions}</div></div>"""

    body = "".join(card(r, "failure") for r in failures)
    body += "".join(card(r, "expectation") for r in unmet)
    return f"""<h1>Triage</h1>
<p class="dim">Failures needing a human, worst first. Pasting replaces the
<em>reading</em> of a capture, never the captured bytes: those are the witness
and stay exactly as fetched (§10.3). A reading you paste is never overwritten
afterwards — it cannot be regenerated from anything.</p>
{body or '<p class="dim">Nothing to triage.</p>'}"""


def render_check(review: Review) -> str:
    checker = Checker(
        str(review.db_path),
        snapshot_path_for(review.db_path) if review.snapshots else None,
        theta=review.store.theta,
    )
    try:
        findings = checker.run(semantic=True)
    finally:
        checker.close()
    descs = dict(review.store.db.execute("SELECT id, desc FROM note"))
    rows = "".join(
        f'<tr><td><span class="pill {"bad" if f.severity == "error" else ""}">'
        f"{E(f.severity)}</span></td><td>{E(f.check)}</td><td>{E(f.message)}</td></tr>"
        for f in findings
    )
    guide = "".join(
        f'<div class="note">{note_link(nid, desc)} <span class="dim">({score})</span></div>'
        for nid, desc, score in reading_guide(findings, descs)
    )
    return f"""<h1>Consistency</h1>
<p class="dim">Never mutates, never auto-fixes. A clean report is not proof of
consistency: §7.1 validates structure, not truth, and §7.2 only generates candidates.</p>
<table>{rows or '<tr><td class=dim>No findings.</td></tr>'}</table>
{f'<h2>where to start reading</h2>{guide}' if guide else ''}"""


def render_search(review: Review, query: str) -> str:
    hits = review.store.context(query, limit=40)
    degraded = ""
    if hits and not any(h.matched and "vector" in h.matched for h in hits):
        degraded = ('<div class="card warn">Keyword results only — the vector side '
                    'did not answer. Either nothing is embedded yet, or the embedder '
                    'is unavailable. These results are worse than usual, not better.</div>')
    rows = "".join(
        f'<div class="note">{note_link(h.id, h.desc)}'
        f'<div class="dim mono">'
        + (f"cos {h.vector_similarity:+.3f} · " if h.vector_similarity is not None else "")
        + (f"{','.join(h.matched)}" if h.matched else "recency")
        + "</div></div>"
        for h in hits
    )
    caveat = ""
    if hits and all((h.vector_similarity or 0) < 0.6 for h in hits if h.vector_similarity):
        caveat = ('<div class="card warn">Every result is weakly matched. The store '
                  'may simply hold nothing on this.</div>')
    return f"""<h1>Search</h1><p class="dim">{E(query)}</p>{degraded}{caveat}
{rows or '<p class=dim>No results.</p>'}"""


# ------------------------------------------------------------------ serving


class Handler(BaseHTTPRequestHandler):
    review: Review = None  # type: ignore[assignment]
    server_version = "seshat"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: A003
        log.debug(fmt, *args)

    def _send(self, body: bytes, status: int = 200, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here should ever be embedded, framed, or fetched cross-origin.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --------------------------------------------------------- extension API

    def _api_authorised(self) -> bool:
        """Token in the Authorization header, and an origin we recognise.

        The token is the real boundary -- it is a 0600 file on disk. Origin
        checking is the second half §10.2 asks for: it costs nothing and it
        stops an ordinary web page from reaching the endpoint at all, leaving
        only installed extensions and local tools as possible callers.
        """
        review = self.review
        if review.api_token is None:
            return False
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(supplied, review.api_token):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = self.headers.get("Host", "")
        return (
            origin.startswith(("chrome-extension://", "moz-extension://", "safari-web-extension://"))
            or origin in (f"http://{host}", f"https://{host}")
        )

    def _cors(self, origin: str | None):
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Vary", "Origin")

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors(self.headers.get("Origin"))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors(self.headers.get("Origin"))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api(self, action: str, payload: dict) -> tuple[dict, int]:
        review = self.review
        if action == "ping":
            return {
                "ok": True,
                "spec_version": SPEC_VERSION,
                "software_version": __version__,
                "capture": review.writer is not None,
                "snapshots": review.snapshots is not None,
            }, 200

        if action == "capture":
            # §6.10: this writes A NOTE with the page attached as evidence --
            # not an unattached capture awaiting a note. Notes stay primary.
            desc = (payload.get("desc") or "").strip()
            url = (payload.get("url") or "").strip()
            if not desc or not url:
                return {"error": "desc and url are required"}, 400
            if review.writer is None:
                return {"error": "capture API disabled"}, 400

            detail = (payload.get("detail") or "").strip()
            body = f"{detail}\n\n" if detail else ""
            # The citation carries the desc as anchor text, which is exactly
            # §6.9's expectation: what was sought here, stated in the act of
            # citing. The capture then satisfies or fails it immediately.
            body += f"Source: [{desc}]({url})"
            note_id, _ = review.writer.create_note(desc, body)

            if review.snapshots is not None:
                review.snapshots.record_browser(
                    note_id,
                    url,
                    payload.get("text") or "",
                    (payload.get("html") or "").encode("utf-8") or None,
                    payload.get("title"),
                    expectation=desc,
                )
                sources = [
                    s for s in review.snapshots.sources_for(note_id) if s["target"] == url
                ]
                status = sources[0]["status"] if sources else "none"
            else:
                status = "none"
            return {"id": note_id, "snapshot_status": status}, 200

        if action == "repair":
            if review.snapshots is None:
                return {"error": "snapshots disabled"}, 400
            note_id = (payload.get("note_id") or "").strip()
            target = (payload.get("target") or "").strip()
            text = payload.get("text") or ""
            if not (note_id and target and text.strip()):
                return {"error": "note_id, target and text are required"}, 400
            saved = review.snapshots.record_browser(
                note_id, target, text,
                (payload.get("html") or "").encode("utf-8") or None,
                payload.get("title"),
            )
            return {"ok": saved}, 200

        if action == "targets":
            # Lets the extension say "this page is in your triage queue" while
            # you happen to be looking at it -- the cheapest possible repair.
            if review.snapshots is None:
                return {"targets": []}, 200
            rows = review.snapshots.failures() + review.snapshots.unmet_expectations()
            return {"targets": [
                {"note_id": r["note_id"], "target": r["target"],
                 "status": r.get("status", "expectation")}
                for r in rows
            ]}, 200

        return {"error": "unknown action"}, 404

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        params = urllib.parse.parse_qs(parsed.query)
        r = self.review
        if parts and parts[0] == "api":
            if not self._api_authorised():
                return self._json({"error": "unauthorised"}, 401)
            payload, status = self._api(parts[1] if len(parts) > 1 else "", {})
            return self._json(payload, status)
        try:
            if not parts:
                return self._send(page("Home", render_home(r), r))
            if parts[0] == "notes":
                mode = params.get("mode", ["all"])[0]
                return self._send(page("Notes", render_notes(r, mode), r))
            if parts[0] == "note" and len(parts) > 1:
                return self._send(page("Note", render_note(r, parts[1]), r))
            if parts[0] == "chain" and len(parts) > 1:
                return self._send(page("Chain", render_chain(r, parts[1]), r))
            if parts[0] == "triage":
                return self._send(page("Triage", render_triage(r), r))
            if parts[0] == "check":
                return self._send(page("Consistency", render_check(r), r))
            if parts[0] == "search":
                return self._send(page("Search", render_search(r, params.get("q", [""])[0]), r))
        except Exception as exc:
            log.exception("review request failed")
            return self._send(page("Error", f"<h1>Error</h1><pre>{E(str(exc))}</pre>", r), 500)
        self._send(page("Not found", "<h1>404</h1>", r), 404)

    def _origin_ok(self) -> bool:
        """Reject cross-site posts.

        "localhost is safe" stops being true the moment a browser is a client:
        any page you visit can post to 127.0.0.1. Cheap to check, and the
        alternative is an interface that edits your store on a stranger's say-so.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # curl and friends; the token is the other half
        host = self.headers.get("Host", "")
        return origin in (f"http://{host}", f"https://{host}")

    def do_POST(self):  # noqa: N802
        r = self.review
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        if parts and parts[0] == "api":
            if not self._api_authorised():
                return self._json({"error": "unauthorised"}, 401)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "invalid json"}, 400)
            try:
                result, status = self._api(parts[1] if len(parts) > 1 else "", payload)
            except Exception as exc:
                log.exception("api action failed")
                return self._json({"error": str(exc)}, 500)
            return self._json(result, status)

        length = int(self.headers.get("Content-Length") or 0)
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        if not self._origin_ok() or fields.get("token", [""])[0] != r.token:
            return self._send(page("Refused", "<h1>Refused</h1>"
                                   "<p class=dim>Bad origin or stale form token.</p>", r), 403)
        if r.snapshots is None:
            return self._send(page("Refused", "<h1>Snapshots disabled</h1>", r), 400)

        note_id = fields.get("note_id", [""])[0]
        target = fields.get("target", [""])[0]
        action = urllib.parse.urlparse(self.path).path.strip("/")
        try:
            if action == "paste":
                text = fields.get("text", [""])[0]
                if text.strip():
                    r.snapshots.paste(note_id, target, text)
            elif action == "retry":
                r.snapshots.retry(note_id, target)
                SnapshotWorker(r.snapshots).run_once()
            elif action == "acknowledge":
                r.snapshots.acknowledge(note_id, target, "acknowledged in review")
        except Exception:
            log.exception("review action %s failed", action)
        self._redirect("/triage")


DEFAULT_HOST = "127.0.0.1"
"""Loopback, because the reader has no authentication and the typical user is
one person on one machine. Widening it is a deliberate act -- see `is_loopback`."""

DEFAULT_PORT = 8765


class RemoteBindRefused(ValueError):
    """A bind address was requested that reaches beyond this machine.

    There is no flag, argument or environment variable that turns this into a
    permitted bind -- see `check_bind`. A ValueError because that is what it
    is: an argument whose value is not acceptable.
    """


def resolve_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address `host` would actually bind. Empty when it resolves to nothing.

    Resolution rather than pattern matching: `localhost` is loopback by
    convention, `127.0.0.2` by arithmetic, and a name that happens to look
    local can resolve off-box. Matching strings would get all three wrong in
    different directions.
    """
    if not host:
        return []  # "" is every interface, exactly like 0.0.0.0
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def is_loopback(host: str) -> bool:
    """Does this bind address reach only this machine?

    False for anything unresolvable, and for the wildcards -- `0.0.0.0`, `::`
    and `""` all include every interface the machine has. The default is
    refusal: an address whose reach cannot be established is treated as remote,
    because the failure that matters is publishing the store by accident.
    """
    addresses = resolve_addresses(host)
    return bool(addresses) and all(address.is_loopback for address in addresses)


def address_family(host: str) -> int:
    """AF_INET6 when the bind address is v6.

    `ThreadingHTTPServer` is AF_INET, so binding `::1` against the default
    raises -- which is why the old localhost-only whitelist accepted an address
    it could never actually serve.
    """
    addresses = resolve_addresses(host)
    if host == "::" or (addresses and all(a.version == 6 for a in addresses)):
        return socket.AF_INET6
    return socket.AF_INET


def display_url(host: str, port: int) -> str:
    """A URL that can be pasted. v6 literals need brackets."""
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}"


def check_bind(host: str, port: int) -> None:
    """Refuse any bind address that reaches beyond this machine (§10, §12).

    **This takes no override parameter, and that is the design.** The reader
    has no authentication of any kind -- only §10.2's extension API holds a
    token -- so a non-loopback bind publishes every note to whoever can reach
    the port. A flag permitting it would be a one-word distance between a
    personal note store and an open one, reachable by a typo, a copied command
    line or an inherited environment variable. Off-box access is a reverse
    proxy's job: it can terminate TLS and authenticate, which this interface
    cannot and should not learn to do.

    Separated from serving so the policy can be tested without a socket: a gate
    whose only test path binds a port fails by *hanging* when the gate is
    removed, which is the one failure mode a safety check must not have.
    """
    if not is_loopback(host):
        raise RemoteBindRefused(
            f"refusing to bind {host or '0.0.0.0'}: the review interface has no "
            f"authentication, so this would publish every note to anyone who can "
            f"reach {host or '0.0.0.0'}:{port}. This is not overridable -- bind a "
            f"loopback address ({DEFAULT_HOST}, ::1) and put a reverse proxy in "
            f"front if you need access from elsewhere."
        )


def serve_review(
    db_path: Path,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
    snapshots: bool = True,
    capture_api: bool = True,
    embeddings: bool = True,
    model: str = DEFAULT_MODEL,
) -> None:
    """Serve the review interface. Loopback only, with no way to widen it.

    `host` chooses *among* loopback addresses -- 127.0.0.1, ::1, or another
    127/8 address -- and anything else is refused before a socket exists. There
    is deliberately no parameter here that permits a wider bind; see
    `check_bind` for why, and §10 for the spec's statement of it.
    """
    check_bind(host, port)

    embedder = OllamaEmbedder(model=model) if embeddings else None
    review = Review(
        db_path, snapshots=snapshots, capture_api=capture_api, embedder=embedder
    )
    # An extension capture writes a note here, not in the MCP server, so this
    # process needs its own drain -- otherwise captures sit unembedded until
    # something else happens to run.
    worker = None
    if embedder is not None and review.writer is not None and review.writer.vector_loaded:
        worker = EmbeddingWorker(review.writer)
        review.writer._on_note_written = worker.notify
        worker.start()
        log.info("embedder: %s (%d to embed)", model, review.writer.embedding_backlog())

    handler = type("BoundHandler", (Handler,), {"review": review})
    server = type(
        "BoundServer", (ThreadingHTTPServer,), {"address_family": address_family(host)}
    )
    httpd = server((host, port), handler)
    log.info("review interface: %s (notes read-only)", display_url(host, port))
    if review.api_token:
        log.info("extension API enabled; token in %s", token_path(review.db_path))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        if worker is not None:
            worker.stop()
        review.close()
