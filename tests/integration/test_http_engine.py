"""HTTP transfer engine against the local fixture server (spec sections 7.6, 9.4)."""
from __future__ import annotations

import hashlib

import pytest

from mediadl.domain.models import HttpLocator, MediaKind, Representation, RepresentationRole
from mediadl.errors import (
    ContentTypeMismatchError,
    IntegrityError,
    RateLimited,
    SizeLimitError,
    UpstreamError,
)
from mediadl.transfer.base import CancelToken, ProgressReporter, TransferLimits, TransferPlan
from mediadl.transfer.http_engine import HttpTransfer
from mediadl.transfer.resume_state import ResumeState


def make_plan(url: str, dest, *, kind=MediaKind.IMAGE, max_bytes=10 * 1024 * 1024, chunk=8192):
    rep = Representation(
        rep_id='primary', kind=kind, role=RepresentationRole.PRIMARY,
        protocol='https', locator=HttpLocator(url),
    )
    return TransferPlan(
        rep=rep, dest=dest,
        limits=TransferLimits(max_bytes=max_bytes, chunk_size=chunk),
        expected_kind=kind,
    )


async def test_full_download_hashes_and_sniffs(server, client, jpeg_bytes, tmp_path):
    server.add('/a.jpg', jpeg_bytes, content_type='image/jpeg')
    plan = make_plan(server.url('/a.jpg'), tmp_path / 'out.jpg')

    result = await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())

    assert result.bytes_written == len(jpeg_bytes)
    assert result.sha256 == hashlib.sha256(jpeg_bytes).hexdigest()
    assert result.path.read_bytes() == jpeg_bytes
    assert result.resumed is False


async def test_resume_appends_when_validators_hold(server, client, jpeg_bytes, tmp_path):
    body = jpeg_bytes * 8
    resource = server.add('/big.jpg', body, content_type='image/jpeg')
    resource.drop_after = 4000
    plan = make_plan(server.url('/big.jpg'), tmp_path / 'big.jpg', chunk=1024)
    engine = HttpTransfer(client)

    with pytest.raises(IntegrityError):
        await engine.transfer(plan, ProgressReporter(), CancelToken())

    # The engine must leave a resumable prefix and its state behind on its own.
    partial = plan.part_path.stat().st_size
    assert 0 < partial < len(body)
    state = ResumeState.read(plan.part_path)
    assert state is not None and state.bytes_written == partial

    resource.drop_after = None
    result = await engine.transfer(plan, ProgressReporter(), CancelToken())

    assert result.resumed is True
    assert result.path.read_bytes() == body
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert resource.range_requests >= 1


async def test_resume_restarts_when_etag_flips(server, client, jpeg_bytes, tmp_path):
    body = jpeg_bytes * 8
    resource = server.add('/flip.jpg', body, content_type='image/jpeg', etag='"v1"')
    resource.drop_after = 4000
    plan = make_plan(server.url('/flip.jpg'), tmp_path / 'flip.jpg', chunk=1024)
    engine = HttpTransfer(client)
    with pytest.raises(IntegrityError):
        await engine.transfer(plan, ProgressReporter(), CancelToken())

    resource.drop_after = None
    resource.etag_after_first = '"v2"'  # validator changed -> server answers 200
    result = await engine.transfer(plan, ProgressReporter(), CancelToken())

    assert result.resumed is False
    assert result.path.read_bytes() == body


async def test_server_ignoring_range_restarts_cleanly(server, client, jpeg_bytes, tmp_path):
    body = jpeg_bytes * 6
    server.add('/ignore.jpg', body, content_type='image/jpeg', ignore_range=True)
    plan = make_plan(server.url('/ignore.jpg'), tmp_path / 'ignore.jpg')
    plan.part_path.write_bytes(body[:1000])
    ResumeState.new(
        __import__('mediadl.net.redact', fromlist=['url_fingerprint']).url_fingerprint(
            server.url('/ignore.jpg')
        ),
        __import__('mediadl.transfer.base', fromlist=['Validators']).Validators('"v1"', None, len(body)),
        1000,
    ).write(plan.part_path)

    result = await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())

    assert result.resumed is False
    assert result.path.read_bytes() == body


