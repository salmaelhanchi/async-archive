"""All configuration. The only module that reads environment variables."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from archive_mirror import __version__
from archive_mirror.domain.models import ListingFormat

PRODUCT_NAME = "async-archive-mirror"


class Settings(BaseSettings):
    # env_ignore_empty: a variable left blank (as in .env.example) means "use the default"
    model_config = SettingsConfigDict(
        env_prefix="ARCHIVE_MIRROR_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    # where state lives
    state_db: Path = Path("data/state.sqlite")

    # identity: one honest User-Agent; the contact URL lets archive admins reach the operator
    contact_url: str | None = None

    # concurrency vs rate: in-flight cap protects us, per-host rate protects the server
    listing_workers: int = Field(default=4, ge=1, le=256)
    max_in_flight: int = Field(default=4, ge=1, le=256)
    per_host_rate: float = Field(default=2.0, gt=0, description="requests per second per host")
    per_host_burst: int = Field(default=2, ge=1)
    respect_robots: bool = True
    http2: bool = True

    # downloads
    mirror_root: Path = Path("data/mirror")
    download_workers: int = Field(default=4, ge=1, le=256)
    download_queue_size: int = Field(default=64, ge=1)
    max_download_bytes: int | None = Field(
        default=None, ge=0, description="byte budget per run, counted by listed size"
    )
    min_free_bytes: int = Field(default=1024**3, ge=0, description="free disk to keep")

    # extraction: archives are unpacked in worker processes, off the event loop
    extract: bool = True
    extract_root: Path = Path("data/extracted")
    extract_workers: int = Field(default=2, ge=1, le=64, description="worker processes")
    extract_queue_size: int = Field(default=16, ge=1)
    max_extract_bytes: int = Field(default=20 * 1024**3, ge=1, description="per archive")
    max_extract_members: int = Field(default=100_000, ge=1, description="per archive")

    # retries
    request_timeout_s: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=5, ge=1)
    backoff_base_s: float = Field(default=0.5, gt=0)
    backoff_cap_s: float = Field(default=30.0, gt=0)
    max_retry_after_s: float = Field(default=300.0, ge=0)

    # budgets and bounds
    max_listing_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    max_depth: int = Field(default=32, ge=0)
    max_listing_pages: int = Field(default=10_000, ge=1)
    write_queue_size: int = Field(default=256, ge=1)
    listing_format: Literal["auto", "apache", "nginx", "iis"] = "auto"

    # observability
    metrics_port: int | None = Field(default=None, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    @property
    def user_agent(self) -> str:
        product = f"{PRODUCT_NAME}/{__version__}"
        return f"{product} (+{self.contact_url})" if self.contact_url else product

    @property
    def forced_format(self) -> ListingFormat | None:
        return None if self.listing_format == "auto" else ListingFormat(self.listing_format)

    def snapshot(self) -> dict[str, Any]:
        """Settings stored with each run as provenance."""
        return self.model_dump(mode="json")
