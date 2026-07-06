"""Download vendor logos from jsdelivr (mirrors simple-icons) and
save them to the frontend/public/providers folder.

The path is constructed relative to the project root so we don't
have to deal with PowerShell's quirks on Chinese paths.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request

# Map of internal provider-id -> simple-icons slug
# (see https://simpleicons.org — slug == path component in the
# `simple-icons` package on npm).
MAP = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "deepseek": "deepseek",
    "moonshot": "moonshotai",
    # zhipu / kimi / groq / cohere don't ship with simple-icons
    # under their brand names. We use moonshotai for kimi (its
    # parent company) and chatgpt for the cohere/groq slots —
    # any svg we manage to fetch is better than a blank chip.
    "zhipu": "zhipu",
    "kimi": "moonshotai",
    "groq": "groq",
    "cohere": "cohere",
    # On-prem / inference providers we draw ourselves.
    "ollama": "ollama",
    "vllm": "vllm",
    "nvidia": "nvidia",
    "openrouter": "openrouter",
    "aliyun": "alibabacloud",
    "doubao": "bytedance",
    "mimo": "xiaomi",
    "hunyuan": "tencentqq",
    "wenxin": "baidu",
    "sensenova": "sensetime",
    "spark": "iflytek",
    "baichuan": "baidu",
}

# Add alternative-slug fallbacks for the brand-name mismatches.
ALT_SLUGS = {
    "zhipu":   ["zhipuai", "zhipu"],
    "kimi":    ["kimi", "moonshotai"],
    "groq":    ["groq"],
    "cohere":  ["cohere"],
    "vllm":    ["vllm"],
    "sensenova": ["sensetime", "sensetimegroup"],
    "spark":   ["iflytek", "xunfei"],
}

# jsdelivr hosts the simple-icons package on npm. We try a few
# known-good versions and fall back to simpleicons.org.
SOURCES = [
    "https://cdn.jsdelivr.net/npm/simple-icons@13/icons/{slug}.svg",
    "https://cdn.jsdelivr.net/npm/simple-icons/icons/{slug}.svg",
    "https://cdn.simpleicons.org/{slug}",
]


def download(url: str) -> bytes | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (logo-fetch)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None
    if len(data) < 200:
        return None
    if b"<svg" not in data:
        return None
    return data


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    # script is in <root>/scripts; destination is <root>/frontend/public/providers
    root = os.path.dirname(here)
    dest = os.path.join(root, "frontend", "public", "providers")
    os.makedirs(dest, exist_ok=True)

    ok, fail = [], []
    for name, primary_slug in MAP.items():
        out = os.path.join(dest, f"{name}.svg")
        saved = False
        # Build slug list: primary first, then the alternatives.
        slugs = [primary_slug] + ALT_SLUGS.get(name, [])
        for slug in slugs:
            for tpl in SOURCES:
                url = tpl.format(slug=slug)
                data = download(url)
                if data is not None:
                    with open(out, "wb") as fp:
                        fp.write(data)
                    ok.append(name)
                    saved = True
                    break
            if saved:
                break
        if not saved:
            fail.append(f"{name} (slugs: {','.join(slugs)})")

    print(f"OK   : {', '.join(ok)}")
    print(f"FAIL : {', '.join(fail)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
