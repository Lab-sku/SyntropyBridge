"""Custom provider management.

Allows administrators to register and manage ANY OpenAI-compatible API
endpoint as a first-class provider, without writing code. Custom providers
are stored in the `custom_providers` table and are dynamically
instantiated at request time.

A custom provider carries:
  - id / slug
  - display name
  - API base URL (e.g. https://api.example.com/v1)
  - API key
  - extra keys (for key rotation / load balancing)
  - a per-provider "model prefix" used in the global model picker
  - enabled flag
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from backend.database import get_db_context
from backend.providers import (
    ProviderError,
)
from backend.providers.base import ProviderRegistry
from backend.providers.openai_compatible import OpenAICompatibleProvider
from backend.security import Security

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")


def _normalize_slug(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s or not SLUG_RE.match(s):
        raise ValueError("标识符只能包含小写字母、数字、-、_，长度2-50")
    return s


def _ensure_valid_slug(slug: str) -> str:
    """Validate a user-supplied slug without falling back to auto-generation.

    Raises ValueError when the slug is missing or invalid.
    """
    s = (slug or "").strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s or not SLUG_RE.match(s):
        raise ValueError("标识符只能包含小写字母、数字、-、_，长度2-50")
    return s


# ---------------------------------------------------------------------------
# SSRF protection: reject URLs that resolve to internal networks.
# ---------------------------------------------------------------------------

_BLOCKED_HOSTNAMES = frozenset(
    {
        "kubernetes",
        "kubernetes.default",
        "kubernetes.default.svc",
        "metadata.google.internal",
        "169.254.169.254",
    }
)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    # IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1) — these bypass
    # naive IPv4-only checks and must be blocked explicitly.
    ipaddress.ip_network("::ffff:127.0.0.0/104"),
    ipaddress.ip_network("::ffff:10.0.0.0/104"),
    ipaddress.ip_network("::ffff:172.16.0.0/108"),
    ipaddress.ip_network("::ffff:192.168.0.0/112"),
    ipaddress.ip_network("::ffff:169.254.0.0/112"),
    # Azure/GCP/AWS instance metadata endpoints
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IPv6 metadata
]

# TTL (seconds) for the per-hostname resolved-IP cache. DNS rebinding
# attacks rely on a public IP at validation time and a private IP at
# request time; the only defence that fully closes the window is to
# re-resolve on every request, so the TTL is 0. The cache is kept
# only for diagnostic diff logging (the "DNS changed" notice below).
# Per-request DNS cost is ~10ms, acceptable for the custom-provider
# path which is not on the hot proxy route.
_IP_TTL_SECONDS = 0

# hostname -> (cached_at_monotonic, [ip strings]) cache. The IPs are
# the public-routable addresses observed at validation time. Re-resolved
# IPs that drift to a private network range are rejected at request time.
_URL_IP_CACHE: Dict[str, Tuple[float, List[str]]] = {}


def _resolve_hostname_ips(hostname: str, port: int) -> List[str]:
    """Resolve *hostname* and return the list of raw IP strings.

    Raises ``ValueError`` when DNS resolution fails or returns no records.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"无法解析主机名: {hostname}")
    except OSError:
        raise ValueError(f"DNS 解析失败: {hostname}")

    if not infos:
        raise ValueError(f"主机名无法解析到任何地址: {hostname}")

    ips: List[str] = []
    for family, _type, _proto, _canonname, sockaddr in infos:
        raw_ip = sockaddr[0]
        if raw_ip and raw_ip not in ips:
            ips.append(raw_ip)
    return ips


def _reject_private_ips(hostname: str, ips: List[str]) -> None:
    """Raise ``ValueError`` when any of *ips* is a private/internal address."""
    for raw_ip in ips:
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        for net in _PRIVATE_NETWORKS:
            if addr in net:
                raise ValueError(f"不允许使用内部/私有地址: {hostname} 解析到 {raw_ip}")


def _validate_provider_url(url: str) -> str:
    """Validate that *url* is a safe, publicly-routable HTTP(S) endpoint.

    Raises ``ValueError`` with a Chinese message when the URL targets an
    internal/private network (SSRF protection). Also seeds the
    per-hostname IP cache used by :func:`_validate_url_at_request_time`.
    """
    if not url:
        raise ValueError("API Base URL 不能为空")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("API Base URL 必须以 http:// 或 https:// 开头")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("API Base URL 缺少主机名")

    # Block well-known internal hostnames (literal match).
    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError(f"不允许使用内部地址: {hostname}")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    ips = _resolve_hostname_ips(hostname, port)
    _reject_private_ips(hostname, ips)

    # Seed the IP cache so request-time validation has a baseline to
    # compare against. Subsequent calls refresh the entry.
    _URL_IP_CACHE[hostname] = (time.monotonic(), list(ips))

    # Return a normalised form (strip trailing slash).
    return url.rstrip("/")


