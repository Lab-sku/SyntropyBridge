"""Structured logging utilities with PII redaction.

Goal
----
The application already calls :func:`logging.getLogger` everywhere, but
the records are free-form. This module provides:

* :class:`SafeFormatter` — formats log records while replacing known
  sensitive values (API keys, JWTs, emails, etc.) with a redaction
  marker so logs can be shipped to third-party observability stacks
  without leaking secrets.
* :func:`configure_logging` — one-stop helper to wire the formatter
  and a couple of common filters onto the root logger.
* :func:`mask_secret` / :func:`redact_payload` — public helpers for
  callers that need to scrub dicts before persisting them (audit logs,
  error responses, request bodies, etc.).

The redactor is intentionally conservative — it errs on the side of
masking rather than revealing.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Regex catalog of things that should never end up in a log line.
# IMPORTANT: the Authorization header is scrubbed *first* so the token
# portion it carries is fully redacted as a single unit. The
# ``sk-/sk_live_/sk_test_`` patterns then mop up any orphan API keys
# that happened to be inline in the message body.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorisation header — must come BEFORE the bare-key patterns so
    # the header value (which often contains a `sk-...` token) is
    # collapsed to a single ``***`` instead of leaking the prefix.
    (re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"), r"\1***"),
    # Bearer / sk- API keys (orphans outside of the auth header).
    (re.compile(r"(sk-[A-Za-z0-9_-]{12,})"), "***"),
    (re.compile(r"(sk_live_[A-Za-z0-9]{12,})"), "***"),
    (re.compile(r"(sk_test_[A-Za-z0-9]{12,})"), "***"),
    # mmx_tk_ 用户令牌（user API tokens）
    (re.compile(r"(mmx_tk_[A-Za-z0-9_-]{12,})"), "***"),
    # Fernet tokens (base64url-encoded, start with gAAAAA)
    (re.compile(r"(gAAAAA[A-Za-z0-9_-]{20,})"), "***"),
    # JWT (header.payload.signature)
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "jwt-***"),
    # Email (keep the domain for debuggability)
    (re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})"), r"***@\2"),
    # PAN-shaped digits (13-19 contiguous)
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "***pan***"),
    # Common Chinese ID card (15 or 18 digits, possibly X)
    (re.compile(r"\b\d{17}[\dXx]\b"), "***id***"),
]

_DYNAMIC_PATTERNS: list[tuple[re.Pattern[str], str]] = []
_DYNAMIC_PATTERNS_INITIALIZED = False


def _init_dynamic_patterns() -> None:
    global _DYNAMIC_PATTERNS_INITIALIZED
    if _DYNAMIC_PATTERNS_INITIALIZED:
        return
    _DYNAMIC_PATTERNS_INITIALIZED = True
    try:
        from backend.config import Config

        for val in (Config.SECRET_KEY, Config.ENCRYPTION_KEY):
            if val and len(val) >= 8:
                escaped = re.escape(val)
                _DYNAMIC_PATTERNS.append((re.compile(escaped), "***"))
    except Exception:
        pass

_MASK = "***REDACTED***"

# Field names that should be masked no matter where they appear in a
# JSON-ish payload.
_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "session",
    "csrf",
    "private_key",
    "credit_card",
    "card_number",
    "cvv",
    "pin",
}


def mask_secret(value: Any, *, keep_tail: int = 4) -> str:
    """Return a redacted representation of ``value``.

    >>> mask_secret("sk-abcdefghij12345")
    'sk-***2345'
    """
    if value is None:
        return ""
    s = str(value)
    if len(s) <= keep_tail:
        return _MASK
    if len(s) <= 12:
        return _MASK
    return f"***{s[-keep_tail:]}"


def redact_payload(payload: Any) -> Any:
    """Recursively walk ``payload`` and mask any sensitive field."""
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for k, v in payload.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                out[k] = mask_secret(v)
            else:
                out[k] = redact_payload(v)
        return out
    if isinstance(payload, (list, tuple)):
        return [redact_payload(x) for x in payload]
    if isinstance(payload, str):
        return _scrub_text(payload)
    return payload


def _scrub_text(text: str) -> str:
    if not text:
        return text
    _init_dynamic_patterns()
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    for pat, repl in _DYNAMIC_PATTERNS:
        out = pat.sub(repl, out)
    return out


class SafeFormatter(logging.Formatter):
    """A :class:`logging.Formatter` that scrubs the formatted message."""

    def __init__(
        self, fmt: Optional[str] = None, datefmt: Optional[str] = None, scrub: bool = True
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.scrub = scrub

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        msg = super().format(record)
        if self.scrub:
            msg = _scrub_text(msg)
        return msg


class JSONFormatter(logging.Formatter):
    _EXTRA_FIELDS = (
        "request_id",
        "user_id",
        "endpoint",
        "status_code",
        "latency_ms",
        "actor_id",
        "actor_type",
        "metadata",
    )

    def __init__(self, scrub: bool = True) -> None:
        super().__init__()
        self.scrub = scrub

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _scrub_text(record.getMessage()) if self.scrub else record.getMessage(),
        }
        for field in self._EXTRA_FIELDS:
            val = getattr(record, field, None)
            if val is not None:
                entry[field] = val
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": _scrub_text(str(record.exc_info[1])) if self.scrub else str(record.exc_info[1]),
            }
        return json.dumps(entry, ensure_ascii=False, default=str)


class RedactFilter(logging.Filter):
    """Filter that scrubs the ``msg``, ``args``, and ``exc_info``
    traceback of a record in place.

    Useful when the formatter isn't enough (e.g. when something reads
    ``record.getMessage()`` directly, or when the exception traceback
    carries sensitive values that the formatter would otherwise append
    verbatim).
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            if isinstance(record.msg, str):
                record.msg = _scrub_text(record.msg)
            if record.args:
                record.args = tuple(
                    _scrub_text(a) if isinstance(a, str) else a for a in record.args
                )
            # Pre-format and scrub the exception traceback so neither
            # SafeFormatter nor JSONFormatter (both read record.exc_text
            # when it is set) ever append the raw exception text to the
            # log line. Without this, stack traces containing API keys,
            # JWTs, or emails would leak verbatim.
            if record.exc_info and not record.exc_text:
                import traceback as _tb

                exc_text = "".join(_tb.format_exception(*record.exc_info))
                record.exc_text = _scrub_text(exc_text)
        except Exception:
            # Never raise from a logging filter.
            pass
        return True


