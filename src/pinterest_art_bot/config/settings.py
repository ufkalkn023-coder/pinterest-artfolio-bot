"""Application settings.

Uses pydantic-settings for environment variable loading with
explicit safety defaults.

Critical invariants:
  - PUBLISH_TO_PINTEREST defaults to False.
  - PINTEREST_AUTO_CREATE_BOARDS defaults to False.
  - PINTEREST_PRODUCTION_SCHEDULE_ENABLED defaults to False.
  - No code path should publish merely because credentials exist.
  - Publishing requires credentials AND the master switch.
  - The rights gate may never be weakened while publishing is enabled.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pinterest_art_bot.domain.rights import AUTO_PUBLISHABLE_STATUSES, RightsStatus

DEFAULT_USER_AGENT = (
    "pinterest-artfolio-bot/0.1.0 (+https://github.com/pinterest-artfolio-bot; contact@example.com)"
)


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Safety switches (ALL default OFF) ───────────────────────────
    publish_to_pinterest: bool = Field(
        default=False,
        description="Master switch for Pinterest publishing. MUST be explicitly enabled.",
    )
    pinterest_auto_create_boards: bool = Field(
        default=False,
        description="Whether to auto-create missing Pinterest boards.",
    )
    pinterest_production_schedule_enabled: bool = Field(
        default=False,
        description="Whether the production scheduler is active.",
    )

    # ── Rights gate ─────────────────────────────────────────────────
    rights_enforce_strict: bool = Field(
        default=True,
        description=(
            "Strict rights mode: attribution-requiring statuses are denied when no "
            "credit line is available. Cannot be disabled while publishing is enabled."
        ),
    )
    rights_auto_publishable_statuses: list[str] = Field(
        default_factory=lambda: sorted(status.value for status in AUTO_PUBLISHABLE_STATUSES),
        description=(
            "Rights statuses that may be published automatically, as a JSON list. "
            "Defaults to the strictest policy: public_domain and cc0. Any status "
            "outside this list is blocked pending a human decision; this system "
            "provides no override path. Env format is a JSON array of strings."
        ),
    )

    # ── Pinterest API ───────────────────────────────────────────────
    pinterest_access_token: str = Field(
        default="",
        description="Pinterest OAuth access token. Empty = not configured.",
    )
    pinterest_api_base_url: str = Field(
        default="https://api.pinterest.com/v5",
        description="Pinterest API base URL.",
    )
    pinterest_request_timeout_seconds: float = Field(
        default=20.0,
        gt=0.0,
        description="Per-request timeout for Pinterest API calls.",
    )
    pinterest_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Max retries for idempotent Pinterest GET requests. Writes are never retried.",
    )
    pinterest_max_pins_per_run: int = Field(
        default=5,
        ge=0,
        description="Hard cap on Pin writes per pipeline run.",
    )
    pinterest_calls_per_hour: int = Field(
        default=900,
        ge=0,
        description=(
            "Client-side budget for Pinterest API calls per hour, to stay below "
            "the published quota. 0 disables client-side limiting."
        ),
    )
    pinterest_calls_burst: int = Field(
        default=20,
        ge=0,
        description="Extra calls allowed on top of the hourly budget.",
    )
    pinterest_max_pins_per_day: int = Field(
        default=50,
        ge=0,
        description="Hard cap on Pin writes per calendar day across all runs.",
    )

    # ── Museum API keys ─────────────────────────────────────────────
    # Only sources with a registered adapter appear here. Adding a key without
    # an adapter creates a setting that silently does nothing.
    rijksmuseum_api_key: str = Field(
        default="",
        description="Rijksmuseum Data API key. Without it, that source is not registered.",
    )

    # ── Museum HTTP ─────────────────────────────────────────────────
    museum_user_agent: str = Field(
        default=DEFAULT_USER_AGENT,
        description="User-Agent sent to museum APIs.",
    )
    default_museum: str = Field(
        default="artic",
        description="Museum source id used when none is specified.",
    )
    default_museum_limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Default number of artworks per ingestion run.",
    )

    # ── Pipeline execution ──────────────────────────────────────────
    pipeline_max_concurrency: int = Field(
        default=4,
        ge=1,
        le=32,
        description="Maximum artworks processed concurrently.",
    )
    pipeline_max_pages: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum discovery pages fetched per run.",
    )

    # ── Classification thresholds ───────────────────────────────────
    movement_confidence_auto_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Minimum confidence for automatic board routing.",
    )
    movement_confidence_minimum: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="Below this, classification is treated as NONE.",
    )
    allow_medium_confidence_routing: bool = Field(
        default=False,
        description="Opt-in: allow MEDIUM confidence classification to auto-route.",
    )

    # ── Media validation ────────────────────────────────────────────
    min_image_width: int = Field(default=600, description="Minimum image width in pixels.")
    min_image_height: int = Field(default=600, description="Minimum image height in pixels.")
    max_image_file_size_mb: int = Field(default=20, description="Maximum image file size in MB.")
    pin_media_mode: str = Field(
        default="url",
        description=(
            "How images reach Pinterest: 'url' passes the museum URL (Pinterest "
            "fetches it), 'base64' uploads a prepared copy with the Pin."
        ),
    )
    image_prepare_enabled: bool = Field(
        default=True,
        description=(
            "Re-encode and downscale museum images before publishing, so a Pin is "
            "not tied to an archival file that may disappear."
        ),
    )
    image_max_width: int = Field(
        default=1000, ge=200, le=4000, description="Maximum published image width in pixels."
    )
    image_jpeg_quality: int = Field(
        default=88, ge=50, le=100, description="JPEG quality for prepared images."
    )
    perceptual_hash_enabled: bool = Field(
        default=True,
        description="Compute perceptual hashes for near-duplicate detection.",
    )
    cross_source_dedup_enabled: bool = Field(
        default=True,
        description=(
            "Detect the same work held by multiple museums, even when the two "
            "records use different titles, artists, and photographs."
        ),
    )
    perceptual_hash_max_distance: int = Field(
        default=6,
        ge=0,
        le=32,
        description="Maximum Hamming distance for a near-duplicate verdict.",
    )

    # ── Pin text limits ─────────────────────────────────────────────
    pin_title_max_length: int = Field(
        default=100, ge=10, le=500, description="Maximum Pin title length."
    )
    pin_description_max_length: int = Field(
        default=800, ge=50, le=5000, description="Maximum Pin description length."
    )
    pin_alt_text_max_length: int = Field(
        default=500, ge=10, le=1000, description="Maximum Pin alt text length."
    )

    # ── State storage ───────────────────────────────────────────────
    state_db_path: str = Field(
        default="data/state.db",
        description="SQLite database path for authoritative publication state.",
    )
    state_reservation_ttl_seconds: int = Field(
        default=3600,
        ge=0,
        description="Reservations older than this are considered abandoned.",
    )
    state_dry_run_retention_seconds: int = Field(
        default=604800,
        ge=0,
        description="Dry-run plans older than this are purged.",
    )
    state_inflight_timeout_seconds: int = Field(
        default=1800,
        ge=0,
        description=(
            "A write left in flight longer than this is treated as abandoned by a "
            "crashed process: PUBLISHING becomes AMBIGUOUS (outcome unknown, needs "
            "reconciliation), RESERVED becomes FAILED (safe to retry). Default: 30 min."
        ),
    )
    state_run_retention_seconds: int = Field(
        default=2592000,
        ge=0,
        description="Run history older than this is purged (default: 30 days).",
    )
    state_fingerprint_retention_seconds: int = Field(
        default=15552000,
        ge=0,
        description=(
            "Media fingerprints older than this are purged, but only for artworks "
            "that can never publish (default: 180 days)."
        ),
    )
    scheduler_lock_path: str = Field(
        default="data/scheduler.lock",
        description="Lock file preventing two schedulers from running at once.",
    )
    reconcile_max_records: int = Field(
        default=20,
        ge=1,
        le=500,
        description="Ambiguous records examined per reconcile run.",
    )
    reconcile_max_pages_per_board: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Pin pages scanned per board while reconciling (100 pins each).",
    )

    # ── Board configuration ─────────────────────────────────────────
    boards_config_path: str = Field(
        default="config/boards.yaml",
        description="Path to the board routing configuration YAML.",
    )
    max_pins_per_board_per_day: int = Field(
        default=0,
        ge=0,
        description=(
            "Maximum Pins per board per day, so one movement cannot dominate a run. "
            "0 disables the per-board limit."
        ),
    )
    board_round_robin: bool = Field(
        default=True,
        description=(
            "Prefer the least recently used boards when several artworks are ready, "
            "keeping a run's Pins spread across the catalogue."
        ),
    )

    # ── Pin text ─────────────────────────────────────────────────────
    pin_templates_path: str = Field(
        default="config/pin_templates.yaml",
        description="Path to the Pin title/description template configuration.",
    )
    pin_extra_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Extra search keywords appended to every Pin, as a JSON list. "
            "Env format is a JSON array of strings."
        ),
    )

    # ── Scheduler ───────────────────────────────────────────────────
    scheduler_interval_seconds: int = Field(
        default=3600,
        ge=1,
        description="Seconds between scheduled pipeline runs.",
    )
    scheduler_max_iterations: int = Field(
        default=0,
        ge=0,
        description="Stop after N iterations. 0 = run until interrupted.",
    )

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", description="Log level.")
    log_format: str = Field(
        default="json",
        description="Log format: 'json' or 'console'.",
    )

    # ── Validated combinations ──────────────────────────────────────
    @model_validator(mode="after")
    def _validate_safety_combinations(self) -> Settings:
        """Refuse unsafe switch combinations at construction time."""
        publishing = self.publish_to_pinterest

        if publishing and not self.pinterest_access_token.strip():
            raise ValueError(
                "PUBLISH_TO_PINTEREST=true requires PINTEREST_ACCESS_TOKEN to be set. "
                "Publishing is never enabled by credentials alone."
            )

        if publishing and not self.rights_enforce_strict:
            raise ValueError(
                "RIGHTS_ENFORCE_STRICT cannot be disabled while PUBLISH_TO_PINTEREST=true. "
                "The rights gate is a hard gate and cannot be weakened in production."
            )

        unknown_statuses = [
            value
            for value in self.rights_auto_publishable_statuses
            if value not in {status.value for status in RightsStatus}
        ]
        if unknown_statuses:
            raise ValueError(
                f"Unknown rights status in RIGHTS_AUTO_PUBLISHABLE_STATUSES: {unknown_statuses}. "
                f"Valid values: {sorted(status.value for status in RightsStatus)}"
            )

        if self.pinterest_auto_create_boards and not publishing:
            raise ValueError(
                "PINTEREST_AUTO_CREATE_BOARDS=true requires PUBLISH_TO_PINTEREST=true. "
                "Board creation is a production write."
            )

        return self

    # ── Derived capabilities ────────────────────────────────────────
    @property
    def auto_publishable_rights_statuses(self) -> frozenset[RightsStatus]:
        """Parsed auto-publish rights policy."""
        return frozenset(RightsStatus(value) for value in self.rights_auto_publishable_statuses)

    @property
    def is_pinterest_configured(self) -> bool:
        """Whether Pinterest credentials are present (does NOT mean publishing is enabled)."""
        return bool(self.pinterest_access_token.strip())

    @property
    def can_publish(self) -> bool:
        """Whether the system is allowed to publish to Pinterest.

        Requires both: credentials present AND publish switch enabled.
        """
        return self.publish_to_pinterest and self.is_pinterest_configured

    @property
    def can_create_boards(self) -> bool:
        """Whether missing Pinterest boards may be created."""
        return self.pinterest_auto_create_boards and self.can_publish

    @property
    def can_schedule(self) -> bool:
        """Whether the production scheduler may run."""
        return self.pinterest_production_schedule_enabled

    @property
    def effective_max_pins_per_run(self) -> int:
        """Per-run pin budget, never exceeding the daily cap."""
        return min(self.pinterest_max_pins_per_run, self.pinterest_max_pins_per_day)

    @property
    def state_db_file(self) -> Path:
        """Resolved state database path."""
        return Path(self.state_db_path)

    @property
    def boards_config_file(self) -> Path:
        """Resolved board configuration path."""
        return Path(self.boards_config_path)


def build_settings(*, load_env_file: bool = True) -> Settings:
    """Build settings, optionally ignoring the on-disk .env file.

    Library code and tests should prefer get_settings() or pass explicit Settings.
    """
    if load_env_file:
        return Settings()
    return Settings(_env_file=None)  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (loads .env)."""
    return Settings()
