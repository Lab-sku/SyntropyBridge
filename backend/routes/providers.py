"""Provider management routes.

Exposes the multi-platform provider registry, allows per-provider model
discovery, and configures the API keys / endpoints used by the proxy.

All routes require admin authentication (session cookie or legacy JWT).
State-changing routes (POST) additionally require CSRF validation.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from backend.database import get_db_context, get_setting, set_setting
from backend.providers import ProviderError, get_provider, list_providers
from backend.providers.base import ProviderRegistry
from backend.routes.admin_auth import _require_admin, _require_admin_csrf
from backend.security import Security
from backend.services.http_client import get_async_client
from backend.services.model_aggregator import (
    _upsert_models,
    aggregate_models,
    fetch_all_provider_models,
    get_cached_provider_models,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class ProviderConfigUpdate(BaseModel):
    api_key: Optional[str] = None
    api_keys: Optional[List[str]] = None  # multi-key: each key becomes a channel
    api_base: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/providers")
async def list_all_providers(request: Request, _admin: None = Depends(_require_admin)):
    """List all registered built-in providers with their current configuration status.

    Custom providers (added via /api/custom-providers) are returned by
    that endpoint instead, to keep the two management surfaces separate.
    """
    builtin_names = {p["name"] for p in list_providers()}
    providers = [p for p in list_providers() if p["name"] in builtin_names]

    # One-shot query for per-provider model counts (avoids N+1).
    model_counts: dict[str, int] = {}
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT provider, COUNT(*) as cnt FROM models WHERE is_active = 1 GROUP BY provider"
            )
            for row in cursor.fetchall():
                model_counts[row["provider"]] = row["cnt"]
    except Exception:
        pass

    for p in providers:
        api_key = get_setting(p["api_key_setting"]) or ""
        api_base = get_setting(p["api_base_setting"]) or p["default_api_base"]
        p["configured"] = bool(api_key) and "your-" not in api_key.lower()
        p["api_key_masked"] = ("*" * 8 + api_key[-4:]) if api_key and len(api_key) > 4 else ""
        p["api_base"] = api_base
        p["enabled"] = (get_setting(f"{p['name']}_enabled") or "true") == "true"
        p["type"] = "builtin"
        p["model_count"] = model_counts.get(p["name"], 0)
    return providers


@router.post("/providers/{name}/test")
async def test_provider(name: str, request: Request, _admin: None = Depends(_require_admin_csrf)):
    """Verify that the provider's API key works by fetching its model list.

    On success the discovered models are also persisted to the ``models``
    table so the chat picker can show them immediately without a separate
    "refresh all" step.
    """
    if name not in ProviderRegistry.all():
        raise HTTPException(status_code=404, detail="Unknown provider")
    api_key = get_setting(f"{name}_api_key") or ""
    api_base = get_setting(f"{name}_api_base") or ""
    if not api_key or "your-" in api_key.lower():
        raise HTTPException(status_code=400, detail="未配置 API Key")

    provider = get_provider(name, api_key=api_key, api_base=api_base)
    try:
        models = await provider.list_models()
        # Persist discovered models so the chat picker can use them.
        _upsert_models(name, models)
        return {
            "success": True,
            "message": f"成功，发现 {len(models)} 个模型",
            "count": len(models),
            "models_cached": True,
        }
    except ProviderError as e:
        return {"success": False, "message": str(e), "status_code": e.status_code}
    except Exception as e:
        logger.exception("Provider test failed")
        return {"success": False, "message": f"测试失败: {e}"}


@router.post("/providers/{name}/config")
async def update_provider_config(
    name: str,
    body: ProviderConfigUpdate,
    request: Request,
    _admin: None = Depends(_require_admin_csrf),
):
    """Update a single provider's API key / base URL / enabled flag.

    When ``api_keys`` (list) is provided with more than one key, the
    endpoint automatically creates a **channel** for each key so the
    proxy can round-robin across them.  The first key is also stored
    as the provider's primary ``api_key`` setting.

    When a new API key is provided (and is not a placeholder), the
    endpoint automatically triggers model discovery in the same request
    so the chat picker can show models immediately.
    """
    if name not in ProviderRegistry.all():
        raise HTTPException(status_code=404, detail="Unknown provider")

    # -- multi-key: create channels --------------------------------
    channels_created = 0
    if body.api_keys is not None and len(body.api_keys) > 0:
        from backend.services.channel_service import ChannelService
        from backend.providers.base import ProviderRegistry as PR
        from backend.security import Security as Sec

        effective_base = (
            body.api_base
            or get_setting(f"{name}_api_base")
            or ""
        )
        if not effective_base:
            cls = PR.get(name)
            if cls:
                effective_base = cls.default_api_base

        # Use the first key as the primary api_key setting
        primary_key = body.api_keys[0]
        set_setting(f"{name}_api_key", primary_key, encrypt=True)

        # Always create channels for new keys (even single key).
        # Deduplicate against existing channels to avoid duplicates.
        existing_channels = ChannelService.list_channels(provider=name)
        existing_keys: set[str] = set()
        for ch in existing_channels:
            secret = ChannelService.get_channel_secret(ch["id"])
            if secret and secret.api_key:
                existing_keys.add(secret.api_key.strip())

        next_idx = len(existing_channels) + 1
        for key in body.api_keys:
            key = (key or "").strip()
            if not key or key in existing_keys:
                continue
            try:
                ChannelService.create_channel(
                    provider=name,
                    name=f"{name}-key-{next_idx}",
                    base_url=effective_base,
                    api_key=key,
                    weight=100,
                    is_active=True,
                )
                channels_created += 1
                existing_keys.add(key)
                next_idx += 1
            except Exception as e:
                logger.warning("Channel create failed for %s key: %s", name, e)
    else:
        # -- single-key path (legacy) --------------------------------
        if body.api_key is not None:
            set_setting(f"{name}_api_key", body.api_key, encrypt=True)

    if body.api_base is not None:
        set_setting(f"{name}_api_base", body.api_base)
    if body.enabled is not None:
        set_setting(f"{name}_enabled", "true" if body.enabled else "false")

    # Auto-discover models when a real API key is being saved.
    models_discovered = 0
    effective_key = (
        (body.api_keys[0] if body.api_keys else None)
        or body.api_key
        or ""
    )
    if effective_key and "your-" not in effective_key.lower():
        try:
            effective_base = body.api_base or get_setting(f"{name}_api_base") or ""
            prov = get_provider(name, api_key=effective_key, api_base=effective_base)
            models = await prov.list_models()
            _upsert_models(name, models)
            models_discovered = len(models)
        except Exception as e:
            logger.warning("Auto model discovery failed for %s: %s", name, e)

    result: dict = {
        "message": "已更新",
        "provider": name,
        "models_discovered": models_discovered,
    }
    if channels_created:
        result["channels_created"] = channels_created
    return result


@router.get("/providers/{name}/models")
async def list_provider_models(
    name: str,
    request: Request,
    refresh: bool = Query(False, description="Force refresh from upstream API"),
    _admin: None = Depends(_require_admin),
):
    """List the models available from a provider.

    The first call hits the upstream /v1/models (or equivalent). The result
    is cached in the `models` table; pass ?refresh=true to bypass the cache.
    """
    if name not in ProviderRegistry.all():
        raise HTTPException(status_code=404, detail="Unknown provider")

    api_key = get_setting(f"{name}_api_key") or ""
    api_base = get_setting(f"{name}_api_base") or ""

    if not api_key or "your-" in api_key.lower():
        raise HTTPException(status_code=400, detail=f"{name} 未配置 API Key")

    if not refresh:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT model_id, display_name, context_length, is_active AS enabled
                FROM models WHERE provider = ? ORDER BY display_name
            """,
                (name,),
            )
            cached = [dict(row) for row in cursor.fetchall()]
        if cached:
            return {"source": "cache", "models": cached}

    provider = get_provider(name, api_key=api_key, api_base=api_base)
    try:
        models = await provider.list_models()
    except ProviderError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        logger.exception("List models failed")
        raise HTTPException(status_code=500, detail=str(e))

    rows = []
    with get_db_context() as conn:
        cursor = conn.cursor()
        for m in models:
            try:
                cursor.execute(
                    """
                    INSERT INTO models (model_id, display_name, provider, is_active, context_length)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(model_id) DO UPDATE SET
                      display_name = excluded.display_name,
                      is_active = 1,
                      context_length = excluded.context_length
                """,
                    (m.id, m.display_name, name, int(getattr(m, "context_length", 0) or 0)),
                )
            except Exception as e:
                logger.warning("Cache insert failed for %s: %s", m.id, e)
        cursor.execute(
            """
            SELECT model_id, display_name, context_length, is_active AS enabled
            FROM models WHERE provider = ? ORDER BY display_name
        """,
            (name,),
        )
        rows = [dict(r) for r in cursor.fetchall()]

    return {"source": "live", "count": len(rows), "models": rows}


