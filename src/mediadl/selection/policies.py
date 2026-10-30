"""Sort keys for the three output policies (spec section 7.1).

Each key returns a tuple compared lexicographically, descending. ``None``
sorts last, so unknown values never beat known ones.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from mediadl.domain.models import MediaKind, Representation
from mediadl.selection.containers import (
    AUDIO_CODEC_TIER,
    DYNAMIC_RANGE_TIER,
    IMAGE_FIDELITY,
    LOSSLESS_AUDIO,
    PROTOCOL_SIMPLICITY,
    VIDEO_CODEC_EFFICIENCY,
    VIDEO_CODEC_TIER,
    normalize_codec,
)

PolicyName = Literal['source_preserving', 'broadly_compatible', 'storage_efficient']


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    name: PolicyName = 'source_preserving'
    max_height: int | None = None
    min_short_side: int | None = None
    max_bytes: int | None = None
    allow_transcode: bool = False


@dataclass(frozen=True, slots=True)
class Option:
    """A playable candidate: one complete representation, or a V+A pair."""

    primary: Representation
    audio: Representation | None = None
    container: str | None = None

    @property
    def is_pair(self) -> bool:
        return self.audio is not None


def _num(value: float | int | None) -> float:
    """None sorts last in a descending comparison."""
    return float(value) if value is not None else float('-inf')


def _short_side(rep: Representation) -> float:
    w, h = rep.declared.width, rep.declared.height
    if w and h:
        return float(min(w, h))
    return _num(h or w)


def _pixels(rep: Representation) -> float:
    w, h = rep.declared.width, rep.declared.height
    if w and h:
        return float(w * h)
    return float('-inf')


def _audio_of(option: Option) -> Representation:
    return option.audio or option.primary


def _needs_no_transcode(option: Option) -> float:
    """H.264 High/8-bit/SDR + AAC in an MP4-compatible container."""
    d = option.primary.declared
    v = normalize_codec(d.video_codec)
    a = normalize_codec(_audio_of(option).declared.audio_codec)
    ok = (
        v == 'h264'
        and (d.bit_depth in (None, 8))
        and (d.dynamic_range in (None, 'SDR'))
        and (a in (None, 'aac', 'mp3'))
    )
    return 1.0 if ok else 0.0


def video_source_preserving(option: Option) -> tuple[float, ...]:
    rep, audio = option.primary, _audio_of(option)
    d, ad = rep.declared, audio.declared
    return (
        1.0 if d.has_video is not False else 0.0,
        1.0 if (ad.has_audio or option.is_pair) else 0.0,
        _short_side(rep),
        _pixels(rep),
        _num(d.fps),
        float(DYNAMIC_RANGE_TIER.get(d.dynamic_range or 'SDR', 0)),
        _num(d.bit_depth),
        _num(d.bitrate_kbps),
        float(VIDEO_CODEC_TIER.get(normalize_codec(d.video_codec) or '', -1)),
        _num(ad.audio_channels),
        _num(ad.sample_rate_hz),
        float(PROTOCOL_SIMPLICITY.get(rep.protocol, 0)),
        # At equal quality a single complete file beats a pair: one transfer,
        # no merge step, and nothing to half-fail.
        0.0 if option.is_pair else 1.0,
        float(rep.source_preference),
    )


def video_broadly_compatible(option: Option, policy: SelectionPolicy) -> tuple[float, ...]:
    rep = option.primary
    height = rep.declared.height
    max_h = policy.max_height or 1080
    within = 1.0 if (height is None or height <= max_h) else 0.0
    return (_needs_no_transcode(option), within, *video_source_preserving(option))


def video_storage_efficient(option: Option, policy: SelectionPolicy) -> tuple[float, ...]:
    rep = option.primary
    floor = policy.min_short_side or 720
    short = _short_side(rep)
    meets_floor = 1.0 if (short == float('-inf') or short >= floor) else 0.0
    efficiency = float(VIDEO_CODEC_EFFICIENCY.get(normalize_codec(rep.declared.video_codec) or '', -1))
    size = rep.declared.filesize_bytes
    smaller_first = -float(size) if size else 0.0
    bitrate = rep.declared.bitrate_kbps
    return (
        meets_floor,
        efficiency,
        smaller_first,
        -_num(bitrate) if bitrate else 0.0,
        *video_source_preserving(option),
    )


def image_source_preserving(option: Option) -> tuple[float, ...]:
    rep = option.primary
    ext = (rep.declared.container or rep.declared.mime_type or '').split('/')[-1].lower()
    return (
        _pixels(rep),
        _num(rep.declared.filesize_bytes),
        float(IMAGE_FIDELITY.get(ext, 0)),
        float(rep.source_preference),
    )


def image_broadly_compatible(option: Option, policy: SelectionPolicy) -> tuple[float, ...]:
    rep = option.primary
    ext = (rep.declared.container or rep.declared.mime_type or '').split('/')[-1].lower()
    return (1.0 if ext in ('jpg', 'jpeg', 'png') else 0.0, *image_source_preserving(option))


def image_storage_efficient(option: Option, policy: SelectionPolicy) -> tuple[float, ...]:
    rep = option.primary
    floor = policy.min_short_side or 720
    short = _short_side(rep)
    meets_floor = 1.0 if (short == float('-inf') or short >= floor) else 0.0
    size = rep.declared.filesize_bytes
    return (meets_floor, -float(size) if size else 0.0, _pixels(rep), float(rep.source_preference))


def audio_source_preserving(option: Option) -> tuple[float, ...]:
    d = option.primary.declared
    codec = normalize_codec(d.audio_codec) or ''
    return (
        1.0 if codec in LOSSLESS_AUDIO else 0.0,
        float(AUDIO_CODEC_TIER.get(codec, 0)),
        _num(d.bitrate_kbps),
        _num(d.audio_channels),
        _num(d.sample_rate_hz),
        float(option.primary.source_preference),
    )


def audio_broadly_compatible(option: Option, policy: SelectionPolicy) -> tuple[float, ...]:
    codec = normalize_codec(option.primary.declared.audio_codec) or ''
    return (1.0 if codec == 'aac' else 0.0, *audio_source_preserving(option))


def audio_storage_efficient(option: Option, policy: SelectionPolicy) -> tuple[float, ...]:
    d = option.primary.declared
    codec = normalize_codec(d.audio_codec) or ''
    bitrate = d.bitrate_kbps
    floor = 128.0
    meets = 1.0 if (bitrate is None or bitrate >= floor) else 0.0
    return (1.0 if codec == 'opus' else 0.0, meets, -_num(bitrate) if bitrate else 0.0,
            *audio_source_preserving(option))


def sort_key(
    policy: SelectionPolicy, kind: MediaKind
) -> Callable[[Option], tuple[float, ...]]:
    """Return the ranking function for (policy, kind)."""
    table = {
        ('source_preserving', MediaKind.VIDEO): lambda o: video_source_preserving(o),
        ('source_preserving', MediaKind.IMAGE): lambda o: image_source_preserving(o),
        ('source_preserving', MediaKind.AUDIO): lambda o: audio_source_preserving(o),
        ('broadly_compatible', MediaKind.VIDEO): lambda o: video_broadly_compatible(o, policy),
        ('broadly_compatible', MediaKind.IMAGE): lambda o: image_broadly_compatible(o, policy),
        ('broadly_compatible', MediaKind.AUDIO): lambda o: audio_broadly_compatible(o, policy),
        ('storage_efficient', MediaKind.VIDEO): lambda o: video_storage_efficient(o, policy),
        ('storage_efficient', MediaKind.IMAGE): lambda o: image_storage_efficient(o, policy),
        ('storage_efficient', MediaKind.AUDIO): lambda o: audio_storage_efficient(o, policy),
    }
    return table[(policy.name, kind)]
