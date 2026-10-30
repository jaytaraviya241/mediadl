"""Post-download verification (spec section 7.1).

Declared properties come from the provider and are often absent or wrong.
What we report is what we measured.
"""
from __future__ import annotations

from pathlib import Path

from mediadl.domain.models import MediaKind, Representation
from mediadl.domain.records import ProbeResult
from mediadl.errors import MaliciousInputError
from mediadl.obs.logging import get_logger
from mediadl.processing.ffmpeg_runner import FfmpegTools, probe_json

log = get_logger(__name__)


async def probe(tools: FfmpegTools, path: Path, *, timeout_s: float = 30) -> ProbeResult:
    data = await probe_json(tools, path, timeout_s=timeout_s)
    fmt = data.get('format', {}) or {}
    streams = tuple(data.get('streams', ()) or ())
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    duration = fmt.get('duration')
    size = fmt.get('size')
    return ProbeResult(
        format_name=str(fmt.get('format_name', '')),
        duration_s=float(duration) if duration not in (None, 'N/A') else None,
        streams=streams,
        width=int(video['width']) if video and video.get('width') else None,
        height=int(video['height']) if video and video.get('height') else None,
        video_codec=str(video['codec_name']) if video and video.get('codec_name') else None,
        audio_codec=str(audio['codec_name']) if audio and audio.get('codec_name') else None,
        pix_fmt=str(video['pix_fmt']) if video and video.get('pix_fmt') else None,
        size_bytes=int(size) if size not in (None, 'N/A') else path.stat().st_size,
        tool_version=f'ffprobe {tools.version}'.strip(),
    )


def check_image_bomb(path: Path, *, max_pixels: int) -> None:
    """Refuse decompression bombs before any decode happens.

    Pillow is optional; when it is missing the ffprobe dimensions still apply
    the same cap, this is just the earlier and cheaper check.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - optional dependency
        return
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as img:
                width, height = img.size
        if width * height > max_pixels:
            raise MaliciousInputError(
                'image exceeds the pixel limit', pixels=width * height, limit=max_pixels
            )
    except MaliciousInputError:
        raise
    except Image.DecompressionBombError as exc:
        raise MaliciousInputError('image is a decompression bomb') from exc
    except Exception as exc:
        if 'DecompressionBomb' in type(exc).__name__:
            raise MaliciousInputError('image is a decompression bomb') from exc
        log.debug('image_probe_skipped', error=type(exc).__name__)
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def compare_declared(declared: Representation, measured: ProbeResult) -> tuple[str, ...]:
    """Report declared-vs-probed mismatches. A warning, never a failure."""
    notes: list[str] = []
    d = declared.declared
    if d.width and measured.width and d.width != measured.width:
        notes.append(f'declared width {d.width} != probed {measured.width}')
    if d.height and measured.height and d.height != measured.height:
        notes.append(f'declared height {d.height} != probed {measured.height}')
    if d.video_codec and measured.video_codec:
        from mediadl.selection.containers import normalize_codec

        if normalize_codec(d.video_codec) != normalize_codec(measured.video_codec):
            notes.append(f'declared vcodec {d.video_codec} != probed {measured.video_codec}')
    return tuple(notes)


def kind_of(probe_result: ProbeResult) -> MediaKind:
    has_video = any(s.get('codec_type') == 'video' for s in probe_result.streams)
    has_audio = any(s.get('codec_type') == 'audio' for s in probe_result.streams)
    still = probe_result.format_name in ('image2', 'png_pipe', 'jpeg_pipe', 'webp_pipe', 'gif')
    if has_video and not still:
        return MediaKind.VIDEO
    if has_audio and not has_video:
        return MediaKind.AUDIO
    return MediaKind.IMAGE
