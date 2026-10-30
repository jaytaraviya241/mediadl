"""Synthetic media fixtures generated with FFmpeg's lavfi sources.

Deterministic, tiny, and offline: no test ever downloads a real file.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from mediadl.processing.ffmpeg_runner import FfmpegTools


def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True, capture_output=True, timeout=300)


def make_video(
    tools: FfmpegTools, out: Path, *, seconds: int = 2, width: int = 320, height: int = 240,
    video_codec: str = 'libx264', audio: bool = True, fps: int = 25,
) -> Path:
    argv = [
        tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', f'testsrc2=size={width}x{height}:rate={fps}:duration={seconds}',
    ]
    if audio:
        argv += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}']
    argv += ['-c:v', video_codec, '-pix_fmt', 'yuv420p', '-preset', 'ultrafast', '-crf', '30']
    if audio:
        argv += ['-c:a', 'aac', '-b:a', '64k', '-shortest']
    argv += ['-y', str(out)]
    _run(argv)
    return out


def make_video_only(tools: FfmpegTools, out: Path, **kwargs) -> Path:
    return make_video(tools, out, audio=False, **kwargs)


def make_audio_only(tools: FfmpegTools, out: Path, *, seconds: int = 2) -> Path:
    _run([
        tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
        '-c:a', 'aac', '-b:a', '64k', '-y', str(out),
    ])
    return out


def make_image(tools: FfmpegTools, out: Path, *, width: int = 320, height: int = 240) -> Path:
    _run([
        tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', f'testsrc2=size={width}x{height}:rate=1:duration=1',
        '-frames:v', '1', '-y', str(out),
    ])
    return out


def make_ts(tools: FfmpegTools, out: Path, *, seconds: int = 2) -> Path:
    _run([
        tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', f'testsrc2=size=320x240:rate=25:duration={seconds}',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '64k', '-shortest', '-f', 'mpegts', '-y', str(out),
    ])
    return out


def make_hls(tools: FfmpegTools, directory: Path, *, seconds: int = 4, name: str = 'index') -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    playlist = directory / f'{name}.m3u8'
    _run([
        tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', f'testsrc2=size=320x240:rate=25:duration={seconds}',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '64k', '-shortest',
        '-g', '25', '-keyint_min', '25', '-sc_threshold', '0',
        '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod',
        '-hls_segment_filename', str(directory / f'{name}%d.ts'),
        '-y', str(playlist),
    ])
    return playlist


def make_hls_fmp4(tools: FfmpegTools, directory: Path, *, seconds: int = 4) -> Path:
    """HLS with fragmented-MP4 segments, which exercises EXT-X-MAP."""
    directory.mkdir(parents=True, exist_ok=True)
    playlist = directory / 'index.m3u8'
    _run([
        tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', f'testsrc2=size=320x240:rate=25:duration={seconds}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30', '-pix_fmt', 'yuv420p',
        '-g', '25', '-keyint_min', '25', '-sc_threshold', '0',
        '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod',
        '-hls_segment_type', 'fmp4',
        '-hls_fmp4_init_filename', 'init.mp4',
        '-hls_segment_filename', str(directory / 'seg%d.m4s'),
        '-y', str(playlist),
    ])
    return playlist


def make_hls_aes128(tools: FfmpegTools, directory: Path, *, seconds: int = 4) -> Path:
    """AES-128 encrypted HLS with a local key file (identity key format)."""
    import os

    directory.mkdir(parents=True, exist_ok=True)
    key = directory / 'enc.key'
    key.write_bytes(os.urandom(16))
    keyinfo = directory / 'enc.keyinfo'
    keyinfo.write_text(f'enc.key\n{key}\n')
    playlist = directory / 'index.m3u8'
    _run([
        tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', f'testsrc2=size=320x240:rate=25:duration={seconds}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30', '-pix_fmt', 'yuv420p',
        '-g', '25', '-keyint_min', '25', '-sc_threshold', '0',
        '-f', 'hls', '-hls_time', '1', '-hls_playlist_type', 'vod',
        '-hls_key_info_file', str(keyinfo),
        '-hls_segment_filename', str(directory / 'seg%d.ts'),
        '-y', str(playlist),
    ])
    keyinfo.unlink()
    return playlist


def make_dash(tools: FfmpegTools, directory: Path, *, seconds: int = 4) -> Path:
    """DASH with SegmentTemplate addressing."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'index.mpd'
    _run([
        tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', f'testsrc2=size=320x240:rate=25:duration={seconds}',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '64k', '-shortest',
        '-g', '25', '-keyint_min', '25', '-sc_threshold', '0',
        '-f', 'dash', '-seg_duration', '1', '-use_template', '1', '-use_timeline', '0',
        '-y', str(manifest),
    ])
    return manifest


async def amake_video(tools: FfmpegTools, out: Path, **kwargs) -> Path:
    return await asyncio.to_thread(make_video, tools, out, **kwargs)
