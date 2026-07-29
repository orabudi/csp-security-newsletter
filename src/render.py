"""Jinja2 rendering: Markdown, HTML (email + archive) and Telegram HTML.

URLs are joined back onto the model's verdicts here, from the fetched item —
the model never saw them (§3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .normalize import Item
from .rank import PROVIDER_LABELS, Digest
from .util import TEMPLATE_DIR, log, now_utc

THIN_DAY_NOTE = "Quiet day — only a handful of security-relevant changes."
EMPTY_DAY_NOTE = "No security-relevant changes found in today's window."


@dataclass
class Rendered:
    subject: str
    markdown: str
    html: str
    telegram: str
    context: dict[str, Any] = field(default_factory=dict)


def _autoescape(name: str | None) -> bool:
    return bool(name) and name.endswith((".html.j2", ".telegram.j2"))


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=_autoescape,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters["provider_label"] = lambda p: PROVIDER_LABELS.get(p, p.upper())
    return env


def _item_view(item: Item) -> dict[str, Any]:
    return {
        "id": item.id,
        "provider": item.provider,
        "provider_label": PROVIDER_LABELS.get(item.provider, item.provider.upper()),
        "service": item.service,
        "title": item.title,
        "url": item.url,
        "tldr": item.tldr or item.title,
        "impact": item.impact,
        "action_required": item.action_required,
        "estate_hit": item.estate_hit,
        "published": item.published_at.strftime("%d %b"),
        "published_iso": item.published_at.isoformat(),
        "tier": item.source_tier,
    }


def build_context(
    digest: Digest,
    *,
    unavailable: list[str],
    alerts: list[str],
    archive_url: str | None,
    classifier_path: str,
    run_date: datetime | None = None,
) -> dict[str, Any]:
    run_date = run_date or now_utc()
    # NB: the key is `entries`, not `items` — in Jinja, `section.items` would
    # resolve to dict.items before the mapping key.
    sections = [
        {
            "key": section.key,
            "emoji": section.emoji,
            "title": section.title,
            "is_action": section.is_action,
            "entries": [_item_view(item) for item in section.items],
        }
        for section in digest.sections
    ]

    note = ""
    if digest.is_empty:
        note = EMPTY_DAY_NOTE
    elif digest.is_thin:
        note = THIN_DAY_NOTE

    return {
        "date_label": run_date.strftime("%a %d %b %Y"),
        "date_iso": run_date.strftime("%Y-%m-%d"),
        "generated_at": run_date.strftime("%Y-%m-%d %H:%M UTC"),
        "lede": digest.lede.strip(),
        "sections": sections,
        "overflow": [_item_view(item) for item in digest.overflow],
        "overflow_count": len(digest.overflow),
        "total_shown": digest.total_shown,
        "note": note,
        "is_empty": digest.is_empty,
        "is_thin": digest.is_thin,
        "unavailable": unavailable,
        "alerts": alerts,
        "archive_url": archive_url,
        "classifier_path": classifier_path,
        "degraded": classifier_path != "llm",
    }


def render(context: dict[str, Any]) -> Rendered:
    env = _env()
    subject_prefix = os.getenv("SUBJECT_PREFIX", "🔐 Cloud Security TLDR")
    subject = f"{subject_prefix} — {context['date_label']}"
    context = {**context, "subject": subject}

    markdown = env.get_template("digest.md.j2").render(**context)
    html = env.get_template("digest.html.j2").render(**context)
    telegram = env.get_template("digest.telegram.j2").render(**context)

    log.info(
        "rendered digest: %d items, %d overflow (%d chars md)",
        context["total_shown"], context["overflow_count"], len(markdown),
    )
    return Rendered(
        subject=subject,
        markdown=markdown.strip() + "\n",
        html=html,
        telegram=telegram.strip(),
        context=context,
    )
