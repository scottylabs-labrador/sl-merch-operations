"""Runtime configuration, all from environment variables.

Every knob the org might need to change lives here so nobody has to edit code
to move pickup, rename the inbox, or rotate a passcode.
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
    # Orders come from the store export an officer uploads on /admin. Creating
    # orders from emails (officer notifications, buyer-forwarded receipts) is
    # off by default because forwarded receipts can be faked.
    email_order_intake: bool = field(default_factory=lambda: _env("EMAIL_ORDER_INTAKE", "0").lower() in ("1", "true", "yes"))
    # Export rows whose item name contains any of these (case-insensitive) are not merch.
    export_ignore_items: List[str] = field(default_factory=lambda: _env_list("EXPORT_IGNORE_ITEMS", "donation"))
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
    # Where and when buyers collect merch. Every buyer email and the support desk use it.
    # GBM_INFO is the old name of this setting and is still read.
    pickup_info: str = field(
        default_factory=lambda: _env("PICKUP_INFO")
        or _env("GBM_INFO")
        or (
            "the ScottyLabs Worksession in Tepper 3808 (the Oval Room), 3rd floor of the Tepper School of Business building, "
            "across from the Swartz Center, on Saturdays from 4:00 to 5:00 PM. Dates, times and the room "
            "can change at ScottyLabs' discretion; any change is announced by email."
        )
    )
    price_text: str = field(default_factory=lambda: _env("PRICE_TEXT", "$10.40 (sold at cost; the price covers the shirt plus card fees)"))
    # Call/text consent only counts for purchases made after the checkout prompt that
    # carries the disclosure went live (docs/checkout-prompt-log.md, version 1).
    consent_prompt_effective_from: str = field(default_factory=lambda: _env("CONSENT_PROMPT_EFFECTIVE_FROM", "2026-09-22T18:55:00Z"))
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
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "z-ai/glm-5.3-flash"))
    # OpenRouter provider routing: only these providers, tried in this order, and
    # only their zero-data-retention endpoints that do not train on prompts.
    llm_providers: List[str] = field(default_factory=lambda: _env_list("LLM_PROVIDERS", "coreweave,fireworks,baseten"))
    llm_zdr: bool = field(default_factory=lambda: _env("LLM_ZDR", "1").lower() in ("1", "true", "yes"))
    llm_timeout_seconds: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_SECONDS", "60") or 60))
    # Bound every call so adversarial prompts cannot make the model think for 20-45 s. The cap
    # covers reasoning AND the answer, so it must leave ample room for a full reply.
    llm_max_tokens: int = field(default_factory=lambda: int(_env("LLM_MAX_TOKENS", "4096") or 4096))
    llm_reasoning_effort: str = field(default_factory=lambda: _env("LLM_REASONING_EFFORT", "low"))

    # --- Jev (Typesafe System One): typed decisions, never text --------------
    # Classifies emails, screens replies, and judges export rows with calibrated
    # probabilities. Served through OpenRouter's decisions endpoint with the
    # OpenRouter key by default; point JEV_URL at Typesafe's own API
    # (https://api.typesafe.ai/v1/systemone) with JEV_API_KEY to go direct.
    # JEV_ENABLED=0 or no key = the regex and LLM paths are used instead.
    jev_enabled: bool = field(default_factory=lambda: _env("JEV_ENABLED", "1").lower() in ("1", "true", "yes"))
    jev_url: str = field(default_factory=lambda: _env("JEV_URL", "https://openrouter.ai/api/alpha/decisions"))
    jev_model: str = field(default_factory=lambda: _env("JEV_MODEL", "typesafe/jev-1.13"))
    jev_api_key: str = field(default_factory=lambda: _env("JEV_API_KEY") or _env("TYPESAFE_API_KEY"))
    jev_timeout_seconds: float = field(default_factory=lambda: float(_env("JEV_TIMEOUT_SECONDS", "15") or 15))

    @property
    def jev_key(self) -> str:
        """The key to send: an explicit Jev key, else the OpenRouter key when Jev goes through OpenRouter."""
        if self.jev_api_key:
            return self.jev_api_key
        return self.llm_api_key if "openrouter.ai" in self.jev_url else ""

    # --- Scheduled jobs ------------------------------------------------------
    # Day-of-week (mon..sun) and 24h time (America/New_York) for the "bring
    # these sizes" email to the org. Empty string disables it.
    bring_list_day: str = field(default_factory=lambda: _env("BRING_LIST_DAY", "sat"))
    bring_list_time: str = field(default_factory=lambda: _env("BRING_LIST_TIME", "10:00"))
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