def configure_logging(level: str = "INFO", *, scrub: bool = True, log_format: str = "text") -> None:
    """Install a SafeFormatter or JSONFormatter + RedactFilter on the root logger.

    Idempotent — safe to call from both ``main.py`` and test fixtures.

    When the ``LOG_FILE`` environment variable is set, a
    :class:`logging.handlers.RotatingFileHandler` is added alongside
    the StreamHandler so logs persist to disk with rotation
    (``maxBytes=100MB``, ``backupCount=10``). Without ``LOG_FILE`` the
    default StreamHandler-only behaviour is preserved (systemd journal
    / Docker logging driver handle persistence in production).
    """
    import os

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = (
        JSONFormatter(scrub=scrub)
        if log_format == "json"
        else SafeFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            scrub=scrub,
        )
    )
    redact = RedactFilter()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redact)
    root.addHandler(stream_handler)

    log_file = (os.getenv("LOG_FILE") or "").strip()
    if log_file:
        try:
            from logging.handlers import RotatingFileHandler

            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=int(os.getenv("LOG_FILE_MAX_BYTES", "104857600") or 104857600),
                backupCount=int(os.getenv("LOG_FILE_BACKUP_COUNT", "10") or 10),
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redact)
            root.addHandler(file_handler)
        except Exception:
            # Falling back to stream-only is safe — disk-full / permission
            # errors shouldn't crash the app. The StreamHandler is still
            # attached so logs go to stdout/stderr.
            root.getLogger(__name__) if False else None
            import sys

            print(
                f"WARNING: failed to configure LOG_FILE={log_file!r}; "
                f"falling back to stderr-only logging",
                file=sys.stderr,
            )

    try:
        root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    except Exception:
        root.setLevel(logging.INFO)


def log_extra(
    actor_id: Optional[int] = None,
    actor_type: str = "system",
    request_id: Optional[str] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """Build a structured ``extra=`` dict for use with logger calls.

    Centralising this means every audit-worthy log entry has the same
    shape (``actor.*``, ``request_id``, etc.) and downstream parsers
    don't have to guess.
    """
    base = {
        "actor_id": actor_id,
        "actor_type": actor_type,
        "request_id": request_id,
    }
    base.update({k: v for k, v in fields.items() if v is not None})
    return base


# ---------------------------------------------------------------------------
# Convenience: a single helper for dumping exception info safely
# ---------------------------------------------------------------------------


def safe_exc_info(exc: BaseException) -> str:
    """Format an exception without leaking the args verbatim."""
    try:
        name = type(exc).__name__
        msg = str(exc)
        msg = _scrub_text(msg)
        return f"{name}: {msg}" if msg else name
    except Exception:  # pragma: no cover - last-ditch safety net
        return "Exception"


__all__ = [
    "SafeFormatter",
    "JSONFormatter",
    "RedactFilter",
    "configure_logging",
    "mask_secret",
    "redact_payload",
    "log_extra",
    "safe_exc_info",
]
