"""
Internationalization (i18n) module.
Loads locale JSON files and provides translation helpers.
Bilingual card text is composed by combining zh + en strings.
"""
from __future__ import annotations

import json
import os
from typing import Optional

_LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

_cache: dict[str, dict] = {}


def _load_locale(lang: str) -> dict:
    """Load a locale JSON file (cached)."""
    if lang not in _cache:
        path = os.path.join(_LOCALES_DIR, f"{lang}.json")
        with open(path, "r", encoding="utf-8") as f:
            _cache[lang] = json.load(f)
    return _cache[lang]


def t(lang: str, dotted_key: str) -> str:
    """Get a translated string by dotted key path.

    Example: t("en", "card.alert_title") -> "MEV Sandwich Attack Alert"
    """
    data = _load_locale(lang)
    for part in dotted_key.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return dotted_key  # fallback: return the key itself
    return str(data)


def bi(dotted_key: str, sep: str = " / ") -> str:
    """Get a bilingual string (Chinese / English).

    Example: bi("card.block_label") -> "区块 / Block"
    """
    zh = t("zh", dotted_key)
    en = t("en", dotted_key)
    if zh == en:
        return zh
    return f"{zh}{sep}{en}"


def get_subscriber_message(lang: str, report, report_url: str) -> str:
    """Build a subscriber notification message in the specified language."""
    m = _load_locale(lang)["main"]
    lines = [
        f"🚨 *{m['subscriber_alert']}*",
        f"{m['subscriber_address']}: `{report.victim_address[:10]}...`",
        f"{m['subscriber_loss']}: {report.victim_loss_native} {report.native_symbol}",
    ]
    if report_url:
        lines.append(f"📋 [{m['subscriber_view_report']}]({report_url})")
    return "\n".join(lines)