def _validate_url_at_request_time(url: str) -> None:
    """Re-verify that *url* still resolves to public IPs at request time.

    Defends against DNS rebinding: a hostname that resolved to a public
    IP at create/update time may resolve to an internal address by the
    time the request is dispatched. Because DNS rebinding can race any
    TTL, we re-resolve on every request and reject private-address
    drift. The resolved-IP cache is kept only for diagnostic diff
    logging; it never short-circuits the re-resolution.

    Logs a warning and raises ``ValueError`` on a private-IP drift.
    """
    if not url:
        return
    parsed = urllib.parse.urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in _BLOCKED_HOSTNAMES:
        # Already validated at create/update time; nothing to do here.
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    cached = _URL_IP_CACHE.get(hostname)
    now = time.monotonic()

    # Re-resolve on every call. A non-zero TTL would re-open the DNS
    # rebinding window: an attacker could flip the hostname to a
    # private IP within the TTL and ride the cached "public" verdict.
    try:
        ips = _resolve_hostname_ips(hostname, port)
    except ValueError:
        # DNS failures at request time are logged but non-fatal: the
        # upstream HTTP call will surface the error in its own way.
        logger.warning("custom provider DNS re-resolution failed: %s", hostname)
        return

    try:
        _reject_private_ips(hostname, ips)
    except ValueError as exc:
        logger.warning("custom provider SSRF drift blocked: %s -> %s", hostname, ips)
        raise

    if cached and set(cached[1]) != set(ips):
        logger.info(
            "custom provider DNS changed for %s: %s -> %s",
            hostname,
            cached[1],
            ips,
        )
    _URL_IP_CACHE[hostname] = (now, list(ips))


def _to_dict(row) -> Dict[str, Any]:
    if row is None:
        return {}
    return {k: row[k] for k in row.keys()}


def _mask_key(key: str) -> str:
    """Mask an API key for display, showing only the last 4 characters."""
    if not key or len(key) <= 4:
        return "****"
    return "*" * 8 + key[-4:]


def list_custom_providers(include_disabled: bool = True) -> List[Dict[str, Any]]:
    """Return all custom providers from the DB with masked API keys."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        if include_disabled:
            cursor.execute("SELECT * FROM custom_providers ORDER BY created_at DESC")
        else:
            cursor.execute(
                "SELECT * FROM custom_providers WHERE is_enabled = 1 ORDER BY created_at DESC"
            )
        rows = []
        for r in cursor.fetchall():
            d = _to_dict(r)
            # Mask encrypted keys for admin display
            raw_key = d.get("api_key") or ""
            raw_keys = d.get("api_keys") or ""
            if raw_key and raw_key.startswith("gAAAAA"):
                decrypted = Security.decrypt(raw_key) or ""
                d["api_key_masked"] = _mask_key(decrypted)
                d["api_key"] = ""  # Don't return encrypted blob
            else:
                d["api_key_masked"] = _mask_key(raw_key) if raw_key else ""
            if raw_keys and raw_keys.startswith("gAAAAA"):
                decrypted = Security.decrypt(raw_keys) or ""
                parts = decrypted.split(",")
                d["api_keys_masked"] = [_mask_key(k) for k in parts if k.strip()]
                d["api_keys"] = ""  # Don't return encrypted blob
            else:
                parts = raw_keys.split(",") if raw_keys else []
                d["api_keys_masked"] = [_mask_key(k) for k in parts if k.strip()]
            rows.append(d)
        return rows


def get_custom_provider(slug: str) -> Optional[Dict[str, Any]]:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM custom_providers WHERE slug = ?", (slug,))
        row = cursor.fetchone()
        if row is None:
            return None
        d = _to_dict(row)
        # Mask encrypted keys
        raw_key = d.get("api_key") or ""
        raw_keys = d.get("api_keys") or ""
        if raw_key and raw_key.startswith("gAAAAA"):
            decrypted = Security.decrypt(raw_key) or ""
            d["api_key_masked"] = _mask_key(decrypted)
            d["api_key"] = ""
        else:
            d["api_key_masked"] = _mask_key(raw_key) if raw_key else ""
        if raw_keys and raw_keys.startswith("gAAAAA"):
            decrypted = Security.decrypt(raw_keys) or ""
            parts = decrypted.split(",")
            d["api_keys_masked"] = [_mask_key(k) for k in parts if k.strip()]
            d["api_keys"] = ""
        else:
            parts = raw_keys.split(",") if raw_keys else []
            d["api_keys_masked"] = [_mask_key(k) for k in parts if k.strip()]
        return d


def create_custom_provider(
    name: str,
    api_base: str,
    api_key: str,
    slug: Optional[str] = None,
    display_name: Optional[str] = None,
    api_keys: Optional[List[str]] = None,
    notes: str = "",
) -> Dict[str, Any]:
    """Insert a new custom provider. Raises ValueError on validation errors."""
    display_name = (display_name or name).strip()
    if not display_name:
        raise ValueError("名称不能为空")
    api_base = _validate_provider_url(api_base)
    if not api_key and not api_keys:
        raise ValueError("请至少配置一个 API Key")

    # The slug is the unique identifier used in the model picker. When the
    # caller does not provide one, derive it from the ASCII-safe `name`
    # field first, then from `display_name`. Non-ASCII display names
    # (e.g. "硅基流动") cannot be slugified, so they fall through to a
    # stable hash-based default.
    if slug:
        final_slug = _ensure_valid_slug(slug)
    elif name and re.match(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$", name or ""):
        final_slug = _normalize_slug(name)
    elif display_name and re.match(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$", display_name or ""):
        final_slug = _normalize_slug(display_name)
    else:
        # Stable deterministic fallback so Chinese names still get a slug.
        import hashlib

        seed = (name or display_name or "provider").encode("utf-8")
        digest = hashlib.md5(seed).hexdigest()[:8]
        final_slug = f"provider-{digest}"
        # Validate the constructed slug to keep the contract clean.
        final_slug = _ensure_valid_slug(final_slug)

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM custom_providers WHERE slug = ?", (final_slug,))
        if cursor.fetchone():
            raise ValueError(f"标识符 {final_slug} 已存在，请使用其他名称")

        keys_json = ",".join(api_keys) if api_keys else api_key
        encrypted_key = Security.encrypt(api_key) if api_key else ""
        encrypted_keys = Security.encrypt(keys_json) if keys_json else ""
        cursor.execute(
            """
            INSERT INTO custom_providers
                (slug, display_name, api_base, api_key, api_keys, notes, is_enabled)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (final_slug, display_name, api_base, encrypted_key, encrypted_keys, notes),
        )
    cfg = get_custom_provider(final_slug)
    if cfg is None:
        raise ValueError("创建后无法读取")
    return cfg


