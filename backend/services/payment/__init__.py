"""Pluggable payment gateway.

Import the provider registry and individual providers from this package::

    from backend.services.payment import get_provider, list_providers

All providers implement :class:`PaymentProvider` defined in ``base.py``.
"""

from __future__ import annotations

import logging
from typing import Dict

from backend.services.payment.base import PaymentProvider

logger = logging.getLogger(__name__)

# Lazy registry: provider name -> class (imported on first access to avoid
# pulling in SDK dependencies for providers that aren't configured).
_PROVIDER_CLASSES: Dict[str, str] = {
    "stripe": "backend.services.payment.stripe_provider.StripeProvider",
    "alipay": "backend.services.payment.alipay_provider.AlipayProvider",
    "wechat": "backend.services.payment.wechat_provider.WechatProvider",
    "usdt": "backend.services.payment.usdt_provider.UsdtProvider",
}

_PROVIDER_INSTANCES: Dict[str, PaymentProvider] = {}


def get_provider(name: str) -> PaymentProvider:
    """Return a singleton instance of the named provider.

    Raises ``KeyError`` for unknown providers and ``RuntimeError`` when
    the provider's SDK / configuration is missing.
    """
    name = name.lower().strip()
    if name not in _PROVIDER_CLASSES:
        raise KeyError(f"Unknown payment provider: {name}")

    if name not in _PROVIDER_INSTANCES:
        module_path, class_name = _PROVIDER_CLASSES[name].rsplit(".", 1)
        import importlib

        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        _PROVIDER_INSTANCES[name] = cls()

    return _PROVIDER_INSTANCES[name]


def list_providers() -> Dict[str, Dict]:
    """Return metadata about every registered provider.

    Each entry contains:
      - ``name``: the provider slug
      - ``available``: whether the provider can be instantiated without errors
      - ``error``: an error message when ``available`` is False
    """
    result: Dict[str, Dict] = {}
    for name in _PROVIDER_CLASSES:
        try:
            get_provider(name)
            result[name] = {"name": name, "available": True, "error": None}
        except Exception as exc:
            result[name] = {"name": name, "available": False, "error": str(exc)}
    return result


def reset_providers() -> None:
    """Clear cached provider instances (used by tests)."""
    _PROVIDER_INSTANCES.clear()
