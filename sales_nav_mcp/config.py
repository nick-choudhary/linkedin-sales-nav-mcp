"""Configuration schema and environment loader.

Dataclasses with a validate() method, populated from the environment and held
in a lazy singleton. The config surface is about the local browser (profile
dir, headless, timeouts) that the tools drive.
"""

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Browser work is slow: navigation plus multi-page scroll can take a while, so
# the default per-tool budget is generous. Human pacing dominates it — a full
# 10-page call spends ~150s worst-case in deliberate delays alone (nine gaps of
# up to 8s, plus the long breaks), before any page actually loads. 300s used to
# be ample; with pacing on it is not, hence 600.
DEFAULT_TOOL_TIMEOUT_SECONDS: float = 600.0
DEFAULT_NAV_TIMEOUT_SECONDS: float = 60.0
DEFAULT_CAPTURE_WAIT_SECONDS: float = 25.0
DEFAULT_LOGIN_TIMEOUT_SECONDS: float = 300.0
# Close the browser after this long with no tool call, relaunching on demand.
# One Chromium is otherwise resident for the whole server lifetime, which is
# fine while you are working and wasteful when you are not. Relaunch costs a
# few seconds and never costs the login, which lives in the profile directory.
# 0 keeps the browser open indefinitely.
DEFAULT_IDLE_BROWSER_TIMEOUT_SECONDS: float = 3600.0
DEFAULT_USER_DATA_DIR: str = "~/.linkedin-sales-nav/profile"
# The store sits beside the profile it was captured with: both are state
# belonging to one LinkedIn account, not to whatever folder you launched in.
DEFAULT_STATE_DIR: str = "~/.linkedin-sales-nav"
# Exports are project artifacts, so this one stays relative to the working
# directory — the CSV lands next to the work it was pulled for.
DEFAULT_OUTPUT_DIR: str = "output"

# Pacing defaults. Deliberately slow: a page every few seconds with a real
# break every handful of pages is what ordinary browsing looks like. Going
# faster is the single easiest way to make this traffic stand out.
# Outreach. Sending is the only thing here that writes to LinkedIn, so every
# default is the safe one: disabled, dry-run, free channel only.
# Pipeline depth the server is willing to run. Depth 1 (search) is
# always allowed. Depth 2 (enrich / Open Profile) is cheap and on by
# default. Depth 3 (full profile fetch) costs a heavy request per lead
# and is opt-in, so a default install cannot be pointed at a list and
# made to pull thousands of full profiles.
DEFAULT_ENABLE_ENRICH: bool = True
DEFAULT_ENABLE_PROFILE: bool = False
DEFAULT_SEND_DAILY_CAP: int = 40
DEFAULT_SEND_DELAY_MIN_SECONDS: float = 45.0
DEFAULT_SEND_DELAY_MAX_SECONDS: float = 120.0
DEFAULT_SUBJECT_MAX_CHARS: int = 120
DEFAULT_BODY_MAX_CHARS: int = 1900

DEFAULT_PAGE_DELAY_MIN_SECONDS: float = 3.0
DEFAULT_PAGE_DELAY_MAX_SECONDS: float = 8.0
DEFAULT_LONG_PAUSE_EVERY_PAGES: int = 5
DEFAULT_LONG_PAUSE_MIN_SECONDS: float = 20.0
DEFAULT_LONG_PAUSE_MAX_SECONDS: float = 45.0


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""


@dataclass
class BrowserConfig:
    """Local browser settings."""

    user_data_dir: str = DEFAULT_USER_DATA_DIR
    # Headed by default and deliberately so: a real window on your own machine
    # carries the genuine fingerprint that keeps the session healthy. Headless
    # is more detectable — only turn it on if you know your setup tolerates it.
    headless: bool = False
    chrome_path: str | None = None
    # Optional proxy for the browser's own traffic. Leave unset when running
    # on your own machine (the whole point is to look like your normal use).
    proxy_server: str | None = None
    nav_timeout_seconds: float = DEFAULT_NAV_TIMEOUT_SECONDS
    capture_wait_seconds: float = DEFAULT_CAPTURE_WAIT_SECONDS
    login_timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS
    idle_timeout_seconds: float = DEFAULT_IDLE_BROWSER_TIMEOUT_SECONDS

    def resolved_user_data_dir(self) -> Path:
        return Path(self.user_data_dir).expanduser()

    def validate(self) -> None:
        # 0 is meaningful here (keep the browser open indefinitely), so it is
        # checked separately from the strictly-positive timeouts below.
        if not (
            math.isfinite(self.idle_timeout_seconds) and self.idle_timeout_seconds >= 0
        ):
            raise ConfigurationError(
                "IDLE_BROWSER_TIMEOUT must be a finite number >= 0 "
                f"(0 disables it), got {self.idle_timeout_seconds}"
            )
        for name in (
            "nav_timeout_seconds",
            "capture_wait_seconds",
            "login_timeout_seconds",
        ):
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0):
                raise ConfigurationError(
                    f"{name} must be a positive finite number, got {value}"
                )
        if self.chrome_path:
            path = Path(self.chrome_path).expanduser()
            if not path.is_file():
                raise ConfigurationError(
                    f"CHROME_PATH '{self.chrome_path}' is not a file"
                )
        if self.proxy_server and "://" not in self.proxy_server:
            raise ConfigurationError(
                f"PROXY_SERVER must be scheme://host:port, got '{self.proxy_server}'"
            )


