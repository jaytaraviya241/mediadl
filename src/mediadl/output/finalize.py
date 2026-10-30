"""Atomic finalization, collision handling, and dedupe (spec section 7.9)."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from mediadl.domain.records import OutputRecord, ProbeResult, ProcessingStep, RenditionBasis
from mediadl.errors import PathRejectedError, StorageError
from mediadl.obs.logging import get_logger
from mediadl.policy.paths import enforce_under_root

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PlannedPath:
    relative: PurePosixPath
    absolute: Path

    def with_suffix_marker(self, marker: int) -> PlannedPath:
        stem = self.absolute.stem
        suffix = self.absolute.suffix
        new_name = f'{stem}~{marker}{suffix}'
        return PlannedPath(
            relative=self.relative.with_name(new_name),
            absolute=self.absolute.with_name(new_name),
        )


class LocalOutputStore:
    """Writes into one configured root. Nothing escapes it."""

    def __init__(self, root: Path, *, collision: str = 'dedupe_or_suffix') -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.collision = collision

    def plan(self, relative: PurePosixPath) -> PlannedPath:
        absolute = enforce_under_root(Path(str(relative)), self.root)
        return PlannedPath(relative=relative, absolute=absolute)

    async def resolve_collision(self, planned: PlannedPath, sha256: str) -> tuple[PlannedPath, bool]:
        """Return the path to write and whether an identical file already exists."""
        if not planned.absolute.exists():
            return planned, False
        if self.collision == 'overwrite':
            return planned, False
        if self.collision == 'fail':
            raise PathRejectedError('output file already exists', path=str(planned.relative))
        from mediadl.transfer.base import sha256_file

        existing = await sha256_file(planned.absolute)
        if existing == sha256:
            return planned, True  # byte-identical: dedupe
        for marker in range(2, 1000):
            candidate = planned.with_suffix_marker(marker)
            if not candidate.absolute.exists():
                return candidate, False
            if await sha256_file(candidate.absolute) == sha256:
                return candidate, True
        raise PathRejectedError('too many colliding outputs', path=str(planned.relative))

    async def finalize(
        self,
        temp: Path,
        planned: PlannedPath,
        *,
        asset_id: str,
        position: int,
        sha256: str,
        mime_type: str,
        selected_rep_id: str,
        rendition_basis: RenditionBasis,
        probe: ProbeResult | None = None,
        processing: tuple[ProcessingStep, ...] = (),
        retained_source: Path | None = None,
    ) -> OutputRecord:
        planned, deduped = await self.resolve_collision(planned, sha256)
        size = temp.stat().st_size

        if deduped:
            temp.unlink(missing_ok=True)
            log.info('output_deduped', path=str(planned.relative), asset_id=asset_id)
        else:
            await asyncio.to_thread(self._atomic_move, temp, planned.absolute)

        retained_rel: str | None = None
        if retained_source is not None and retained_source.exists():
            keep = planned.absolute.with_name(planned.absolute.stem + '.source' + retained_source.suffix)
            await asyncio.to_thread(self._atomic_move, retained_source, keep)
            retained_rel = str(keep.relative_to(self.root))

        return OutputRecord(
            asset_id=asset_id,
            position=position,
            relative_path=str(planned.relative),
            size_bytes=size,
            sha256=sha256,
            mime_type=mime_type,
            selected_rep_id=selected_rep_id,
            downloaded_at=datetime.now(UTC),
            rendition_basis=rendition_basis,
            probe=probe,
            processing=processing,
            retained_source_path=retained_rel,
        )

    @staticmethod
    def _atomic_move(temp: Path, final: Path) -> None:
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(temp, 'rb') as fh:
                os.fsync(fh.fileno())
            os.replace(temp, final)
            if hasattr(os, 'O_DIRECTORY'):
                dir_fd = os.open(final.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except OSError as exc:
            if exc.errno == 28:  # ENOSPC
                raise StorageError('not enough space to finalize the download') from exc
            raise
