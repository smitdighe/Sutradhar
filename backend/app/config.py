"""Typed application settings loaded from ``backend/.env``.

Import of this module is deliberately eager: :func:`get_settings` is invoked at
the bottom of the file so a misconfigured deployment fails at startup rather
than on the first request that happens to touch a missing variable.

Hard requirements (absent => import fails):
    DATABASE_URL, DATABASE_URL_SYNC, JWT_PRIVATE_KEY_PATH, JWT_PUBLIC_KEY_PATH,
    PENDING_TOKEN_SECRET, CURSOR_SECRET

Soft requirements (absent => the matching feature reports itself unavailable):
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, PINATA_JWT, CHAIN_SIGNER_PRIVATE_KEY
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

AppEnv = Literal["local", "staging", "production"]
LogLevel = Literal["debug", "info", "warning", "error", "critical"]


class Settings(BaseSettings):
    """Every environment-driven knob the backend reads."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_env: AppEnv = "local"
    app_port: int = 8000
    app_base_url: str = "http://localhost:8000"
    api_prefix: str = "/api/v1"
    # The FRONTEND origin, not this service. It is the base of the URL printed
    # into every QR tag, and a printed URL outlives any deployment decision:
    # pointing it at the backend would make a scan depend on this process being
    # awake, which on a free tier means a cold start between a shopper and the
    # answer. Phase 11's /v/{code} page is served by the frontend.
    public_base_url: str = "http://localhost:3000"
    # Where the public verification routes are mounted on THIS service. Empty by
    # default, so they answer at a bare `/v/{tag_code}` -- the same shape as the
    # printed payload, one path segment, no version. The public surface is the
    # one place a URL is chosen by a printer rather than by a client library,
    # and it cannot be renamed later without reprinting cloth.
    public_prefix: str = ""
    # How long a public verification response may be reused. Short: trust levels
    # and dispute state change on the next read, and a consumer standing in a
    # shop deserves the current answer, not last hour's.
    public_cache_seconds: int = 60
    log_level: LogLevel = "info"
    cors_allowed_origins: str = "http://localhost:3000"

    # --- Database ---
    database_url: str
    database_url_sync: str
    test_database_url: str | None = None
    db_pool_size: int = 5
    db_pool_max_overflow: int = 2

    # --- Session tokens ---
    jwt_private_key_path: Path
    jwt_public_key_path: Path
    jwt_algorithm: str = "EdDSA"
    jwt_issuer: str = "sutradhar"
    jwt_audience: str = "sutradhar/api"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000
    refresh_cookie_name: str = "sutradhar_rt"
    refresh_cookie_secure: bool = False

    # --- Pending (OAuth completion) token ---
    pending_token_secret: str = Field(min_length=32)
    pending_token_audience: str = "sutradhar/pending"
    pending_token_ttl_seconds: int = 600

    # --- Password hashing ---
    password_pepper: str = ""
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 65_536
    argon2_parallelism: int = 2

    # --- OAuth (Google only) ---
    oauth_redirect_base_url: str = "http://localhost:8000"
    oauth_state_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_path: str = "/api/v1/auth/oauth/google/callback"
    frontend_completion_url: str = "http://localhost:3000/auth/complete"
    frontend_post_login_url: str = "http://localhost:3000/dashboard"
    frontend_auth_error_url: str = "http://localhost:3000/auth/error"

    # --- Pagination ---
    cursor_secret: str = Field(min_length=32)

    # --- Rate limiting ---
    rate_limit_enabled: bool = True
    rate_limit_login_per_minute: int = 5
    rate_limit_login_ip_per_minute: int = 20
    rate_limit_register_per_hour: int = 10
    rate_limit_refresh_per_minute: int = 30
    rate_limit_oauth_start_per_minute: int = 10
    rate_limit_complete_per_minute: int = 5
    rate_limit_scan_per_minute: int = 60

    # --- Chain ---
    chain_rpc_url: str = "http://127.0.0.1:8545"
    chain_id: int = 80_002
    contract_address: str = "0x" + "0" * 40
    contract_abi_path: Path = Path("./app/chain/abi/Sutradhar.json")
    chain_signer_private_key: str = ""
    chain_confirmations: int = 3
    chain_max_fee_gwei: int = 100
    chain_tx_timeout_seconds: int = 120
    chain_write_enabled: bool = True
    local_chain_rpc_url: str = "http://127.0.0.1:8545"
    alchemy_cu_monthly_budget: int = 300_000_000
    # Estimates are a lower bound, not a promise -- a block that fills between
    # estimate and inclusion costs more than the estimate said.
    chain_gas_buffer_percent: int = 20
    # 12.5% is the minimum bump most nodes accept for a same-nonce replacement;
    # anything smaller is rejected as an underpriced duplicate.
    chain_rbf_bump_bps: int = 1_250
    # Ceiling on same-nonce replacements before the job is parked. Each bump is
    # 12.5% compounding, so this bounds the worst case at roughly 2.3x the
    # original fee -- and the absolute cap still applies on top.
    chain_max_rbf_attempts: int = 7
    chain_rpc_max_retries: int = 5
    chain_rpc_timeout_seconds: int = 20
    # Alchemy compute units are charged per call; flushing every call would put
    # a Postgres write in front of every eth_getBlockByNumber in a 5s poll loop.
    chain_quota_flush_units: int = 500
    chain_quota_flush_seconds: int = 30

    # --- Crypto-shredding / DPDP ---
    identity_hash_pepper: str = ""

    # --- IPFS / media ---
    pinata_jwt: str = ""
    pinata_gateway_url: str = "https://gateway.pinata.cloud/ipfs"
    ipfs_mirror_dir: Path = Path("./media_mirror")
    media_max_bytes: int = 5_242_880
    pinata_storage_budget_bytes: int = 1_073_741_824
    pinata_timeout_seconds: int = 30
    # The database copy is what survives a redeploy, and it is also the only
    # tier whose limit takes the whole API down when it is reached: a full
    # Postgres refuses every write, not just uploads. So it gets its own
    # ceiling and its own budget, both smaller than the Pinata ones.
    #
    # 2 MB inline covers every photograph the app accepts and excludes video,
    # which is where the bytes actually are.
    media_blob_max_bytes: int = 2_097_152
    media_blob_budget_bytes: int = 268_435_456
    # Pin retries reuse the Phase 7 outbox, so they reuse its backoff too; this
    # is only how often the media drain looks.
    pin_retry_poll_seconds: int = 120
    pin_max_attempts: int = 6

    # --- Scan anomaly thresholds ---
    scan_anomaly_max_regions: int = 3
    scan_anomaly_max_scans: int = 50
    scan_anomaly_velocity_km_per_h: int = 900
    # Distinct device fingerprints inside the window below. A retail display
    # scanned by a dozen shoppers is normal; one tag answering to a dozen
    # devices in an hour when it should be in one person's hands is not.
    scan_anomaly_max_devices: int = 5
    scan_anomaly_device_window_minutes: int = 60
    # Two scans from the same device, place and network inside this window are
    # one scan that got retried, not two events. Without it a double-tap on a
    # phone inflates the volume signal.
    scan_dedupe_window_seconds: int = 60

    # --- Workers ---
    scheduler_enabled: bool = True
    outbox_poll_seconds: int = 5
    confirmation_poll_seconds: int = 15
    indexer_poll_seconds: int = 20
    reconcile_cron: str = "*/30 * * * *"
    outbox_max_attempts: int = 6
    outbox_batch_size: int = 10
    # A worker that dies holding a claim releases it after this long. Must stay
    # comfortably above CHAIN_TX_TIMEOUT_SECONDS, or a slow-but-alive send gets
    # its row stolen by a second worker and sent twice.
    outbox_lock_stale_seconds: int = 600
    outbox_backoff_cap_seconds: int = 300
    # eth_getLogs responses are capped by the provider; a fixed window keeps
    # every request inside that cap instead of discovering it at 10k blocks.
    indexer_block_range: int = 2_000
    merkle_batch_size: int = 1_000
    # The demo anchors items one at a time so each registration has its own
    # visible transaction. Batching is built, tested, and off by default.
    batching_enabled: bool = False

    # --- Bootstrap ---
    # Not a .local / .test address: those are reserved TLDs and EmailStr
    # rejects them, so an admin seeded under one could be created but never
    # log in through /auth/login.
    seed_admin_email: str = "admin@sutradhar.example.com"
    seed_admin_password: str = "change_me_locally"

    # ---------------------------------------------------------------- validators
    @field_validator("database_url", "database_url_sync", "test_database_url")
    @classmethod
    def _reject_blank_dsn(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("database DSN must not be empty")
        return value

    @field_validator("public_prefix")
    @classmethod
    def _normalise_mount(cls, value: str) -> str:
        """Empty, or a path starting with ``/`` and not ending in one.

        Surrounding whitespace and a trailing slash are typing, so they are
        normalised away. Anything that cannot be a mount path is refused
        outright rather than mangled into something that boots: this value is
        the prefix of the URL printed on cloth, and a prefix that quietly
        became a different string than the operator wrote is the one
        misconfiguration nobody would catch before the labels came off the
        printer. Starlette would accept ``/http:`` without complaint.
        """
        cleaned = value.strip()
        if not cleaned:
            return ""

        if cleaned.startswith("//") or "://" in cleaned:
            raise ValueError(
                f"PUBLIC_PREFIX must be a path, not a URL or a host: {value!r}. "
                "The origin belongs in PUBLIC_BASE_URL."
            )
        if any(character.isspace() for character in cleaned):
            raise ValueError(
                f"PUBLIC_PREFIX must not contain whitespace: {value!r}"
            )
        for character in "?#":
            if character in cleaned:
                raise ValueError(
                    f"PUBLIC_PREFIX must not contain {character!r}: {value!r}. "
                    "It is a mount path, not a URL with a query or a fragment."
                )

        # "/" and "" mean the same mount; everything else keeps its shape.
        cleaned = cleaned.rstrip("/")
        if not cleaned:
            return ""
        return cleaned if cleaned.startswith("/") else f"/{cleaned}"

    @field_validator("app_base_url", "public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """Normalise the origin so joins never produce a double slash.

        A QR payload is compared byte for byte in tests and printed on fabric;
        ``.../v/CODE`` and ``...//v/CODE`` are two different strings and one of
        them is wrong.
        """
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            raise ValueError("base URL must not be empty")
        return cleaned

    @field_validator("jwt_private_key_path", "jwt_public_key_path")
    @classmethod
    def _resolve_key_path(cls, value: Path) -> Path:
        resolved = value if value.is_absolute() else (BACKEND_DIR / value)
        resolved = resolved.resolve()
        if not resolved.is_file():
            raise ValueError(
                f"signing key not found at {resolved} — run `uv run python scripts/gen_keys.py`"
            )
        return resolved

    @field_validator("contract_abi_path", "ipfs_mirror_dir")
    @classmethod
    def _anchor_to_backend(cls, value: Path) -> Path:
        return value if value.is_absolute() else (BACKEND_DIR / value)

    # ---------------------------------------------------------------- computed
    @property
    def cors_origins(self) -> list[str]:
        """CORS_ALLOWED_ORIGINS split into a list, blanks dropped."""
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @property
    def google_oauth_enabled(self) -> bool:
        """True only when both Google client credentials are present."""
        return bool(self.google_client_id.strip() and self.google_client_secret.strip())

    @property
    def pinata_enabled(self) -> bool:
        """True when a Pinata JWT is configured; otherwise only the local mirror is usable."""
        return bool(self.pinata_jwt.strip())

    @property
    def chain_signer_configured(self) -> bool:
        """True when a relayer key is present; otherwise the outbox can never send."""
        return bool(self.chain_signer_private_key.strip())

    @property
    def google_redirect_uri(self) -> str:
        """Absolute callback URI registered with the Google OAuth client."""
        return f"{self.oauth_redirect_base_url.rstrip('/')}{self.google_redirect_path}"

    @property
    def is_production(self) -> bool:
        """True when running under the production environment profile."""
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton."""
    # Values are supplied by the environment and .env, not by keyword arguments.
    return Settings()


# Fail fast: surface configuration errors at import time, not at first request.
get_settings()
