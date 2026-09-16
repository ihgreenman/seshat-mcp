"""Consistency checking. Spec §7.

An offline batch operation over the whole store that produces a **report for
human review**. It never mutates, never auto-creates edges, never auto-fixes --
and that is enforced rather than promised: every connection it opens is
`mode=ro`, so a stray write is an error from SQLite rather than a silent
correction to somebody's belief history.

Deliberately not an MCP tool (§7). It is periodic maintenance, and every tool in
the surface costs context on every turn.

The report doubles as a reading guide (§7.3): §8 argues the store must
periodically be read in full, and this says where to start.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .store import DEFAULT_THETA

SEVERITIES = ("error", "warning", "review", "info")
"""Ordered worst first. `review` is not a lesser warning -- it marks a judgement
only a human can make, which is most of what this checker produces."""

DEFAULT_SIMILARITY = 0.90
"""Cosine threshold for suggested links (§7.2). Open question §9.5: no
principled basis. Start high and tune down -- topical relatedness is not
supersession, and most candidates will be rejected."""

NEAR_IDENTICAL = 0.98
"""Text similarity above which a `desc` change is classified as a description
fix (§7.2's one decidable exception)."""


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    message: str
    notes: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "notes": list(self.notes),
            **({"detail": self.detail} if self.detail else {}),
        }


def _read_only(path: str) -> sqlite3.Connection:
    """Open a database that physically cannot be written through."""
    if path != ":memory:" and not Path(path).exists():
        raise FileNotFoundError(path)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


