"""End-to-end runs through the real orchestrator (spec Phases 1-3, 5)."""
from __future__ import annotations

import hashlib
import json

import pytest

from mediadl.jobs.states import JobState
from mediadl.processing.ffmpeg_runner import FfmpegTools
from mediadl.testing.media_fixtures import make_video

tools = FfmpegTools.try_discover()


async def test_direct_url_image_download_writes_media_and_sidecar(server, service, jpeg_bytes):
    server.add('/photo.jpg', jpeg_bytes, content_type='image/jpeg')

    outcome = await service.get(server.url('/photo.jpg'))

    assert outcome.state is JobState.COMPLETED
    record = outcome.results[0].record
    media = service.output_root() / record.relative_path
    assert media.read_bytes() == jpeg_bytes
    assert record.sha256 == hashlib.sha256(jpeg_bytes).hexdigest()
    assert media.suffix == '.jpg', 'extension must come from sniffed bytes'

    meta = json.loads(media.with_name(media.name + '.meta.json').read_text())
    assert meta['schema'] == 'mediadl.asset/1'
    assert meta['app']['rendition_basis'] == 'direct_source'
    assert meta['app']['output']['sha256'] == record.sha256
    assert meta['source']['connector_id'] == 'direct_url'
    # A signed media URL must never be written into a sidecar.
    assert 'access_token' not in json.dumps(meta)


async def test_extension_comes_from_bytes_not_url(server, service, jpeg_bytes):
    server.add('/actually-a-jpeg.png', jpeg_bytes, content_type='image/jpeg')

    outcome = await service.get(server.url('/actually-a-jpeg.png'))

    assert outcome.state is JobState.COMPLETED
    assert outcome.results[0].record.relative_path.endswith('.jpg')


async def test_private_address_is_refused_end_to_end(service, app):
    # Restore the production policy: loopback must be rejected by the guard.
    from mediadl.config import NetworkConfig
    from mediadl.domain.models import HttpLocator
    from mediadl.net.guarded_client import GuardedHttpClient, UrlPolicy
    from mediadl.transfer.http_engine import HttpTransfer

    strict = GuardedHttpClient(NetworkConfig(), policy=UrlPolicy())
    app.transfers.engines[HttpLocator] = HttpTransfer(strict)
    try:
        outcome = await service.get('https://127.0.0.1/secret.jpg')
    finally:
        await strict.aclose()

    assert outcome.state is JobState.FAILED
    assert not any(r.ok for r in outcome.results)


async def test_local_file_import_keeps_bytes(service, tmp_path, jpeg_bytes):
    source = tmp_path / 'original.jpg'
    source.write_bytes(jpeg_bytes)

    outcome = await service.get(str(source))

    assert outcome.state is JobState.COMPLETED
    record = outcome.results[0].record
    assert (service.output_root() / record.relative_path).read_bytes() == jpeg_bytes
    meta = json.loads(
        (service.output_root() / record.relative_path).with_name(
            (service.output_root() / record.relative_path).name + '.meta.json'
        ).read_text()
    )
    assert meta['app']['rendition_basis'] == 'user_original'


async def test_directory_import_is_an_ordered_collection(service, tmp_path, jpeg_bytes):
    batch = tmp_path / 'batch'
    batch.mkdir()
    for index in range(3):
        (batch / f'{index}.jpg').write_bytes(jpeg_bytes + bytes([index]))

    outcome = await service.get(str(batch))

    assert outcome.state is JobState.COMPLETED
    assert [r.position for r in outcome.results] == [1, 2, 3]
    manifest = json.loads(outcome.manifest_path.read_text())
    assert manifest['counts']['completed'] == 3
    assert [i['position'] for i in manifest['items']] == [1, 2, 3]


async def test_resubmitting_the_same_request_is_idempotent(server, service, jpeg_bytes):
    server.add('/same.jpg', jpeg_bytes, content_type='image/jpeg')
    first = await service.submit(server.url('/same.jpg'))
    second = await service.submit(server.url('/same.jpg'))
    assert first.id == second.id


async def test_duplicate_download_dedupes_instead_of_rewriting(server, service, jpeg_bytes):
    server.add('/dup.jpg', jpeg_bytes, content_type='image/jpeg')
    first = await service.get(server.url('/dup.jpg'))
    # A second job for the same bytes, forced past idempotency by a new path.
    server.add('/dup2.jpg', jpeg_bytes, content_type='image/jpeg')
    second = await service.get(server.url('/dup2.jpg'))
    assert first.state is JobState.COMPLETED and second.state is JobState.COMPLETED
    assert first.results[0].record.sha256 == second.results[0].record.sha256


@pytest.mark.skipif(tools is None, reason='ffmpeg not installed')
async def test_video_download_is_probed(server, service, tmp_path):
    source = make_video(tools, tmp_path / 'clip.mp4', seconds=1)
    server.add('/clip.mp4', source.read_bytes(), content_type='video/mp4')

    outcome = await service.get(server.url('/clip.mp4'))

    assert outcome.state is JobState.COMPLETED
    record = outcome.results[0].record
    assert record.probe is not None
    assert record.probe.video_codec == 'h264'
    assert record.probe.width == 320
    assert record.processing == () or record.processing[0].stream_copy


async def test_job_events_record_every_transition(server, service, app, jpeg_bytes):
    server.add('/events.jpg', jpeg_bytes, content_type='image/jpeg')
    outcome = await service.get(server.url('/events.jpg'))
    events = await app.jobs.events(outcome.job_id)
    states = [e['to_state'] for e in events]
    assert states == [
        'QUEUED', 'VALIDATING', 'RESOLVING', 'DOWNLOADING', 'PROCESSING', 'COMPLETED'
    ]


@pytest.mark.skipif(tools is None, reason='ffmpeg not installed')
async def test_hls_stream_runs_through_the_whole_pipeline(server, app, settings, tmp_path):
    """A manifest URL exercises resolve -> select -> segment transfer -> remux -> finalize."""
    from mediadl.app.service import MediaDLService
    from mediadl.connectors.stream_manifest import StreamManifestConnector
    from mediadl.testing.media_fixtures import make_hls

    source_dir = tmp_path / 'stream'
    make_hls(tools, source_dir, seconds=2)
    server.add_directory('/live', source_dir)

    app.registry.register(StreamManifestConnector())
    app.admission.enabled_connectors = frozenset({*settings.sources.enabled, 'stream_manifest'})
    service = MediaDLService(app, principal_id='tester')

    outcome = await service.get(server.url('/live/index.m3u8'))

    assert outcome.state is JobState.COMPLETED
    record = outcome.results[0].record
    assert record.relative_path.endswith('.mp4')
    assert record.probe is not None and record.probe.video_codec == 'h264'
    assert (service.output_root() / record.relative_path).stat().st_size > 0
