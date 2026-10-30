"""The built-in connectors must pass the shipped contract suite."""
from __future__ import annotations

import time

import pytest

from mediadl.config import NetworkConfig
from mediadl.connectors.direct_url import DirectUrlConnector
from mediadl.connectors.stream_manifest import StreamManifestConnector
from mediadl.connectors.user_files import UserFilesConnector
from mediadl.domain.models import MediaKind
from mediadl.net.guarded_client import GuardedHttpClient, UrlPolicy
from mediadl.sources.base import AccessContext, SourceRequest
from mediadl.testing.connector_contract import ConnectorContractTests, ContractCase


async def _context():
    client = GuardedHttpClient(NetworkConfig(), policy=UrlPolicy(allow_private=True))
    return AccessContext('tester', None, None, client, time.monotonic() + 30)


class TestDirectUrlConnector(ConnectorContractTests):
    connector = DirectUrlConnector()
    cases = [
        ContractCase('https://cdn.example.com/a/photo.jpg', expected_kind=MediaKind.IMAGE),
        ContractCase('https://cdn.example.com/a/clip.mp4', expected_kind=MediaKind.VIDEO),
        ContractCase('https://cdn.example.com/a/track.m4a', expected_kind=MediaKind.AUDIO),
    ]
    rejects = [
        'https://www.instagram.com/p/ABC/',   # only the authorized connector may take these
        'https://cdn.example.com/v/index.m3u8',
        'ig-media:123',
        '/not/a/url',
    ]

    async def resolve(self, case):
        ctx = await _context()
        try:
            return await self.connector.resolve(SourceRequest(case.raw), ctx)
        finally:
            await ctx.http.aclose()


class TestStreamManifestConnector(ConnectorContractTests):
    connector = StreamManifestConnector()
    cases = [
        ContractCase('https://cdn.example.com/v/index.m3u8', expected_kind=MediaKind.VIDEO),
        ContractCase('https://cdn.example.com/v/index.mpd', expected_kind=MediaKind.VIDEO),
    ]
    rejects = ['https://cdn.example.com/a/photo.jpg', 'https://www.instagram.com/p/ABC/']

    async def resolve(self, case):
        ctx = await _context()
        try:
            return await self.connector.resolve(SourceRequest(case.raw), ctx)
        finally:
            await ctx.http.aclose()


@pytest.fixture(scope='module')
def sample_files(tmp_path_factory):
    directory = tmp_path_factory.mktemp('imports')
    (directory / 'a.jpg').write_bytes(b'\xff\xd8\xff\xe0' + b'x' * 500 + b'\xff\xd9')
    (directory / 'b.jpg').write_bytes(b'\xff\xd8\xff\xe0' + b'y' * 500 + b'\xff\xd9')
    return directory


class TestUserFilesConnector(ConnectorContractTests):
    connector = UserFilesConnector()
    cases: list[ContractCase] = []
    rejects = ['https://cdn.example.com/a.jpg', 'ig-media:123']

    @pytest.fixture(autouse=True)
    def _cases(self, sample_files):
        type(self).cases = [
            ContractCase(str(sample_files / 'a.jpg'), expected_kind=MediaKind.IMAGE),
            ContractCase(
                str(sample_files), expect_collection=True, expected_positions=(1, 2)
            ),
        ]

    async def resolve(self, case):
        ctx = await _context()
        try:
            return await self.connector.resolve(SourceRequest(case.raw), ctx)
        finally:
            await ctx.http.aclose()