class Checker:
    """Runs §7's checks. Construct, `run()`, discard."""

    def __init__(
        self,
        db_path: str,
        snapshot_path: str | None = None,
        theta: float = DEFAULT_THETA,
        similarity: float = DEFAULT_SIMILARITY,
    ):
        self.db = _read_only(db_path)
        self.theta = theta
        self.similarity = similarity
        self.snapshots: sqlite3.Connection | None = None
        if snapshot_path:
            try:
                self.snapshots = _read_only(snapshot_path)
            except (FileNotFoundError, sqlite3.OperationalError):
                # No snapshot database is a legitimate configuration, not a
                # finding: the store may be running with capture disabled.
                self.snapshots = None

    def close(self) -> None:
        self.db.close()
        if self.snapshots is not None:
            self.snapshots.close()

    # ------------------------------------------------------------- helpers

    def _edges(self) -> list[tuple[str, str, float]]:
        return [
            (r["old_id"], r["new_id"], r["retained"])
            for r in self.db.execute("SELECT old_id, new_id, retained FROM supersession")
        ]

    def _descs(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT id, desc FROM note"))

    def _out(self) -> dict[str, list[tuple[str, float]]]:
        out: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for old, new, retained in self._edges():
            out[old].append((new, retained))
        return out

    def _retracted(self) -> set[str]:
        """Notes some live edge retracts outright."""
        return {old for old, _, retained in self._edges() if retained == 0.0}

    # ------------------------------------------------------ §7.1 structural

    def check_cycles(self) -> list[Finding]:
        """Should be unreachable given §3.4's write-time rejection -- which is
        exactly why it is worth checking. A cycle makes head resolution
        non-terminating, so it is an error, not a review item."""
        edges = self._edges()
        indegree: dict[str, int] = defaultdict(int)
        nodes = {r[0] for r in self.db.execute("SELECT id FROM note")}
        adjacency: dict[str, list[str]] = defaultdict(list)
        for old, new, _ in edges:
            adjacency[old].append(new)
            indegree[new] += 1

        queue = [n for n in nodes if indegree[n] == 0]
        seen = 0
        while queue:
            node = queue.pop()
            seen += 1
            for nxt in adjacency[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if seen == len(nodes):
            return []
        stuck = sorted(n for n in nodes if indegree[n] > 0)
        return [Finding(
            "cycle", "error",
            f"supersession graph contains a cycle through {len(stuck)} notes; "
            f"head resolution will not terminate",
            tuple(stuck[:20]),
        )]

    def check_self_supersession(self) -> list[Finding]:
        """Checked against `assessment`, not the latest-wins view: a self-edge
        recorded once is a defect even if a later assessment supersedes it."""
        rows = self.db.execute(
            "SELECT DISTINCT old_id FROM assessment WHERE old_id = new_id"
        ).fetchall()
        return [
            Finding("self-supersession", "error", f"{r['old_id']} supersedes itself", (r["old_id"],))
            for r in rows
        ]

    def check_noop_supersession(self) -> list[Finding]:
        rows = self.db.execute(
            """SELECT s.old_id, s.new_id FROM supersession s
               JOIN note a ON a.id = s.old_id
               JOIN note b ON b.id = s.new_id
               WHERE s.retained = 1.0 AND a.desc = b.desc AND a.text = b.text"""
        ).fetchall()
        return [
            Finding(
                "no-op supersession", "warning",
                f"{r['new_id']} supersedes {r['old_id']} at 1.0 with identical desc and text",
                (r["old_id"], r["new_id"]),
            )
            for r in rows
        ]

    def check_conflicting_fork(self) -> list[Finding]:
        """A note whose outgoing edges span θ: one descendant says the content
        survives, another says it does not."""
        findings = []
        for note_id, edges in sorted(self._out().items()):
            above = [(n, r) for n, r in edges if r >= self.theta]
            below = [(n, r) for n, r in edges if r < self.theta]
            if above and below:
                findings.append(Finding(
                    "conflicting fork", "review",
                    f"{note_id} is superseded at {max(r for _, r in above):.2f} by "
                    f"{above[0][0]} and at {min(r for _, r in below):.2f} by {below[0][0]}",
                    (note_id,),
                    {"above": above, "below": below},
                ))
        return findings

    def check_orphaned_retraction(self) -> list[Finding]:
        """§5.4, the follow-up you owe -- and §7.1 calls it the highest-value
        check in the list.

        If A is retracted but some descendant B asserted A's content at high
        `retained` and B is itself un-superseded, then B is contaminated: it
        rests on something now known to be wrong, and nothing records that.
        Without this check the non-locality of maximum is a trap rather than a
        feature.

        Direct edges only, matching §5.1's scope.
        """
        out = self._out()
        has_out = set(out)
        findings = []
        for note_id, edges in sorted(out.items()):
            if not any(r == 0.0 for _, r in edges):
                continue
            for other, retained in edges:
                if retained >= self.theta and other not in has_out:
                    findings.append(Finding(
                        "orphaned retraction", "review",
                        f"{note_id} is retracted, but {other} asserted it at "
                        f"{retained:.2f} and has not been superseded",
                        (note_id, other),
                        {"retained": retained},
                    ))
        return findings

    def check_dead_justification(self) -> list[Finding]:
        """Specific to maximum (§5.2): A stays in the pool because of an edge to
        B, and B is itself retracted -- so the justification is dead."""
        retracted = self._retracted()
        findings = []
        for note_id, edges in sorted(self._out().items()):
            supporting = [(n, r) for n, r in edges if r >= self.theta]
            if not supporting:
                continue
            if all(n in retracted for n, _ in supporting):
                findings.append(Finding(
                    "dead justification", "review",
                    f"{note_id} is in the pool only via {', '.join(n for n, _ in supporting)}, "
                    f"which {'are' if len(supporting) > 1 else 'is'} retracted",
                    (note_id, *[n for n, _ in supporting]),
                ))
        return findings

    def check_double_retraction(self) -> list[Finding]:
        """A retracted by B, B retracted by C. Is A back? Context-dependent by
        nature -- flagging is the whole job (§7.1)."""
        out = self._out()
        findings = []
        for a, edges in sorted(out.items()):
            for b, retained in edges:
                if retained != 0.0:
                    continue
                for c, second in out.get(b, []):
                    if second == 0.0:
                        findings.append(Finding(
                            "double retraction", "review",
                            f"{a} was retracted by {b}, which was itself retracted by {c}; "
                            f"whether {a} is back is a judgement call",
                            (a, b, c),
                        ))
        return findings

    def check_dangling_internal_links(self) -> list[Finding]:
        rows = self.db.execute(
            """SELECT l.note_id, l.target FROM link l
               LEFT JOIN note n ON n.id = l.target
               WHERE l.kind = 'note' AND n.id IS NULL"""
        ).fetchall()
        return [
            Finding(
                "dangling internal link", "review",
                f"{r['note_id']} references {r['target']}, which does not exist "
                f"-- likely a fabricated or corrupted id",
                (r["note_id"],),
                {"target": r["target"]},
            )
            for r in rows
        ]

    # --------------------------------------------------- §7.1 snapshot checks

    def check_snapshots(self) -> list[Finding]:
        """Thin and missing snapshots (§6.6, §7.1).

        Skipped entirely when there is no snapshot database: a store running
        with capture disabled has no preservation to have failed.
        """
        if self.snapshots is None:
            return []
        findings = []

        thin = self.snapshots.execute(
            "SELECT note_id, target FROM snapshot WHERE status = 'thin'"
        ).fetchall()
        for row in thin:
            findings.append(Finding(
                "thin snapshot", "review",
                f"extraction of {row['target']} yielded implausibly little "
                f"-- TIME-SENSITIVE: the target may still be live",
                (row["note_id"],),
                {"target": row["target"]},
            ))

        unmet = self.snapshots.execute(
            """SELECT note_id, target, expectation, expectation_source
               FROM snapshot WHERE expectation IS NOT NULL AND text IS NOT NULL"""
        ).fetchall()
        for row in unmet:
            from .extract import expectation_met

            text = self.snapshots.execute(
                "SELECT text FROM snapshot WHERE note_id = ? AND target = ?",
                (row["note_id"], row["target"]),
            ).fetchone()["text"]
            if expectation_met(row["expectation"], text) is False:
                findings.append(Finding(
                    "expectation not met", "review",
                    f"{row['target']} does not contain what the citation sought "
                    f"({row['expectation']!r}, from {row['expectation_source']}) "
                    f"-- a candidate, not a verdict",
                    (row["note_id"],),
                    {"target": row["target"], "expectation": row["expectation"]},
                ))

        captured = {
            (r["note_id"], r["target"])
            for r in self.snapshots.execute("SELECT note_id, target FROM snapshot")
        }
        queued = {
            (r["note_id"], r["target"])
            for r in self.snapshots.execute("SELECT note_id, target FROM fetch_queue")
        }
        for row in self.db.execute("SELECT note_id, target FROM link WHERE kind = 'url'"):
            key = (row["note_id"], row["target"])
            if key not in captured and key not in queued:
                findings.append(Finding(
                    "missing snapshot", "error",
                    f"{row['target']} was never captured for {row['note_id']} and is not "
                    f"queued -- preservation failed silently",
                    (row["note_id"],),
                    {"target": row["target"]},
                ))
        return findings

    # ----------------------------------------------------- §7.2 semantic

    def _vectors(self) -> dict[str, list[float]]:
        try:
            from . import vectors as module

            if not module.load_extension(self.db):
                return {}
            rows = self.db.execute(
                "SELECT note_id, embedding FROM note_vec"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        import struct

        out = {}
        for row in rows:
            blob = row["embedding"]
            out[row["note_id"]] = list(struct.unpack(f"<{len(blob) // 4}f", blob))
        return out

    def _reachable(self) -> dict[str, set[str]]:
        """Descendant sets, so 'is there a path between these two' is a lookup."""
        adjacency: dict[str, list[str]] = defaultdict(list)
        for old, new, _ in self._edges():
            adjacency[old].append(new)
        memo: dict[str, set[str]] = {}

        def descendants(node: str, stack: frozenset[str] = frozenset()) -> set[str]:
            if node in memo:
                return memo[node]
            if node in stack:  # a cycle; check_cycles reports it separately
                return set()
            found: set[str] = set()
            for nxt in adjacency.get(node, ()):
                found.add(nxt)
                found |= descendants(nxt, stack | {node})
            memo[node] = found
            return found

        return {n: descendants(n) for n in {e[0] for e in self._edges()} | set(adjacency)}

    def check_suggested_links(self) -> list[Finding]:
        """Pairs with high cosine similarity and no path between them (§7.2).

        Strictly candidate generation. Expect noise: topical relatedness is not
        supersession, and most candidates will be rejected by the human reading
        the report.
        """
        vectors = self._vectors()
        if len(vectors) < 2:
            return []
        reach = self._reachable()
        ids = sorted(vectors)
        descs = self._descs()
        findings = []
        for i, a in enumerate(ids):
            va = vectors[a]
            for b in ids[i + 1 :]:
                if b in reach.get(a, ()) or a in reach.get(b, ()):
                    continue
                score = sum(x * y for x, y in zip(va, vectors[b]))
                if score >= self.similarity:
                    findings.append(Finding(
                        "suggested link", "info",
                        f"{a} and {b} are {score:.3f} similar with no supersession path "
                        f"between them",
                        (a, b),
                        {"similarity": round(score, 4),
                         "a": descs.get(a, ""), "b": descs.get(b, "")},
                    ))
        findings.sort(key=lambda f: -f.detail["similarity"])
        return findings

    def check_description_fixes(self) -> list[Finding]:
        """§7.2's one decidable exception: near-identical `text` with a changed
        `desc` is a description fix, and §9.3 wants to know about it.

        Character-level, not embedding-based -- this is the case where a cheap
        exact test is more reliable than a semantic one.
        """
        import difflib

        rows = self.db.execute(
            """SELECT s.old_id, s.new_id, a.desc AS old_desc, b.desc AS new_desc,
                      a.text AS old_text, b.text AS new_text
               FROM supersession s
               JOIN note a ON a.id = s.old_id
               JOIN note b ON b.id = s.new_id
               WHERE a.desc != b.desc"""
        ).fetchall()
        findings = []
        for row in rows:
            ratio = difflib.SequenceMatcher(
                None, row["old_text"], row["new_text"]
            ).ratio()
            if ratio >= NEAR_IDENTICAL:
                findings.append(Finding(
                    "description fix", "info",
                    f"{row['new_id']} changes only the desc of {row['old_id']} "
                    f"({ratio:.3f} text similarity)",
                    (row["old_id"], row["new_id"]),
                    {"was": row["old_desc"], "now": row["new_desc"]},
                ))
        return findings

    # ----------------------------------------------------------------- run

    def run(self, semantic: bool = True) -> list[Finding]:
        findings: list[Finding] = []
        findings += self.check_cycles()
        findings += self.check_self_supersession()
        findings += self.check_noop_supersession()
        findings += self.check_conflicting_fork()
        findings += self.check_orphaned_retraction()
        findings += self.check_dead_justification()
        findings += self.check_double_retraction()
        findings += self.check_dangling_internal_links()
        findings += self.check_snapshots()
        if semantic:
            findings += self.check_suggested_links()
            findings += self.check_description_fixes()
        order = {s: i for i, s in enumerate(SEVERITIES)}
        findings.sort(key=lambda f: (order[f.severity], f.check))
        return findings


def reading_guide(findings: Iterable[Finding], descs: dict[str, str], limit: int = 10):
    """§7.3: the report says where to start reading.

    Notes implicated by the most findings, worst severity first -- which is a
    better entry point into a store than either recency or the graph.
    """
    weight = {"error": 3, "warning": 2, "review": 2, "info": 1}
    tally: dict[str, int] = defaultdict(int)
    for finding in findings:
        for note_id in finding.notes:
            tally[note_id] += weight[finding.severity]
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [(note_id, descs.get(note_id, "(missing note)"), score) for note_id, score in ranked]


def format_report(findings: list[Finding], descs: dict[str, str], total_notes: int) -> str:
    lines = [f"seshat consistency report -- {total_notes} notes, {len(findings)} findings", ""]
    if not findings:
        lines.append("No findings. (That is not proof of consistency -- §7.2's checks are")
        lines.append("candidate generation, and §7.1 validates structure, not truth.)")
        return "\n".join(lines)

    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    lines.append("  " + "   ".join(f"{s}: {counts[s]}" for s in SEVERITIES if counts[s]))
    lines.append("")

    current = None
    for finding in findings:
        if finding.severity != current:
            current = finding.severity
            lines.append(f"--- {current.upper()} " + "-" * (68 - len(current)))
        lines.append(f"  [{finding.check}] {finding.message}")
        for note_id in finding.notes[:4]:
            if note_id in descs:
                lines.append(f"      {note_id}  {descs[note_id][:64]}")
    guide = reading_guide(findings, descs)
    if guide:
        lines += ["", "--- WHERE TO START READING " + "-" * 49]
        for note_id, desc, score in guide:
            lines.append(f"  {score:>3}  {note_id}  {desc[:60]}")
    return "\n".join(lines)
