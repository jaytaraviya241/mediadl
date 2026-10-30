"""Native HLS engine against FFmpeg-generated streams (spec section 7.7, Phase 7b)."""
from __future__ import annotations

import json

import pytest

from mediadl.domain.models import HlsLocator, MediaKind, Representation, RepresentationRole
from mediadl.errors import DrmError, InputError, RetriesExhaustedError, SizeLimitError
from mediadl.processing.ffmpeg_runner import FfmpegTools, frame_md5
from mediadl.processing.probe import probe
from mediadl.testing.media_fixtures import make_hls, make_hls_aes128, make_hls_fmp4
from mediadl.transfer.base import CancelToken, ProgressReporter, TransferLimits, TransferPlan
from mediadl.transfer.hls.engine import HlsTransfer

tools = FfmpegTools.try_discover()
pytestmark = pytest.mark.skipif(tools is None, reason='ffmpeg not installed')


def make_plan(url, dest, *, max_bytes=50 * 1024 * 1024):
    rep = Representation(
        rep_id='hls', kind=MediaKind.VIDEO, role=RepresentationRole.PRIMARY,
        protocol='hls', locator=HlsLocator(playlist_url=url),
    )
    return TransferPlan(
        rep=rep, dest=dest,
        limits=TransferLimits(max_bytes=max_bytes, chunk_size=65536),
        expected_kind=MediaKind.VIDEO,
    )


async def test_ts_stream_assembles_losslessly(server, client, tmp_path):
    source_dir = tmp_path / 'src'
    make_hls(tools, source_dir, seconds=4)
    server.add_directory('/hls', source_dir)
    plan = make_plan(server.url('/hls/index.m3u8'), tmp_path / 'out.mp4')

    result = await HlsTransfer(client, tools).transfer(plan, ProgressReporter(), CancelToken())

    assert result.bytes_written > 0
    measured = await probe(tools, result.path)
    assert measured.video_codec == 'h264'
    assert measured.duration_s == pytest.approx(4.0, abs=0.5)
    # The assembled stream must decode to the same frames as the source.
    assert await frame_md5(tools, result.path) == await frame_md5(
        tools, source_dir / 'index.m3u8'
    )


async def test_fmp4_stream_uses_the_init_segment(server, client, tmp_path):
    source_dir = tmp_path / 'src'
    make_hls_fmp4(tools, source_dir, seconds=4)
    server.add_directory('/fmp4', source_dir)
    plan = make_plan(server.url('/fmp4/index.m3u8'), tmp_path / 'out.mp4')

    result = await HlsTransfer(client, tools).transfer(plan, ProgressReporter(), CancelToken())

    measured = await probe(tools, result.path)
    assert measured.video_codec == 'h264'
    assert await frame_md5(tools, result.path) == await frame_md5(tools, source_dir / 'index.m3u8')


async def test_aes128_stream_is_decrypted(server, client, tmp_path):
    source_dir = tmp_path / 'src'
    make_hls_aes128(tools, source_dir, seconds=4)
    server.add_directory('/aes', source_dir)
    plan = make_plan(server.url('/aes/index.m3u8'), tmp_path / 'out.mp4')

    result = await HlsTransfer(client, tools).transfer(plan, ProgressReporter(), CancelToken())

    measured = await probe(tools, result.path)
    assert measured.video_codec == 'h264'
    # The engine's own FFmpeg calls deliberately exclude the crypto protocol,
    # so the reference decode for an encrypted playlist is made here instead.
    assert await frame_md5(tools, result.path) == await _encrypted_reference(source_dir)


async def _encrypted_reference(source_dir):
    import hashlib

    from mediadl.processing.ffmpeg_runner import run

    stdout, _ = await run(
        [tools.ffmpeg, '-nostdin', '-hide_banner', '-v', 'error',
         '-protocol_whitelist', 'file,crypto,data', '-allowed_extensions', 'ALL',
         '-i', str(source_dir / 'index.m3u8'), '-map', '0:v:0', '-f', 'framemd5', '-'],
        timeout_s=120,
    )
    hashes = [
        line.rsplit(',', 1)[-1].strip()
        for line in stdout.decode().splitlines()
        if line and not line.startswith('#')
    ]
    return hashlib.sha256('\n'.join(hashes).encode()).hexdigest()


