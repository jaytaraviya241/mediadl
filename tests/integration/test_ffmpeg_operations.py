"""FFmpeg operations against lavfi fixtures (spec sections 7.5, 9.4 Phase 5)."""
from __future__ import annotations

import pytest

from mediadl.errors import ProcessingError, ProcessingTimeoutError, ToolMissingError
from mediadl.processing import operations as ops
from mediadl.processing.ffmpeg_runner import FfmpegTools, frame_md5, run, stream_md5
from mediadl.processing.probe import probe
from mediadl.testing.media_fixtures import (
    make_audio_only,
    make_image,
    make_ts,
    make_video,
    make_video_only,
)

tools = FfmpegTools.try_discover()
pytestmark = pytest.mark.skipif(tools is None, reason='ffmpeg not installed')


async def test_merge_is_a_true_stream_copy(tmp_path):
    video = make_video_only(tools, tmp_path / 'v.mp4')
    audio = make_audio_only(tools, tmp_path / 'a.m4a')
    out = tmp_path / 'merged.mp4'

    step = await ops.merge(tools, video, audio, out)

    assert step.stream_copy is True
    assert step.operation == 'merge'
    # Same container family: the compressed packets must be byte-identical.
    assert await stream_md5(tools, video, '0:v:0') == await stream_md5(tools, out, '0:v:0')
    assert await stream_md5(tools, audio, '0:a:0') == await stream_md5(tools, out, '0:a:0')
    result = await probe(tools, out)
    assert result.video_codec == 'h264'
    assert result.audio_codec == 'aac'


async def test_remux_ts_to_mp4_preserves_decoded_frames(tmp_path):
    source = make_ts(tools, tmp_path / 's.ts')
    out = tmp_path / 'from_ts.mp4'

    step = await ops.remux(tools, source, out, adts_to_asc=True)

    assert step.stream_copy is True
    # Packet bytes legitimately change (Annex-B -> AVCC); frames must not.
    assert await frame_md5(tools, source) == await frame_md5(tools, out)
    assert (await probe(tools, out)).format_name.startswith('mov,mp4')


async def test_metadata_embed_keeps_streams_but_changes_file_bytes(tmp_path):
    import hashlib

    source = make_video(tools, tmp_path / 'in.mp4')
    out = tmp_path / 'tagged.mp4'

    step = await ops.embed_metadata(tools, source, out, {'comment': 'source=test'})

    assert step.stream_copy is True
    assert await stream_md5(tools, source, '0:v:0') == await stream_md5(tools, out, '0:v:0')
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    after = hashlib.sha256(out.read_bytes()).hexdigest()
    # This is exactly why metadata goes in a sidecar by default.
    assert before != after


async def test_transcode_reencodes_to_the_requested_codec(tmp_path):
    source = make_video(tools, tmp_path / 'in.mp4')
    out = tmp_path / 'compat.mp4'
    profile = ops.TranscodeProfile(
        video_codec='h264', audio_codec='aac', crf=30, preset='ultrafast', pix_fmt='yuv420p'
    )

    step = await ops.transcode(tools, source, out, profile)

    assert step.stream_copy is False
    result = await probe(tools, out)
    assert result.video_codec == 'h264'
    assert result.pix_fmt == 'yuv420p'
    assert await stream_md5(tools, source, '0:v:0') != await stream_md5(tools, out, '0:v:0')


async def test_transcode_never_upscales(tmp_path):
    source = make_video(tools, tmp_path / 'small.mp4', width=160, height=120)
    out = tmp_path / 'up.mp4'
    profile = ops.TranscodeProfile(
        video_codec='h264', crf=30, preset='ultrafast', max_height=1080
    )

    await ops.transcode(tools, source, out, profile)

    result = await probe(tools, out)
    assert result.height == 120, 'a 120px source must not be upscaled to the 1080 cap'


async def test_timeout_kills_the_process(tmp_path):
    source = make_video(tools, tmp_path / 'in.mp4', seconds=3)
    with pytest.raises(ProcessingTimeoutError):
        await run(
            [tools.ffmpeg, '-nostdin', '-v', 'error', '-re', '-i', str(source),
             '-c:v', 'libx264', '-preset', 'veryslow', '-y', str(tmp_path / 'slow.mp4')],
            timeout_s=0.4,
        )


async def test_truncated_input_fails_safely(tmp_path):
    source = make_video(tools, tmp_path / 'in.mp4')
    truncated = tmp_path / 'broken.mp4'
    truncated.write_bytes(source.read_bytes()[:200])
    with pytest.raises(ProcessingError):
        await probe(tools, truncated)


async def test_probe_reads_images(tmp_path):
    image = make_image(tools, tmp_path / 'still.png', width=640, height=480)
    result = await probe(tools, image)
    assert (result.width, result.height) == (640, 480)


async def test_missing_tool_raises_tool_missing():
    with pytest.raises(ToolMissingError):
        FfmpegTools.discover(configured_path='/nonexistent-directory-for-tests')


async def test_progress_events_are_parsed(tmp_path):
    source = make_video(tools, tmp_path / 'in.mp4')
    audio = make_audio_only(tools, tmp_path / 'a.m4a')
    events: list[tuple[str, str]] = []
    await ops.merge(
        tools, source, audio, tmp_path / 'out.mp4',
        on_progress=lambda k, v: events.append((k, v)),
    )
    assert any(key == 'progress' for key, _ in events)
    assert events[-1] == ('progress', 'end')