@dataclass
class PacingConfig:
    """Human-like delays between result pages (see `pacing.py`).

    Only the page-cadence knobs are environment-configurable — those are the
    ones that change how much traffic you generate. The scroll parameters are
    cosmetic rhythm and stay code-tunable to keep the env surface small.
    """

    enabled: bool = True
    page_delay_min: float = DEFAULT_PAGE_DELAY_MIN_SECONDS
    page_delay_max: float = DEFAULT_PAGE_DELAY_MAX_SECONDS
    # 0 disables the long break entirely.
    long_pause_every: int = DEFAULT_LONG_PAUSE_EVERY_PAGES
    long_pause_min: float = DEFAULT_LONG_PAUSE_MIN_SECONDS
    long_pause_max: float = DEFAULT_LONG_PAUSE_MAX_SECONDS
    scroll_steps_min: int = 2
    scroll_steps_max: int = 4
    scroll_pixels_min: int = 1200
    scroll_pixels_max: int = 2600
    scroll_gap_min: float = 0.4
    scroll_gap_max: float = 1.2

    def validate(self) -> None:
        pairs = (
            ("page_delay_min", "page_delay_max"),
            ("long_pause_min", "long_pause_max"),
            ("scroll_gap_min", "scroll_gap_max"),
            ("scroll_steps_min", "scroll_steps_max"),
            ("scroll_pixels_min", "scroll_pixels_max"),
        )
        for low_name, high_name in pairs:
            low, high = getattr(self, low_name), getattr(self, high_name)
            for name, value in ((low_name, low), (high_name, high)):
                if not (math.isfinite(value) and value >= 0):
                    raise ConfigurationError(
                        f"{name} must be a non-negative finite number, got {value}"
                    )
            if low > high:
                raise ConfigurationError(
                    f"{low_name} ({low}) must not exceed {high_name} ({high})"
                )
        if self.scroll_steps_min < 1:
            raise ConfigurationError(
                f"scroll_steps_min must be at least 1, got {self.scroll_steps_min}"
            )
        if self.long_pause_every < 0:
            raise ConfigurationError(
                "long_pause_every must be 0 (disabled) or a positive number of "
                f"pages, got {self.long_pause_every}"
            )


@dataclass
class ServerConfig:
    """MCP server configuration (transport, logging, timeouts)."""

    transport: Literal["stdio", "streamable-http"] = "stdio"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "WARNING"
    host: str = "127.0.0.1"
    port: int = 9000
    path: str = "/mcp"
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS

    def validate(self) -> None:
        if not (
            math.isfinite(self.tool_timeout_seconds) and self.tool_timeout_seconds > 0
        ):
            raise ConfigurationError(
                f"tool_timeout_seconds must be a positive finite number, "
                f"got {self.tool_timeout_seconds}"
            )
        if not (1 <= self.port <= 65535):
            raise ConfigurationError(
                f"Port {self.port} is not in valid range (1-65535)"
            )
        if not self.path.startswith("/") or len(self.path) < 2:
            raise ConfigurationError(
                f"HTTP path '{self.path}' must start with '/' and be at least "
                "2 characters"
            )


@dataclass
class StorageConfig:
    """Where the store lives, and where exports are written.

    Two homes, because the two have different lifetimes. The database and the
    archived raw captures are account state: they follow the browser profile
    and stay the same wherever the server is launched from. Exports are
    project artifacts, so they resolve against the working directory and land
    in the project you ran the search for.
    """

    state_dir: str = DEFAULT_STATE_DIR
    output_dir: str = DEFAULT_OUTPUT_DIR

    def resolved_state_dir(self) -> Path:
        return Path(self.state_dir).expanduser().resolve()

    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir).expanduser().resolve()

    def db_path(self) -> Path:
        return self.resolved_state_dir() / "sales_nav.db"

    def raw_dir(self, url_hash: str) -> Path:
        """Archived API responses for one query — state, kept with the DB."""
        return self.resolved_state_dir() / url_hash / "raw"

    def export_dir(self, url_hash: str) -> Path:
        """Export destination for one query — an artifact, kept in the project."""
        return self.resolved_output_dir() / url_hash

    def validate(self) -> None:
        # Directories are created on first write, not here — validation must
        # not have side effects.
        if not self.state_dir:
            raise ConfigurationError("STATE_DIR must not be empty")
        if not self.output_dir:
            raise ConfigurationError("OUTPUT_DIR must not be empty")


