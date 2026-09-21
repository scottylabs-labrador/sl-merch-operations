"""Structured model calls through OpenRouter.

Every model use in the service (purchase-email parse fallback, buyer-reply
intent, support-desk decision) goes through ``structured_json``. The model can
only ever return JSON that matches a strict schema; it never composes free text
to a buyer.

OpenRouter speaks the OpenAI Chat Completions format, so the official ``openai``
package is the client with ``base_url`` pointed at OpenRouter. Point
``LLM_BASE_URL`` at any other OpenAI-compatible endpoint to switch providers.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .config import settings

log = logging.getLogger(__name__)


def llm_enabled() -> bool:
    return bool(settings.llm_api_key)


def _is_openrouter() -> bool:
    return "openrouter.ai" in settings.llm_base_url


def _client():
    from openai import OpenAI  # lazy: never imported when no key is configured

    headers: Dict[str, str] = {}
    if _is_openrouter():
        # Optional attribution headers OpenRouter uses for its app listings.
        headers = {"HTTP-Referer": settings.public_base_url, "X-Title": f"{settings.org_name} Merch Desk"}
    return OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        default_headers=headers,
        timeout=settings.llm_timeout_seconds,
        max_retries=1,
    )


def structured_json(
    *, label: str, system: str, user: str, schema_name: str, schema: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """One chat completion that must return JSON matching ``schema``.

    Returns the parsed dict, or ``None`` when no API key is configured or when
    anything fails (network, provider, schema, JSON). Failures are logged under
    ``label`` and the caller degrades to its no-model behaviour.
    """
    if not settings.llm_api_key:
        return None
    try:
        extra: Dict[str, Any] = {}
        if _is_openrouter():
            # Only route to providers that honour response_format, so strict JSON is real.
            extra["provider"] = {"require_parameters": True}
        response = _client().chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
            extra_body=extra or None,
        )
        content = response.choices[0].message.content or ""
        return json.loads(content)
    except Exception as exc:  # noqa: BLE001 - any failure degrades to "no model help"
        log.warning("%s failed: %s", label, exc)
        return None