@router.get("/providers/models/all")
async def list_all_available_models(request: Request, _admin: None = Depends(_require_admin)):
    """Aggregate models across all configured providers (DB cache only)."""
    return get_cached_provider_models()


@router.post("/providers/refresh-all")
async def refresh_all_providers(request: Request, _admin: None = Depends(_require_admin_csrf)):
    """Force a fresh fetch of models from every configured provider."""
    results = await fetch_all_provider_models(force=True)
    total = sum(len(r.get("models", [])) for r in results)
    configured = [r["provider"] for r in results if r.get("configured")]
    return {
        "message": f"已刷新 {len(configured)} 个平台，共 {total} 个模型",
        "providers": configured,
        "total": total,
        "results": results,
    }


@router.get("/models/aggregated")
async def models_aggregated(
    request: Request,
    refresh: bool = Query(False, description="Force refresh from upstream APIs"),
    _admin: None = Depends(_require_admin),
):
    """Unified model catalog grouped by provider with prefixes.

    This is the source of truth for the frontend model picker: it pulls
    model lists from every configured provider dynamically, caches them,
    and prefixes each id with its provider name so the proxy can route
    requests back to the right upstream.
    """
    payload = await aggregate_models(force=refresh)
    return payload


# ---------------------------------------------------------------------------
# Provider key management (tag-style UI)
# ---------------------------------------------------------------------------


