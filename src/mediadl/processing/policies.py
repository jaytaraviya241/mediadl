"""Output policy planning (spec section 7.2).

source_preserving keeps the retrieved bytes untouched. The other two policies
only ever act on *measured* properties, and never upscale or enlarge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mediadl.config import CompatiblePolicyConfig, EfficientPolicyConfig, PoliciesConfig
from mediadl.domain.models import MediaKind
from mediadl.domain.records import ProbeResult
from mediadl.processing.operations import TranscodeProfile
from mediadl.selection.containers import normalize_codec

Plan = Literal['keep'] | TranscodeProfile

#: Codecs and pixel formats that play essentially everywhere.
COMPATIBLE_VIDEO = frozenset({'h264'})
COMPATIBLE_AUDIO = frozenset({'aac', 'mp3', None})
COMPATIBLE_PIX_FMT = frozenset({'yuv420p', 'yuvj420p'})
COMPATIBLE_CONTAINERS = ('mov,mp4', 'mp4')
EFFICIENT_VIDEO = frozenset({'hevc', 'av1', 'vp9'})


@dataclass(frozen=True, slots=True)
class ProcessingPolicy:
    name: str = 'source_preserving'
    keep_source: bool = True
    config: PoliciesConfig | None = None
    embed_metadata: bool = False

    @property
    def compatible(self) -> CompatiblePolicyConfig:
        return (self.config or PoliciesConfig()).broadly_compatible

    @property
    def efficient(self) -> EfficientPolicyConfig:
        return (self.config or PoliciesConfig()).storage_efficient


def plan(policy: ProcessingPolicy, probe: ProbeResult, kind: MediaKind) -> Plan:
    """Decide whether the downloaded file needs re-encoding under this policy."""
    if policy.name == 'source_preserving':
        return 'keep'
    if kind is MediaKind.IMAGE:
        return _plan_image(policy, probe)
    if policy.name == 'broadly_compatible':
        return _plan_compatible(policy, probe, kind)
    return _plan_efficient(policy, probe, kind)


def _plan_image(policy: ProcessingPolicy, probe: ProbeResult) -> Plan:
    if policy.name == 'broadly_compatible':
        fmt = (probe.format_name or '').lower()
        if 'png' in fmt or 'jpeg' in fmt or 'jpg' in fmt or 'image2' in fmt:
            return 'keep'
        return TranscodeProfile(container='jpg', crf=0)
    # storage_efficient keeps images as-is unless conversion is opted into,
    # because re-encoding a photo trades real quality for uncertain savings.
    return 'keep'


def _plan_compatible(policy: ProcessingPolicy, probe: ProbeResult, kind: MediaKind) -> Plan:
    cfg = policy.compatible
    if kind is MediaKind.AUDIO:
        if normalize_codec(probe.audio_codec) == 'aac':
            return 'keep'
        return TranscodeProfile(
            video_codec=None, audio_codec=cfg.audio.codec,
            audio_bitrate=cfg.audio.bitrate, container='m4a',
        )

    video_ok = normalize_codec(probe.video_codec) in COMPATIBLE_VIDEO
    audio_ok = normalize_codec(probe.audio_codec) in COMPATIBLE_AUDIO
    pix_ok = probe.pix_fmt in COMPATIBLE_PIX_FMT or probe.pix_fmt is None
    container_ok = (probe.format_name or '').startswith(COMPATIBLE_CONTAINERS)
    height_ok = probe.height is None or probe.height <= cfg.max_height

    if video_ok and audio_ok and pix_ok and height_ok:
        # Already compatible: at most a container change, which is a copy.
        return 'keep' if container_ok else TranscodeProfile(
            video_codec=None, audio_codec=None, container='mp4'
        )
    return TranscodeProfile(
        video_codec=cfg.video.codec if not (video_ok and pix_ok and height_ok) else None,
        audio_codec=cfg.audio.codec if not audio_ok else None,
        crf=cfg.video.crf,
        preset=cfg.video.preset,
        pix_fmt=cfg.video.pix_fmt,
        audio_bitrate=cfg.audio.bitrate,
        max_height=cfg.max_height,
        container='mp4',
    )


def _plan_efficient(policy: ProcessingPolicy, probe: ProbeResult, kind: MediaKind) -> Plan:
    cfg = policy.efficient
    if kind is MediaKind.AUDIO:
        if normalize_codec(probe.audio_codec) == 'opus':
            return 'keep'
        return TranscodeProfile(
            video_codec=None, audio_codec=cfg.audio.codec,
            audio_bitrate=cfg.audio.bitrate, container='opus',
        )
    if normalize_codec(probe.video_codec) in EFFICIENT_VIDEO:
        return 'keep'
    return TranscodeProfile(
        video_codec=cfg.video.codec,
        audio_codec=None,  # keep the audio; video dominates the size
        crf=cfg.video.crf,
        preset=cfg.video.preset,
        pix_fmt=cfg.video.pix_fmt,
        tag=cfg.video.tag,
        container='mp4',
    )


def keeps_smaller_output(policy: ProcessingPolicy) -> bool:
    """storage_efficient must never produce a file larger than the source."""
    return policy.name == 'storage_efficient'


def required_savings_ratio(policy: ProcessingPolicy) -> float:
    return policy.efficient.require_savings_ratio if policy.name == 'storage_efficient' else 0.0
