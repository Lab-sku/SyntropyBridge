import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.config import Config
from backend.database import get_db_context
from backend.security import Security
from backend.services.custom_providers import _validate_provider_url


_COOLDOWN_LOCK = threading.Lock()

# Per-process round-robin cursor for deterministic channel selection
# when all candidates share the same weight.
_RR_LOCK = threading.Lock()
_RR_COUNTER: dict[str, int] = {}


@dataclass(frozen=True)
class Channel:
    id: int
    provider: str
    name: str
    base_url: str
    api_key: str
    weight: int
    is_active: bool
    cooldown_until: Optional[str]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChannelService:
    @staticmethod
    def list_channels(*, provider: Optional[str] = None) -> list[dict]:
        with get_db_context() as conn:
            cursor = conn.cursor()
            if provider:
                cursor.execute(
                    """
                    SELECT id, provider, name, base_url, weight, is_active, cooldown_until, last_health_at, last_error, created_at, updated_at
                    FROM channels
                    WHERE provider = ?
                    ORDER BY provider, weight DESC, id ASC
                    """,
                    (provider,),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, provider, name, base_url, weight, is_active, cooldown_until, last_health_at, last_error, created_at, updated_at
                    FROM channels
                    ORDER BY provider, weight DESC, id ASC
                    """
                )
            rows = cursor.fetchall()

        results: list[dict] = []
        for row in rows:
            results.append(
                {
                    "id": int(row["id"]),
                    "provider": row["provider"],
                    "name": row["name"],
                    "base_url": row["base_url"],
                    "weight": int(row["weight"] or 0),
                    "is_active": bool(row["is_active"]),
                    "cooldown_until": row["cooldown_until"],
                    "last_health_at": row["last_health_at"],
                    "last_error": row["last_error"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return results

    @staticmethod
    def create_channel(
        *,
        provider: str,
        name: str,
        base_url: str,
        api_key: str,
        weight: int = 100,
        is_active: bool = True,
    ) -> int:
        provider = (provider or "").strip().lower()
        name = (name or "").strip()
        base_url = (base_url or "").strip().rstrip("/")
        if not provider:
            raise ValueError("provider 不能为空")
        if not name:
            raise ValueError("name 不能为空")
        if not base_url:
            raise ValueError("base_url 不能为空")
        if not api_key:
            raise ValueError("api_key 不能为空")

        # SSRF protection: validate that base_url doesn't target internal networks
        _validate_provider_url(base_url)

        encrypted = Security.encrypt(api_key)
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO channels (provider, name, base_url, api_key_encrypted, weight, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (provider, name, base_url, encrypted, int(weight or 0), 1 if is_active else 0),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def update_channel(
        *,
        channel_id: int,
        provider: str,
        name: str,
        base_url: str,
        api_key: Optional[str],
        weight: int,
        is_active: bool,
    ) -> bool:
        provider = (provider or "").strip().lower()
        name = (name or "").strip()
        base_url = (base_url or "").strip().rstrip("/")
        if not provider:
            raise ValueError("provider 不能为空")
        if not name:
            raise ValueError("name 不能为空")
        if not base_url:
            raise ValueError("base_url 不能为空")

        params: list = [provider, name, base_url, int(weight or 0), 1 if is_active else 0]
        set_api_key_sql = ""
        if api_key is not None and api_key.strip():
            set_api_key_sql = ", api_key_encrypted = ?"
            params.insert(3, Security.encrypt(api_key.strip()))
        params.append(int(channel_id))

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE channels
                SET provider = ?, name = ?, base_url = ?{set_api_key_sql}, weight = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                tuple(params),
            )
            return cursor.rowcount > 0

    @staticmethod
    def delete_channel(channel_id: int) -> bool:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM channels WHERE id = ?", (int(channel_id),))
            return cursor.rowcount > 0

    @staticmethod
    def set_active(channel_id: int, is_active: bool) -> bool:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE channels SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (1 if is_active else 0, int(channel_id)),
            )
            return cursor.rowcount > 0

    @staticmethod
    def reset_cooldown(channel_id: int) -> bool:
        """Manually clear the cooldown timer so the channel re-enters rotation."""
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE channels
                SET cooldown_until = NULL, last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(channel_id),),
            )
            return cursor.rowcount > 0

    @staticmethod
    def toggle_active(channel_id: int) -> Optional[bool]:
        """Flip is_active. Returns the new state, or None if the channel does not exist."""
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_active FROM channels WHERE id = ?", (int(channel_id),))
            row = cursor.fetchone()
            if not row:
                return None
            new_state = 0 if row["is_active"] else 1
            cursor.execute(
                "UPDATE channels SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_state, int(channel_id)),
            )
            return bool(new_state)

    @staticmethod
    def _fetch_candidates(*, provider: str, exclude_ids: set[int]) -> list[Channel]:
        provider = (provider or "").strip().lower()
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, provider, name, base_url, api_key_encrypted, weight, is_active, cooldown_until
                FROM channels
                WHERE provider = ?
                  AND is_active = 1
                  AND (cooldown_until IS NULL OR cooldown_until <= CURRENT_TIMESTAMP)
                ORDER BY weight DESC, id ASC
                """,
                (provider,),
            )
            rows = cursor.fetchall()

        channels: list[Channel] = []
        for row in rows:
            cid = int(row["id"])
            if cid in exclude_ids:
                continue
            channels.append(
                Channel(
                    id=cid,
                    provider=row["provider"],
                    name=row["name"],
                    base_url=row["base_url"],
                    api_key=Security.decrypt(row["api_key_encrypted"]) or "",
                    weight=int(row["weight"] or 0),
                    is_active=bool(row["is_active"]),
                    cooldown_until=row["cooldown_until"],
                )
            )
        return channels

    @staticmethod
    def select_channel(
        *, provider: str, exclude_ids: Optional[set[int]] = None
    ) -> Optional[Channel]:
        """Select a channel using weighted selection with round-robin
        fallback for equal weights.

        Each candidate's ``weight`` column controls its share of traffic.
        For example weights 100/50/50 produce roughly 50%/25%/25%
        distribution over many requests.

        Weights of 0 or less are clamped to 1 so the channel still
        receives some traffic; to fully disable a channel, set
        ``is_active=0`` instead.

        When all candidates share the same weight the selection falls
        back to deterministic round-robin so every channel receives
        exactly equal traffic in small-sample scenarios (avoids the
        streaky distribution pure randomness can produce).
        """
        exclude = exclude_ids or set()
        candidates = ChannelService._fetch_candidates(provider=provider, exclude_ids=exclude)
        if not candidates:
            return None
        # Weight defaults to 100, minimum 1 (zero/negative weights clamped
        # so the channel still receives SOME traffic; admin should use
        # is_active=0 to fully disable).
        weights = [max(int(c.weight or 100), 1) for c in candidates]
        # Deterministic round-robin when all weights are equal.
        if len(set(weights)) == 1:
            with _RR_LOCK:
                key = f"{provider}:{','.join(str(c.id) for c in candidates)}"
                idx = _RR_COUNTER.get(key, 0) % len(candidates)
                _RR_COUNTER[key] = idx + 1
            return candidates[idx]
        return random.choices(candidates, weights=weights, k=1)[0]

    @staticmethod
    def mark_failed(*, channel_id: int, error: str) -> None:
        cooldown_seconds = max(5, int(Config.CHANNEL_COOLDOWN_SECONDS or 60))
        cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
        # Use strftime to match SQLite's CURRENT_TIMESTAMP format
        # (YYYY-MM-DD HH:MM:SS — no 'T' separator, no timezone suffix).
        cooldown_str = cooldown_until.strftime("%Y-%m-%d %H:%M:%S")
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE channels
                SET cooldown_until = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cooldown_str, (error or "")[:300], int(channel_id)),
            )

    @staticmethod
    def mark_healthy(*, channel_id: int) -> None:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE channels
                SET last_health_at = ?, last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (_now_iso(), int(channel_id)),
            )

    @staticmethod
    def get_channel_secret(channel_id: int) -> Optional[Channel]:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, provider, name, base_url, api_key_encrypted, weight, is_active, cooldown_until
                FROM channels
                WHERE id = ?
                """,
                (int(channel_id),),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return Channel(
            id=int(row["id"]),
            provider=row["provider"],
            name=row["name"],
            base_url=row["base_url"],
            api_key=Security.decrypt(row["api_key_encrypted"]) or "",
            weight=int(row["weight"] or 0),
            is_active=bool(row["is_active"]),
            cooldown_until=row["cooldown_until"],
        )
