"""Command line entry point. `seshat serve` is the MCP server; the rest is for
poking a store by hand without going through an assistant.

The consistency checker (spec §7) is deliberately a CLI subcommand rather than
an MCP tool -- it is periodic maintenance, and every tool costs context on every
turn. It is not implemented in this increment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .embeddings import DEFAULT_MODEL, EmbedderUnavailable, OllamaEmbedder
from .server import default_db_path, help_payload, serve
from .snapshots import SnapshotStore, SnapshotWorker, snapshot_path_for
from .store import DEFAULT_THETA, SeshatError, Store
from .worker import EmbeddingWorker


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seshat", description="persistent note store")
    p.add_argument("--db", type=Path, default=None, help="store path (default: $SESHAT_DB)")
    p.add_argument("--theta", type=float, default=DEFAULT_THETA, help="pool threshold (§5.1)")
    p.add_argument(
        "--no-snapshots", dest="snapshots", action="store_false",
        default=os.environ.get("SESHAT_SNAPSHOTS", "1") != "0",
        help="do not fetch or preserve the content of links in note text (§6.6). "
             "Capture is once-only: links written while this is off are "
             "unrecoverable later.",
    )
    p.add_argument(
        "--no-embeddings", dest="embeddings", action="store_false",
        default=os.environ.get("SESHAT_EMBEDDINGS", "1") != "0",
        help="do not embed notes or use vector search (§6.3). Retrieval stays "
             "FTS-only; nothing is lost, since embeddings are re-derivable.",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help="Ollama embedding model")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the MCP server on stdio")
    sub.add_parser("info", help="store path, counts, versions and capabilities")
    sub.add_parser("snapshots", help="capture status histogram (§6.6)")
    sub.add_parser("reindex-links", help="rebuild the derived link index (§6.5)")
    sub.add_parser("embed", help="embed every note that needs it, now")
    sub.add_parser("reembed", help="discard all embeddings and rebuild (model migration, §6.3)")

    f = sub.add_parser("fetch", help="drain the snapshot capture queue now")
    f.add_argument("--limit", type=int, default=0, help="0 means drain everything")

    c = sub.add_parser("context", help="search the store")
    c.add_argument("query", nargs="?", default="")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("--since", default=None)

    r = sub.add_parser("read", help="read one note")
    r.add_argument("id")

    ch = sub.add_parser("chain", help="ancestor/descendant closure of a note")
    ch.add_argument("id")

    w = sub.add_parser("why", help="search assessment rationales (§5.5)")
    w.add_argument("query")

    m = sub.add_parser("misses", help="list recorded id lookup failures (§6.1)")
    m.add_argument("--limit", type=int, default=50)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    path = args.db or default_db_path()

    if args.command == "serve":
        serve(path, theta=args.theta, snapshots=args.snapshots,
              embeddings=args.embeddings, model=args.model)
        return 0

    snaps = SnapshotStore(snapshot_path_for(path)) if args.snapshots else None
    embedder = OllamaEmbedder(model=args.model) if args.embeddings else None
    store = Store(path, theta=args.theta, snapshots=snaps, embedder=embedder)
    try:
        if args.command == "info":
            notes = store.db.execute("SELECT COUNT(*) FROM note").fetchone()[0]
            edges = store.db.execute("SELECT COUNT(*) FROM supersession").fetchone()[0]
            assessments = store.db.execute("SELECT COUNT(*) FROM assessment").fetchone()[0]
            pool = store.db.execute(
                "SELECT COUNT(*) FROM pool_retained WHERE retained >= ?", (store.theta,)
            ).fetchone()[0]
            report = help_payload(store)
            report.update({
                "embedding_backlog": store.embedding_backlog(),
                "path": store.path, "notes": notes, "edges": edges,
                "assessments": assessments, "pool": pool, "theta": store.theta,
                "links": store.db.execute("SELECT COUNT(*) FROM link").fetchone()[0],
                "retrieval": "hybrid" if report["capabilities"]["vector"] else "fts-only",
            })
            print(json.dumps(report, indent=2))
        elif args.command == "context":
            hits = store.context(args.query, args.since, args.limit)
            for h in hits:
                print(f"{h.score:.5f}  {h.id}  {h.desc}")
        elif args.command == "read":
            from dataclasses import asdict
            print(json.dumps(asdict(store.read(args.id)), indent=2))
        elif args.command == "chain":
            print(json.dumps(store.chain(args.id), indent=2))
        elif args.command == "why":
            print(json.dumps(store.search_rationales(args.query), indent=2))
        elif args.command == "snapshots":
            if snaps is None:
                print("snapshot capture is disabled (--no-snapshots)", file=sys.stderr)
                return 1
            print(json.dumps(snaps.histogram(), indent=2))
        elif args.command == "reindex-links":
            # Safe: `link` is derived from note text. The witnesses in the
            # snapshot database are in a different file and are never touched.
            print(f"rebuilt {store.reindex_links()} link rows")
        elif args.command == "fetch":
            if snaps is None:
                print("snapshot capture is disabled (--no-snapshots)", file=sys.stderr)
                return 1
            worker = SnapshotWorker(snaps)
            print(f"captured {worker.run_once()} of {len(snaps.pending())} queued")
        elif args.command == "embed":
            if embedder is None:
                print("embeddings are disabled (--no-embeddings)", file=sys.stderr)
                return 1
            print(f"embedded {EmbeddingWorker(store).drain()} notes; "
                  f"{store.embedding_backlog()} still outstanding")
        elif args.command == "reembed":
            # Model migration is a DELETE plus a drain (§6.3) -- and embeddings
            # are derived data, so this destroys nothing that cannot come back.
            print(f"cleared {store.clear_embeddings()} embeddings")
            if embedder is not None:
                print(f"re-embedded {EmbeddingWorker(store).drain()} notes")
        elif args.command == "misses":
            rows = store.db.execute(
                "SELECT * FROM near_miss ORDER BY hit_count DESC, first_seen LIMIT ?",
                (args.limit,),
            ).fetchall()
            for r in rows:
                kind = ("corrupted" if r["candidate"]
                        else "ambiguous" if r["distance"] is not None else "FABRICATED")
                print(f"{r['hit_count']:>4}  {kind:<10}  {r['requested']}  -> {r['candidate']}")
    except EmbedderUnavailable as exc:
        print(f"embedder unavailable: {exc}", file=sys.stderr)
        print("notes remain keyword-searchable; run `seshat embed` when it is back.",
              file=sys.stderr)
        return 1
    except SeshatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
        if snaps is not None:
            snaps.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
