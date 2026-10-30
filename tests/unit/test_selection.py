"""Quality-selection engine (spec section 7.1, Phase 3 acceptance: 40+ sets)."""
from __future__ import annotations

import pytest

from mediadl.domain.models import (
    Asset,
    Availability,
    HttpLocator,
    MediaKind,
    Representation,
    RepresentationRole,
    SourceRef,
    TechnicalInfo,
)
from mediadl.errors import DrmError, NoAcceptableRepresentationError
from mediadl.selection import SelectionPolicy, choose

SOURCE = SelectionPolicy(name='source_preserving')
COMPATIBLE = SelectionPolicy(name='broadly_compatible', max_height=None)
EFFICIENT = SelectionPolicy(name='storage_efficient', min_short_side=720)


def rep(rep_id, **kwargs):
    role = kwargs.pop('role', RepresentationRole.PRIMARY)
    kind = kwargs.pop('kind', MediaKind.VIDEO)
    drm = kwargs.pop('drm', False)
    protocol = kwargs.pop('protocol', 'https')
    preference = kwargs.pop('source_preference', 0)
    return Representation(
        rep_id=rep_id, kind=kind, role=role, protocol=protocol,
        locator=HttpLocator(f'https://cdn.example.com/{rep_id}'),
        declared=TechnicalInfo(**kwargs), drm=drm, label=rep_id,
        source_preference=preference,
    )


def asset(*reps, kind=MediaKind.VIDEO, availability=Availability.AVAILABLE):
    return Asset(
        asset_id='c:1', ref=SourceRef('c', '1'), kind=kind, position=1,
        availability=availability, representations=tuple(reps),
        unavailable_reason=None if availability is Availability.AVAILABLE else 'withheld',
    )


_AV = dict(has_video=True, has_audio=True, audio_codec='aac')
VIDEO_SD = dict(width=640, height=360, video_codec='h264', **_AV)
VIDEO_HD = dict(width=1920, height=1080, video_codec='h264', **_AV)
VIDEO_4K = dict(width=3840, height=2160, video_codec='hevc', **_AV)


# --- resolution, fps, range, codec ordering -------------------------------
@pytest.mark.parametrize(
    ('reps', 'expected'),
    [
        ([rep('sd', **VIDEO_SD), rep('hd', **VIDEO_HD)], 'hd'),
        ([rep('hd', **VIDEO_HD), rep('uhd', **VIDEO_4K)], 'uhd'),
        ([rep('a', **VIDEO_HD | {'fps': 30}), rep('b', **VIDEO_HD | {'fps': 60})], 'b'),
        ([rep('sdr', **VIDEO_HD | {'dynamic_range': 'SDR'}),
          rep('hdr', **VIDEO_HD | {'dynamic_range': 'HDR10'})], 'hdr'),
        ([rep('hdr', **VIDEO_HD | {'dynamic_range': 'HDR10'}),
          rep('dv', **VIDEO_HD | {'dynamic_range': 'DV'})], 'dv'),
        ([rep('8bit', **VIDEO_HD | {'bit_depth': 8}),
          rep('10bit', **VIDEO_HD | {'bit_depth': 10})], '10bit'),
        ([rep('low', **VIDEO_HD | {'bitrate_kbps': 2000}),
          rep('high', **VIDEO_HD | {'bitrate_kbps': 8000})], 'high'),
        ([rep('h264', **VIDEO_HD), rep('av1', **VIDEO_HD | {'video_codec': 'av1'})], 'av1'),
        ([rep('mono', **VIDEO_HD | {'audio_channels': 1}),
          rep('stereo', **VIDEO_HD | {'audio_channels': 2})], 'stereo'),
        ([rep('dash', protocol='https', **VIDEO_HD),
          rep('https', protocol='https', **VIDEO_HD, source_preference=5)], 'https'),
    ],
)
def test_source_preserving_ranking(reps, expected):
    assert choose(asset(*reps), SOURCE).primary.rep_id == expected


# --- exclusions -----------------------------------------------------------
def test_drm_representation_is_never_selected():
    selected = choose(asset(rep('clean', **VIDEO_SD), rep('drm', drm=True, **VIDEO_4K)), SOURCE)
    assert selected.primary.rep_id == 'clean'
    assert any('EXCLUDED_DRM' in reason for reason in selected.reasons)


def test_all_drm_raises_drm_error():
    with pytest.raises(DrmError):
        choose(asset(rep('a', drm=True, **VIDEO_HD)), SOURCE)


def test_preview_is_never_selected_even_when_larger():
    selected = choose(
        asset(
            rep('primary', **VIDEO_SD),
            rep('cover', kind=MediaKind.IMAGE, role=RepresentationRole.PREVIEW,
                width=9999, height=9999),
        ),
        SOURCE,
    )
    assert selected.primary.rep_id == 'primary'


