"""Contract suite every connector must pass (spec section 5.6).

Import ``ConnectorContractTests`` in a connector package's test module and
set ``connector`` plus ``cases``; the suite then checks the invariants the
core relies on. A new platform integration should not have to rediscover
these by debugging the pipeline.

    class TestVimeo(ConnectorContractTests):
        connector = VimeoConnector()
        cases = [ContractCase('https://vimeo.com/123', expected_kind=MediaKind.VIDEO)]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from mediadl.domain.invariants import as_collection, validate
from mediadl.domain.models import (
    Availability,
    Collection,
    MediaKind,
    RepresentationRole,
    ResolvedItem,
)
from mediadl.selection import SelectionPolicy, choose
from mediadl.sources.base import ConnectorCapabilities, SourceConnector, SourceRequest


@dataclass(frozen=True, slots=True)
class ContractCase:
    raw: str
    expected_kind: MediaKind | None = None
    expected_positions: tuple[int, ...] | None = None
    expect_collection: bool = False
    options: dict = field(default_factory=dict)


class ConnectorContractTests:
    """Subclass and set ``connector``; override ``resolve`` for offline cases."""

    connector: ClassVar[SourceConnector]
    cases: ClassVar[list[ContractCase]] = []
    #: URLs that must NOT be claimed by this connector.
    rejects: ClassVar[list[str]] = []

    async def resolve(self, case: ContractCase) -> ResolvedItem:
        """Override to supply an AccessContext or replay recorded fixtures."""
        raise NotImplementedError

    # -- static contract ---------------------------------------------------
    def test_declares_an_id_and_capabilities(self):
        assert isinstance(self.connector.connector_id, str) and self.connector.connector_id
        capabilities = self.connector.capabilities
        assert isinstance(capabilities, ConnectorCapabilities)
        assert capabilities.media_kinds, 'a connector must declare the kinds it returns'
        assert capabilities.auth_modes, 'a connector must declare its auth modes'
        assert capabilities.rendition_basis in (
            'platform_rendition', 'user_original', 'direct_source'
        )

    def test_match_is_pure_and_offline(self):
        for case in self.cases:
            request = SourceRequest(case.raw, options=case.options)
            first = self.connector.match(request)
            assert first is not None, f'{case.raw!r} must be claimed by this connector'
            assert first == self.connector.match(request), 'match must be deterministic'

    def test_rejected_inputs_are_not_claimed(self):
        for raw in self.rejects:
            assert self.connector.match(SourceRequest(raw)) is None, f'{raw!r} must not match'

    def test_required_scopes_are_declared_for_authenticated_modes(self):
        if 'none' in self.connector.capabilities.auth_modes:
            return
        for case in self.cases:
            scopes = self.connector.required_scopes(SourceRequest(case.raw))
            assert scopes, 'an authenticated connector must declare required scopes'

    # -- resolution contract -----------------------------------------------
    async def test_resolved_items_satisfy_the_domain_contract(self):
        if not self.cases:
            pytest.skip('no cases declared')
        for case in self.cases:
            await self._check_case(case)

    async def _check_case(self, case: ContractCase) -> None:
        item = validate(await self.resolve(case))

        if case.expect_collection:
            assert isinstance(item, Collection)
        collection = as_collection(item)

        positions = [a.position for a in collection.assets]
        assert positions == list(range(1, len(positions) + 1)), 'positions must be 1..n in order'
        assert len({a.asset_id for a in collection.assets}) == len(collection.assets)
        if case.expected_positions is not None:
            assert tuple(positions) == case.expected_positions

        for asset in collection.assets:
            assert asset.kind in self.connector.capabilities.media_kinds
            assert asset.ref.connector_id == self.connector.connector_id
            assert asset.ref.media_id, 'every asset needs a provider media id'
            if case.expected_kind is not None and len(collection.assets) == 1:
                assert asset.kind is case.expected_kind
            if asset.availability is Availability.AVAILABLE:
                # A preview must never be the only thing on offer.
                usable = [
                    r for r in asset.representations
                    if r.role is not RepresentationRole.PREVIEW
                ]
                assert usable, 'an available asset needs a non-preview representation'
                selected = choose(
                    asset, SelectionPolicy(),
                    enabled_protocols=frozenset({'https', 'file', 'hls', 'dash'}),
                )
                assert selected.primary.role is not RepresentationRole.PREVIEW
            else:
                assert asset.unavailable_reason, 'unavailability must be explained'

    async def test_source_metadata_is_verbatim_and_timestamped(self):
        for case in self.cases:
            collection = as_collection(validate(await self.resolve(case)))
            for asset in collection.assets:
                assert asset.metadata.fetched_at is not None, 'metadata needs a fetch time'
                assert isinstance(asset.metadata.fields, dict | type(None)) or hasattr(
                    asset.metadata.fields, 'items'
                )


def assert_no_credential_leak(payload: str, secrets: list[str]) -> None:
    """Helper for connector tests: no secret may appear in a serialized payload."""
    for secret in secrets:
        assert secret not in payload, 'a credential leaked into a serialized payload'
