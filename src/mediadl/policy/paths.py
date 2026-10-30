"""Filesystem-safety policy (spec sections 6.7, 7.9)."""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from mediadl.errors import PathRejectedError

_WINDOWS_RESERVED = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL']
    + [f'COM{i}' for i in range(1, 10)]
    + [f'LPT{i}' for i in range(1, 10)]
)
_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_WHITESPACE = re.compile(r'\s+')

MAX_COMPONENT_BYTES = 80
MAX_PATH_BYTES = 240

#: Extensions we are willing to create, per media kind. Mirrors yt-dlp's
#: `_UnsafeExtensionError` allowlist idea (GHSA-79w7-vh3h-8g4j).
SAFE_EXTENSIONS: dict[str, frozenset[str]] = {
    'image': frozenset({'jpg', 'jpeg', 'png', 'webp', 'gif', 'heic', 'heif', 'avif', 'bmp', 'tiff'}),
    'video': frozenset({'mp4', 'm4v', 'mov', 'webm', 'mkv', 'ts', 'avi', '3gp'}),
    'audio': frozenset({'m4a', 'mp3', 'opus', 'ogg', 'flac', 'wav', 'aac'}),
}
ALL_SAFE_EXTENSIONS = frozenset().union(*SAFE_EXTENSIONS.values())


def sanitize_component(value: str, *, max_bytes: int = MAX_COMPONENT_BYTES, fallback: str = '_') -> str:
    """Make one path component safe on POSIX and Windows.

    Provider-supplied text never reaches a path unsanitized, and captions are
    excluded upstream regardless (privacy, length, injection).
    """
    if value is None:
        return fallback
    text = unicodedata.normalize('NFC', str(value))
    text = _ILLEGAL.sub('_', text)
    text = _WHITESPACE.sub('_', text).strip('._- ')
    while '..' in text:
        text = text.replace('..', '_')
    if not text:
        return fallback
    stem = text.split('.')[0].upper()
    if stem in _WINDOWS_RESERVED:
        text = f'{text}_'
    encoded = text.encode('utf-8')
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode('utf-8', 'ignore').rstrip('._- ') or fallback
    return text or fallback


def validate_extension(ext: str, kind: str | None = None) -> str:
    ext = ext.lower().lstrip('.')
    if not ext or not ext.isalnum():
        raise PathRejectedError(f'unsafe file extension {ext!r}', extension=ext)
    allowed = SAFE_EXTENSIONS.get(kind or '', ALL_SAFE_EXTENSIONS)
    if ext not in allowed:
        raise PathRejectedError(f'extension {ext!r} is not allowed for kind {kind!r}', extension=ext)
    return ext


def enforce_under_root(candidate: Path | str, root: Path | str) -> Path:
    """Resolve and require containment; reject symlinked parents."""
    root = Path(root).expanduser().resolve()
    candidate = Path(candidate)
    combined = candidate if candidate.is_absolute() else root / candidate
    resolved = Path(os.path.normpath(combined)).expanduser()
    try:
        final = resolved.resolve()
    except OSError as exc:
        raise PathRejectedError(f'cannot resolve output path: {exc}') from exc
    if not final.is_relative_to(root):
        raise PathRejectedError(
            'output path escapes the configured root', root=str(root), path=str(final)
        )
    for parent in list(final.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise PathRejectedError('output path traverses a symlink', path=str(parent))
    if len(str(final).encode('utf-8')) > MAX_PATH_BYTES + len(str(root).encode('utf-8')):
        raise PathRejectedError('output path is too long', path=str(final))
    return final


def open_exclusive(path: Path) -> int:
    """Create a file, failing if it exists and refusing to follow a symlink."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, 0o600)
