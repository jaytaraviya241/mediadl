"""Domain contract enforcement (spec section 5.3, Phase 0 acceptance)."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mediadl.domain.invariants import as_collection, validate, validate_collection
from mediadl.domain.models import (
    Asset,
    Availability,
    Collection,
    CollectionKind,
    HttpLocator,
    MediaKind,
    Representation,
    RepresentationRole,
    SourceMetadata,
    SourceRef,
)
from mediadl.errors import ConnectorContractError


def make_asset(position=1, asset_id='c:1', media_id='1', reps=None, availability=Availability.AVAILABLE):
    if reps is None:
        reps = (
            Representation(
                'primary', MediaKind.IMAGE, RepresentationRole.PRIMARY, 'https',
                HttpLocator('https://cdn/x.jpg'),
            ),
        )
    return Asset(
        asset_id=asset_id, ref=SourceRef('c', media_id), kind=MediaKind.IMAGE,
        position=position, availability=availability, representations=tuple(reps),
        unavailable_reason=None if availability is Availability.AVAILABLE else 'withheld',
    )


def make_collection(assets):
    return Collection(
        collection_id='c:coll', ref=SourceRef('c', 'coll'), kind=CollectionKind.CAROUSEL,
        assets=tuple(assets), position_basis='api_response_order', metadata=SourceMetadata(),
    )


def test_valid_collection_passes():
    collection = make_collection([
        make_asset(1, 'c:1', '1'), make_asset(2, 'c:2', '2'), make_asset(3, 'c:3', '3')
    ])
    assert validate_collection(collection) is collection


def test_positions_must_be_contiguous_from_one():
    with pytest.raises(ConnectorContractError, match='contiguous'):
        validate_collection(make_collection([make_asset(1, 'c:1', '1'), make_asset(3, 'c:3', '3')]))


def test_positions_must_be_ordered():
    with pytest.raises(ConnectorContractError, match='contiguous'):
        validate_collection(make_collection([make_asset(2, 'c:2', '2'), make_asset(1, 'c:1', '1')]))


def test_duplicate_asset_ids_are_rejected():
    with pytest.raises(ConnectorContractError, match='duplicate asset_id'):
        validate_collection(make_collection([make_asset(1, 'c:1', '1'), make_asset(2, 'c:1', '2')]))


def test_duplicate_rep_ids_are_rejected():
    duplicate = (
        Representation('p', MediaKind.IMAGE, RepresentationRole.PRIMARY, 'https', HttpLocator('https://a')),
        Representation('p', MediaKind.IMAGE, RepresentationRole.PREVIEW, 'https', HttpLocator('https://b')),
    )
    with pytest.raises(ConnectorContractError, match='duplicate rep_id'):
        validate(make_asset(reps=duplicate))


def test_available_asset_needs_a_usable_representation():
    with pytest.raises(ConnectorContractError, match='primary or component'):
        validate(make_asset(reps=()))


def test_unavailable_asset_must_explain_itself():
    asset = Asset(
        asset_id='c:1', ref=SourceRef('c', '1'), kind=MediaKind.IMAGE, position=1,
        availability=Availability.UNAVAILABLE_COPYRIGHT, representations=(),
        unavailable_reason=None,
    )
    with pytest.raises(ConnectorContractError, match='explain'):
        validate(asset)


def test_empty_collection_is_rejected():
    with pytest.raises(ConnectorContractError, match='no assets'):
        validate_collection(make_collection([]))


def test_asset_requires_a_provider_media_id():
    asset = Asset(
        asset_id='c:1', ref=SourceRef('c', ''), kind=MediaKind.IMAGE, position=1,
        availability=Availability.AVAILABLE,
        representations=(
            Representation('p', MediaKind.IMAGE, RepresentationRole.PRIMARY, 'https',
                           HttpLocator('https://a')),
        ),
    )
    with pytest.raises(ConnectorContractError, match='media_id'):
        validate(asset)


def test_unknown_protocol_is_rejected():
    bad = (
        Representation('p', MediaKind.IMAGE, RepresentationRole.PRIMARY, 'ftp',  # type: ignore[arg-type]
                       HttpLocator('ftp://a')),
    )
    with pytest.raises(ConnectorContractError, match='protocol'):
        validate(make_asset(reps=bad))


def test_single_asset_becomes_a_one_item_collection():
    collection = as_collection(make_asset())
    assert len(collection.assets) == 1
    assert collection.assets[0].position == 1
    validate_collection(collection)


@settings(max_examples=100, deadline=None)
@given(st.integers(min_value=1, max_value=25))
def test_any_contiguous_collection_validates(count):
    collection = make_collection([
        make_asset(i, f'c:{i}', str(i)) for i in range(1, count + 1)
    ])
    assert validate_collection(collection) is collection


@settings(max_examples=100, deadline=None)
@given(st.lists(st.integers(min_value=1, max_value=10), min_size=2, max_size=6, unique=True))
def test_non_contiguous_positions_always_fail(positions):
    if sorted(positions) == list(range(1, len(positions) + 1)):
        return  # that arrangement is the valid one
    assets = [make_asset(p, f'c:{p}', str(p)) for p in positions]
    with pytest.raises(ConnectorContractError):
        validate_collection(make_collection(assets))
