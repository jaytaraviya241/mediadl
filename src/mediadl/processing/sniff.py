"""Magic-byte content detection (spec sections 6.7, 7.6, 12.8).

The extension we write comes from the bytes, never from the URL or from a
provider-supplied filename. A declared ``Content-Type`` is only a hint and is
cross-checked against the sniffed kind.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mediadl.domain.models import MediaKind

#: How many bytes the sniffer needs. ISO-BMFF puts ``ftyp`` at offset 4 and
#: the brand right after, so 32 is comfortably enough for every signature.
SNIFF_BYTES = 64


@dataclass(frozen=True, slots=True)
class Sniffed:
    kind: MediaKind | None
    ext: str | None
    mime: str | None

    @property
    def known(self) -> bool:
        return self.kind is not None


_ISO_BRANDS_VIDEO = (
    b'isom', b'iso2', b'iso4', b'iso5', b'iso6', b'mp41', b'mp42', b'avc1',
    b'dash', b'hev1', b'hvc1', b'av01', b'qt  ', b'3gp', b'M4V ', b'mmp4', b'msnv',
)
_ISO_BRANDS_AUDIO = (b'M4A ', b'M4B ', b'F4A ')
_ISO_BRANDS_IMAGE = {b'heic': ('heic', 'image/heic'), b'heix': ('heic', 'image/heic'),
                     b'mif1': ('heif', 'image/heif'), b'avif': ('avif', 'image/avif'),
                     b'avis': ('avif', 'image/avif')}


def detect_bytes(head: bytes) -> Sniffed:
    """Identify a media container from its leading bytes."""
    if len(head) < 4:
        return Sniffed(None, None, None)

    # --- images ---
    if head.startswith(b'\xff\xd8\xff'):
        return Sniffed(MediaKind.IMAGE, 'jpg', 'image/jpeg')
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return Sniffed(MediaKind.IMAGE, 'png', 'image/png')
    if head.startswith(b'GIF87a') or head.startswith(b'GIF89a'):
        return Sniffed(MediaKind.IMAGE, 'gif', 'image/gif')
    if head.startswith(b'BM'):
        return Sniffed(MediaKind.IMAGE, 'bmp', 'image/bmp')
    if head[:4] in (b'II*\x00', b'MM\x00*'):
        return Sniffed(MediaKind.IMAGE, 'tiff', 'image/tiff')
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return Sniffed(MediaKind.IMAGE, 'webp', 'image/webp')

    # --- ISO base media (mp4 / mov / m4a / heic / avif) ---
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in _ISO_BRANDS_IMAGE:
            ext, mime = _ISO_BRANDS_IMAGE[brand]
            return Sniffed(MediaKind.IMAGE, ext, mime)
        if brand in _ISO_BRANDS_AUDIO:
            return Sniffed(MediaKind.AUDIO, 'm4a', 'audio/mp4')
        if brand.startswith(b'qt'):
            return Sniffed(MediaKind.VIDEO, 'mov', 'video/quicktime')
        if brand.startswith(b'3gp'):
            return Sniffed(MediaKind.VIDEO, '3gp', 'video/3gpp')
        if brand in _ISO_BRANDS_VIDEO:
            return Sniffed(MediaKind.VIDEO, 'mp4', 'video/mp4')
        return Sniffed(MediaKind.VIDEO, 'mp4', 'video/mp4')  # unknown brand, still ISO-BMFF

    # --- matroska / webm ---
    if head.startswith(b'\x1a\x45\xdf\xa3'):
        ext = 'webm' if b'webm' in head[:SNIFF_BYTES] else 'mkv'
        return Sniffed(MediaKind.VIDEO, ext, 'video/webm' if ext == 'webm' else 'video/x-matroska')

    # --- audio ---
    if head.startswith(b'OggS'):
        if b'OpusHead' in head[:SNIFF_BYTES]:
            return Sniffed(MediaKind.AUDIO, 'opus', 'audio/opus')
        return Sniffed(MediaKind.AUDIO, 'ogg', 'audio/ogg')
    if head.startswith(b'fLaC'):
        return Sniffed(MediaKind.AUDIO, 'flac', 'audio/flac')
    if head.startswith(b'ID3') or (head[0] == 0xFF and (head[1] & 0xE6) == 0xE2):
        return Sniffed(MediaKind.AUDIO, 'mp3', 'audio/mpeg')
    if head.startswith(b'RIFF') and head[8:12] == b'WAVE':
        return Sniffed(MediaKind.AUDIO, 'wav', 'audio/wav')
    if head[:2] == b'\xff\xf1' or head[:2] == b'\xff\xf9':
        return Sniffed(MediaKind.AUDIO, 'aac', 'audio/aac')

    # --- MPEG-TS: 0x47 sync byte every 188 bytes ---
    if head[0] == 0x47 and (len(head) < 189 or head[188] == 0x47):
        return Sniffed(MediaKind.VIDEO, 'ts', 'video/mp2t')

    # --- AVI ---
    if head.startswith(b'RIFF') and head[8:12] == b'AVI ':
        return Sniffed(MediaKind.VIDEO, 'avi', 'video/x-msvideo')

    return Sniffed(None, None, None)


def detect(path: Path) -> Sniffed:
    with open(path, 'rb') as fh:
        return detect_bytes(fh.read(SNIFF_BYTES * 4))


def compatible(sniffed: Sniffed, kind: MediaKind) -> bool:
    """True when the bytes match the media kind the connector promised.

    A video asset may legitimately arrive as an audio-only container (a
    component of a pair), so audio is accepted for video; the reverse is not.
    """
    if sniffed.kind is None:
        return False
    if sniffed.kind is kind:
        return True
    return kind is MediaKind.VIDEO and sniffed.kind is MediaKind.AUDIO


def expected_families(kind: MediaKind) -> tuple[str, ...]:
    """Acceptable ``Content-Type`` prefixes for a kind."""
    if kind is MediaKind.IMAGE:
        return ('image/',)
    if kind is MediaKind.AUDIO:
        return ('audio/', 'application/octet-stream', 'binary/octet-stream')
    return ('video/', 'audio/', 'application/octet-stream', 'binary/octet-stream',
            'application/mp4', 'application/x-mpegurl', 'application/vnd.apple.mpegurl')