@dataclass
class OutreachConfig:
    """Sending messages — the only capability that writes to LinkedIn.

    Every default is chosen so that a fresh install, including one from PyPI,
    cannot send anything. `enabled` is off; even switched on, `send_message`
    defaults to a dry run, and spending an InMail credit needs a second,
    separate opt-in. The free Open Profile channel is the intended path.
    """

    enabled: bool = False
    # Pipeline depth gates. See DEFAULT_ENABLE_* above.
    enable_enrich: bool = DEFAULT_ENABLE_ENRICH
    enable_profile: bool = DEFAULT_ENABLE_PROFILE
    # Allow messages that consume an InMail credit. Open Profile messages are
    # free and unlimited-ish; credits are a finite monthly budget, so spending
    # one is never implicit.
    allow_credit_spend: bool = False
    daily_cap: int = DEFAULT_SEND_DAILY_CAP
    # Gap between sends. Far longer than page pacing: a burst of messages is a
    # much louder signal than a burst of reads.
    delay_min_seconds: float = DEFAULT_SEND_DELAY_MIN_SECONDS
    delay_max_seconds: float = DEFAULT_SEND_DELAY_MAX_SECONDS
    subject_max_chars: int = DEFAULT_SUBJECT_MAX_CHARS
    body_max_chars: int = DEFAULT_BODY_MAX_CHARS
    # Path to YOUR offer/positioning file. Deliberately has no default that
    # exists -- the composer prompt refuses to render without it, so a public
    # install has nothing to sell and cannot pretend otherwise.
    offer_file: str = ""

    def resolved_offer_file(self) -> Path | None:
        if not self.offer_file:
            return None
        return Path(self.offer_file).expanduser().resolve()

    def validate(self) -> None:
        if self.daily_cap < 0:
            raise ConfigurationError("SEND_DAILY_CAP must be >= 0")
        if self.delay_min_seconds < 0 or self.delay_max_seconds < 0:
            raise ConfigurationError("Send delays must be >= 0")
        if self.delay_min_seconds > self.delay_max_seconds:
            raise ConfigurationError(
                "SEND_DELAY_MIN must be <= SEND_DELAY_MAX "
                f"(got {self.delay_min_seconds} > {self.delay_max_seconds})"
            )
        if self.subject_max_chars < 1 or self.body_max_chars < 1:
            raise ConfigurationError("Message length caps must be >= 1")
        # A missing offer file must NOT be fatal. It is needed only by the
        # compose prompt, which already explains its absence and refuses to
        # render. Raising here would take down search, enrichment and export
        # too -- an optional outreach file bricking the whole server is a far
        # worse failure than a prompt that declines to draft.
        path = self.resolved_offer_file()
        if path is not None and not path.is_file():
            reason = "is a directory, not a file" if path.is_dir() else "does not exist"
            logger.warning(
                "OFFER_FILE '%s' %s; the compose prompt will refuse to render "
                "until it points at a readable file. Everything else is "
                "unaffected.",
                self.offer_file,
                reason,
            )


