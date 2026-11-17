"""INGEST: multi-channel canonicalisation, fault-tolerant defaults and pre-persistence PII/secret redaction (DEP-02)."""
import hashlib
import hmac
import html
import re
import unicodedata
import uuid
from typing import Any

from pydantic import SecretStr

from src.config import CHANNEL_ALIASES, CHANNELS, EMPTY_BODY, FLUENCY, TIERS
from src.guardrails import Scrubber

# Input:  none
# What:   Characters that break downstream text handling: control characters and invisible zero-width/bidi marks
# Output: re.Pattern pair
_CTRL = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_INVISIBLE = re.compile(r"[\u200B-\u200F\u2028\u2029\u202A-\u202E\u2060-\u2064\uFEFF]")

# Input:  none
# What:   Channel-specific noise removers: email HTML/MIME/reply chains/signatures, chat timestamps/typing indicators,
#         forum BBCode quotes/pagination, docs-comment anchors/breadcrumbs/widgets
# Output: dict[str, list[tuple[re.Pattern, str]]]
_QUIRKS = {ch: [(re.compile(p), r) for p, r in rules] for ch, rules in {
    "email": [(r"(?is)<(script|style)\b.*?</\1>", " "), (r"</?[A-Za-z][^>]*>", " "),
              (r"(?im)^(?:from|to|cc|bcc|date|subject|reply-to|message-id|mime-version|content-type|content-transfer-encoding|x-[\w-]+):.*$", ""),
              (r"(?is)(?:^|\n)\s*(?:on\s[^\n]{0,200}?wrote:|-{2,}\s*original message\s*-{2,}).*", ""),
              (r"(?is)(?:^|\n)\s*(?:--\s*\n|sent from my\b).*", ""), (r"(?m)^\s*>.*$", "")],
    "chat": [(r"\[\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?\]", " "), (r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?\s*[-:]\s*", ""),
             (r"(?i)\b(?:\w+\s){0,3}is typing(?:\.{3}|\u2026)?", " "), (r"(?im)^\s*(?:customer|visitor|user|you)\s*:\s*", "")],
    "forum": [(r"(?is)\[quote[^\]]*\].*?\[/quote\]", " "), (r"(?i)\[/?(?:b|i|u|s|url|img|code|size|color|font|center|list|\*)(?:=[^\]]*)?\]", ""),
              (r"(?i)\bpage\s+\d+\s+of\s+\d+\b", " "), (r"(?im)^\s*(?:posted by|last edited|reply #?\d*|quote from)\b.*$", "")],
    "docs_comment": [(r"\{#[\w-]+\}", ""), (r"\[([^\]]+)\]\(#[^)]*\)", r"\1"), (r"(?im)^\s*(?:home|docs|documentation)\s*[>\u203a\u00bb/].*$", ""),
                     (r"(?i)was this (?:page|article) helpful\??(?:\s*(?:yes|no)\b(?:\s*/\s*(?:yes|no)\b)?)?", " ")],
}.items()}


# Input:  v (Any) – a raw field value (str, bytes, None or anything else)
# What:   Coerces it to text without ever raising (bytes are decoded with replacement characters)
# Output: str
def _text(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else "" if v is None else str(v)


# Input:  customer_id (str); salt (str) – secret key from CUSTOMER_ID_SALT
# What:   Keyed HMAC-SHA256 so stored hashes cannot be reversed with an unsalted lookup table; an empty salt is
#         rejected in production by Settings
# Output: str – 64-character hex digest
def customer_id_hash(customer_id: str, salt: str) -> str:
    return hmac.new(salt.encode("utf-8"), customer_id.encode("utf-8"), hashlib.sha256).hexdigest()


# Input:  text (str); channel (str) – canonical channel
# What:   Repairs broken Unicode (NFKC, lone surrogates, invisible/control chars), strips channel quirks, collapses whitespace
# Output: tuple[str, bool] – cleaned text and whether Unicode repair changed anything
def _clean(text: str, channel: str) -> tuple[str, bool]:
    t = unicodedata.normalize("NFKC", text.encode("utf-8", "replace").decode("utf-8"))
    t = _CTRL.sub("", _INVISIBLE.sub("", t))
    changed = t != text
    t = html.unescape(t) if channel == "email" else t
    for rx, rep in _QUIRKS[channel]:
        t = rx.sub(rep, t)
    return " ".join(t.split()), changed


# Input:  payload (dict | Any) – one raw ticket from any channel (fields may be missing, null or malformed);
#         scrubber (Scrubber); salt (str) – secret salt for hashing customer IDs
# What:   Builds the canonical ticket: canonical channel, cleaned + redacted text, safe defaults and anomaly flags.
#         It never raises for bad input, so a ticket can never be dropped at the perimeter.
# Output: dict – TicketState field updates
def normalize_and_sanitize_ticket(payload: Any, *, scrubber: Scrubber, salt: str = "") -> dict[str, Any]:
    p, flags = payload if isinstance(payload, dict) else {}, []
    raw_ch = _text(p.get("channel") or p.get("source_channel")).strip().lower()
    ch = CHANNEL_ALIASES.get(raw_ch, raw_ch)
    if ch not in CHANNELS:
        ch = "email"
        flags.append("CHANNEL_DEFAULTED")

    subj_raw, body_raw = _text(p.get("subject")), _text(p.get("body"))
    (subj, u1), (body, u2) = _clean(subj_raw, ch), _clean(body_raw, ch)
    if u1 or u2:
        flags.append("UNICODE_SANITIZED")
    (subj, c1), (body, c2) = scrubber.scrub(subj), scrubber.scrub(body)
    redactions = c1 + c2

    if not body and subj:
        body = subj
        flags.append("BODY_FALLBACK_TO_SUBJECT")
    elif not body:
        body = EMPTY_BODY
        flags.append("EMPTY_PAYLOAD_FLAGGED")

    tier = _text(p.get("customer_tier")).strip().lower()
    if tier not in TIERS:
        tier = "standard"
        flags.append("METADATA_DEFAULTED_STANDARD")
    fluency = _text(p.get("language_fluency")).strip().lower()
    ref = _text(p.get("external_ticket_ref") or p.get("ticket_id")).strip()[:128] or f"AUTO-{uuid.uuid4().hex[:16]}"

    return {
        "ticket_id": str(uuid.uuid4()), "trace_id": f"trc-{uuid.uuid4().hex}", "external_ticket_ref": ref,
        "source_channel": ch, "received_at": _text(p.get("received_at")) or None,
        "raw_subject_preserved": SecretStr(subj_raw), "raw_body_preserved": SecretStr(body_raw),
        "sanitized_subject": subj, "sanitized_body": body, "ingest_anomaly_flags": flags,
        "pii_redacted_at_ingest": bool(redactions), "pii_redaction_types": sorted(redactions), "pii_redaction_counts": dict(redactions),
        "customer_id_hash": customer_id_hash(_text(p.get("customer_id")) or "anonymous", salt),
        "customer_tier": tier, "language_fluency": fluency if fluency in FLUENCY else "unknown",
    }
