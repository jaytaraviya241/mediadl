"""Optional ZIP packaging (spec section 7.9).

Media is already compressed, so entries are stored, not deflated. The
archive is built from finalized files only and renamed into place.
"""
from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path


def _build(root: Path, files: list[Path], destination: Path) -> Path:
    tmp = destination.with_suffix('.tmp')
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for file in files:
            if not file.exists() or file.is_symlink():
                continue
            archive.write(file, arcname=str(file.relative_to(root)))
    tmp.replace(destination)
    return destination


async def package(root: Path, files: list[Path], destination: Path) -> Path:
    return await asyncio.to_thread(_build, root, files, destination)
