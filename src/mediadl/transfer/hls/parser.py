"""HLS playlist parsing, RFC 8216 subset (spec section 7.7).

Scope is deliberately narrow: unprotected video on demand. Live playlists
and every DRM key format are refused here, before a single segment is
fetched, rather than being partially downloaded and failing later.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from mediadl.errors import DrmError, InputError

#: Key formats that mean DRM. ``identity`` (or an absent KEYFORMAT) is the
#: only one this program will decrypt, and only with METHOD=AES-128.
DRM_KEYFORMATS = (
    'com.apple.streamingkeydelivery',   # FairPlay
    'com.microsoft.playready',          # PlayReady
    'urn:uuid:edef8ba9',                # Widevine
    'com.widevine',
)
DRM_METHODS = ('SAMPLE-AES', 'SAMPLE-AES-CTR', 'SAMPLE-AES-CENC')

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attributes(line: str) -> dict[str, str]:
    """Parse an ``#EXT-X-TAG:A=1,B="two"`` attribute list."""
    _, _, body = line.partition(':')
    return {
        key: value[1:-1] if value.startswith('"') and value.endswith('"') else value
        for key, value in _ATTR_RE.findall(body)
    }


@dataclass(frozen=True, slots=True)
class EncryptionKey:
    method: str
    uri: str | None = None
    iv: bytes | None = None
    key_format: str = 'identity'

    @property
    def encrypted(self) -> bool:
        return self.method.upper() != 'NONE'

    def check_supported(self) -> None:
        method = self.method.upper()
        if method == 'NONE':
            return
        if method in DRM_METHODS:
            raise DrmError(f'{method} is a DRM scheme', method=method)
        key_format = (self.key_format or 'identity').lower()
        if key_format != 'identity':
            raise DrmError('non-identity KEYFORMAT indicates DRM', key_format=self.key_format)
        if self.uri and self.uri.startswith('skd://'):
            raise DrmError('FairPlay key URI', key_format=self.key_format)
        if method != 'AES-128':
            raise DrmError(f'unsupported encryption method {method}', method=method)


@dataclass(frozen=True, slots=True)
class Segment:
    uri: str
    index: int
    duration: float = 0.0
    byte_range: tuple[int, int] | None = None   # (offset, length)
    key: EncryptionKey | None = None
    init_uri: str | None = None
    discontinuity: bool = False
    media_sequence: int = 0


@dataclass(frozen=True, slots=True)
class MediaPlaylist:
    url: str
    segments: tuple[Segment, ...]
    target_duration: float = 0.0
    is_vod: bool = True
    independent_init: str | None = None

    @property
    def encrypted(self) -> bool:
        return any(s.key and s.key.encrypted for s in self.segments)

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.segments)


@dataclass(frozen=True, slots=True)
class Variant:
    uri: str
    bandwidth: int | None = None
    average_bandwidth: int | None = None
    resolution: tuple[int, int] | None = None
    codecs: tuple[str, ...] = ()
    frame_rate: float | None = None
    audio_group: str | None = None

    @property
    def video_codec(self) -> str | None:
        for codec in self.codecs:
            if codec.startswith(('avc', 'hvc', 'hev', 'av01', 'vp0', 'dvh')):
                return codec
        return None

    @property
    def audio_codec(self) -> str | None:
        for codec in self.codecs:
            if codec.startswith(('mp4a', 'ac-3', 'ec-3', 'opus', 'fLaC')):
                return codec
        return None


@dataclass(frozen=True, slots=True)
class Rendition:
    type: str
    group_id: str
    name: str
    uri: str | None = None
    default: bool = False
    language: str | None = None


@dataclass(frozen=True, slots=True)
class MasterPlaylist:
    url: str
    variants: tuple[Variant, ...] = ()
    audio_renditions: tuple[Rendition, ...] = field(default=())

    def audio_for(self, variant: Variant) -> Rendition | None:
        if not variant.audio_group:
            return None
        candidates = [r for r in self.audio_renditions if r.group_id == variant.audio_group and r.uri]
        if not candidates:
            return None
        for rendition in candidates:
            if rendition.default:
                return rendition
        return candidates[0]


def is_master(text: str) -> bool:
    return '#EXT-X-STREAM-INF' in text


