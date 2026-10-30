"""FFmpeg build license gate (spec section 8.3).

``--enable-nonfree`` builds are not redistributable, so a release pipeline
should fail on them rather than ship one by accident.
"""
from __future__ import annotations

from dataclasses import dataclass

from mediadl.errors import PolicyDeniedError
from mediadl.obs.logging import get_logger
from mediadl.processing.ffmpeg_runner import FfmpegTools, build_config, license_text

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BuildLicense:
    nonfree: bool
    gpl: bool
    version3: bool
    license_line: str

    @property
    def summary(self) -> str:
        if self.nonfree:
            return 'nonfree (not redistributable)'
        if self.gpl:
            return 'GPL v3 or later' if self.version3 else 'GPL v2 or later'
        return 'LGPL v3 or later' if self.version3 else 'LGPL v2.1 or later'


async def inspect(tools: FfmpegTools) -> BuildLicense:
    config = await build_config(tools)
    try:
        line = (await license_text(tools)).strip().splitlines()[0]
    except (IndexError, Exception):
        line = ''
    return BuildLicense(
        nonfree='--enable-nonfree' in config,
        gpl='--enable-gpl' in config,
        version3='--enable-version3' in config,
        license_line=line,
    )


async def enforce(tools: FfmpegTools, *, fail_on_nonfree: bool) -> BuildLicense:
    info = await inspect(tools)
    if info.nonfree and fail_on_nonfree:
        raise PolicyDeniedError(
            'the configured FFmpeg build is --enable-nonfree and cannot be distributed',
            license=info.summary,
        )
    log.debug('ffmpeg_license', summary=info.summary)
    return info