@dataclass
class AppConfig:
    """Main application configuration."""

    browser: BrowserConfig = field(default_factory=BrowserConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    pacing: PacingConfig = field(default_factory=PacingConfig)
    outreach: OutreachConfig = field(default_factory=OutreachConfig)
    # --login one-shot mode, set from the CLI, not the environment.
    login: bool = False

    def validate(self) -> None:
        self.browser.validate()
        self.server.validate()
        self.storage.validate()
        self.pacing.validate()
        self.outreach.validate()


def _float_env(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigurationError(f"Invalid {key}: '{raw}'. Must be a number.") from e


def _int_env(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigurationError(f"Invalid {key}: '{raw}'. Must be an integer.") from e


def _bool_env(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_config() -> AppConfig:
    """Build an AppConfig from the environment (.env is honoured)."""
    load_dotenv()

    config = AppConfig()

    if user_data_dir := os.environ.get("USER_DATA_DIR"):
        config.browser.user_data_dir = user_data_dir
    config.browser.headless = _bool_env("HEADLESS", False)
    if chrome_path := os.environ.get("CHROME_PATH"):
        config.browser.chrome_path = chrome_path
    if proxy_server := os.environ.get("PROXY_SERVER"):
        config.browser.proxy_server = proxy_server
    config.browser.nav_timeout_seconds = _float_env(
        "NAV_TIMEOUT", DEFAULT_NAV_TIMEOUT_SECONDS
    )
    config.browser.capture_wait_seconds = _float_env(
        "CAPTURE_WAIT", DEFAULT_CAPTURE_WAIT_SECONDS
    )
    config.browser.idle_timeout_seconds = _float_env(
        "IDLE_BROWSER_TIMEOUT", DEFAULT_IDLE_BROWSER_TIMEOUT_SECONDS
    )
    config.browser.login_timeout_seconds = _float_env(
        "LOGIN_TIMEOUT", DEFAULT_LOGIN_TIMEOUT_SECONDS
    )

    if transport := os.environ.get("TRANSPORT"):
        if transport not in ("stdio", "streamable-http"):
            raise ConfigurationError(
                f"Invalid TRANSPORT: '{transport}'. "
                "Must be 'stdio' or 'streamable-http'."
            )
        config.server.transport = transport  # type: ignore[assignment]
    config.outreach.enabled = _bool_env("ENABLE_SENDING", False)
    config.outreach.enable_enrich = _bool_env("ENABLE_ENRICH", DEFAULT_ENABLE_ENRICH)
    config.outreach.enable_profile = _bool_env("ENABLE_PROFILE", DEFAULT_ENABLE_PROFILE)
    config.outreach.allow_credit_spend = _bool_env("ALLOW_CREDIT_SPEND", False)
    config.outreach.daily_cap = _int_env("SEND_DAILY_CAP", DEFAULT_SEND_DAILY_CAP)
    config.outreach.delay_min_seconds = _float_env(
        "SEND_DELAY_MIN", DEFAULT_SEND_DELAY_MIN_SECONDS
    )
    config.outreach.delay_max_seconds = _float_env(
        "SEND_DELAY_MAX", DEFAULT_SEND_DELAY_MAX_SECONDS
    )
    config.outreach.subject_max_chars = _int_env(
        "SUBJECT_MAX_CHARS", DEFAULT_SUBJECT_MAX_CHARS
    )
    config.outreach.body_max_chars = _int_env("BODY_MAX_CHARS", DEFAULT_BODY_MAX_CHARS)
    if offer_file := os.environ.get("OFFER_FILE"):
        config.outreach.offer_file = offer_file
    if log_level := os.environ.get("LOG_LEVEL"):
        level = log_level.upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigurationError(
                f"Invalid LOG_LEVEL: '{log_level}'. "
                "Must be DEBUG, INFO, WARNING, or ERROR."
            )
        config.server.log_level = level  # type: ignore[assignment]
    if host := os.environ.get("HOST"):
        config.server.host = host
    if port := os.environ.get("PORT"):
        try:
            config.server.port = int(port)
        except ValueError as e:
            raise ConfigurationError(
                f"Invalid PORT: '{port}'. Must be an integer."
            ) from e
    if path := os.environ.get("HTTP_PATH"):
        config.server.path = path
    config.server.tool_timeout_seconds = _float_env(
        "TOOL_TIMEOUT", DEFAULT_TOOL_TIMEOUT_SECONDS
    )

    if state_dir := os.environ.get("STATE_DIR"):
        config.storage.state_dir = state_dir
    if output_dir := os.environ.get("OUTPUT_DIR"):
        config.storage.output_dir = output_dir

    config.pacing.enabled = _bool_env("PACING_ENABLED", True)
    config.pacing.page_delay_min = _float_env(
        "PAGE_DELAY_MIN", DEFAULT_PAGE_DELAY_MIN_SECONDS
    )
    config.pacing.page_delay_max = _float_env(
        "PAGE_DELAY_MAX", DEFAULT_PAGE_DELAY_MAX_SECONDS
    )
    config.pacing.long_pause_every = _int_env(
        "LONG_PAUSE_EVERY", DEFAULT_LONG_PAUSE_EVERY_PAGES
    )
    config.pacing.long_pause_min = _float_env(
        "LONG_PAUSE_MIN", DEFAULT_LONG_PAUSE_MIN_SECONDS
    )
    config.pacing.long_pause_max = _float_env(
        "LONG_PAUSE_MAX", DEFAULT_LONG_PAUSE_MAX_SECONDS
    )

    config.validate()
    return config


_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Return the process-wide configuration, loading it on first use."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Testing hook: drop the cached configuration."""
    global _config
    _config = None
