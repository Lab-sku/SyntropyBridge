"""Provider detection utilities for AI model routing.

Defers to the canonical `detect_provider_from_model` in the provider
base module so model routing stays consistent across the codebase.
"""

import json
import logging

from backend.providers import detect_provider_from_model as _detect

logger = logging.getLogger(__name__)


def get_provider_for_model(model: str) -> str:
    """Determine the upstream API provider for a given model name.

    Order of preference:

    1. The admin-configured ``model_provider_map`` setting (explicit
       overrides win over everything).
    2. :func:`backend.providers.detect_provider_from_model` — the
       canonical prefix/keyword detector shared with the rest of the
       platform.

    Returns:
        The provider name registered in `ProviderRegistry`. Defaults to
        "minimax" when no rule matches.
    """
    if not model:
        return "minimax"

    # 1. Check admin-configured model-provider mapping
    try:
        from backend.database import get_setting

        raw = get_setting("model_provider_map") or ""
        if raw:
            mapping = {}
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    mapping = {str(k): str(v) for k, v in parsed.items()}
            except Exception:
                mapping = {}

            if mapping:
                try:
                    from backend.providers.base import ProviderRegistry

                    valid = set(ProviderRegistry.all().keys())
                except Exception:
                    valid = set()
                if model in mapping and mapping[model] in valid:
                    return mapping[model]
                for k, v in mapping.items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        continue
                    if k.endswith("*") and v in valid:
                        prefix = k[:-1]
                        if prefix and model.startswith(prefix):
                            return v
    except Exception:
        pass

    # 2. Fall back to canonical detector
    return _detect(model)
