"""Filename templating (spec section 7.9).

Every substituted field is sanitized *after* formatting, so a format spec
like ``{date:%Y-%m-%d}`` or ``{position:02d}`` still works while provider
text can never inject a path separator. Captions never appear in a filename:
IDs already guarantee uniqueness, and captions carry privacy, length, and
injection problems.
"""
from __future__ import annotations

import string
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from mediadl.domain.models import Asset, Collection, MediaKind
from mediadl.domain.records import SelectedRendition
from mediadl.errors import PathRejectedError
from mediadl.policy.paths import sanitize_component, validate_extension

DEFAULT_TEMPLATE = (
    '{source}/{owner}/{date}_{collection_or_media_id}/'
    '{position:02d}-of-{count:02d}_{kind}_{media_id}_{rep_label}.{ext}'
)


class _SanitizingFormatter(string.Formatter):
    def format_field(self, value: object, format_spec: str) -> str:
        if isinstance(value, datetime) and format_spec:
            rendered = format(value, format_spec)
        else:
            rendered = format(value, format_spec)
        return sanitize_component(rendered)

    def get_value(self, key: int | str, args: tuple, kwargs: dict) -> object:  # type: ignore[override]
        if isinstance(key, str) and key not in kwargs:
            raise PathRejectedError(f'unknown template field {{{key}}}', field=key)
        return super().get_value(key, args, kwargs)


@dataclass(frozen=True, slots=True)
class NamingContext:
    source: str
    owner: str
    date: datetime
    collection_or_media_id: str
    position: int
    count: int
    kind: MediaKind
    media_id: str
    rep_label: str
    ext: str

    def as_fields(self) -> dict[str, object]:
        return {
            'source': self.source,
            'owner': self.owner,
            'date': self.date,
            'collection_or_media_id': self.collection_or_media_id,
            'position': self.position,
            'count': self.count,
            'kind': str(self.kind),
            'media_id': self.media_id,
            'rep_label': self.rep_label,
            'ext': self.ext,
        }

    @classmethod
    def build(
        cls,
        asset: Asset,
        collection: Collection,
        rendition: SelectedRendition,
        *,
        ext: str,
        owner_fallback: str = 'unknown',
    ) -> NamingContext:
        fields = asset.metadata.fields or {}
        parent = collection.metadata.fields or {}
        owner = str(
            fields.get('username')
            or parent.get('username')
            or fields.get('owner')
            or owner_fallback
        )
        timestamp = (
            _parse_time(fields.get('timestamp'))
            or _parse_time(parent.get('timestamp'))
            or asset.metadata.fetched_at
            or collection.metadata.fetched_at
            or datetime.now()
        )
        label = rendition.primary.label or _auto_label(rendition)
        return cls(
            source=asset.ref.connector_id,
            owner=owner,
            date=timestamp,
            collection_or_media_id=collection.ref.media_id or asset.ref.media_id,
            position=asset.position,
            count=len(collection.assets),
            kind=asset.kind,
            media_id=asset.ref.media_id,
            rep_label=label,
            ext=ext,
        )


def _auto_label(rendition: SelectedRendition) -> str:
    d = rendition.primary.declared
    parts = []
    if d.width and d.height:
        parts.append(f'{d.width}x{d.height}')
    if d.video_codec:
        parts.append(d.video_codec)
    return '-'.join(parts) or rendition.primary.rep_id


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        for candidate in (text, text.replace('+0000', '+00:00'), text.replace('Z', '+00:00')):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                continue
    return None


def render(template: str, context: NamingContext, *, kind: MediaKind | None = None) -> PurePosixPath:
    """Render a template to a relative POSIX path with every field sanitized."""
    ext = validate_extension(context.ext, str(kind or context.kind))
    fields = context.as_fields()
    fields['ext'] = ext
    # Format date with a default spec when the template does not give one.
    if '{date}' in template:
        fields['date'] = context.date.strftime('%Y-%m-%d')
    try:
        rendered = _SanitizingFormatter().vformat(template, (), fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise PathRejectedError(f'invalid output template: {exc}', template=template) from exc

    parts = [p for p in rendered.split('/') if p not in ('', '.', '..')]
    if not parts:
        raise PathRejectedError('template rendered an empty path', template=template)
    return PurePosixPath(*parts)
