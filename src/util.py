"""Shared helpers: paths, config loading, logging, text and regex utilities."""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
TEMPLATE_DIR = ROOT / "templates"
DOCS_DIR = ROOT / "docs"
STATE_PATH = ROOT / "state.json"

log = logging.getLogger("csp-sec-tldr")


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_yaml(name: str) -> Any:
    """Load a config file from config/. Returns {} for a missing file."""
    path = CONFIG_DIR / name
    if not path.exists():
        log.warning("config %s not found, using empty config", name)
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(raw: str | None) -> str:
    """Feed descriptions are HTML. Reduce to a single line of plain text."""
    if not raw:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", " ", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return cut + "…"


def clip_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).rstrip(" ,.;:-") + "…"


def sha1(*parts: str) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# regex
# --------------------------------------------------------------------------- #


def compile_patterns(patterns: Iterable[str] | None) -> list[re.Pattern]:
    """Compile a list of config patterns, skipping (loudly) any that are bad."""
    compiled: list[re.Pattern] = []
    for pattern in patterns or []:
        try:
            compiled.append(re.compile(pattern, re.I))
        except re.error as exc:
            log.error("bad pattern %r in config: %s", pattern, exc)
    return compiled


def first_match(patterns: list[re.Pattern], text: str) -> str | None:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found.group(0)
    return None


def any_match(patterns: list[re.Pattern], text: str) -> bool:
    return first_match(patterns, text) is not None


def contains_any(needles: Iterable[str], haystack: str) -> str | None:
    """Case-insensitive substring match; returns the needle that matched."""
    low = haystack.lower()
    for needle in needles or []:
        if needle and needle.lower() in low:
            return needle
    return None
