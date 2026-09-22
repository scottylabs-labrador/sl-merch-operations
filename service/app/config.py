"""Runtime configuration, all from environment variables.

Every knob the org might need to change lives here so nobody has to edit code
to move the GBM, rename the inbox, or rotate a passcode.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

# Load ../.env (repo root) and ./.env (service dir) if present. Railway injects
# real env vars, so this is a no-op in production.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_list(name: str, default: str = "") -> List[str]:
    raw = _env(name, default)
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # --- AgentMail -----------------------------------------------------------
    agentmail_api_key: str = field(
        default_factory=lambda: _env("AGENTMAIL_API_KEY")
        or _env("SCOTTYLABS_MERCH_AGENTMAIL_API_TOKEN")
    )
    agentmail_inbox_id: str = field(
        default_factory=lambda: _env("AGENTMAIL_INBOX_ID", "scottylabs-merch@agentmail.to")
    )
    agentmail_webhook_secret: str = field(default_factory=lambda: _env("AGENTMAIL_WEBHOOK_SECRET"))
    agentmail_base_url: str = field(
        default_factory=lambda: _env("AGENTMAIL_BASE_URL", "https://api.agentmail.to/v0")
    )
    from_display_name: str = field(default_factory=lambda: _env("FROM_DISPLAY_NAME", "ScottyLabs Merch"))

    # --- Order intake --------------------------------------------------------
    # Only emails whose From address is in this list are treated as purchase
    # notifications. tartanconnect@andrew.cmu.edu is the platform sender; the
    # others cover Gmail auto-forwarding from an officer or the org account.
    trusted_senders: List[str] = field(
        default_factory=lambda: _env_list(
            "TRUSTED_SENDERS",
            "tartanconnect@andrew.cmu.edu,scottylabs@cmu.edu,scottylabs@andrew.cmu.edu",
        )
    )
    # The platform's own sender. Mail from here that is not a purchase/refund is
    # ignored; mail from any human (including officers on TRUSTED_SENDERS) that is
    # not a purchase goes to the support agent.
    platform_senders: List[str] = field(default_factory=lambda: _env_list("PLATFORM_SENDERS", "tartanconnect@andrew.cmu.edu"))
    store_name: str = field(default_factory=lambda: _env("STORE_NAME", "ScottyLabs Merch Store"))

    # --- Org / pickup logistics ---------------------------------------------
    org_email: str = field(default_factory=lambda: _env("ORG_EMAIL", "scottylabs@cmu.edu"))
    org_name: str = field(default_factory=lambda: _env("ORG_NAME", "ScottyLabs"))
    gbm_info: str = field(
        default_factory=lambda: _env(
            "GBM_INFO",
            "our weekly ScottyLabs GBM (general body meeting). Times and rooms are posted at https://scottylabs.org and https://luma.com/scottylabs.",
        )
    )
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000"))

    # --- Access control ------------------------------------------------------
    volunteer_passcode: str = field(default_factory=lambda: _env("VOLUNTEER_PASSCODE", "change-me-volunteer"))
    admin_passcode: str = field(default_factory=lambda: _env("ADMIN_PASSCODE", "change-me-admin"))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET", "change-me-session-secret"))

    # --- Storage -------------------------------------------------------------
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./merch.db"))

    # --- Optional LLM assist (OpenRouter) ------------------------------------
    # Any OpenAI-compatible Chat Completions endpoint works; OpenRouter is the
    # default so one key covers every model vendor. Empty key = no model calls.
    llm_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://openrouter.ai/api/v1"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "openai/gpt-6-astra"))
    llm_timeout_seconds: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_SECONDS", "60") or 60))

    # --- Scheduled jobs ------------------------------------------------------
    # Day-of-week (mon..sun) and 24h time (America/New_York) for the "bring
    # these sizes" email to the org. Empty string disables it.
    bring_list_day: str = field(default_factory=lambda: _env("BRING_LIST_DAY", "tue"))
    bring_list_time: str = field(default_factory=lambda: _env("BRING_LIST_TIME", "09:00"))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "America/New_York"))
    # Inbox polling backstop (seconds between polls; 0 disables) and how far back to look.
    poll_interval_seconds: int = field(default_factory=lambda: int(_env("POLL_INTERVAL_SECONDS", "60") or 0))
    poll_max_age_hours: int = field(default_factory=lambda: int(_env("POLL_MAX_AGE_HOURS", "24") or 24))

    @property
    def database_url_normalized(self) -> str:
        """Railway hands out postgres:// URLs; SQLAlchemy wants postgresql+psycopg://."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    @property
    def is_production(self) -> bool:
        return not self.database_url.startswith("sqlite")


settings = Settings()