def update_custom_provider(
    slug: str,
    display_name: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    api_keys: Optional[List[str]] = None,
    notes: Optional[str] = None,
    is_enabled: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    existing = get_custom_provider(slug)
    if not existing:
        return None

    updates = []
    params: List[Any] = []
    if display_name is not None:
        updates.append("display_name = ?")
        params.append(display_name.strip())
    if api_base is not None:
        api_base = _validate_provider_url(api_base)
        updates.append("api_base = ?")
        params.append(api_base)
    if api_key is not None:
        updates.append("api_key = ?")
        params.append(Security.encrypt(api_key))
    if api_keys is not None:
        updates.append("api_keys = ?")
        params.append(Security.encrypt(",".join(api_keys)))
    if notes is not None:
        updates.append("notes = ?")
        params.append(notes)
    if is_enabled is not None:
        updates.append("is_enabled = ?")
        params.append(1 if is_enabled else 0)

    if not updates:
        return existing

    params.append(slug)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE custom_providers SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE slug = ?",
            params,
        )
    return get_custom_provider(slug)


def delete_custom_provider(slug: str) -> bool:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM custom_providers WHERE slug = ?", (slug,))
        return cursor.rowcount > 0


def parse_keys(cfg: Dict[str, Any]) -> List[str]:
    """Return the list of API keys for a custom provider, falling back
    gracefully when the legacy single-key field is used. Keys are
    decrypted from storage before being returned."""
    out: List[str] = []
    raw = cfg.get("api_keys") or ""
    if raw:
        decrypted = Security.decrypt(raw) or ""
        for piece in str(decrypted).split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    if not out:
        legacy = (cfg.get("api_key") or "").strip()
        if legacy:
            decrypted = Security.decrypt(legacy) or ""
            out.append(decrypted)
    return [k for k in out if k and "your-" not in k.lower()]


