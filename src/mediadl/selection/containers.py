"""Codec/container compatibility used when pairing video with audio (spec section 7.1).

A pair is only offered when both streams fit a container *without*
re-encoding. If nothing standard fits, MKV takes anything.
"""
from __future__ import annotations

from mediadl.domain.models import Representation

MP4_VIDEO = frozenset({'h264', 'avc1', 'avc', 'hevc', 'h265', 'hvc1', 'hev1', 'av01', 'av1', 'mpeg4'})
MP4_AUDIO = frozenset({'aac', 'mp4a', 'alac', 'ac3', 'ec3', 'mp3'})
WEBM_VIDEO = frozenset({'vp8', 'vp9', 'vp09', 'av01', 'av1'})
WEBM_AUDIO = frozenset({'opus', 'vorbis'})


def normalize_codec(codec: str | None) -> str | None:
    if not codec or codec in ('none', 'unknown'):
        return None
    base = codec.split('.')[0].lower().strip()
    return {
        'avc1': 'h264', 'avc': 'h264', 'h265': 'hevc', 'hvc1': 'hevc', 'hev1': 'hevc',
        'av01': 'av1', 'vp09': 'vp9', 'mp4a': 'aac',
    }.get(base, base)


def pair_container(video: Representation, audio: Representation) -> str | None:
    """Return the container that holds both streams with a stream copy."""
    v = normalize_codec(video.declared.video_codec)
    a = normalize_codec(audio.declared.audio_codec)
    if v is None:
        return None
    if (v in MP4_VIDEO or v is None) and (a is None or a in MP4_AUDIO):
        return 'mp4'
    if v in WEBM_VIDEO and (a is None or a in WEBM_AUDIO):
        return 'webm'
    return 'mkv'


def container_for_single(rep: Representation) -> str | None:
    declared = rep.declared
    if declared.container:
        return declared.container
    v = normalize_codec(declared.video_codec)
    a = normalize_codec(declared.audio_codec)
    if v and v in MP4_VIDEO and (a is None or a in MP4_AUDIO):
        return 'mp4'
    if v and v in WEBM_VIDEO:
        return 'webm'
    return None


#: Higher is better. Used only as a tie-breaker in source_preserving.
VIDEO_CODEC_TIER = {'av1': 5, 'vp9': 4, 'hevc': 3, 'h264': 2, 'vp8': 1, 'mpeg4': 0}
#: Efficiency ordering for the storage_efficient policy.
VIDEO_CODEC_EFFICIENCY = {'av1': 4, 'hevc': 3, 'vp9': 2, 'h264': 1, 'vp8': 0}
AUDIO_CODEC_TIER = {'flac': 6, 'alac': 6, 'wav': 5, 'opus': 4, 'aac': 3, 'vorbis': 2, 'mp3': 1}
LOSSLESS_AUDIO = frozenset({'flac', 'alac', 'wav', 'pcm_s16le', 'pcm_s24le'})

#: Image format fidelity: lossless beats lossy; modern lossy sits lowest
#: because it implies a re-encode somewhere upstream.
IMAGE_FIDELITY = {
    'png': 4, 'tiff': 4, 'bmp': 3, 'jpg': 2, 'jpeg': 2,
    'webp': 1, 'heic': 1, 'avif': 1, 'gif': 0,
}

#: Progressive HTTP is simplest and most resumable; segmented formats cost more.
PROTOCOL_SIMPLICITY = {'https': 3, 'file': 3, 'hls': 2, 'dash': 1}

DYNAMIC_RANGE_TIER = {'DV': 3, 'HDR10': 2, 'HLG': 1, 'SDR': 0}