def test_preferred_format_overrides_the_default_quality_ranking():
    selected = choose(
        asset(rep('sd', **VIDEO_SD), rep('hd', **VIDEO_HD)),
        SOURCE,
        preferred_rep_id='sd',
    )
    assert selected.primary.rep_id == 'sd'


def test_preferred_component_keeps_a_compatible_audio_pair():
    video = rep(
        'video', role=RepresentationRole.COMPONENT, width=1920, height=1080,
        has_video=True, has_audio=False, video_codec='h264',
    )
    audio = rep(
        'audio', kind=MediaKind.AUDIO, role=RepresentationRole.COMPONENT,
        has_video=False, has_audio=True, audio_codec='aac',
    )
    selected = choose(asset(video, audio), SOURCE, preferred_rep_id='video')
    assert (selected.primary.rep_id, selected.paired_audio.rep_id) == ('video', 'audio')


def test_unknown_preferred_format_is_rejected_instead_of_silently_substituted():
    with pytest.raises(NoAcceptableRepresentationError) as exc_info:
        choose(asset(rep('hd', **VIDEO_HD)), SOURCE, preferred_rep_id='no-longer-here')
    assert exc_info.value.detail['format_id'] == 'no-longer-here'


def test_unsupported_protocol_is_excluded():
    with pytest.raises(NoAcceptableRepresentationError):
        choose(asset(rep('hls', protocol='hls', **VIDEO_HD)), SOURCE,
               enabled_protocols=frozenset({'https'}))


def test_oversize_representation_is_excluded():
    policy = SelectionPolicy(name='source_preserving', max_bytes=1000)
    with pytest.raises(NoAcceptableRepresentationError):
        choose(asset(rep('big', filesize_bytes=5000, **VIDEO_HD)), policy)


def test_live_representation_is_excluded():
    with pytest.raises(NoAcceptableRepresentationError):
        choose(asset(rep('live', is_live=True, **VIDEO_HD)), SOURCE)


def test_unavailable_asset_cannot_be_selected():
    with pytest.raises(NoAcceptableRepresentationError):
        choose(asset(availability=Availability.UNAVAILABLE_COPYRIGHT), SOURCE)


def test_drm_availability_raises_drm_error():
    with pytest.raises(DrmError):
        choose(asset(availability=Availability.UNAVAILABLE_DRM), SOURCE)


# --- pairing --------------------------------------------------------------
def test_video_and_audio_components_are_paired_into_mp4():
    video = rep('v', role=RepresentationRole.COMPONENT, width=1920, height=1080,
                has_video=True, has_audio=False, video_codec='h264')
    audio = rep('a', kind=MediaKind.AUDIO, role=RepresentationRole.COMPONENT,
                has_video=False, has_audio=True, audio_codec='aac')
    selected = choose(asset(video, audio), SOURCE)
    assert (selected.primary.rep_id, selected.paired_audio.rep_id) == ('v', 'a')
    assert selected.container == 'mp4'


def test_vp9_and_opus_pair_into_webm():
    video = rep('v', role=RepresentationRole.COMPONENT, width=1920, height=1080,
                has_video=True, has_audio=False, video_codec='vp9')
    audio = rep('a', kind=MediaKind.AUDIO, role=RepresentationRole.COMPONENT,
                has_video=False, has_audio=True, audio_codec='opus')
    assert choose(asset(video, audio), SOURCE).container == 'webm'


def test_incompatible_pair_falls_back_to_mkv():
    video = rep('v', role=RepresentationRole.COMPONENT, width=1920, height=1080,
                has_video=True, has_audio=False, video_codec='vp9')
    audio = rep('a', kind=MediaKind.AUDIO, role=RepresentationRole.COMPONENT,
                has_video=False, has_audio=True, audio_codec='alac')
    assert choose(asset(video, audio), SOURCE).container == 'mkv'


def test_complete_file_beats_a_pair_of_the_same_quality():
    complete = rep('combined', **VIDEO_HD)
    video = rep('v', role=RepresentationRole.COMPONENT, width=1920, height=1080,
                has_video=True, has_audio=False, video_codec='h264')
    audio = rep('a', kind=MediaKind.AUDIO, role=RepresentationRole.COMPONENT,
                has_video=False, has_audio=True, audio_codec='aac', bitrate_kbps=96)
    selected = choose(asset(complete, video, audio), SOURCE)
    assert selected.paired_audio is None or selected.primary.rep_id == 'combined'


