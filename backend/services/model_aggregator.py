"""Global model aggregator.

Fetches models from every configured provider in parallel, caches them
in the `models` table, and exposes a unified view to the frontend.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from backend.database import get_db_context, get_setting, set_setting
from backend.providers import ProviderError, get_provider
from backend.providers.base import ProviderRegistry
from backend.services import custom_providers
from backend.services.redis_service import RedisService

logger = logging.getLogger(__name__)


CACHE_KEY = "models:aggregated:v1"
CACHE_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Model-type detection
# ---------------------------------------------------------------------------
#
# Different upstream vendors host wildly different model families on the
# same provider. The most common case is NVIDIA NIM, which exposes both
# chat completions and embedding models side-by-side. If we forward an
# embedding model id to ``/v1/chat/completions`` the upstream returns
# ``405 Method Not Allowed`` and the user gets a confusing error in the
# chat UI. To prevent that, every entry in the catalog now carries an
# explicit ``type`` field (``chat``, ``embedding``, ``image``, ``audio``)
# which the chat surface can filter on.
#
# The detector is intentionally a small set of well-known substrings
# rather than a full registry — adding a brand new model line is
# something an admin can do in the source tree, and being overly
# aggressive with patterns would risk marking real chat models as
# embedding (and silently breaking them in the picker).

_EMBEDDING_PATTERNS = (
    "embed",
    "embedding",
    "embedcode",
    "text-embedding",
    "text_embedding",
    "nv-embed",
    "e5-",
    "bge-",
    "rerank",
    "retrieval",
)

_IMAGE_PATTERNS = (
    "sdxl",
    "stable-diffusion",
    "stable_diffusion",
    "flux-",
    "kandinsky",
    "imagen",
    "image-generation",
    "dall-e",
    "dalle",
)

_AUDIO_PATTERNS = (
    "whisper",
    "tts",
    "text-to-speech",
    "text_to_speech",
    "asr",
    "speech",
    "audio",
)


def _normalize_token(model_id: str) -> str:
    """Lowercased, last path segment of a model id (NVIDIA-style ``a/b``)."""
    raw = (model_id or "").lower()
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    return raw


def detect_model_type(model_id: str) -> str:
    """Return one of ``chat`` / ``embedding`` / ``image`` / ``audio``.

    Defaults to ``chat`` when nothing matches, because the chat surface
    is the dominant consumer and we never want a brand-new chat model
    to silently disappear from the picker.
    """
    token = _normalize_token(model_id)
    # ``embedcode`` matches first because ``embed`` is a prefix of it.
    for pat in _EMBEDDING_PATTERNS:
        if pat in token:
            return "embedding"
    for pat in _IMAGE_PATTERNS:
        if pat in token:
            return "image"
    for pat in _AUDIO_PATTERNS:
        if pat in token:
            return "audio"
    return "chat"


def _cache_get(key: str) -> Optional[Any]:
    try:
        raw = RedisService.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _cache_set(key: str, value: Any, ttl: int = CACHE_TTL) -> None:
    try:
        RedisService.set_with_expiry(key, json.dumps(value, default=str), ttl)
    except Exception:
        pass


def _cache_clear(key: str) -> None:
    try:
        RedisService.delete(key)
    except Exception:
        pass


def _is_configured(name: str) -> bool:
    if name.startswith("custom:"):
        slug = name.split(":", 1)[1]
        cfg = custom_providers.get_custom_provider(slug)
        if not cfg or not cfg.get("is_enabled"):
            return False
        return bool(custom_providers.parse_keys(cfg))
    api_key = get_setting(f"{name}_api_key") or ""
    enabled = (get_setting(f"{name}_enabled") or "true") == "true"
    if not enabled:
        return False
    if not api_key or "your-" in api_key.lower():
        return False
    return True


def _provider_credentials(name: str) -> Tuple[str, str]:
    if name.startswith("custom:"):
        slug = name.split(":", 1)[1]
        cfg = custom_providers.get_custom_provider(slug)
        if not cfg:
            return "", ""
        keys = custom_providers.parse_keys(cfg)
        return (keys[0] if keys else ""), (cfg.get("api_base") or "")

    api_key = get_setting(f"{name}_api_key") or ""
    api_base = get_setting(f"{name}_api_base") or ""
    if not api_base:
        provider_cls = ProviderRegistry.get(name)
        if provider_cls:
            api_base = provider_cls.default_api_base
    return api_key, api_base


def _upsert_models(provider: str, models: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with get_db_context() as conn:
        cursor = conn.cursor()
        for m in models:
            model_id = m.id
            display = m.display_name or model_id
            ctx = int(getattr(m, "context_length", 0) or 0)
            try:
                cursor.execute(
                    """
                    INSERT INTO models (model_id, display_name, provider, is_active, context_length, last_synced)
                    VALUES (?, ?, ?, 1, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(model_id) DO UPDATE SET
                      display_name = excluded.display_name,
                      is_active = 1,
                      context_length = excluded.context_length,
                      last_synced = CURRENT_TIMESTAMP
                """,
                    (model_id, display, provider, ctx),
                )
            except Exception as e:
                logger.warning("Cache insert failed for %s/%s: %s", provider, model_id, e)
        cursor.execute(
            """
            SELECT model_id, display_name, context_length, is_active
            FROM models WHERE provider = ? ORDER BY display_name
        """,
            (provider,),
        )
        rows = [dict(r) for r in cursor.fetchall()]

    # Auto-populate model_provider_map so routing stays consistent with
    # the cached model catalogue.  Existing admin overrides are preserved
    # (we only write entries that don't already exist in the map).
    try:
        raw = get_setting("model_provider_map") or ""
        existing: dict = json.loads(raw) if raw else {}
        changed = False
        for r in rows:
            mid = r.get("model_id", "")
            if mid and mid not in existing:
                existing[mid] = provider
                changed = True
        if changed:
            set_setting("model_provider_map", json.dumps(existing, ensure_ascii=False))
    except Exception as e:
        logger.debug("model_provider_map sync skipped: %s", e)

    return rows


async def _fetch_provider_models(name: str) -> Dict[str, Any]:
    if not _is_configured(name):
        return {"provider": name, "configured": False, "models": [], "error": None}
    api_key, api_base = _provider_credentials(name)
    try:
        if name.startswith("custom:"):
            slug = name.split(":", 1)[1]
            rows = await custom_providers.fetch_custom_models(slug, force=True)
            return {
                "provider": name,
                "configured": True,
                "models": rows,
                "count": len(rows),
                "error": None,
            }
        provider = get_provider(name, api_key=api_key, api_base=api_base)
        models = await provider.list_models()
        rows = _upsert_models(name, models)
        return {
            "provider": name,
            "configured": True,
            "models": rows,
            "count": len(rows),
            "error": None,
        }
    except ProviderError as e:
        return {"provider": name, "configured": True, "models": [], "error": str(e)}
    except Exception as e:
        logger.exception("Failed to fetch models for %s", name)
        return {"provider": name, "configured": True, "models": [], "error": str(e)}


def _all_provider_names() -> List[str]:
    names = list(ProviderRegistry.all().keys())
    for cfg in custom_providers.list_custom_providers(include_disabled=True):
        names.append(f"custom:{cfg['slug']}")
    # de-dup while preserving order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


async def fetch_all_provider_models(force: bool = False) -> List[Dict[str, Any]]:
    """Fetch models for every configured provider in parallel.

    Results are cached in Redis (if available) for 5 minutes; set
    `force=True` to bypass the cache.
    """
    if not force:
        cached = _cache_get(CACHE_KEY)
        if cached is not None:
            return cached

    tasks = [_fetch_provider_models(name) for name in _all_provider_names()]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    _cache_set(CACHE_KEY, results, CACHE_TTL)
    return results


def get_cached_provider_models() -> List[Dict[str, Any]]:
    """Return models straight from the local database cache."""
    results: List[Dict[str, Any]] = []
    for name in _all_provider_names():
        if not _is_configured(name):
            continue
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT model_id, display_name, context_length, is_active
                FROM models WHERE provider = ? AND is_active = 1
                ORDER BY display_name
            """,
                (name,),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        results.append(
            {
                "provider": name,
                "configured": True,
                "models": rows,
                "count": len(rows),
            }
        )
    return results


async def aggregate_models(force: bool = False) -> Dict[str, Any]:
    """Return a flat list of all available models with provider metadata."""
    sources = await fetch_all_provider_models(force=force)
    flat: List[Dict[str, Any]] = []
    for entry in sources:
        provider_name = entry["provider"]
        # For custom providers, look up the friendly name from the DB
        if provider_name.startswith("custom:"):
            slug = provider_name.split(":", 1)[1]
            cfg = custom_providers.get_custom_provider(slug)
            display_name = (cfg or {}).get("display_name") or provider_name
        else:
            provider_cls = ProviderRegistry.get(provider_name)
            display_name = provider_cls.display_name if provider_cls else provider_name
        for m in entry.get("models", []):
            model_id = m.get("model_id") if isinstance(m, dict) else m.id
            if not model_id:
                continue
            raw = str(model_id)
            prefix = f"{provider_name}/"
            if raw.startswith(prefix):
                prefixed = raw
            else:
                prefixed = f"{prefix}{raw}"
            flat.append(
                {
                    "provider": provider_name,
                    "provider_display": display_name,
                    "id": prefixed,
                    "name": raw,
                    "display_name": m.get("display_name", raw) if isinstance(m, dict) else raw,
                    "context_length": (m.get("context_length") or 0) if isinstance(m, dict) else 0,
                    "type": detect_model_type(raw),
                }
            )
    return {
        "total": len(flat),
        "sources": sources,
        "models": flat,
    }
