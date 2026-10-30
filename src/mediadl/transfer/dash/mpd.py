"""DASH MPD parsing, ISO/IEC 23009-1 subset (spec section 7.7).

Parsed with ``defusedxml``: an MPD is untrusted input and plain ElementTree
is vulnerable to entity expansion and external-entity attacks.

Scope: static (on-demand), single period, no ContentProtection. Anything
else is refused rather than half-supported.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING

from defusedxml import ElementTree as DefusedET

if TYPE_CHECKING:  # pragma: no cover - typing only
    from xml.etree.ElementTree import Element

from mediadl.errors import DrmError, InputError

NS = {'mpd': 'urn:mpeg:dash:schema:mpd:2011'}
_DURATION_RE = re.compile(
    r'^P(?:(?P<days>\d+(?:\.\d+)?)D)?'
    r'(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?'
    r'(?:(?P<minutes>\d+(?:\.\d+)?)M)?'
    r'(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$'
)


def parse_duration(value: str | None) -> float | None:
    """ISO 8601 duration as used by ``mediaPresentationDuration``."""
    if not value:
        return None
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    parts = {k: float(v) for k, v in match.groupdict(default='0').items()}
    return parts['days'] * 86400 + parts['hours'] * 3600 + parts['minutes'] * 60 + parts['seconds']


@dataclass(frozen=True, slots=True)
class DashSegment:
    url: str
    index: int
    byte_range: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class DashRepresentation:
    rep_id: str
    mime_type: str
    codecs: str | None
    bandwidth: int | None
    width: int | None
    height: int | None
    frame_rate: float | None
    audio_sampling_rate: int | None
    init_url: str | None
    segments: tuple[DashSegment, ...]
    content_type: str  # 'video' | 'audio' | other

    @property
    def is_video(self) -> bool:
        return self.content_type == 'video'

    @property
    def is_audio(self) -> bool:
        return self.content_type == 'audio'

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)


@dataclass(frozen=True, slots=True)
class DashManifest:
    url: str
    representations: tuple[DashRepresentation, ...]
    duration_s: float | None = None

    def best_video(self) -> DashRepresentation | None:
        videos = [r for r in self.representations if r.is_video]
        if not videos:
            return None
        return max(videos, key=lambda r: (r.pixels, r.bandwidth or 0, r.rep_id))

    def best_audio(self) -> DashRepresentation | None:
        audios = [r for r in self.representations if r.is_audio]
        if not audios:
            return None
        return max(audios, key=lambda r: (r.bandwidth or 0, r.rep_id))


def _text(element: Element, attr: str) -> str | None:
    value = element.get(attr)
    return value if value not in (None, '') else None


def _int(element: Element, attr: str) -> int | None:
    raw = element.get(attr)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _base_url(element: Element, parent_base: str) -> str:
    node = element.find('mpd:BaseURL', NS)
    if node is not None and (node.text or '').strip():
        return str(urllib.parse.urljoin(parent_base, (node.text or '').strip()))
    return parent_base


def _check_protection(element: Element, *, where: str) -> None:
    if element.find('mpd:ContentProtection', NS) is not None:
        raise DrmError(f'{where} is protected by ContentProtection', where=where)


def _frame_rate(value: str | None) -> float | None:
    if not value:
        return None
    if '/' in value:
        numerator, _, denominator = value.partition('/')
        try:
            return float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(value)
    except ValueError:
        return None


def _expand_template(template: str, *, rep_id: str, number: int | None = None,
                     time: int | None = None, bandwidth: int | None = None) -> str:
    """Substitute $RepresentationID$, $Number$, $Time$, $Bandwidth$ with %0Nd support."""

    def replace(match: re.Match[str]) -> str:
        name, _, fmt = match.group(1).partition('%')
        value: object
        if name == 'RepresentationID':
            return rep_id
        if name == 'Number':
            value = number if number is not None else 0
        elif name == 'Time':
            value = time if time is not None else 0
        elif name == 'Bandwidth':
            value = bandwidth or 0
        elif name == '':
            return '$'
        else:
            return match.group(0)
        return format(value, fmt.rstrip('d') + 'd') if fmt else str(value)

    return re.sub(r'\$([^$]*)\$', replace, template)


def parse(text: str, base_url: str) -> DashManifest:
    try:
        root = DefusedET.fromstring(text)
    except Exception as exc:
        raise InputError(f'MPD could not be parsed: {type(exc).__name__}') from exc

    if root.get('type', 'static') != 'static':
        raise InputError('live (dynamic) DASH manifests are out of scope')
    _check_protection(root, where='MPD')

    duration = parse_duration(root.get('mediaPresentationDuration'))
    mpd_base = _base_url(root, base_url)

    periods = root.findall('mpd:Period', NS)
    if not periods:
        raise InputError('MPD has no Period')
    if len(periods) > 1:
        raise InputError('multi-period DASH is not supported in this version')
    period = periods[0]
    _check_protection(period, where='Period')
    period_base = _base_url(period, mpd_base)
    period_duration = parse_duration(period.get('duration')) or duration

    representations: list[DashRepresentation] = []
    for adaptation in period.findall('mpd:AdaptationSet', NS):
        _check_protection(adaptation, where='AdaptationSet')
        adaptation_base = _base_url(adaptation, period_base)
        content_type = (
            adaptation.get('contentType')
            or (adaptation.get('mimeType') or '').split('/')[0]
        )
        for representation in adaptation.findall('mpd:Representation', NS):
            _check_protection(representation, where='Representation')
            representations.append(
                _parse_representation(
                    representation, adaptation, adaptation_base,
                    content_type=content_type, period_duration=period_duration,
                )
            )

    if not representations:
        raise InputError('MPD has no usable representations')
    return DashManifest(url=base_url, representations=tuple(representations), duration_s=duration)


def _parse_representation(
    representation: Element, adaptation: Element, base: str, *,
    content_type: str, period_duration: float | None,
) -> DashRepresentation:
    rep_id = representation.get('id') or 'rep'
    bandwidth = _int(representation, 'bandwidth')
    rep_base = _base_url(representation, base)
    mime = representation.get('mimeType') or adaptation.get('mimeType') or ''
    resolved_type = content_type or mime.split('/')[0]

    template = representation.find('mpd:SegmentTemplate', NS)
    if template is None:
        template = adaptation.find('mpd:SegmentTemplate', NS)
    segment_list = representation.find('mpd:SegmentList', NS)
    segment_base = representation.find('mpd:SegmentBase', NS)

    init_url: str | None = None
    segments: list[DashSegment] = []

    if template is not None:
        init_url, segments = _from_template(
            template, rep_id=rep_id, bandwidth=bandwidth, base=rep_base,
            period_duration=period_duration,
        )
    elif segment_list is not None:
        init_url, segments = _from_list(segment_list, rep_base)
    elif segment_base is not None or rep_base:
        # A single self-contained file, optionally with an index range.
        segments = [DashSegment(url=rep_base, index=0)]
    else:
        raise InputError(f'representation {rep_id} has no segment addressing')

    return DashRepresentation(
        rep_id=rep_id,
        mime_type=mime,
        codecs=representation.get('codecs') or adaptation.get('codecs'),
        bandwidth=bandwidth,
        width=_int(representation, 'width') or _int(adaptation, 'width'),
        height=_int(representation, 'height') or _int(adaptation, 'height'),
        frame_rate=_frame_rate(
            representation.get('frameRate') or adaptation.get('frameRate')
        ),
        audio_sampling_rate=(
            _int(representation, 'audioSamplingRate') or _int(adaptation, 'audioSamplingRate')
        ),
        init_url=init_url,
        segments=tuple(segments),
        content_type=resolved_type,
    )


def _from_template(
    template: Element, *, rep_id: str, bandwidth: int | None, base: str,
    period_duration: float | None,
) -> tuple[str | None, list[DashSegment]]:
    init_url = None
    if initialization := template.get('initialization'):
        init_url = urllib.parse.urljoin(
            base, _expand_template(initialization, rep_id=rep_id, bandwidth=bandwidth)
        )
    media = template.get('media')
    if not media:
        raise InputError('SegmentTemplate without a media attribute')

    start_number = _int(template, 'startNumber')
    start_number = 1 if start_number is None else start_number
    timescale = _int(template, 'timescale') or 1
    timeline = template.find('mpd:SegmentTimeline', NS)
    segments: list[DashSegment] = []

    if timeline is not None:
        current_time = 0
        index = 0
        number = start_number
        for entry in timeline.findall('mpd:S', NS):
            start = _int(entry, 't')
            if start is not None:
                current_time = start
            duration = _int(entry, 'd') or 0
            repeat = _int(entry, 'r') or 0
            if repeat < 0:
                # A negative @r repeats until the period ends.
                if not period_duration or duration <= 0:
                    raise InputError('negative SegmentTimeline @r needs a period duration')
                remaining = int(period_duration * timescale) - current_time
                repeat = max(0, remaining // duration - 1)
            for _ in range(repeat + 1):
                segments.append(
                    DashSegment(
                        url=urllib.parse.urljoin(
                            base,
                            _expand_template(
                                media, rep_id=rep_id, number=number,
                                time=current_time, bandwidth=bandwidth,
                            ),
                        ),
                        index=index,
                    )
                )
                current_time += duration
                number += 1
                index += 1
    else:
        fixed_duration = _int(template, 'duration')
        if not fixed_duration or not period_duration:
            raise InputError('SegmentTemplate needs duration and a period duration')
        count = max(1, int(-(-period_duration * timescale // fixed_duration)))
        for index in range(count):
            segments.append(
                DashSegment(
                    url=urllib.parse.urljoin(
                        base,
                        _expand_template(
                            media, rep_id=rep_id, number=start_number + index,
                            bandwidth=bandwidth,
                        ),
                    ),
                    index=index,
                )
            )
    return init_url, segments


def _from_list(segment_list: Element, base: str) -> tuple[str | None, list[DashSegment]]:
    init_url = None
    initialization = segment_list.find('mpd:Initialization', NS)
    if initialization is not None and initialization.get('sourceURL'):
        init_url = urllib.parse.urljoin(base, initialization.get('sourceURL'))
    segments = []
    for index, url_node in enumerate(segment_list.findall('mpd:SegmentURL', NS)):
        media = url_node.get('media')
        if not media:
            continue
        segments.append(DashSegment(url=urllib.parse.urljoin(base, media), index=index))
    if not segments:
        raise InputError('SegmentList has no SegmentURL entries')
    return init_url, segments