# --- policies -------------------------------------------------------------
def test_compatible_policy_prefers_the_playable_everywhere_option():
    av1 = rep('av1', width=3840, height=2160, has_video=True, has_audio=True,
              video_codec='av1', audio_codec='opus')
    h264 = rep('h264', width=1920, height=1080, has_video=True, has_audio=True,
               video_codec='h264', audio_codec='aac')
    assert choose(asset(av1, h264), COMPATIBLE).primary.rep_id == 'h264'
    # The same inputs under source_preserving keep the higher-quality one.
    assert choose(asset(av1, h264), SOURCE).primary.rep_id == 'av1'


def test_efficient_policy_prefers_the_efficient_codec_above_the_floor():
    h264 = rep('h264', width=1920, height=1080, has_video=True, has_audio=True,
               video_codec='h264', audio_codec='aac', filesize_bytes=50_000_000)
    hevc = rep('hevc', width=1920, height=1080, has_video=True, has_audio=True,
               video_codec='hevc', audio_codec='aac', filesize_bytes=25_000_000)
    assert choose(asset(h264, hevc), EFFICIENT).primary.rep_id == 'hevc'


def test_efficient_policy_respects_the_quality_floor():
    tiny = rep('tiny', width=320, height=180, has_video=True, has_audio=True,
               video_codec='av1', audio_codec='opus', filesize_bytes=1_000_000)
    good = rep('good', width=1280, height=720, has_video=True, has_audio=True,
               video_codec='h264', audio_codec='aac', filesize_bytes=20_000_000)
    assert choose(asset(tiny, good), EFFICIENT).primary.rep_id == 'good'


# --- images and audio -----------------------------------------------------
def test_image_ranking_prefers_more_pixels():
    small = rep('small', kind=MediaKind.IMAGE, width=640, height=640)
    large = rep('large', kind=MediaKind.IMAGE, width=1080, height=1350)
    assert choose(asset(small, large, kind=MediaKind.IMAGE), SOURCE).primary.rep_id == 'large'


def test_image_ranking_prefers_lossless_at_equal_pixels():
    jpeg = rep('jpeg', kind=MediaKind.IMAGE, width=1080, height=1080, container='jpg')
    png = rep('png', kind=MediaKind.IMAGE, width=1080, height=1080, container='png')
    assert choose(asset(jpeg, png, kind=MediaKind.IMAGE), SOURCE).primary.rep_id == 'png'


def test_compatible_image_policy_prefers_jpeg_or_png():
    heic = rep('heic', kind=MediaKind.IMAGE, width=4032, height=3024, container='heic')
    jpg = rep('jpg', kind=MediaKind.IMAGE, width=1080, height=1080, container='jpg')
    assert choose(asset(heic, jpg, kind=MediaKind.IMAGE), COMPATIBLE).primary.rep_id == 'jpg'


def test_audio_ranking_prefers_lossless():
    mp3 = rep('mp3', kind=MediaKind.AUDIO, audio_codec='mp3', bitrate_kbps=320, has_video=False)
    flac = rep('flac', kind=MediaKind.AUDIO, audio_codec='flac', has_video=False)
    assert choose(asset(mp3, flac, kind=MediaKind.AUDIO), SOURCE).primary.rep_id == 'flac'


def test_compatible_audio_prefers_aac():
    opus = rep('opus', kind=MediaKind.AUDIO, audio_codec='opus', bitrate_kbps=160, has_video=False)
    aac = rep('aac', kind=MediaKind.AUDIO, audio_codec='aac', bitrate_kbps=128, has_video=False)
    assert choose(asset(opus, aac, kind=MediaKind.AUDIO), COMPATIBLE).primary.rep_id == 'aac'


# --- determinism and explanation -----------------------------------------
def test_selection_is_deterministic_across_input_order():
    reps = [rep('a', **VIDEO_HD), rep('b', **VIDEO_HD), rep('c', **VIDEO_HD)]
    first = choose(asset(*reps), SOURCE).primary.rep_id
    for _ in range(25):
        assert choose(asset(*reversed(reps)), SOURCE).primary.rep_id == first


def test_ties_break_on_source_preference_then_rep_id():
    low = rep('aaa', source_preference=1, **VIDEO_HD)
    high = rep('bbb', source_preference=9, **VIDEO_HD)
    assert choose(asset(low, high), SOURCE).primary.rep_id == 'bbb'


def test_explanation_names_the_choice_and_the_exclusions():
    selected = choose(asset(rep('hd', **VIDEO_HD), rep('drm', drm=True, **VIDEO_4K)), SOURCE)
    joined = '; '.join(selected.reasons)
    assert 'picked hd' in joined
    assert 'EXCLUDED_DRM' in joined
    assert selected.policy_name == 'source_preserving'


def test_unknown_metadata_does_not_exclude_a_single_option():
    selected = choose(asset(rep('unknown')), SOURCE)
    assert selected.primary.rep_id == 'unknown'
