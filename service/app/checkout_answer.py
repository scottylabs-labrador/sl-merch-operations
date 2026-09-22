"""The one free-text answer TartanConnect collects at checkout.

TartanConnect gives each store item a single, unenforced text box. The store's
checkout prompt (logged in docs/checkout-prompt-log.md) asks buyers to type:

    YES, <CMU email>, <own mobile number>[, CALLS OK]

    YES        agreement to the terms (pickup only, no shipping, no refunds)
               and to emails about the order. Required.
    CMU email  an @andrew.cmu.edu or @cmu.edu address. Required.
    phone      any common US format, or + and a country code. Required.
    CALLS OK   optional consent to automated / AI-voice reminder calls and
               texts. Anything else, including a blank, is NO.

Consent to calls and texts is an ALLOWLIST: it is true only when a row holds
the exact opt-in phrase and nothing else that could change its meaning (no
question mark, no negation, no leftover words), and the order has exactly one
valid US mobile number. Everything the parser is unsure about is recorded as a
note for an officer and treated as NO. The raw text is kept on the order as
the record of what the buyer typed.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,185}\.[A-Za-z]{2,24}(?![A-Za-z0-9-])")
MAX_EMAIL_LEN = 254
ALLOWED_CMU_DOMAINS = {"andrew.cmu.edu", "cmu.edu"}
PLACEHOLDER_LOCALPARTS = {"andrewid", "yourandrewid", "andrew_id", "your_andrew_id", "youremail", "your.email", "email", "example"}
TYPO_CMU_DOMAINS = {"andrew.cmu.ed", "andrewcmu.edu", "andrew.edu", "andrew.cmu.com", "cmu.ed", "andrew.cum.edu", "andew.cmu.edu"}

# International numbers are read FIRST: '+91 9172345678' must never be mistaken for the US
# number +1 917 234 5678. Country codes other than NANP's 1 start with 2-9.
INTL_FIRST_RE = re.compile(r"(?:\+|(?<![\d+])00)\s*([2-9]\d{0,2})[\s.-]*(\d(?:[\d\s.-]{4,14})\d)(?![\d])")
US_PHONE_RE = re.compile(r"(?<![\w+])(?:\+1[\s.-]*|1[\s.-]+)?\(?([2-9]\d{2})\)?[\s.-]*([2-9]\d{2})[\s.-]*(\d{4})(?!\d)")
INTL_PHONE_RE = re.compile(r"\+([2-9]\d[\d\s.-]{6,16}\d)")
BARE_ELEVEN_RE = re.compile(r"(?<![\w+])1\d{10}(?!\d)")

OPT_IN_RE = re.compile(r"\bcalls?\s*(?:and|&|/)\s*texts?\s*[-:]?\s*ok(?:ay)?\b|\bcalls?\s*[-:]?\s*ok(?:ay)?\b", re.IGNORECASE)
NEGATION_RE = re.compile(
    r"\b(?:no|n|not|nope|nah|never|don'?t|dont|do\s+not|without|rather\s+not|stop|opt\s*-?\s*out|unsubscribe|cancel)\b",
    re.IGNORECASE,
)
AMBIGUOUS_OPT_IN_RE = re.compile(r"\b(?:texts?\s*ok(?:ay)?|calls?\s*(?:yes|fine|allowed)|ok(?:ay)?\s*to\s*(?:call|text)|opt\s*-?\s*in|calls?)\b", re.IGNORECASE)
TYPO_OPT_IN_RE = re.compile(r"\bca+l+s*\W*o+k+(?:ay)?\b|\bcal+z\W*ok\b|\bcalls?\W*okk+\b|\bcals\W*ok\b|\bcalsl\W*ok\b", re.IGNORECASE)
YES_RE = re.compile(r"\byes\b", re.IGNORECASE)
AGREE_EQUIV_RE = re.compile(r"\b(?:y|yep|yeah|yup|ya|agreed?|i\s+agree|confirm(?:ed)?|understood|i\s+understand|ok(?:ay)?)\b", re.IGNORECASE)
BARE_NO_RE = re.compile(r"\bno\b", re.IGNORECASE)
FILLER_RE = re.compile(r"\b(?:thanks?|thank\s+you|thx|ty|please|pls|plz)\b", re.IGNORECASE)
# Any ASCII word of 2+ letters, any non-ASCII letters or symbols (other scripts, accents, emoji),
# or an empty checkbox "[ ]" left after the recognized parts counts as extra text.
LEFTOVER_WORD_RE = re.compile(r"[A-Za-z]{2,}|[^\x00-\x7f\s]+|\[\s*\]")

_APOSTROPHES = dict.fromkeys(map(ord, "‘’ʼ`´＇"), "'")
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_DQUOTES = dict.fromkeys(map(ord, "“”„«»"), '"')


def normalize(text: str) -> str:
    """Fold what phone keyboards do to text, for matching only (the raw text is kept separately)."""
    t = unicodedata.normalize("NFKC", (text or "").replace("\x00", " ")).translate(_APOSTROPHES).translate(_DASHES).translate(_DQUOTES)
    t = re.sub(r"\s*@\s*", "@", t)
    t = re.sub(r"(@[A-Za-z0-9.-]*?)\.\s+(?=(?:andrew\.)?cmu\b|edu\b)", r"\1.", t, flags=re.IGNORECASE)
    t = re.sub(r"<([^<>\s]+@[^<>\s]+)>", r" \1 ", t)  # <addr@x> -> addr@x; other brackets are kept
    return t


@dataclass
class CheckoutAnswer:
    raw: str
    agreed: bool = False
    cmu_email: Optional[str] = None
    other_email: Optional[str] = None
    phone: Optional[str] = None
    calls_opt_in: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def missing(self) -> List[str]:
        out = []
        if not self.agreed:
            out.append("YES agreement")
        if not self.cmu_email:
            out.append("CMU email")
        if not self.phone:
            out.append("phone number")
        return out

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def warnings(self) -> List[str]:  # kept for callers that read .warnings
        return self.notes

    def summary(self) -> str:
        return "; ".join([
            "agreed" if self.agreed else "NOT agreed",
            self.cmu_email or (f"non-CMU email {self.other_email}" if self.other_email else "no CMU email"),
            self.phone or "no phone",
            "calls/texts OK" if self.calls_opt_in else "no call/text consent",
        ])


def _note(ans: CheckoutAnswer, text: str) -> None:
    if text not in ans.notes:
        ans.notes.append(text)


@dataclass
class _Row:
    text: str
    rest: str  # text with emails and phones removed
    has_opt_in: bool = False
    opt_in_clean: bool = False


def _extract_emails(t: str, ans: CheckoutAnswer) -> str:
    cmu_seen: List[str] = []
    for e in EMAIL_RE.findall(t):
        e = e.lower().strip(".")
        if len(e) > MAX_EMAIL_LEN:
            _note(ans, "email address too long, ignored")
            continue
        local, _, domain = e.partition("@")
        if local in PLACEHOLDER_LOCALPARTS:
            _note(ans, f"placeholder email {e} ignored")
            continue
        if domain in ALLOWED_CMU_DOMAINS or domain.endswith(".cmu.edu"):
            if domain not in ALLOWED_CMU_DOMAINS:
                _note(ans, f"CMU domain not on list: {e}")
            if e not in cmu_seen:
                cmu_seen.append(e)
        elif domain in TYPO_CMU_DOMAINS:
            _note(ans, f"email domain looks like a typo: {e} (did they mean {local}@andrew.cmu.edu?)")
            ans.other_email = ans.other_email or e
        else:
            ans.other_email = ans.other_email or e
    for e in cmu_seen:
        if ans.cmu_email is None:
            ans.cmu_email = e
        elif e != ans.cmu_email:
            _note(ans, f"more than one CMU email ({ans.cmu_email}, {e}); using the first")
    if ans.other_email and not ans.cmu_email:
        _note(ans, "CMU email missing (gave a non-CMU address)")
    return EMAIL_RE.sub(" ", t)


def _extract_phones(t: str, found: List[str], ans: CheckoutAnswer) -> str:
    for m in INTL_FIRST_RE.finditer(t):
        digits = m.group(1) + re.sub(r"\D", "", m.group(2))
        if 8 <= len(digits) <= 15:
            found.append("+" + digits)
            _note(ans, "international number: no automated calls/texts")
    t = INTL_FIRST_RE.sub(" ", t)
    for m in US_PHONE_RE.finditer(t):
        area, exch, line = m.groups()
        e164 = f"+1{area}{exch}{line}"
        if exch == "555" and 100 <= int(line) <= 199:
            _note(ans, f"placeholder phone {m.group(0).strip()} ignored")
            continue
        found.append(e164)
    t = US_PHONE_RE.sub(" ", t)
    for m in INTL_PHONE_RE.finditer(t):
        digits = re.sub(r"\D", "", m.group(1))
        if 8 <= len(digits) <= 15:
            found.append("+" + digits)
            _note(ans, "international number: no automated calls/texts")
    t = INTL_PHONE_RE.sub(" ", t)
    for m in BARE_ELEVEN_RE.finditer(t):
        found.append("raw:" + m.group(0))
        _note(ans, f"possible non-US number {m.group(0)}, confirm with buyer")
    return BARE_ELEVEN_RE.sub(" ", t)


def parse_rows(texts: Iterable[str]) -> CheckoutAnswer:
    raw_rows = [t.strip() for t in texts if t and t.strip()]
    ans = CheckoutAnswer(raw=" | ".join(raw_rows))
    if not raw_rows:
        return ans

    rows: List[_Row] = []
    phones: List[str] = []
    for raw in raw_rows:
        t = normalize(raw)
        t = _extract_emails(t, ans)
        t = _extract_phones(t, phones, ans)
        rows.append(_Row(text=raw, rest=t))

    distinct = list(dict.fromkeys(phones))
    usable = [p for p in distinct if not p.startswith("raw:")]
    if usable:
        ans.phone = usable[0]
    elif distinct:
        ans.phone = distinct[0][4:]
    if len(distinct) > 1:
        _note(ans, "more than one phone number: " + ", ".join(p.replace("raw:", "") for p in distinct))

    any_question = any("?" in r.rest for r in rows)
    any_negation = any(NEGATION_RE.search(OPT_IN_RE.sub(" ", r.rest)) for r in rows)

    agreed_any = False
    yes_any = False
    any_words = False  # leftover words in ANY box (e.g. a second size's box retracting CALLS OK) block consent
    for r in rows:
        rest = r.rest
        r.has_opt_in = bool(OPT_IN_RE.search(rest))
        rest = OPT_IN_RE.sub(" ", rest)
        rest = re.sub(r"\bnot\s+ok(?:ay)?\b", " not ", rest, flags=re.IGNORECASE)
        if YES_RE.search(rest):
            yes_any = agreed_any = True
            rest = YES_RE.sub(" ", rest, count=1)
        else:
            m = AGREE_EQUIV_RE.search(rest)
            if m:
                agreed_any = True
                _note(ans, f"non-standard agreement word '{m.group(0)}'")
                rest = rest[: m.start()] + " " + rest[m.end():]
        leftover = FILLER_RE.sub(" ", rest)
        words = [w for w in LEFTOVER_WORD_RE.findall(leftover) if w.lower() not in ("yes",)]
        r.opt_in_clean = r.has_opt_in and not words and "?" not in r.rest
        if words:
            any_words = True
            if TYPO_OPT_IN_RE.search(r.rest) and not r.has_opt_in:
                _note(ans, "possible opt-in typo, treated as NO")
            elif AMBIGUOUS_OPT_IN_RE.search(leftover) and not r.has_opt_in:
                _note(ans, "ambiguous opt-in, treated as NO")
            else:
                _note(ans, "extra text: " + " ".join(words)[:120])

    bare_no = any(BARE_NO_RE.search(OPT_IN_RE.sub(" ", r.rest)) for r in rows)
    if agreed_any:
        ans.agreed = True
        if bare_no and yes_any:
            _note(ans, "trailing NO read as declining calls")
    elif bare_no:
        _note(ans, "terms possibly declined")

    has_opt_in = any(r.has_opt_in for r in rows)
    one_us_phone = len(distinct) == 1 and bool(usable) and usable[0].startswith("+1")
    ans.calls_opt_in = (
        has_opt_in
        and any(r.opt_in_clean for r in rows)
        and not any_question
        and not any_negation
        and not any_words
        and one_us_phone
    )
    if has_opt_in and not ans.calls_opt_in:
        _note(ans, "opt-in phrase with extra words, a question, a negation or no single US mobile number, treated as NO")
    return ans


def parse_checkout_answer(text: Optional[str]) -> CheckoutAnswer:
    return parse_rows([text or ""])


def merge_answers(texts: Iterable[str]) -> CheckoutAnswer:
    """One checkout can have one answer per item (size); each row is read on its own, then merged."""
    return parse_rows(texts)