def _mask_key(key: str) -> str:
    """Show first 4 + last 4 chars, mask the middle."""
    if not key or len(key) <= 8:
        return "****"
    return f"{key[:4]}••••{key[-4:]}"


@router.get("/providers/{name}/keys")
async def list_provider_keys(
    name: str,
    request: Request,
    _admin: None = Depends(_require_admin),
):
    """Return existing API keys for a provider as masked tags.

    Combines:
    - channels (multi-key, created via /config with api_keys)
    - primary api_key setting (single-key legacy path)
    """
    if name not in ProviderRegistry.all():
        raise HTTPException(status_code=404, detail="Unknown provider")

    from backend.services.channel_service import ChannelService

    keys: list[dict] = []

    # Channels first
    channels = ChannelService.list_channels(provider=name)
    for ch in channels:
        # get_channel_secret decrypts the key
        secret_ch = ChannelService.get_channel_secret(ch["id"])
        plain = secret_ch.api_key if secret_ch else ""
        keys.append(
            {
                "id": ch["id"],
                "masked": _mask_key(plain),
                "source": "channel",
                "is_active": ch["is_active"],
                "weight": ch["weight"],
                "cooldown_until": ch.get("cooldown_until"),
            }
        )

    # If no channels, auto-promote the primary setting key to a channel
    # so it gets an ID and can be pinged / deleted from the UI.
    if not keys:
        primary = get_setting(f"{name}_api_key") or ""
        if primary and "your-" not in primary.lower():
            from backend.providers.base import ProviderRegistry as PR

            effective_base = (
                get_setting(f"{name}_api_base") or ""
            )
            if not effective_base:
                cls = PR.get(name)
                if cls:
                    effective_base = cls.default_api_base
            try:
                ch_id = ChannelService.create_channel(
                    provider=name,
                    name=f"{name}-key-1",
                    base_url=effective_base,
                    api_key=primary,
                    weight=100,
                    is_active=True,
                )
                # Re-fetch as a channel
                keys.append(
                    {
                        "id": ch_id,
                        "masked": _mask_key(primary),
                        "source": "channel",
                        "is_active": True,
                        "weight": 100,
                        "cooldown_until": None,
                    }
                )
            except Exception as e:
                logger.debug("Auto-promote primary key failed: %s", e)
                # Fallback: show as primary (no ID)
                keys.append(
                    {
                        "id": None,
                        "masked": _mask_key(primary),
                        "source": "primary",
                        "is_active": True,
                        "weight": 100,
                        "cooldown_until": None,
                    }
                )

    return {"keys": keys, "count": len(keys)}


@router.delete("/providers/{name}/keys/{channel_id}")
async def delete_provider_key(
    name: str,
    channel_id: int,
    request: Request,
    _admin: None = Depends(_require_admin_csrf),
):
    """Delete a channel (API key) from a provider."""
    if name not in ProviderRegistry.all():
        raise HTTPException(status_code=404, detail="Unknown provider")

    from backend.services.channel_service import ChannelService

    ok = ChannelService.delete_channel(channel_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"message": "已删除", "channel_id": channel_id}


@router.post("/providers/{name}/keys/{channel_id}/ping")
async def ping_provider_key(
    name: str,
    channel_id: int,
    request: Request,
    _admin: None = Depends(_require_admin_csrf),
):
    """Test a specific channel key and return latency in ms."""
    if name not in ProviderRegistry.all():
        raise HTTPException(status_code=404, detail="Unknown provider")

    from backend.services.channel_service import ChannelService

    channel = ChannelService.get_channel_secret(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    provider = get_provider(name, api_key=channel.api_key, api_base=channel.base_url)
    t0 = time.time()
    try:
        # Lightweight probe: fetch models endpoint (GET, no body)
        await provider.list_models()
        latency_ms = int((time.time() - t0) * 1000)
        return {"success": True, "latency_ms": latency_ms}
    except ProviderError as e:
        latency_ms = int((time.time() - t0) * 1000)
        return {"success": False, "latency_ms": latency_ms, "error": str(e)}
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        return {"success": False, "latency_ms": latency_ms, "error": str(e)}
