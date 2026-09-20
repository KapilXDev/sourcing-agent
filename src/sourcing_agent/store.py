"""SQLite persistence: posting store, dedupe index, spend ledger, run log.

One file holds everything the agent needs to be restartable and auditable:

* ``postings`` - the normalized corpus, keyed by ``source:source_id`` with an
  index on the cross-source ``fingerprint``.
* ``decisions`` / ``assessments`` - what the gate and the funnel concluded, so a
  second run does not pay to re-judge the same posting.
* ``spend`` - every model call, its pre-flight estimate and its actual cost.
* ``receipts`` - what was transmitted. A partial unique index makes a second
  successful submission to the same posting impossible at the storage layer,
  not merely unlikely in application code.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection, Iterable, Iterator

from .models import FitAssessment, GateDecision, Posting, Receipt

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS postings (
    key              TEXT PRIMARY KEY,
    fingerprint      TEXT NOT NULL,
    source           TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    url              TEXT NOT NULL,
    title            TEXT NOT NULL,
    company          TEXT NOT NULL,
    location         TEXT,
    remote           INTEGER,
    description      TEXT NOT NULL DEFAULT '',
    posted_at        TEXT,
    compensation_raw TEXT,
    apply_handle     TEXT,
    raw_json         TEXT NOT NULL DEFAULT '{}',
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postings_fingerprint ON postings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_postings_source      ON postings(source);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    budget_usd  REAL NOT NULL DEFAULT 0,
    spend_usd   REAL NOT NULL DEFAULT 0,
    report_json TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    posting_key TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    passed      INTEGER NOT NULL,
    score       INTEGER NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (posting_key, run_id)
);

CREATE TABLE IF NOT EXISTS assessments (
    posting_key TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    fit_score   INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (posting_key, run_id)
);
CREATE INDEX IF NOT EXISTS idx_assessments_key ON assessments(posting_key, created_at);

CREATE TABLE IF NOT EXISTS spend (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    stage          TEXT NOT NULL,
    model          TEXT NOT NULL,
    estimated_usd  REAL NOT NULL,
    actual_usd     REAL NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_read     INTEGER NOT NULL DEFAULT 0,
    cache_write    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_run ON spend(run_id);

CREATE TABLE IF NOT EXISTS receipts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    posting_key  TEXT NOT NULL,
    connector    TEXT NOT NULL,
    route        TEXT NOT NULL,
    submitted    INTEGER NOT NULL,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    external_id  TEXT,
    detail       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

-- Apply at most once per posting, enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_submitted_once
    ON receipts(posting_key) WHERE submitted = 1 AND dry_run = 0;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class AlreadySubmitted(RuntimeError):
    """Raised when a posting already has a real submission receipt."""


class Store:
    """Thin repository over SQLite. All writes are transactional."""

    def __init__(self, path: str | Path = "sourcing.db") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- postings ----------------------------------------------------------

    def upsert_postings(self, postings: Iterable[Posting]) -> tuple[int, int]:
        """Insert or refresh postings.

        Returns ``(stored, duplicates)`` where *duplicates* counts postings whose
        cross-source fingerprint was already present under a different key - the
        same job found again on another board.
        """
        stored = 0
        duplicates = 0
        now = _now()
        with self.tx() as conn:
            for p in postings:
                row = conn.execute(
                    "SELECT key FROM postings WHERE fingerprint = ? AND key != ?",
                    (p.fingerprint, p.key),
                ).fetchone()
                if row is not None:
                    duplicates += 1
                conn.execute(
                    """
                    INSERT INTO postings (key, fingerprint, source, source_id, url, title,
                        company, location, remote, description, posted_at, compensation_raw,
                        apply_handle, raw_json, first_seen, last_seen)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                        url=excluded.url,
                        title=excluded.title,
                        company=excluded.company,
                        location=excluded.location,
                        remote=excluded.remote,
                        description=CASE
                            WHEN length(excluded.description) > length(postings.description)
                            THEN excluded.description ELSE postings.description END,
                        posted_at=COALESCE(excluded.posted_at, postings.posted_at),
                        compensation_raw=COALESCE(excluded.compensation_raw, postings.compensation_raw),
                        apply_handle=COALESCE(excluded.apply_handle, postings.apply_handle),
                        raw_json=excluded.raw_json,
                        last_seen=excluded.last_seen
                    """,
                    (
                        p.key,
                        p.fingerprint,
                        p.source,
                        p.source_id,
                        p.url,
                        p.title,
                        p.company,
                        p.location,
                        None if p.remote is None else int(p.remote),
                        p.description,
                        p.posted_at.isoformat() if p.posted_at else None,
                        p.compensation_raw,
                        p.apply_handle,
                        json.dumps(p.raw, default=str),
                        now,
                        now,
                    ),
                )
                stored += 1
        return stored, duplicates

    def _row_to_posting(self, row: sqlite3.Row) -> Posting:
        return Posting(
            source=row["source"],
            source_id=row["source_id"],
            url=row["url"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            remote=None if row["remote"] is None else bool(row["remote"]),
            description=row["description"],
            posted_at=_dt(row["posted_at"]),
            compensation_raw=row["compensation_raw"],
            apply_handle=row["apply_handle"],
            raw=json.loads(row["raw_json"] or "{}"),
        )

    def get_posting(self, key: str) -> Posting | None:
        row = self.conn.execute("SELECT * FROM postings WHERE key = ?", (key,)).fetchone()
        return self._row_to_posting(row) if row else None

    def all_postings(self, limit: int | None = None) -> list[Posting]:
        sql = "SELECT * FROM postings ORDER BY last_seen DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._row_to_posting(r) for r in self.conn.execute(sql)]

    def dedupe(
        self, postings: Iterable[Posting], prefer_sources: Collection[str] = ()
    ) -> list[Posting]:
        """Collapse a batch to one posting per fingerprint.

        Ties break toward a source you can actually apply through, then toward
        an apply handle, then toward the richest description.

        Preferring description length alone is wrong, and quietly so: the two
        sources whose list endpoints omit the body (SmartRecruiters, Workday)
        would always lose to an aggregator's copy of the same role, discarding
        the only posting carrying an apply handle. The body comes back anyway -
        the gate's rescue pass fetches it - but the right to submit does not.
        """
        prefer = set(prefer_sources)

        def rank(p: Posting) -> tuple[bool, bool, int]:
            return (p.source in prefer, bool(p.apply_handle), len(p.description))

        best: dict[str, Posting] = {}
        for p in postings:
            current = best.get(p.fingerprint)
            if current is None or rank(p) > rank(current):
                best[p.fingerprint] = p
        return list(best.values())

    def recently_assessed(self, within_days: int) -> set[str]:
        """Posting keys assessed inside the window - skip re-spending on them."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
        rows = self.conn.execute(
            "SELECT DISTINCT posting_key FROM assessments WHERE created_at >= ?", (cutoff,)
        )
        return {r["posting_key"] for r in rows}

    # -- runs --------------------------------------------------------------

    def start_run(self, run_id: str, budget_usd: float) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, started_at, budget_usd) VALUES (?,?,?)",
                (run_id, _now(), budget_usd),
            )

    def finish_run(self, run_id: str, spend_usd: float, report_json: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, spend_usd=?, report_json=? WHERE run_id=?",
                (_now(), spend_usd, report_json, run_id),
            )

    def last_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        )

    # -- gate + funnel results --------------------------------------------

    def record_decision(self, run_id: str, posting_key: str, decision: GateDecision) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO decisions
                   (posting_key, run_id, passed, score, payload, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    posting_key,
                    run_id,
                    int(decision.passed),
                    decision.prefilter_score,
                    decision.model_dump_json(),
                    _now(),
                ),
            )

    def record_assessment(
        self, run_id: str, posting_key: str, assessment: FitAssessment
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO assessments
                   (posting_key, run_id, fit_score, payload, created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    posting_key,
                    run_id,
                    assessment.fit_score,
                    assessment.model_dump_json(),
                    _now(),
                ),
            )

    # -- spend -------------------------------------------------------------

    def record_spend(
        self,
        run_id: str,
        stage: str,
        model: str,
        estimated_usd: float,
        actual_usd: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """INSERT INTO spend (run_id, stage, model, estimated_usd, actual_usd,
                       input_tokens, output_tokens, cache_read, cache_write, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    stage,
                    model,
                    estimated_usd,
                    actual_usd,
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_write,
                    _now(),
                ),
            )

    def run_spend(self, run_id: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(actual_usd), 0.0) AS total FROM spend WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return float(row["total"])

    def spend_by_stage(self, run_id: str) -> dict[str, float]:
        rows = self.conn.execute(
            """SELECT stage, SUM(actual_usd) AS total FROM spend
               WHERE run_id = ? GROUP BY stage""",
            (run_id,),
        )
        return {r["stage"]: float(r["total"]) for r in rows}

    # -- receipts ----------------------------------------------------------

    def has_submitted(self, posting_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM receipts WHERE posting_key=? AND submitted=1 AND dry_run=0",
            (posting_key,),
        ).fetchone()
        return row is not None

    def record_receipt(self, run_id: str, receipt: Receipt) -> None:
        try:
            with self.tx() as conn:
                conn.execute(
                    """INSERT INTO receipts (run_id, posting_key, connector, route,
                           submitted, dry_run, external_id, detail, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        receipt.posting_key,
                        receipt.connector,
                        receipt.route.value,
                        int(receipt.submitted),
                        int(receipt.dry_run),
                        receipt.external_id,
                        receipt.detail,
                        receipt.at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AlreadySubmitted(
                f"{receipt.posting_key} already has a submission receipt"
            ) from exc
