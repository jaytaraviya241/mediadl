"""Importing local files must work with the shipped defaults.

Regression: the transfer engine used to reject every path when no import
roots were configured, while the connector accepted them. The result was
that `mediadl get ./file.jpg` failed on a default install.
"""
from __future__ import annotations

import pytest

from mediadl.app.context import build
from mediadl.app.service import MediaDLService
from mediadl.config import JobsConfig, OutputConfig, Settings, SourcesConfig
from mediadl.jobs.states import JobState


@pytest.fixture
def default_settings(tmp_path):
    """No import_roots at all — exactly what a fresh install has."""
    return Settings(
        sources=SourcesConfig(enabled=['direct_url', 'user_files']),
        output=OutputConfig(root=str(tmp_path / 'out')),
        jobs=JobsConfig(database=str(tmp_path / 'jobs.sqlite3')),
    )


@pytest.fixture
async def default_service(default_settings, tmp_path):
    ctx = build(default_settings, output_root=tmp_path / 'out')
    try:
        yield MediaDLService(ctx, principal_id='tester')
    finally:
        await ctx.aclose()


async def test_single_file_imports_without_configured_roots(
    default_service, tmp_path, jpeg_bytes
):
    source = tmp_path / 'holiday.jpg'
    source.write_bytes(jpeg_bytes)

    outcome = await default_service.get(str(source))

    assert outcome.state is JobState.COMPLETED, 'a default install must import a local file'
    record = outcome.results[0].record
    assert (default_service.output_root() / record.relative_path).read_bytes() == jpeg_bytes


async def test_folder_imports_without_configured_roots(default_service, tmp_path, jpeg_bytes):
    folder = tmp_path / 'album'
    folder.mkdir()
    for index in range(3):
        (folder / f'{index}.jpg').write_bytes(jpeg_bytes + bytes([index]))

    outcome = await default_service.get(str(folder))

    assert outcome.state is JobState.COMPLETED
    assert [r.position for r in outcome.results] == [1, 2, 3]


async def test_configured_roots_still_restrict(tmp_path, jpeg_bytes):
    """Setting import_roots is what turns "anywhere" into a restriction."""
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    outside = tmp_path / 'outside.jpg'
    outside.write_bytes(jpeg_bytes)

    settings = Settings(
        sources=SourcesConfig(enabled=['user_files'], import_roots=[str(allowed)]),
        output=OutputConfig(root=str(tmp_path / 'out')),
        jobs=JobsConfig(database=str(tmp_path / 'jobs2.sqlite3')),
    )
    ctx = build(settings, output_root=tmp_path / 'out')
    try:
        service = MediaDLService(ctx, principal_id='tester')
        outcome = await service.get(str(outside))
        assert outcome.state is JobState.FAILED

        inside = allowed / 'ok.jpg'
        inside.write_bytes(jpeg_bytes)
        assert (await service.get(str(inside))).state is JobState.COMPLETED
    finally:
        await ctx.aclose()


async def test_a_symlink_to_your_own_file_is_followed(default_service, tmp_path, jpeg_bytes):
    """With no roots configured, a link to your own file is just that file."""
    real = tmp_path / 'real.jpg'
    real.write_bytes(jpeg_bytes)
    link = tmp_path / 'link.jpg'
    link.symlink_to(real)

    outcome = await default_service.get(str(link))

    assert outcome.state is JobState.COMPLETED


async def test_a_symlink_cannot_escape_a_configured_root(tmp_path, jpeg_bytes):
    """The case that matters: a link inside an approved root pointing out of it."""
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    secret = tmp_path / 'secret.jpg'
    secret.write_bytes(jpeg_bytes)
    (allowed / 'escape.jpg').symlink_to(secret)

    settings = Settings(
        sources=SourcesConfig(enabled=['user_files'], import_roots=[str(allowed)]),
        output=OutputConfig(root=str(tmp_path / 'out')),
        jobs=JobsConfig(database=str(tmp_path / 'jobs3.sqlite3')),
    )
    ctx = build(settings, output_root=tmp_path / 'out')
    try:
        service = MediaDLService(ctx, principal_id='tester')
        outcome = await service.get(str(allowed / 'escape.jpg'))
        assert outcome.state is JobState.FAILED, 'a link out of the root must not be read'
    finally:
        await ctx.aclose()
