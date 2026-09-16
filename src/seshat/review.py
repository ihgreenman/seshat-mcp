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
* **127.0.0.1 only** (§10, §12). No authentication, no remote access; making it
  network-reachable would demand an auth model this tool has no business owning.
* **Origin-checked, token-guarded POSTs.** The moment a browser can reach an
  interface, "localhost is safe" stops being true: any page you visit can post
  to it. §10.2 names this for the extension, and it applies the instant a POST
  endpoint exists -- which is now.

Stdlib only, server-rendered, no JavaScript and no external assets.
"""

from __future__ import annotations

import html
import logging
import secrets
import threading
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .checker import Checker, reading_guide
from .snapshots import SnapshotStore, SnapshotWorker, snapshot_path_for
from .store import Store

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


class Review:
    """Everything the handler needs, built once and shared across requests."""

    def __init__(self, db_path: Path, snapshots: bool = True):
        self.db_path = Path(db_path)
        self.store = Store(self.db_path, read_only=True)
        self.snapshots = SnapshotStore(snapshot_path_for(self.db_path)) if snapshots else None
        # Regenerated per process: a form from a previous run is not a form.
        self.token = secrets.token_urlsafe(24)

    def close(self) -> None:
        self.store.close()
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
    return f"""<h1>Search</h1><p class="dim">{E(query)}</p>{caveat}
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

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        params = urllib.parse.parse_qs(parsed.query)
        r = self.review
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


def serve_review(
    db_path: Path, port: int = 8765, host: str = "127.0.0.1", snapshots: bool = True
) -> None:
    review = Review(db_path, snapshots=snapshots)
    if host not in ("127.0.0.1", "localhost", "::1"):
        # §10/§12: remote transport is out of scope, and binding wider would
        # demand an auth model this tool has no business owning.
        raise ValueError(f"refusing to bind {host}: the review interface is localhost-only")

    handler = type("BoundHandler", (Handler,), {"review": review})
    httpd = ThreadingHTTPServer((host, port), handler)
    log.info("review interface: http://%s:%d (notes read-only)", host, port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        review.close()
