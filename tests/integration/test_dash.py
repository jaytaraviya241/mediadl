"""Native DASH engine against FFmpeg-generated manifests (spec section 7.7)."""
from __future__ import annotations

import pytest

from mediadl.domain.models import DashLocator, MediaKind, Representation, RepresentationRole
from mediadl.errors import DrmError, InputError
from mediadl.processing.ffmpeg_runner import FfmpegTools, frame_md5
from mediadl.processing.probe import probe
from mediadl.testing.media_fixtures import make_dash
from mediadl.transfer.base import CancelToken, ProgressReporter, TransferLimits, TransferPlan
from mediadl.transfer.dash import mpd
from mediadl.transfer.dash.engine import DashTransfer

tools = FfmpegTools.try_discover()
pytestmark = pytest.mark.skipif(tools is None, reason='ffmpeg not installed')

PROTECTED_MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
  <Period>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
      <Representation id="0" bandwidth="500000" width="320" height="240">
        <SegmentTemplate media="seg-$Number$.m4s" duration="1" startNumber="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

DYNAMIC_MPD = PROTECTED_MPD.replace('type="static"', 'type="dynamic"').replace(
    '<ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>', ''
)

XXE_MPD = """<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>&xxe;</Period></MPD>
"""


def make_plan(url, dest):
    rep = Representation(
        rep_id='dash', kind=MediaKind.VIDEO, role=RepresentationRole.PRIMARY,
        protocol='dash', locator=DashLocator(mpd_url=url),
    )
    return TransferPlan(
        rep=rep, dest=dest,
        limits=TransferLimits(max_bytes=50 * 1024 * 1024, chunk_size=65536),
        expected_kind=MediaKind.VIDEO,
    )


async def test_dash_stream_assembles_and_merges(server, client, tmp_path):
    source_dir = tmp_path / 'src'
    make_dash(tools, source_dir, seconds=4)
    server.add_directory('/dash', source_dir)
    plan = make_plan(server.url('/dash/index.mpd'), tmp_path / 'out.mp4')

    result = await DashTransfer(client, tools).transfer(plan, ProgressReporter(), CancelToken())

    measured = await probe(tools, result.path)
    assert measured.video_codec == 'h264'
    assert measured.audio_codec == 'aac'
    assert measured.duration_s == pytest.approx(4.0, abs=0.7)
    # FFmpeg in this build cannot demux a local .mpd, so the reference is
    # built by concatenating the fixture's own files here, independently of
    # the engine's assembly code.
    reference = _concat_reference(source_dir, tmp_path / 'reference.mp4')
    assert await frame_md5(tools, result.path) == await frame_md5(tools, reference)


def _concat_reference(source_dir, destination):
    init = source_dir / 'init-stream0.m4s'
    chunks = sorted(source_dir.glob('chunk-stream0-*.m4s'))
    with open(destination, 'wb') as out:
        out.write(init.read_bytes())
        for chunk in chunks:
            out.write(chunk.read_bytes())
    return destination


async def test_content_protection_is_refused():
    with pytest.raises(DrmError):
        mpd.parse(PROTECTED_MPD, 'https://cdn.example.com/x.mpd')


async def test_dynamic_manifest_is_refused():
    with pytest.raises(InputError, match='live'):
        mpd.parse(DYNAMIC_MPD, 'https://cdn.example.com/x.mpd')


async def test_external_entities_are_not_expanded():
    with pytest.raises(InputError):
        mpd.parse(XXE_MPD, 'https://cdn.example.com/x.mpd')


async def test_multi_period_is_refused():
    manifest = PROTECTED_MPD.replace(
        '<ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>', ''
    ).replace('</Period>', '</Period><Period><AdaptationSet/></Period>')
    with pytest.raises(InputError, match='multi-period'):
        mpd.parse(manifest, 'https://cdn.example.com/x.mpd')


async def test_segment_timeline_addressing():
    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT6S">
  <Period>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="v0" bandwidth="500000" width="640" height="360">
        <SegmentTemplate media="v0-$Time$.m4s" initialization="v0-init.mp4" timescale="1000">
          <SegmentTimeline>
            <S t="0" d="2000" r="2"/>
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""
    parsed = mpd.parse(manifest, 'https://cdn.example.com/dash/x.mpd')
    video = parsed.best_video()
    assert [s.url.rsplit('/', 1)[-1] for s in video.segments] == [
        'v0-0.m4s', 'v0-2000.m4s', 'v0-4000.m4s'
    ]
    assert video.init_url.endswith('v0-init.mp4')


async def test_segment_list_addressing():
    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
  <Period>
    <AdaptationSet contentType="audio" mimeType="audio/mp4">
      <Representation id="a0" bandwidth="64000">
        <SegmentList>
          <Initialization sourceURL="a0-init.mp4"/>
          <SegmentURL media="a0-1.m4s"/>
          <SegmentURL media="a0-2.m4s"/>
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""
    parsed = mpd.parse(manifest, 'https://cdn.example.com/dash/x.mpd')
    audio = parsed.best_audio()
    assert len(audio.segments) == 2
    assert audio.init_url.endswith('a0-init.mp4')


async def test_negative_repeat_expands_to_the_period_duration():
    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
  <Period duration="PT10S">
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="v0" bandwidth="500000" width="640" height="360">
        <SegmentTemplate media="v0-$Number$.m4s" timescale="1" startNumber="1">
          <SegmentTimeline><S t="0" d="2" r="-1"/></SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""
    parsed = mpd.parse(manifest, 'https://cdn.example.com/dash/x.mpd')
    assert len(parsed.best_video().segments) == 5


async def test_best_representation_is_selected_by_pixels_then_bandwidth():
    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
  <Period>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="low" bandwidth="300000" width="640" height="360">
        <SegmentTemplate media="low-$Number$.m4s" duration="1" timescale="1" startNumber="1"/>
      </Representation>
      <Representation id="high" bandwidth="2000000" width="1920" height="1080">
        <SegmentTemplate media="high-$Number$.m4s" duration="1" timescale="1" startNumber="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""
    parsed = mpd.parse(manifest, 'https://cdn.example.com/dash/x.mpd')
    assert parsed.best_video().rep_id == 'high'
