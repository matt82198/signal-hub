"""Snapshot store (design section 2b).

Layout: ``<root>/snapshots/<source>/<ts>.json`` with ``<ts>`` compact basic
ISO UTC (``20260813T140200Z``) so lexical sort == chronological sort.  No
pointer/latest file, no symlinks (Windows).  Writes are atomic: full JSON
into a ``.tmp`` sibling, then ``os.replace`` -- a reader never sees a
half-written snapshot.

Retention: keep the newest ``keep`` (default 200) per source; only the two
newest are load-bearing, the tail is forensics.

An ERROR snapshot is still written (error text, no payload): inputs always
produce outputs -- a missing file means the tick never ran, which is a
louder and different failure than a source being down.

Collision note (design is silent): two writes inside the same clock second
bump the timestamp forward one second until the name is free, keeping
filenames unique and lexical order == write order.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .clock import iso_utc, parse_compact, parse_iso, system_now, ts_compact

STATUSES = ("OK", "UNCHANGED", "STALE", "SKIPPED", "ERROR")
SCHEMA_VERSION = 1

_ONE_SECOND = timedelta(seconds=1)


@dataclass(frozen=True)
class Snapshot:
    """A snapshot document read back from disk."""

    source: str
    schema_version: int
    captured_at: datetime
    status: str
    etag: Optional[str]
    error: Optional[str]
    payload: Any
    path: Path


class SnapshotStore:
    """Atomic, retention-bounded snapshot store under ``<root>/snapshots/``."""

    def __init__(
        self,
        root: Path,
        now: Callable[[], datetime] = system_now,
        keep: int = 200,
    ) -> None:
        self.root = Path(root)
        self.now = now
        self.keep = keep

    # -- paths ------------------------------------------------------------

    def dir_for(self, source: str) -> Path:
        return self.root / "snapshots" / source

    def paths(self, source: str) -> list[Path]:
        """All snapshot files for a source, oldest -> newest (lexical)."""
        d = self.dir_for(source)
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.suffix == ".json")

    # -- write ------------------------------------------------------------

    def write(
        self,
        source: str,
        payload: Any = None,
        status: str = "OK",
        etag: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Path:
        if status not in STATUSES:
            raise ValueError(
                "status must be one of %s, got %r" % ("|".join(STATUSES), status)
            )
        if status == "ERROR" and not error:
            raise ValueError("ERROR snapshot requires error text")

        captured = self.now()
        d = self.dir_for(source)
        d.mkdir(parents=True, exist_ok=True)
        target = d / (ts_compact(captured) + ".json")
        while target.exists():  # same-second collision: bump one second
            captured = captured + _ONE_SECOND
            target = d / (ts_compact(captured) + ".json")

        doc: dict[str, Any] = {
            "source": source,
            "schema_version": SCHEMA_VERSION,
            "captured_at": iso_utc(captured),
            "status": status,
            "etag": etag,
        }
        if status == "ERROR":
            doc["error"] = error
        else:
            doc["payload"] = payload
            if error:
                doc["error"] = error

        tmp = target.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        return target

    # -- read -------------------------------------------------------------

    def read(self, path: Path) -> Snapshot:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        return Snapshot(
            source=doc["source"],
            schema_version=doc["schema_version"],
            captured_at=parse_iso(doc["captured_at"]),
            status=doc["status"],
            etag=doc.get("etag"),
            error=doc.get("error"),
            payload=doc.get("payload"),
            path=Path(path),
        )

    def latest(self, source: str) -> Optional[Snapshot]:
        paths = self.paths(source)
        return self.read(paths[-1]) if paths else None

    def previous(self, source: str) -> Optional[Snapshot]:
        paths = self.paths(source)
        return self.read(paths[-2]) if len(paths) >= 2 else None

    # -- retention --------------------------------------------------------

    def prune(self, source: str) -> list[Path]:
        """Delete all but the newest ``keep`` snapshots; return removed paths."""
        paths = self.paths(source)
        doomed = paths[: max(0, len(paths) - self.keep)]
        for p in doomed:
            p.unlink()
        return doomed

    def prune_all(self) -> int:
        """Prune every source directory; return total files removed."""
        base = self.root / "snapshots"
        if not base.is_dir():
            return 0
        return sum(len(self.prune(d.name)) for d in base.iterdir() if d.is_dir())

    # -- misc -------------------------------------------------------------

    def sources(self) -> list[str]:
        base = self.root / "snapshots"
        if not base.is_dir():
            return []
        return sorted(d.name for d in base.iterdir() if d.is_dir())


def ts_of(path: Path) -> datetime:
    """Filename stamp -> aware UTC datetime (lexical name == chronology)."""
    return parse_compact(Path(path).stem)