async def test_interrupted_run_resumes_at_segment_granularity(server, client, tmp_path):
    source_dir = tmp_path / 'src'
    make_hls(tools, source_dir, seconds=4)
    server.add_directory('/resume', source_dir)
    plan = make_plan(server.url('/resume/index.m3u8'), tmp_path / 'out.mp4')
    engine = HlsTransfer(client, tools)

    # Fail on the third segment, leaving the first two on disk.
    resource = server.resources['/resume/index2.ts']
    resource.fail_sequence = [500, 500, 500, 500, 500, 500]
    with pytest.raises(RetriesExhaustedError):
        await engine.transfer(plan, ProgressReporter(), CancelToken())

    work = plan.part_path.parent / f'{plan.part_path.stem}.hls' / 'video'
    state = json.loads((work / 'stream.state.json').read_text())
    assert len(state['completed']) >= 2
    downloaded_before = {f.name for f in work.glob('seg-*')}

    resource.fail_sequence = []
    result = await engine.transfer(plan, ProgressReporter(), CancelToken())

    assert result.bytes_written > 0
    # The already-fetched segments were not requested again.
    assert server.resources['/resume/index0.ts'].hits == 1
    assert downloaded_before


async def test_drm_playlist_is_refused_before_any_segment_is_fetched(server, client, tmp_path):
    server.add(
        '/drm/index.m3u8',
        (
            b'#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n'
            b'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key",'
            b'KEYFORMAT="com.apple.streamingkeydelivery"\n'
            b'#EXTINF:4.0,\nseg0.ts\n#EXT-X-ENDLIST\n'
        ),
        content_type='application/vnd.apple.mpegurl',
    )
    server.add('/drm/seg0.ts', b'\x47' + b'\x00' * 187, content_type='video/mp2t')
    plan = make_plan(server.url('/drm/index.m3u8'), tmp_path / 'out.mp4')

    with pytest.raises(DrmError):
        await HlsTransfer(client, tools).transfer(plan, ProgressReporter(), CancelToken())

    assert server.resources['/drm/seg0.ts'].hits == 0, 'no segment may be fetched for a DRM stream'


async def test_live_playlist_is_refused(server, client, tmp_path):
    server.add(
        '/live/index.m3u8',
        b'#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg0.ts\n',
        content_type='application/vnd.apple.mpegurl',
    )
    plan = make_plan(server.url('/live/index.m3u8'), tmp_path / 'out.mp4')
    with pytest.raises(InputError, match='live'):
        await HlsTransfer(client, tools).transfer(plan, ProgressReporter(), CancelToken())


async def test_oversized_playlist_is_refused(server, client, tmp_path):
    server.add('/big/index.m3u8', b'#EXTM3U\n' + b'#\n' * 100_000,
               content_type='application/vnd.apple.mpegurl')
    plan = make_plan(server.url('/big/index.m3u8'), tmp_path / 'out.mp4')
    plan.limits = TransferLimits(max_playlist_bytes=1024)
    with pytest.raises(SizeLimitError):
        await HlsTransfer(client, tools).transfer(plan, ProgressReporter(), CancelToken())


async def test_master_playlist_selects_the_highest_variant(server, client, tmp_path):
    low = tmp_path / 'low'
    high = tmp_path / 'high'
    make_hls(tools, low, seconds=2)
    make_hls(tools, high, seconds=2)
    server.add_directory('/multi/low', low)
    server.add_directory('/multi/high', high)
    server.add(
        '/multi/master.m3u8',
        (
            b'#EXTM3U\n'
            b'#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=320x240,CODECS="avc1.64001f"\n'
            b'low/index.m3u8\n'
            b'#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="avc1.640028"\n'
            b'high/index.m3u8\n'
        ),
        content_type='application/vnd.apple.mpegurl',
    )
    plan = make_plan(server.url('/multi/master.m3u8'), tmp_path / 'out.mp4')

    await HlsTransfer(client, tools).transfer(plan, ProgressReporter(), CancelToken())

    assert server.resources['/multi/high/index.m3u8'].hits == 1
    assert server.resources['/multi/low/index.m3u8'].hits == 0