def _require_m3u8(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith('#EXTM3U'):
        raise InputError('not an M3U8 playlist (missing #EXTM3U)')
    return lines


def parse_master(text: str, base_url: str) -> MasterPlaylist:
    lines = _require_m3u8(text)
    variants: list[Variant] = []
    renditions: list[Rendition] = []
    pending: dict[str, str] | None = None

    for line in lines:
        if line.startswith('#EXT-X-MEDIA:'):
            attrs = parse_attributes(line)
            renditions.append(
                Rendition(
                    type=attrs.get('TYPE', ''),
                    group_id=attrs.get('GROUP-ID', ''),
                    name=attrs.get('NAME', ''),
                    uri=urllib.parse.urljoin(base_url, attrs['URI']) if attrs.get('URI') else None,
                    default=attrs.get('DEFAULT', 'NO').upper() == 'YES',
                    language=attrs.get('LANGUAGE'),
                )
            )
        elif line.startswith('#EXT-X-SESSION-KEY:'):
            key = _key_from(parse_attributes(line), base_url)
            key.check_supported()  # a DRM session key fails the whole playlist
        elif line.startswith('#EXT-X-STREAM-INF:'):
            pending = parse_attributes(line)
        elif not line.startswith('#') and pending is not None:
            resolution = None
            if raw := pending.get('RESOLUTION'):
                try:
                    width, _, height = raw.partition('x')
                    resolution = (int(width), int(height))
                except ValueError:
                    resolution = None
            variants.append(
                Variant(
                    uri=urllib.parse.urljoin(base_url, line),
                    bandwidth=_int(pending.get('BANDWIDTH')),
                    average_bandwidth=_int(pending.get('AVERAGE-BANDWIDTH')),
                    resolution=resolution,
                    codecs=tuple(c.strip() for c in pending.get('CODECS', '').split(',') if c.strip()),
                    frame_rate=_float(pending.get('FRAME-RATE')),
                    audio_group=pending.get('AUDIO'),
                )
            )
            pending = None

    if not variants:
        raise InputError('master playlist has no variants')
    return MasterPlaylist(
        url=base_url,
        variants=tuple(variants),
        audio_renditions=tuple(r for r in renditions if r.type.upper() == 'AUDIO'),
    )


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    lines = _require_m3u8(text)
    if '#EXT-X-ENDLIST' not in text and 'VOD' not in text:
        raise InputError('live playlists are out of scope (no #EXT-X-ENDLIST)')

    segments: list[Segment] = []
    current_key: EncryptionKey | None = None
    init_uri: str | None = None
    duration = 0.0
    byte_range: tuple[int, int] | None = None
    discontinuity = False
    target_duration = 0.0
    media_sequence = 0
    last_end: int | None = None
    index = 0

    for line in lines:
        if line.startswith('#EXT-X-TARGETDURATION:'):
            target_duration = _float(line.split(':', 1)[1]) or 0.0
        elif line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            media_sequence = _int(line.split(':', 1)[1]) or 0
        elif line.startswith('#EXT-X-KEY:'):
            current_key = _key_from(parse_attributes(line), base_url)
            current_key.check_supported()
        elif line.startswith('#EXT-X-MAP:'):
            attrs = parse_attributes(line)
            if attrs.get('URI'):
                init_uri = urllib.parse.urljoin(base_url, attrs['URI'])
        elif line.startswith('#EXTINF:'):
            duration = _float(line.split(':', 1)[1].split(',')[0]) or 0.0
        elif line.startswith('#EXT-X-BYTERANGE:'):
            spec = line.split(':', 1)[1]
            length_text, _, offset_text = spec.partition('@')
            length = _int(length_text) or 0
            offset = _int(offset_text) if offset_text else (last_end or 0)
            byte_range = (offset or 0, length)
        elif line.startswith('#EXT-X-DISCONTINUITY'):
            discontinuity = True
        elif not line.startswith('#'):
            segments.append(
                Segment(
                    uri=urllib.parse.urljoin(base_url, line),
                    index=index,
                    duration=duration,
                    byte_range=byte_range,
                    key=current_key,
                    init_uri=init_uri,
                    discontinuity=discontinuity,
                    media_sequence=media_sequence + index,
                )
            )
            if byte_range:
                last_end = byte_range[0] + byte_range[1]
            index += 1
            duration, byte_range, discontinuity = 0.0, None, False

    if not segments:
        raise InputError('media playlist has no segments')
    return MediaPlaylist(
        url=base_url,
        segments=tuple(segments),
        target_duration=target_duration,
        is_vod=True,
        independent_init=init_uri,
    )


def _key_from(attrs: dict[str, str], base_url: str) -> EncryptionKey:
    iv_text = attrs.get('IV')
    iv = None
    if iv_text:
        try:
            iv = bytes.fromhex(iv_text.lower().removeprefix('0x'))
        except ValueError:
            iv = None
    uri = attrs.get('URI')
    return EncryptionKey(
        method=attrs.get('METHOD', 'NONE'),
        uri=urllib.parse.urljoin(base_url, uri) if uri and not uri.startswith('skd://') else uri,
        iv=iv,
        key_format=attrs.get('KEYFORMAT', 'identity'),
    )


def default_iv(media_sequence: int) -> bytes:
    """RFC 8216 section 5.2: the sequence number is the implicit IV."""
    return media_sequence.to_bytes(16, 'big')


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
