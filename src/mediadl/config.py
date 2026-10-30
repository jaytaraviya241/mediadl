"""Typed configuration (spec section 12.9).

TOML on disk, environment overrides with the ``MEDIADL_`` prefix, validated
by pydantic at the process edge. The core passes plain dataclasses around;
pydantic never leaks into the domain.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PolicyName = Literal['source_preserving', 'broadly_compatible', 'storage_efficient']


class SourcesConfig(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: ['direct_url', 'user_files', 'instagram_graph'])
    import_roots: list[str] = Field(default_factory=list)


class OutputConfig(BaseModel):
    root: str = '~/Downloads/mediadl'
    template: str = (
        '{source}/{owner}/{date}_{collection_or_media_id}/'
        '{position:02d}-of-{count:02d}_{kind}_{media_id}_{rep_label}.{ext}'
    )
    policy: PolicyName = 'source_preserving'
    keep_source: bool = True
    sidecar: bool = True
    embed_metadata: bool = False
    zip: bool = False
    collision: Literal['dedupe_or_suffix', 'fail', 'overwrite'] = 'dedupe_or_suffix'
    save_previews: bool = False

    def resolved_root(self) -> Path:
        return Path(self.root).expanduser().resolve()


class VideoProfile(BaseModel):
    codec: str = 'h264'
    crf: int = 18
    preset: str = 'medium'
    pix_fmt: str = 'yuv420p'
    tag: str | None = None


class AudioProfile(BaseModel):
    codec: str = 'aac'
    bitrate: str = '192k'


class ImageProfile(BaseModel):
    format: str = 'jpeg'
    quality: int = 95


class CompatiblePolicyConfig(BaseModel):
    max_height: int = 1080
    video: VideoProfile = Field(default_factory=VideoProfile)
    audio: AudioProfile = Field(default_factory=AudioProfile)
    image: ImageProfile = Field(default_factory=ImageProfile)


class EfficientPolicyConfig(BaseModel):
    min_short_side: int = 720
    video: VideoProfile = Field(
        default_factory=lambda: VideoProfile(codec='hevc', crf=26, preset='medium', tag='hvc1')
    )
    audio: AudioProfile = Field(default_factory=lambda: AudioProfile(codec='opus', bitrate='128k'))
    image: ImageProfile = Field(default_factory=lambda: ImageProfile(format='keep', quality=90))
    require_savings_ratio: float = 0.25


class PoliciesConfig(BaseModel):
    broadly_compatible: CompatiblePolicyConfig = Field(default_factory=CompatiblePolicyConfig)
    storage_efficient: EfficientPolicyConfig = Field(default_factory=EfficientPolicyConfig)


class LimitsConfig(BaseModel):
    max_asset_bytes: int = 4 * 1024**3
    max_assets_per_collection: int = 50
    max_duration_s: int = 1800
    max_image_pixels: int = 100_000_000
    max_video_pixels: int = 7680 * 4320
    job_timeout_s: int = 7200
    ffmpeg_timeout_factor: float = 3.0
    ffmpeg_timeout_floor_s: float = 60.0
    max_playlist_bytes: int = 5 * 1024 * 1024


class NetworkConfig(BaseModel):
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 30.0
    write_timeout_s: float = 30.0
    max_redirects: int = 5
    allowed_schemes: list[str] = Field(default_factory=lambda: ['https'])
    allowed_ports: list[int] = Field(default_factory=lambda: [443])
    per_host_concurrency: int = 4
    chunk_size: int = 1024 * 1024

    @field_validator('allowed_schemes')
    @classmethod
    def _no_file_scheme(cls, value: list[str]) -> list[str]:
        bad = set(value) - {'http', 'https'}
        if bad:
            raise ValueError(f'only http/https may be enabled for network transfers, got {sorted(bad)}')
        return value


class RetryConfig(BaseModel):
    max_attempts: int = 5
    base_s: float = 1.0
    cap_s: float = 60.0
    park_threshold_s: float = 900.0
    asset_attempts: int = 3


class ConcurrencyConfig(BaseModel):
    assets_per_job: int = 3
    segments: int = 4
    active_jobs_per_user: int = 3


class InstagramConfig(BaseModel):
    auth_mode: Literal['instagram_login', 'facebook_login'] = 'instagram_login'
    api_version: str = 'v25.0'
    scopes: list[str] = Field(default_factory=lambda: ['instagram_business_basic'])
    requests_per_second_per_connection: float = 1.0
    auth_broker_url: str | None = None
    client_id: str | None = None
    redirect_uri: str | None = None
    index_page_size: int = 50
    index_max_pages: int = 200


class FfmpegConfig(BaseModel):
    path: str = ''
    fail_on_nonfree_build: bool = True
    enabled: bool = True


class JobsConfig(BaseModel):
    database: str = ''  # empty -> <data_dir>/jobs.sqlite3
    lease_seconds: int = 60
    heartbeat_seconds: int = 20


class LoggingConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    level: str = 'INFO'
    json_output: bool = Field(default=False, alias='json')


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix='MEDIADL_', env_nested_delimiter='__', extra='forbid'
    )

    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    policies: PoliciesConfig = Field(default_factory=PoliciesConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    instagram_graph: InstagramConfig = Field(default_factory=InstagramConfig)
    ffmpeg: FfmpegConfig = Field(default_factory=FfmpegConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def load(cls, path: str | Path | None = None, **overrides: Any) -> Settings:
        data: dict[str, Any] = {}
        if path is not None:
            file = Path(path).expanduser()
            if not file.is_file():
                raise FileNotFoundError(f'config file not found: {file}')
            data = tomllib.loads(file.read_text('utf-8'))
        data.update(overrides)
        return cls(**data)

    def data_dir(self) -> Path:
        from platformdirs import user_data_dir

        directory = Path(user_data_dir('mediadl', 'mediadl'))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def jobs_database(self) -> Path:
        if self.jobs.database:
            return Path(self.jobs.database).expanduser()
        return self.data_dir() / 'jobs.sqlite3'
