"""L17: Lightweight math CAPTCHA service.

A simple, dependency-free CAPTCHA that presents a math challenge
(e.g. "3 + 7 = ?") to the client. Used by the login flow when an
IP or username has accumulated too many failed attempts (but is
not yet hard-locked by the brute-force lockout).

Design choices:
  * In-process dict with TTL (5 min) — sufficient for single-worker
    SQLite deployments. Multi-worker setups would need Redis.
  * Math challenge instead of image distortion — accessible, no
    canvas/image library required, works in all browsers.
  * One-shot: each challenge can only be verified once.
"""
from __future__ import annotations

import logging
import random
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_CAPTCHA_TTL_SECONDS = 300  # 5 minutes
_MAX_ENTRIES = 1000  # cap memory use


@dataclass
class _Entry:
    answer: int
    expires_at: float


class _CaptchaStore:
    """Thread-safe in-memory CAPTCHA store with TTL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def create(self, answer: int) -> str:
        captcha_id = secrets.token_urlsafe(16)
        expires_at = time.time() + _CAPTCHA_TTL_SECONDS
        with self._lock:
            self._entries[captcha_id] = _Entry(answer, expires_at)
            # Prune expired + enforce cap
            now = time.time()
            self._entries = {
                k: v
                for k, v in self._entries.items()
                if v.expires_at > now
            }
            while len(self._entries) > _MAX_ENTRIES:
                # Drop the oldest by expiry
                oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
                self._entries.pop(oldest, None)
        return captcha_id

    def verify(self, captcha_id: str, answer: int) -> bool:
        if not captcha_id or answer is None:
            return False
        with self._lock:
            entry = self._entries.get(captcha_id)
            if entry is None:
                return False
            # One-shot: consume regardless of correctness
            self._entries.pop(captcha_id, None)
            if time.time() >= entry.expires_at:
                return False
            return entry.answer == int(answer)


_store = _CaptchaStore()


def generate_challenge() -> Tuple[str, str]:
    """Create a new math CAPTCHA challenge.

    Returns ``(captcha_id, question)`` where ``question`` is a human-
    readable string like ``"3 + 7 = ?"``.
    """
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    op = random.choice(["+", "-"])
    if op == "-" and b > a:
        a, b = b, a  # keep result non-negative
    answer = a + b if op == "+" else a - b
    captcha_id = _store.create(answer)
    question = f"{a} {op} {b} = ?"
    return captcha_id, question


def verify(captcha_id: Optional[str], answer: Optional[int]) -> bool:
    """Verify a CAPTCHA answer. One-shot: the challenge is consumed
    whether the answer is right or wrong."""
    if not captcha_id or answer is None:
        return False
    try:
        return _store.verify(captcha_id, int(answer))
    except (ValueError, TypeError):
        return False


def should_require_captcha(failure_count: int, threshold: int = 3) -> bool:
    """Decide whether the caller should be challenged with a CAPTCHA.

    Called by the login route after ``check_allowed`` — if the IP or
    username has accumulated ``threshold`` or more failures (but is
    not yet hard-locked), the login flow requires a CAPTCHA answer.
    """
    return failure_count >= threshold
