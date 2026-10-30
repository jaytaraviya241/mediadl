"""FFmpeg operations (spec section 7.5).

Every operation records a ``ProcessingStep`` saying whether it was a stream
copy. COPY operations keep the compressed data byte-identical; the per-stream
MD5 check in the tests proves it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from mediadl.domain.records import ProcessingStep
from mediadl.errors import ProcessingError
from mediadl.net.redact import redact_argv
from mediadl.processing.ffmpeg_runner import LOCAL_PROTOCOLS, FfmpegTools, run


@dataclass(frozen=True, slots=True)
class TranscodeProfile:
    video_codec: str | None = None
    audio_codec: str | None = None
    crf: int = 20
    preset: str = 'medium'
    pix_fmt: str | None = 'yuv420p'
    tag: str | None = None
    audio_bitrate: str = '192k'
    max_height: int | None = None
    container: str = 'mp4'


_ENCODERS = {
    'h264': 'libx264',
    'hevc': 'libx265',
    'av1': 'libsvtav1',
    'vp9': 'libvpx-vp9',
    'aac': 'aac',
    'opus': 'libopus',
}


def _base(tools: FfmpegTools, *, progress: bool = False) -> list[str]:
    argv = [
        tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
        '-protocol_whitelist', LOCAL_PROTOCOLS,
    ]
    if progress:
        argv += ['-progress', 'pipe:1', '-nostats']
    return argv


async def _execute(
    argv: list[str], *, operation: str, stream_copy: bool, tools: FfmpegTools,
    inputs: tuple[Path, ...], output: Path, timeout_s: float, on_progress=None,
) -> ProcessingStep:
    started = time.monotonic()
    await run(argv, timeout_s=timeout_s, on_progress=on_progress)
    if not output.exists() or output.stat().st_size == 0:
        raise ProcessingError(f'{operation} produced no output', operation=operation)
    return ProcessingStep(
        operation=operation,  # type: ignore[arg-type]
        stream_copy=stream_copy,
        tool=f'ffmpeg {tools.version}'.strip(),
        argv_redacted=redact_argv(argv),
        input_sha256=(),
        output_sha256='',
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def merge(
    tools: FfmpegTools, video: Path, audio: Path, out: Path, *,
    timeout_s: float = 600, faststart: bool = True, on_progress=None,
) -> ProcessingStep:
    """COPY: mux a video-only and an audio-only file without re-encoding."""
    argv = _base(tools, progress=on_progress is not None) + [
        '-i', str(video), '-i', str(audio),
        '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy',
    ]
    if faststart and out.suffix.lower() in ('.mp4', '.m4v', '.mov', '.m4a'):
        argv += ['-movflags', '+faststart']
    argv += ['-y', str(out)]
    return await _execute(
        argv, operation='merge', stream_copy=True, tools=tools,
        inputs=(video, audio), output=out, timeout_s=timeout_s, on_progress=on_progress,
    )


async def remux(
    tools: FfmpegTools, src: Path, out: Path, *,
    timeout_s: float = 600, adts_to_asc: bool = False, on_progress=None,
) -> ProcessingStep:
    """COPY: change container only. ADTS AAC needs the bitstream filter."""
    argv = _base(tools, progress=on_progress is not None) + ['-i', str(src), '-map', '0', '-c', 'copy']
    if adts_to_asc:
        argv += ['-bsf:a', 'aac_adtstoasc']
    if out.suffix.lower() in ('.mp4', '.m4v', '.mov', '.m4a'):
        argv += ['-movflags', '+faststart']
    argv += ['-y', str(out)]
    return await _execute(
        argv, operation='remux', stream_copy=True, tools=tools,
        inputs=(src,), output=out, timeout_s=timeout_s, on_progress=on_progress,
    )


async def transcode(
    tools: FfmpegTools, src: Path, out: Path, profile: TranscodeProfile, *,
    timeout_s: float = 3600, on_progress=None,
) -> ProcessingStep:
    """RE-ENCODE: lossy. Only ever run because a policy asked for it."""
    argv = _base(tools, progress=on_progress is not None)
    argv += ['-i', str(src), '-map', '0:v:0?', '-map', '0:a:0?']

    if profile.video_codec:
        encoder = _ENCODERS.get(profile.video_codec, profile.video_codec)
        argv += ['-c:v', encoder, '-crf', str(profile.crf)]
        if encoder == 'libsvtav1':
            argv += ['-preset', str(profile.preset if str(profile.preset).isdigit() else 8)]
        else:
            argv += ['-preset', str(profile.preset)]
        if profile.pix_fmt:
            argv += ['-pix_fmt', profile.pix_fmt]
        if profile.tag:
            argv += ['-tag:v', profile.tag]
        if profile.max_height:
            # Never upscale: the min() keeps a smaller source untouched.
            argv += ['-vf', f'scale=-2:min({profile.max_height}\\,ih)']
    else:
        argv += ['-c:v', 'copy']

    if profile.audio_codec:
        argv += [
            '-c:a', _ENCODERS.get(profile.audio_codec, profile.audio_codec),
            '-b:a', profile.audio_bitrate,
        ]
    else:
        argv += ['-c:a', 'copy']

    if out.suffix.lower() in ('.mp4', '.m4v', '.mov'):
        argv += ['-movflags', '+faststart']
    argv += ['-y', str(out)]
    return await _execute(
        argv, operation='transcode', stream_copy=False, tools=tools,
        inputs=(src,), output=out, timeout_s=timeout_s, on_progress=on_progress,
    )


async def convert_image(
    tools: FfmpegTools, src: Path, out: Path, *, quality: int = 95, timeout_s: float = 120
) -> ProcessingStep:
    """RE-ENCODE: image format conversion for the compatibility policy."""
    argv = _base(tools) + ['-i', str(src)]
    if out.suffix.lower() in ('.jpg', '.jpeg'):
        # ffmpeg's -q:v runs 2 (best) .. 31 (worst); map a 0-100 quality onto it.
        qscale = max(2, min(31, round(31 - (quality / 100) * 29)))
        argv += ['-q:v', str(qscale)]
    argv += ['-y', str(out)]
    return await _execute(
        argv, operation='image_convert', stream_copy=False, tools=tools,
        inputs=(src,), output=out, timeout_s=timeout_s,
    )


async def embed_metadata(
    tools: FfmpegTools, src: Path, out: Path, metadata: dict[str, str], *, timeout_s: float = 300
) -> ProcessingStep:
    """COPY streams, but the file bytes still change; off by default."""
    argv = _base(tools) + ['-i', str(src), '-map', '0', '-c', 'copy']
    for key, value in metadata.items():
        argv += ['-metadata', f'{key}={value}']
    if out.suffix.lower() in ('.mp4', '.m4v', '.mov', '.m4a'):
        argv += ['-movflags', '+faststart']
    argv += ['-y', str(out)]
    return await _execute(
        argv, operation='metadata_embed', stream_copy=True, tools=tools,
        inputs=(src,), output=out, timeout_s=timeout_s,
    )


async def verify_decodable(tools: FfmpegTools, path: Path, *, timeout_s: float = 300) -> None:
    """Full decode with no output. CPU-heavy, so it is opt-in."""
    await run(
        _base(tools) + ['-i', str(path), '-f', 'null', '-'],
        timeout_s=timeout_s,
    )


async def preview_frame(
    tools: FfmpegTools, src: Path, out: Path, *, at_s: float = 1.0, timeout_s: float = 120
) -> ProcessingStep:
    argv = _base(tools) + ['-ss', str(at_s), '-i', str(src), '-frames:v', '1', '-q:v', '2', '-y', str(out)]
    return await _execute(
        argv, operation='image_convert', stream_copy=False, tools=tools,
        inputs=(src,), output=out, timeout_s=timeout_s,
    )
