"""Jev (Typesafe System One): typed decisions with calibrated probabilities.

Jev never writes text. It answers a batch of questions about a state: a
choice among options, a score on a rubric, or the probability that a
statement is true. Every answer carries a probability, so code can act on
high confidence, hand low confidence to a human, and never guess. This module
holds every judgement call in the service: what an email wants, whether it is
safe to send a reply, whether an export row is merch. Text generation stays
with the LLM in app/llm.py.

Jev is reached through OpenRouter's decisions endpoint (model
``typesafe/jev-1.13``) with the OpenRouter key by default, or through
Typesafe's own API when JEV_URL/JEV_API_KEY say so. Everything here returns
None when Jev is disabled, unconfigured, or failing, so the regex and LLM
paths remain the fallback and the service keeps working.

Choice answers are gated on the probability of the chosen option, not on
Typesafe's peakedness "confidence": a 0.76 vote for one intent against a
catch-all "other" is a clear read even when the distribution is not sharp.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Thresholds follow Typesafe's guidance: act automatically on a clear read,
# hand anything ambiguous to a person. Sending mail is the risky action, so the
# bar to auto-reply is higher than the bar to escalate.
AUTOMATED_THRESHOLD = 0.85   # ignore the message as machine-generated
ESCALATE_THRESHOLD = 0.70    # money, complaints, other people's orders -> officer, no LLM
TEMPLATE_THRESHOLD = 0.80    # answer from a fixed template without the LLM
GUARD_THRESHOLD = 0.70       # block an LLM-written reply
NOT_MERCH_THRESHOLD = 0.80   # export row is not a pickup item
NOTIFICATION_THRESHOLD = 0.80  # trusted-sender email is a refund / purchase notice


def enabled() -> bool:
    return settings.jev_enabled and bool(settings.jev_key)


def _via_openrouter() -> bool:
    return "openrouter.ai" in settings.jev_url


def noul(instructions: str, criteria: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    q: Dict[str, Any] = {"type": "noul", "instructions": instructions}
    if criteria:
        q["criteria"] = criteria
    return q


def choice(instructions: str, criteria: Dict[str, str]) -> Dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions: str, criteria: List[str]) -> Dict[str, Any]:
    return {"type": "score", "instructions": instructions, "criteria": criteria}


def _client() -> httpx.Client:
    headers = {"Authorization": f"Bearer {settings.jev_key}"}
    if _via_openrouter():
        headers.update({"HTTP-Referer": settings.public_base_url, "X-Title": f"{settings.org_name} Merch Desk"})
    return httpx.Client(headers=headers, timeout=settings.jev_timeout_seconds)


def ask(*, label: str, state: Any, questions: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Any]]]:
    """One System One request. Returns the answers keyed by question id, or None."""
    if not enabled():
        return None
    payload: Dict[str, Any] = {"model": settings.jev_model, "state": state, "questions": questions}
    if _via_openrouter():
        # Same data policy as the LLM calls: zero retention, no training on prompts.
        payload["provider"] = {"zdr": True, "data_collection": "deny"}
    try:
        with _client() as client:
            r = client.post(settings.jev_url, json=payload)
            r.raise_for_status()
            body = r.json()
        answers = body.get("answers") or {}
        missing = [k for k in questions if k not in answers]
        if missing:
            log.warning("%s (jev): missing answers %s", label, missing)
            return None
        usage = body.get("usage") or {}
        log.info("%s (jev %s via %s): %d questions, %s input tokens, cost %s", label, body.get("model", "?"), body.get("provider", "direct"), len(questions), usage.get("input_tokens", "?"), usage.get("cost", "?"))
        return answers
    except Exception as exc:  # noqa: BLE001 - any failure degrades to the non-Jev path
        log.warning("%s (jev) failed: %s", label, exc)
        return None


def _p(answer: Dict[str, Any], key: str = "noul") -> float:
    try:
        return float(answer.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _chosen(answer: Dict[str, Any]) -> Tuple[str, float]:
    """(choice, probability of that choice) for a choice answer."""
    choice_key = str(answer.get("choice") or "other")
    probs = answer.get("probabilities") or {}
    try:
        p = float(probs.get(choice_key, answer.get("confidence") or 0.0))
    except (TypeError, ValueError):
        p = 0.0
    return choice_key, p


# --------------------------------------------------------------------------- #
# buyer reply intent (responder)
# --------------------------------------------------------------------------- #

INTENT_CRITERIA = {
    "code_request": "They lost, never got, or want their pickup code again.",
    "delegate": "They ask whether a friend or someone else can pick the order up for them.",
    "cant_make_it": "They cannot attend the meeting and ask what to do.",
    "other": "Anything else: refunds, size changes, complaints, product questions, unrelated.",
}


def classify_intent(text: str) -> Optional[Tuple[str, float]]:
    """(intent, confidence) for a buyer's reply, or None when Jev is unavailable."""
    answers = ask(label="reply intent", state=text[:6000], questions={"intent": choice("What does the buyer want?", INTENT_CRITERIA)})
    if not answers:
        return None
    return _chosen(answers["intent"])


# --------------------------------------------------------------------------- #
# support inbox triage (one fan-out call per email)
# --------------------------------------------------------------------------- #

