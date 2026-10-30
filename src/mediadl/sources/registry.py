"""Connector discovery and matching (spec sections 5.3, 12.2).

Connectors load from the ``mediadl.connectors`` entry-point group, filtered
by the configured allowlist. Exactly one connector must claim a request; an
equal-score tie is an error rather than a silent coin flip.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from importlib.metadata import entry_points

from mediadl.errors import AmbiguousSourceError, UnsupportedSourceError
from mediadl.obs.logging import get_logger
from mediadl.sources.base import SourceConnector, SourceRequest

log = get_logger(__name__)

ENTRY_POINT_GROUP = 'mediadl.connectors'


class SourceRegistry:
    def __init__(self, connectors: Iterable[SourceConnector] = ()) -> None:
        self._connectors: dict[str, SourceConnector] = {}
        for connector in connectors:
            self.register(connector)

    def register(self, connector: SourceConnector) -> None:
        self._connectors[connector.connector_id] = connector

    @property
    def connectors(self) -> tuple[SourceConnector, ...]:
        return tuple(self._connectors.values())

    def get(self, connector_id: str) -> SourceConnector:
        try:
            return self._connectors[connector_id]
        except KeyError:
            raise UnsupportedSourceError(
                f'connector {connector_id!r} is not enabled', connector=connector_id
            ) from None

    @classmethod
    def from_entry_points(
        cls, enabled: Sequence[str], **connector_kwargs: object
    ) -> SourceRegistry:
        registry = cls()
        allow = set(enabled)
        for entry in entry_points(group=ENTRY_POINT_GROUP):
            if entry.name not in allow:
                log.debug('connector_skipped', connector=entry.name, reason='not in allowlist')
                continue
            try:
                factory = entry.load()
            except Exception as exc:
                log.warning('connector_load_failed', connector=entry.name, error=str(exc))
                continue
            kwargs = connector_kwargs.get(entry.name, {})  # type: ignore[union-attr]
            registry.register(factory(**kwargs) if kwargs else factory())
        missing = allow - set(registry._connectors)
        if missing:
            log.warning('connectors_not_found', missing=sorted(missing))
        return registry

    def select(self, request: SourceRequest) -> SourceConnector:
        candidates: list[tuple[int, str, SourceConnector]] = []
        for connector in self._connectors.values():
            score = connector.match(request)
            if score is not None:
                candidates.append((score, connector.connector_id, connector))
        if not candidates:
            raise UnsupportedSourceError(
                'no enabled connector accepts this input', request=request.redacted()
            )
        candidates.sort(key=lambda item: (-item[0], item[1]))
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            raise AmbiguousSourceError(
                'more than one connector claims this input',
                connectors=[c[1] for c in candidates[:2]],
            )
        return candidates[0][2]
