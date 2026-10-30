"""Local-file import engine (spec section 5.3).

User-provided originals are often the highest-quality copy in existence, so
they are copied verbatim and hashed; nothing is re-encoded on import.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import ClassVar

from mediadl.domain.models import LocalFileLocator, Representation
from mediadl.errors import ContentTypeMismatchError, NotFoundError, SizeLimitError
from mediadl.processing import sniff
from mediadl.transfer.base import (
    CancelToken,
    ProgressReporter,
    TransferPlan,
    TransferResult,
    Validators,
)


class LocalFileTransfer:
    engine_id: ClassVar[str] = 'file'

    def __init__(self, import_roots: tuple[Path, ...] = ()) -> None:
        self.import_roots = tuple(Path(r).expanduser().resolve() for r in import_roots)

    def supports(self, rep: Representation) -> bool:
        return isinstance(rep.locator, LocalFileLocator)

    def resolve_source(self, locator: LocalFileLocator) -> Path:
        """Resolve an import path, honouring the configured roots.

        With no roots configured the process may read any path it has
        permission to, which is what a local single-user install expects and
        what ``UserFilesConnector`` already allows. Configuring roots is what
        turns that into a restriction, and the two must agree or every local
        import fails at transfer time.

        Symlinks are followed, then the *resolved* path must satisfy the
        roots. That is the boundary that matters: a link planted inside an
        approved root cannot be used to read outside it.
        """
        candidate = Path(str(locator.relative_path)).expanduser()

        if not self.import_roots:
            resolved = candidate.resolve()
            if not resolved.exists():
                raise NotFoundError('file not found', path=str(resolved))
            return resolved

        for root in self.import_roots:
            probe = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
            if probe.is_relative_to(root) and probe.exists():
                return probe
        raise NotFoundError(
            'file is not under an approved import root',
            roots=[str(r) for r in self.import_roots],
        )

    async def transfer(
        self, plan: TransferPlan, progress: ProgressReporter, cancel: CancelToken
    ) -> TransferResult:
        import asyncio
        import hashlib

        locator = plan.rep.locator
        assert isinstance(locator, LocalFileLocator)
        cancel.raise_if_cancelled()
        source = self.resolve_source(locator)
        size = source.stat().st_size
        if size > plan.limits.max_bytes:
            raise SizeLimitError('file exceeds the size limit', size=size, limit=plan.limits.max_bytes)

        detected = sniff.detect(source)
        if plan.expected_kind is not None and not sniff.compatible(detected, plan.expected_kind):
            raise ContentTypeMismatchError(
                f'expected {plan.expected_kind}, file looks like {detected.kind}',
                detected=str(detected.kind),
            )

        dest = plan.part_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _copy() -> str:
            digest = hashlib.sha256()
            with open(source, 'rb') as src, open(dest, 'wb') as out:
                shutil.copyfileobj(src, out, length=plan.limits.chunk_size)
                out.flush()
                os.fsync(out.fileno())
            with open(dest, 'rb') as fh:
                while chunk := fh.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest()

        sha = await asyncio.to_thread(_copy)
        progress(plan.rep.rep_id, size, size, force=True)
        return TransferResult(
            path=dest, bytes_written=size, sha256=sha,
            validators=Validators(None, None, size),
            resumed=False, attempts=1,
            url_fingerprint=f'file/{detected.ext or "bin"}',
            content_type=detected.mime, rep_id=plan.rep.rep_id,
        )