TRIAGE_CRITERIA = {
    "lost_code": "Wants their pickup code (lost it, never got it, asks what it is).",
    "delegate": "Asks whether someone else can pick the order up for them.",
    "cant_make_it": "Cannot attend the meeting and asks what to do.",
    "pickup_logistics": "Asks when or where pickup happens or how it works.",
    "order_status": "Asks whether their order went through or was picked up.",
    "sizing_or_product": "Asks about sizes, fit, material, or what the product looks like.",
    "how_to_buy": "Asks how or where to buy.",
    "about_org": "Asks about the organization, joining, events, unrelated to the store.",
    "refund_or_money": "Asks for a refund, exchange, or raises a payment problem.",
    "complaint": "Is unhappy with the product, the process, or the organization.",
    "spam_or_unrelated": "Marketing, spam, or nothing to do with the store or the org.",
    "other": "Anything that does not fit the categories above.",
}

# Triage categories that a fixed template answers, and the template name.
TEMPLATE_INTENTS = {"lost_code": "code_request", "delegate": "delegate", "cant_make_it": "cant_make_it"}


@dataclass
class Triage:
    automated: float
    refund_or_money: float
    about_others_order: float
    complaint: float
    category: str
    category_confidence: float
    frustration: float

    @property
    def escalate(self) -> bool:
        return max(self.refund_or_money, self.about_others_order, self.complaint) >= ESCALATE_THRESHOLD

    @property
    def escalate_reason(self) -> str:
        flags = [n for n, v in (("refund/money", self.refund_or_money), ("someone else's order", self.about_others_order), ("complaint", self.complaint)) if v >= ESCALATE_THRESHOLD]
        return ", ".join(flags)

    def summary(self) -> str:
        return (
            f"category {self.category} (conf {self.category_confidence:.2f}); refund/money {self.refund_or_money:.2f}; "
            f"others' order {self.about_others_order:.2f}; complaint {self.complaint:.2f}; frustration {self.frustration:.1f}/2"
        )


def triage_email(text: str, subject: str, from_addr: str) -> Optional[Triage]:
    state = {"from": from_addr, "subject": subject, "body": text[:6000]}
    answers = ask(
        label="support triage",
        state=state,
        questions={
            "automated": noul("Is this an automated or bulk message (auto-reply, out-of-office, bounce, newsletter, system notification) rather than a person writing?"),
            "refund_or_money": noul("Does the sender ask for a refund, a charge reversal, or an exchange, or raise a problem with a payment?"),
            "about_others_order": noul("Does the sender ask for the details or the pickup code of an order that belongs to a different person, not their own purchase?"),
            "complaint": noul("Is the sender complaining or expressing dissatisfaction with the product, the process, or the organization?"),
            "category": choice("What is the sender's main request?", TRIAGE_CRITERIA),
            "frustration": score("How frustrated does the sender appear?", ["Calm and neutral", "Concerned but civil", "Very frustrated or angry"]),
        },
    )
    if not answers:
        return None
    return Triage(
        automated=_p(answers["automated"]),
        refund_or_money=_p(answers["refund_or_money"]),
        about_others_order=_p(answers["about_others_order"]),
        complaint=_p(answers["complaint"]),
        category=_chosen(answers["category"])[0],
        category_confidence=_chosen(answers["category"])[1],
        frustration=_p(answers["frustration"], "score"),
    )


# --------------------------------------------------------------------------- #
# outbound guardrail on LLM-written replies
# --------------------------------------------------------------------------- #

@dataclass
class Guard:
    promises_money: float
    invents_logistics: float
    commits_org: float

    @property
    def flags(self) -> List[str]:
        return [n for n, v in (("promises money", self.promises_money), ("invents logistics", self.invents_logistics), ("commits the org", self.commits_org)) if v >= GUARD_THRESHOLD]


def guard_reply(reply_text: str, policy: Dict[str, Any]) -> Optional[Guard]:
    """Screen a draft reply before it is sent. None when Jev is unavailable."""
    answers = ask(
        label="reply guard",
        state={"reply": reply_text[:6000], "policy": policy},
        questions={
            "promises_money": noul("Does the reply promise or imply a refund, credit, reimbursement, or exchange?"),
            "invents_logistics": noul("Does the reply state a specific pickup date, time, room, or location that is not in the policy?"),
            "commits_org": noul("Does the reply commit the organization to an action the policy does not allow, such as shipping, holding an item, or a special arrangement?"),
        },
    )
    if not answers:
        return None
    return Guard(promises_money=_p(answers["promises_money"]), invents_logistics=_p(answers["invents_logistics"]), commits_org=_p(answers["commits_org"]))


# --------------------------------------------------------------------------- #
# trusted-sender notifications (webhook) and export rows (store_export)
# --------------------------------------------------------------------------- #

def classify_notification(text: str, subject: str) -> Optional[Dict[str, float]]:
    """P(refund notice), P(purchase notice) for an email from the platform or an officer."""
    answers = ask(
        label="notification kind",
        state={"subject": subject, "body": text[:6000]},
        questions={
            "refund": noul("Is this a notification or request about refunding a purchase?"),
            "purchase": noul("Is this a notification that someone bought an item from a store (an order confirmation, receipt, or seller notification)?"),
        },
    )
    if not answers:
        return None
    return {"refund": _p(answers["refund"]), "purchase": _p(answers["purchase"])}


def classify_items(names: List[str]) -> Dict[str, float]:
    """P(not a pickup item) per unique export item name, in one call."""
    names = list(dict.fromkeys(n for n in names if n))[:40]
    if not names:
        return {}
    questions = {
        f"i{idx}": noul(f"Is items[{idx}] something other than a physical merchandise product that is handed to the buyer in person (for example a donation, a fee, a ticket, or a test listing)?")
        for idx in range(len(names))
    }
    answers = ask(label="export items", state={"items": names}, questions=questions)
    if not answers:
        return {}
    return {name: _p(answers[f"i{idx}"]) for idx, name in enumerate(names)}
