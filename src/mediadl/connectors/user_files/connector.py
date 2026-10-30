"""User-provided files connector (spec section 4.5.4).

The user's own originals are frequently the highest-quality copy that
exists, since platforms serve transcoded renditions. Upload *is* the
authorization here; the files are still treated as untrusted input.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar

from mediadl.domain.models import (
    Asset,
    Availability,
    Collection,
    CollectionKind,
    LocalFileLocator,
    MediaKind,
    Representation,
    RepresentationRole,
    ResolvedItem,
    SourceMetadata,
    SourceRef,
    TechnicalInfo,
)
from mediadl.errors import InputError, NotFoundError
from mediadl.processing import sniff
from mediadl.sources.base import AccessContext, BaseConnector, ConnectorCapabilities, SourceRequest

PREFIX = 'file:'


class UserFilesConnector(BaseConnector):
    connector_id: ClassVar[str] = 'user_files'
    capabilities: ClassVar[ConnectorCapabilities] = ConnectorCapabilities(
        media_kinds=frozenset(MediaKind),
        auth_modes=frozenset({'none'}),
        rendition_basis='user_original',
    )

    def __init__(self, import_roots: tuple[str, ...] = ()) -> None:
        self.import_roots = tuple(Path(r).expanduser().resolve() for r in import_roots)

    def match(self, request: SourceRequest) -> int | None:
        raw = request.raw.strip()
        if raw.startswith(PREFIX):
            return 90
        if raw.startswith(('http://', 'https://', 'ig-media:')):
            return None
        path = Path(raw).expanduser()
        if path.exists():
            return 50
        return None

    def _candidate(self, raw: str) -> Path:
        raw = raw.strip()
        if raw.startswith(PREFIX):
            raw = raw[len(PREFIX):]
        path = Path(raw).expanduser()
        if not path.exists():
            raise NotFoundError('file not found', path=str(path))
        return path.resolve()

    def _check_root(self, path: Path) -> None:
        if not self.import_roots:
            return
        if not any(path.is_relative_to(root) for root in self.import_roots):
            raise NotFoundError(
                'file is not under an approved import root',
                roots=[str(r) for r in self.import_roots],
            )

    async def resolve(self, request: SourceRequest, ctx: AccessContext) -> ResolvedItem:
        path = self._candidate(request.raw)
        self._check_root(path)
        if path.is_dir():
            files = sorted(
                p for p in path.iterdir()
                if p.is_file() and not p.is_symlink() and not p.name.startswith('.')
            )
            if not files:
                raise InputError('directory contains no importable files', path=str(path))
            assets = tuple(
                self._asset(file, position=index)
                for index, file in enumerate(files, start=1)
            )
            collection_id = _stable_id(str(path))
            return Collection(
                collection_id=f'{self.connector_id}:{collection_id}',
                ref=SourceRef(self.connector_id, collection_id, canonical_url=None),
                kind=CollectionKind.UPLOAD_BATCH,
                assets=assets,
                position_basis='user_order',
                metadata=SourceMetadata(
                    fields={'directory': path.name}, fetched_at=datetime.now(UTC)
                ),
            )
        # _candidate() already resolved the path, so a symlink has been
        # followed by now; containment against the import roots is what
        # actually constrains where the bytes may come from.
        return self._asset(path, position=1)

    def _asset(self, path: Path, *, position: int) -> Asset:
        detected = sniff.detect(path)
        if not detected.known:
            kind = MediaKind.IMAGE if path.suffix.lower() in ('.jpg', '.png') else MediaKind.VIDEO
        else:
            kind = detected.kind  # type: ignore[assignment]
        media_id = _stable_id(str(path))
        stat = path.stat()
        rep = Representation(
            rep_id='original',
            kind=kind,
            role=RepresentationRole.PRIMARY,
            protocol='file',
            locator=LocalFileLocator(relative_path=PurePosixPath(str(path))),
            declared=TechnicalInfo(
                mime_type=detected.mime,
                container=detected.ext,
                filesize_bytes=stat.st_size,
            ),
            label=detected.ext or 'original',
        )
        return Asset(
            asset_id=f'{self.connector_id}:{media_id}',
            ref=SourceRef(self.connector_id, media_id, position=position),
            kind=kind,
            position=position,
            availability=Availability.AVAILABLE,
            representations=(rep,),
            metadata=SourceMetadata(
                fields={
                    'filename': path.name,
                    'size_bytes': stat.st_size,
                    'modified_at': datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                },
                fetched_at=datetime.now(UTC),
            ),
        )


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
