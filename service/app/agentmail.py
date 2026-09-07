"""Thin AgentMail REST client (httpx). Only the handful of calls we need.

Docs: https://docs.agentmail.to/api-reference.md
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from .config import settings

log = logging.getLogger(__name__)


class AgentMailError(RuntimeError):
    pass


class AgentMail:
    def __init__(self, api_key: Optional[str] = None, inbox_id: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or settings.agentmail_api_key
        self.inbox_id = inbox_id or settings.agentmail_inbox_id
        self.base_url = (base_url or settings.agentmail_base_url).rstrip("/")
        if not self.api_key:
            raise AgentMailError("AGENTMAIL_API_KEY is not set")

    # --- internals -----------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _inbox_path(self, suffix: str) -> str:
        return f"{self.base_url}/inboxes/{quote(self.inbox_id, safe='')}{suffix}"

    def _request(self, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        with httpx.Client(timeout=30) as client:
            resp = client.request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code >= 400:
            raise AgentMailError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")
        if not resp.content:
            return {}
        return resp.json()

    # --- inbox / auth ---------------------------------------------------------
    def whoami(self) -> Dict[str, Any]:
        return self._request("GET", f"{self.base_url}/auth/me")

    # --- messages ------------------------------------------------------------
    def get_message(self, message_id: str) -> Dict[str, Any]:
        return self._request("GET", self._inbox_path(f"/messages/{quote(message_id, safe='')}"))

    def list_messages(self, limit: int = 20, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        if labels:
            params["labels"] = labels
        return self._request("GET", self._inbox_path("/messages"), params=params)

    def send(
        self,
        to: List[str],
        subject: str,
        text: str,
        html: Optional[str] = None,
        inline_png: Optional[bytes] = None,
        inline_png_cid: str = "qr",
        labels: Optional[List[str]] = None,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        for addr in to:
            domain = addr.rsplit("@", 1)[-1].lower()
            if domain.endswith((".invalid", ".test", ".example")) or domain in ("example.com", "example.org", "example.net"):
                raise AgentMailError(f"refusing to send to reserved test address {addr}")
        body: Dict[str, Any] = {"to": to, "subject": subject, "text": text}
        if html:
            body["html"] = html
        if labels:
            body["labels"] = labels
        if reply_to:
            body["reply_to"] = reply_to
        if inline_png:
            body["attachments"] = [
                {
                    "filename": "pickup-code.png",
                    "content_type": "image/png",
                    "content_disposition": "inline",
                    "content_id": inline_png_cid,
                    "content": base64.b64encode(inline_png).decode("ascii"),
                }
            ]
        return self._request("POST", self._inbox_path("/messages/send"), json=body)

    def reply(self, message_id: str, text: str, html: Optional[str] = None, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"text": text}
        if html:
            body["html"] = html
        if labels:
            body["labels"] = labels
        return self._request("POST", self._inbox_path(f"/messages/{quote(message_id, safe='')}/reply"), json=body)

    def forward(self, message_id: str, to: List[str], text: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"to": to}
        if text:
            body["text"] = text
        return self._request("POST", self._inbox_path(f"/messages/{quote(message_id, safe='')}/forward"), json=body)

    def update_message_labels(self, message_id: str, add: Optional[List[str]] = None, remove: Optional[List[str]] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if add:
            body["add_labels"] = add
        if remove:
            body["remove_labels"] = remove
        return self._request("PATCH", self._inbox_path(f"/messages/{quote(message_id, safe='')}"), json=body)

    # --- webhooks ------------------------------------------------------------
    def list_webhooks(self) -> Dict[str, Any]:
        return self._request("GET", self._inbox_path("/webhooks"))

    def create_webhook(self, url: str, event_types: Optional[List[str]] = None, client_id: str = "scottylabs-merch-webhook") -> Dict[str, Any]:
        body = {"url": url, "event_types": event_types or ["message.received"], "client_id": client_id}
        return self._request("POST", self._inbox_path("/webhooks"), json=body)

    def delete_webhook(self, webhook_id: str) -> Dict[str, Any]:
        return self._request("DELETE", self._inbox_path(f"/webhooks/{quote(webhook_id, safe='')}"))