async def test_416_with_exact_length_completes(server, client, jpeg_bytes, tmp_path):
    from mediadl.net.redact import url_fingerprint
    from mediadl.transfer.base import Validators

    body = jpeg_bytes
    server.add('/done.jpg', body, content_type='image/jpeg')
    plan = make_plan(server.url('/done.jpg'), tmp_path / 'done.jpg')
    plan.part_path.write_bytes(body)  # already complete
    ResumeState.new(
        url_fingerprint(server.url('/done.jpg')),
        Validators('"v1"', None, len(body)),
        len(body),
    ).write(plan.part_path)

    result = await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())

    assert result.bytes_written == len(body)
    assert result.sha256 == hashlib.sha256(body).hexdigest()


async def test_416_with_wrong_length_restarts(server, client, jpeg_bytes, tmp_path):
    from mediadl.net.redact import url_fingerprint
    from mediadl.transfer.base import Validators

    body = jpeg_bytes
    server.add('/mismatch.jpg', body, content_type='image/jpeg')
    plan = make_plan(server.url('/mismatch.jpg'), tmp_path / 'm.jpg')
    plan.part_path.write_bytes(body + b'EXTRA-BYTES-BEYOND-END')
    ResumeState.new(
        url_fingerprint(server.url('/mismatch.jpg')),
        Validators('"v1"', None, len(body)),
        len(body) + 22,
    ).write(plan.part_path)

    with pytest.raises(IntegrityError):
        await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())
    assert not plan.part_path.exists()


async def test_short_body_is_integrity_error(server, client, jpeg_bytes, tmp_path):
    server.add(
        '/short.jpg', jpeg_bytes, content_type='image/jpeg',
        content_length_override=len(jpeg_bytes) + 500,
    )
    plan = make_plan(server.url('/short.jpg'), tmp_path / 's.jpg')
    with pytest.raises(IntegrityError):
        await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())


async def test_wrong_magic_bytes_rejected(server, client, tmp_path):
    server.add('/fake.jpg', b'<html>not an image</html>' * 50, content_type='image/jpeg')
    plan = make_plan(server.url('/fake.jpg'), tmp_path / 'f.jpg')
    with pytest.raises(ContentTypeMismatchError):
        await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())


async def test_declared_content_type_family_is_enforced(server, client, jpeg_bytes, tmp_path):
    server.add('/page.jpg', jpeg_bytes, content_type='text/html')
    plan = make_plan(server.url('/page.jpg'), tmp_path / 'p.jpg')
    with pytest.raises(ContentTypeMismatchError):
        await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())


async def test_size_cap_aborts_mid_stream(server, client, jpeg_bytes, tmp_path):
    server.add('/huge.jpg', jpeg_bytes * 40, content_type='image/jpeg')
    plan = make_plan(server.url('/huge.jpg'), tmp_path / 'h.jpg', max_bytes=5_000)
    with pytest.raises(SizeLimitError):
        await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())


async def test_429_raises_rate_limited_with_retry_after(server, client, jpeg_bytes, tmp_path):
    resource = server.add('/slow.jpg', jpeg_bytes, content_type='image/jpeg')
    resource.fail_sequence = [429]
    resource.retry_after = '30'
    plan = make_plan(server.url('/slow.jpg'), tmp_path / 'r.jpg')
    with pytest.raises(RateLimited) as excinfo:
        await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())
    assert excinfo.value.detail['retry_after'] == 30.0


async def test_redirect_chain_is_followed_and_capped(server, client, jpeg_bytes, tmp_path):
    server.add('/final.jpg', jpeg_bytes, content_type='image/jpeg')
    plan = make_plan(server.url('/redirect/3/final.jpg'), tmp_path / 'rd.jpg')
    result = await HttpTransfer(client).transfer(plan, ProgressReporter(), CancelToken())
    assert result.bytes_written == len(jpeg_bytes)

    plan2 = make_plan(server.url('/redirect/9/final.jpg'), tmp_path / 'rd2.jpg')
    with pytest.raises(UpstreamError, match='redirect'):
        await HttpTransfer(client).transfer(plan2, ProgressReporter(), CancelToken())


async def test_progress_reports_final_total(server, client, jpeg_bytes, tmp_path):
    server.add('/prog.jpg', jpeg_bytes * 4, content_type='image/jpeg')
    seen: list[tuple[str, int, int | None]] = []
    reporter = ProgressReporter(callback=lambda a, d, t: seen.append((a, d, t)), min_interval_s=0)
    plan = make_plan(server.url('/prog.jpg'), tmp_path / 'pr.jpg')
    result = await HttpTransfer(client).transfer(plan, reporter, CancelToken())
    assert seen[-1] == ('primary', result.bytes_written, len(jpeg_bytes) * 4)
