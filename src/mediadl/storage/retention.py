"""Retention sweeps (spec section 7.9).

Temp directories belong to a job. If a worker dies, its scratch directory
survives; the next run resumes from it, and anything older than the TTL is
garbage.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from mediadl.obs.logging import get_logger

log = get_logger(__name__)

DEFAULT_TEMP_TTL_S = 24 * 3600


@dataclass(frozen=True, slots=True)
class SweepResult:
    removed: tuple[str, ...] = ()
    bytes_freed: int = 0
    kept: int = 0


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob('*'):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _sweep(temp_root: Path, ttl_s: float, active_job_ids: frozenset[str]) -> SweepResult:
    if not temp_root.is_dir():
        return SweepResult()
    now = time.time()
    removed: list[str] = []
    freed = 0
    kept = 0
    for entry in temp_root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in active_job_ids:
            kept += 1
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < ttl_s:
            kept += 1
            continue
        freed += _directory_size(entry)
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry.name)
    return SweepResult(tuple(removed), freed, kept)


async def sweep_temp(
    temp_root: Path,
    *,
    ttl_s: float = DEFAULT_TEMP_TTL_S,
    active_job_ids: frozenset[str] = frozenset(),
) -> SweepResult:
    """Delete orphaned job scratch directories older than ``ttl_s``."""
    result = await asyncio.to_thread(_sweep, Path(temp_root), ttl_s, active_job_ids)
    if result.removed:
        log.info(
            'temp_swept', removed=len(result.removed), bytes_freed=result.bytes_freed,
            kept=result.kept,
        )
    return result