class _CustomOpenAIProvider(OpenAICompatibleProvider):
    """Per-request OpenAI-compatible provider with multi-key rotation.

    Subclasses OpenAICompatibleProvider so the rest of the platform
    (proxy, model aggregator, billing) treats it as a normal provider
    and benefits from streaming, function calling, etc. transparently.
    """

    def __init__(self, slug: str, display_name: str, api_base: str, keys: List[str]):
        # Bypass Provider.__init__ which only stores a single api_key
        self.api_key = keys[0] if keys else ""
        self.api_base = api_base
        self._keys = keys
        self._key_index = 0
        self._slug = slug
        self.name = slug
        self.display_name = display_name
        self.api_key_setting = f"custom_{slug}_api_key"
        self.api_base_setting = f"custom_{slug}_api_base"
        self.model_prefix = slug
        self.default_api_base = api_base
        self.requires_api_key = True

    def _next_key(self) -> str:
        if not self._keys:
            return ""
        key = self._keys[self._key_index % len(self._keys)]
        self._key_index = (self._key_index + 1) % len(self._keys)
        self.api_key = key
        return key

    def _headers(self) -> Dict[str, str]:
        # Re-validate the API base URL at request time to defend against
        # DNS rebinding (a hostname that resolved to a public IP at
        # create time may resolve to an internal address now). The TTL
        # cache keeps this cheap on the hot path.
        try:
            _validate_url_at_request_time(self.api_base)
        except ValueError as exc:
            # Convert to ProviderError so the proxy layer surfaces a
            # clean 502 instead of an unhandled exception.
            raise ProviderError(str(exc), status_code=502)
        return {
            "Authorization": f"Bearer {self._next_key()}",
            "Content-Type": "application/json",
        }

    def is_configured(self) -> bool:
        return len(self._keys) > 0


def get_provider_class(slug: str) -> Optional[type]:
    """Return a dynamic provider class for a custom slug, registering it
    on the fly so the global registry sees it. Returns None if the slug
    is not configured."""
    cfg = get_custom_provider(slug)
    if not cfg:
        return None
    keys = parse_keys(cfg)
    if not cfg.get("is_enabled") or not keys:
        return None

    existing = ProviderRegistry.get(slug)
    if existing is not None and getattr(existing, "_custom_for", None) == cfg.get("id"):
        return existing

    cls = type(
        f"CustomProvider_{slug}",
        (_CustomOpenAIProvider,),
        {
            "_custom_for": cfg.get("id"),
            "name": slug,
            "display_name": cfg.get("display_name") or slug,
            "default_api_base": cfg.get("api_base"),
            "api_key_setting": f"custom_{slug}_api_key",
            "api_base_setting": f"custom_{slug}_api_base",
            "model_prefix": slug,
        },
    )

    def _factory(api_key: str = "", api_base: str = ""):
        return _CustomOpenAIProvider(
            slug=slug,
            display_name=cfg.get("display_name") or slug,
            api_base=api_base or cfg.get("api_base"),
            keys=keys,
        )

    cls.create = classmethod(lambda cls_, api_key="", api_base="": _factory(api_key, api_base))  # type: ignore
    ProviderRegistry._providers[slug] = cls  # type: ignore
    return cls


def ensure_all_registered() -> None:
    """Register all enabled custom providers with the global registry."""
    for cfg in list_custom_providers(include_disabled=False):
        try:
            get_provider_class(cfg["slug"])
        except Exception as e:
            logger.warning("Failed to register custom provider %s: %s", cfg["slug"], e)


async def test_custom_provider(slug: str) -> Dict[str, Any]:
    cfg = get_custom_provider(slug)
    if not cfg:
        return {"success": False, "message": "自定义平台不存在"}
    keys = parse_keys(cfg)
    if not keys:
        return {"success": False, "message": "未配置 API Key"}
    instance = _CustomOpenAIProvider(
        slug=slug,
        display_name=cfg.get("display_name") or slug,
        api_base=cfg.get("api_base"),
        keys=keys,
    )
    try:
        models = await instance.list_models()
        return {
            "success": True,
            "message": f"成功，发现 {len(models)} 个模型",
            "count": len(models),
        }
    except ProviderError as e:
        return {"success": False, "message": str(e), "status_code": e.status_code}
    except Exception as e:
        logger.exception("Custom provider test failed")
        return {"success": False, "message": f"测试失败: {e}"}


async def fetch_custom_models(slug: str, force: bool = False) -> List[Dict[str, Any]]:
    """List models for a custom provider and refresh the cache."""
    cfg = get_custom_provider(slug)
    if not cfg:
        return []
    keys = parse_keys(cfg)
    if not keys:
        return []
    instance = _CustomOpenAIProvider(
        slug=slug,
        display_name=cfg.get("display_name") or slug,
        api_base=cfg.get("api_base"),
        keys=keys,
    )
    models = await instance.list_models()
    rows: List[Dict[str, Any]] = []
    with get_db_context() as conn:
        cursor = conn.cursor()
        for m in models:
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
                    (
                        m.id,
                        m.display_name,
                        f"custom:{slug}",
                        int(getattr(m, "context_length", 0) or 0),
                    ),
                )
            except Exception as e:
                logger.warning("Cache insert failed for custom:%s/%s: %s", slug, m.id, e)
        cursor.execute(
            "SELECT model_id, display_name, context_length FROM models WHERE provider = ? ORDER BY display_name",
            (f"custom:{slug}",),
        )
        rows = [dict(r) for r in cursor.fetchall()]
    return rows
