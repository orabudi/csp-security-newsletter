"""Feed entries -> the common item schema (§10).

Per-provider quirks live here and nowhere else:
  * GCP release notes publish ONE entry per calendar day whose body contains
    every product's notes for that day, marked up as
    <h2 class="release-note-product-title">Product</h2><h3>Category</h3>...
    Each of those sections becomes its own item — treating the entry as a
    single item would collapse ~40 daily GCP changes into one line.
  * Azure prefixes titles with "[Launched]" / "[In preview]" and exposes the
    same status as the first feed category, followed by topic tags, the
    product name and type tags.
  * AWS What's New has no taxonomy at all; the service is inferred from the
    title, and classification does the real work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import feedparser

from .fetch import FetchResult, Source
from .util import log, now_utc, sha1, strip_html, truncate

BLURB_LIMIT = 400

# GCP daily entries: product sections, then category sub-sections.
GCP_PRODUCT_SPLIT_RE = re.compile(
    r"<h2[^>]*class=[\"']?release-note-product-title[\"']?[^>]*>", re.I
)
GCP_H3_SPLIT_RE = re.compile(r"<h3[^>]*>", re.I)
GCP_CLOSE_H2_RE = re.compile(r"</h2>", re.I)
GCP_CLOSE_H3_RE = re.compile(r"</h3>", re.I)

AZURE_STATUSES = {
    "in development",
    "in preview",
    "launched",
    "generally available",
    "public preview",
    "private preview",
    "retirements",
    "retirement",
    "retired",
}

# Azure tags that are topics or content types rather than a product name.
AZURE_NON_PRODUCT_TAGS = {
    "features", "feature", "services", "service", "regions", "region",
    "compliance", "security", "management", "pricing & offerings", "open source",
    "sdk and tools", "microsoft build", "microsoft ignite", "operating system",
    "analytics", "ai + machine learning", "compute", "containers", "databases",
    "developer tools", "devops", "hybrid + multicloud", "identity", "integration",
    "internet of things", "media", "migration", "mixed reality", "mobile",
    "networking", "storage", "virtual desktop infrastructure", "web",
    "windows virtual desktop", "gaming", "blockchain",
}

AZURE_PRODUCT_HINTS = ("azure", "microsoft", "entra", "defender", "sentinel", "purview")

AWS_SERVICE_RE = re.compile(
    r"^(?:Announcing\s+|Introducing\s+|New\s*[–—-]\s*)?"
    r"((?:AWS|Amazon)(?:\s+[A-Z0-9][\w./-]*){1,4})"
)

AZURE_TITLE_PREFIX_RE = re.compile(
    r"^\s*\[?\s*(Generally Available|General Availability|Public Preview|"
    r"Private Preview|Preview|Retirement|Retirements|Retired|In development|"
    r"In preview|Launched|Now available)\s*\]?\s*[:\-–]?\s*",
    re.I,
)


@dataclass
class Item:
    """One normalised news item. Fields after `content_hash` are filled in later."""

    id: str
    provider: str
    source: str
    source_tier: int
    title: str
    url: str
    published_at: datetime
    blurb: str
    content_hash: str
    service: str | None = None
    raw_category: str | None = None
    status: str | None = None

    # populated by classify.py
    is_security: bool = False
    category: str = "governance"
    tldr: str = ""
    impact: str = "medium"
    action_required: bool = False

    # populated by rank.py
    score: float = 0.0
    promote_reason: str | None = None
    estate_hit: str | None = None
    security_service_hit: str | None = None

    # bookkeeping
    classified_by: str = "none"
    drop_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The blob every rule and lexicon match runs against."""
        return f"{self.title}\n{self.service or ''}\n{self.blurb}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "source": self.source,
            "source_tier": self.source_tier,
            "service": self.service,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at.isoformat(),
            "blurb": self.blurb,
            "content_hash": self.content_hash,
            "raw_category": self.raw_category,
            "status": self.status,
            "is_security": self.is_security,
            "category": self.category,
            "tldr": self.tldr,
            "impact": self.impact,
            "action_required": self.action_required,
            "score": round(self.score, 2),
            "promote_reason": self.promote_reason,
            "estate_hit": self.estate_hit,
            "security_service_hit": self.security_service_hit,
            "classified_by": self.classified_by,
            "drop_reason": self.drop_reason,
        }


def _entry_datetime(entry: Any) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    # No usable date: treat as "now" so the item survives the freshness window
    # rather than being silently dropped. Recall over precision (§1).
    return now_utc()


def _entry_body(entry: Any) -> str:
    content = entry.get("content")
    if content:
        joined = " ".join(part.get("value", "") for part in content)
        if joined.strip():
            return joined
    return entry.get("summary", "") or entry.get("description", "") or ""


def _entry_tags(entry: Any) -> list[str]:
    tags = []
    for tag in entry.get("tags") or []:
        term = (tag.get("term") or "").strip()
        if term:
            tags.append(term)
    return tags


def _entry_url(entry: Any, source: Source) -> str:
    for key in ("link", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    links = entry.get("links") or []
    for link in links:
        href = link.get("href", "")
        if href.startswith("http"):
            return href
    return source.url


def _aws_service(title: str) -> str | None:
    match = AWS_SERVICE_RE.match(title)
    if not match:
        return None
    # Trim trailing verbs the greedy match may have swallowed.
    name = match.group(1).strip()
    stop = re.search(
        r"\b(now|is|are|adds|supports|announces|launches|introduces|expands|"
        r"available|releases|enables|adds|has|can|will|customers)\b",
        name,
        re.I,
    )
    if stop:
        name = name[: stop.start()].strip()
    return name or None


def _azure_service(tags: list[str]) -> str | None:
    """Tags look like [status, ...topics..., Product, ...types...].

    Topics ("Networking") and types ("Features") are indistinguishable from a
    product by position alone, so prefer a tag that names a Microsoft product
    and fall back to whatever is left after removing the known non-products.
    """
    candidates = [
        tag.strip()
        for tag in tags
        if tag.strip().lower() not in AZURE_STATUSES
        and tag.strip().lower() not in AZURE_NON_PRODUCT_TAGS
    ]
    for tag in candidates:
        if any(hint in tag.lower() for hint in AZURE_PRODUCT_HINTS):
            return tag
    return candidates[0] if candidates else None


def _azure_status(title: str, tags: list[str]) -> str | None:
    for tag in tags:
        if tag.strip().lower() in AZURE_STATUSES:
            return tag.strip()
    match = AZURE_TITLE_PREFIX_RE.match(title)
    return match.group(1) if match else None


def _make_item(
    result: FetchResult,
    *,
    guid: str,
    title: str,
    blurb: str,
    url: str,
    published_at: datetime,
    service: str | None,
    raw_category: str | None = None,
    status: str | None = None,
) -> Item | None:
    if not title and not blurb:
        return None
    if not title:
        title = truncate(blurb, 140)
    blurb = truncate(blurb, BLURB_LIMIT)
    return Item(
        id=sha1(result.source.key, guid)[:16],
        provider=result.source.provider,
        source=result.source.key,
        source_tier=result.source.tier,
        service=service.strip() if isinstance(service, str) and service.strip() else None,
        title=title,
        url=url,
        published_at=published_at,
        blurb=blurb,
        content_hash=sha1(title, blurb)[:16],
        raw_category=raw_category,
        status=status,
    )


def _split_gcp_entry(entry: Any, result: FetchResult) -> list[Item]:
    """One GCP daily entry -> one item per (product, category) section."""
    body = _entry_body(entry)
    url = _entry_url(entry, result.source)
    published = _entry_datetime(entry)
    day = strip_html(entry.get("title", "")).strip()
    guid_base = str(entry.get("id") or entry.get("guid") or url)

    chunks = GCP_PRODUCT_SPLIT_RE.split(body)[1:]  # [0] is preamble, if any
    if not chunks:
        return []

    items: list[Item] = []
    for chunk in chunks:
        parts = GCP_CLOSE_H2_RE.split(chunk, maxsplit=1)
        head = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        product = strip_html(head).strip()
        if not product:
            continue

        sections = GCP_H3_SPLIT_RE.split(rest)
        # Anything before the first <h3> has no category of its own.
        pairs: list[tuple[str | None, str]] = []
        if sections and strip_html(sections[0]).strip():
            pairs.append((None, sections[0]))
        for section in sections[1:]:
            label_parts = GCP_CLOSE_H3_RE.split(section, maxsplit=1)
            label = strip_html(label_parts[0]).strip() or None
            pairs.append((label, label_parts[1] if len(label_parts) > 1 else ""))

        for index, (category, note_html) in enumerate(pairs):
            note = strip_html(note_html).strip()
            if not note:
                continue
            headline = truncate(note, 160)
            item = _make_item(
                result,
                guid=f"{guid_base}|{product}|{category or ''}|{index}",
                title=f"{product}: {headline}",
                blurb=note,
                url=url,
                published_at=published,
                service=product,
                raw_category=category,
                status=day or None,
            )
            if item:
                items.append(item)
    return items


def _normalize_entry(entry: Any, result: FetchResult) -> list[Item]:
    source = result.source

    if source.provider == "gcp" and source.tier == 2:
        split = _split_gcp_entry(entry, result)
        if split:
            return split
        # Feed markup changed — fall through and keep the day as one item
        # rather than dropping it silently.
        log.warning("gcp entry %r had no product sections, keeping whole",
                    entry.get("title", "")[:40])

    title = strip_html(entry.get("title", "")).strip()
    blurb = strip_html(_entry_body(entry))
    tags = _entry_tags(entry)
    url = _entry_url(entry, source)
    service = source.service
    raw_category = tags[0] if tags else None
    status = None

    if source.provider == "azure":
        status = _azure_status(title, tags)
        service = service or _azure_service(tags)
        raw_category = status or raw_category
        title = AZURE_TITLE_PREFIX_RE.sub("", title).strip() or title
    elif source.provider == "aws":
        service = service or _aws_service(title)
    elif source.provider == "oci":
        service = service or (tags[0] if tags else None)

    item = _make_item(
        result,
        guid=str(entry.get("id") or entry.get("guid") or url),
        title=title,
        blurb=blurb,
        url=url,
        published_at=_entry_datetime(entry),
        service=service,
        raw_category=raw_category,
        status=status,
    )
    return [item] if item else []


def normalize(results: list[FetchResult]) -> tuple[list[Item], dict[str, int]]:
    """Parse every successful fetch into items. Returns (items, per-source counts)."""
    items: list[Item] = []
    counts: dict[str, int] = {}
    seen_ids: set[str] = set()

    for result in results:
        key = result.source.key
        if not result.ok:
            counts[key] = 0
            continue
        try:
            parsed = feedparser.parse(result.body)
        except Exception as exc:  # noqa: BLE001 - a malformed feed is not fatal
            log.error("source %s: parse failed: %s", key, exc)
            counts[key] = 0
            result.ok = False
            result.error = f"parse failed: {exc}"[:200]
            continue

        if parsed.get("bozo") and not parsed.get("entries"):
            log.warning(
                "source %s: unparseable feed (%s)", key, parsed.get("bozo_exception")
            )
            counts[key] = 0
            result.ok = False
            result.error = "unparseable feed"
            continue

        count = 0
        for entry in parsed.get("entries", []):
            try:
                entry_items = _normalize_entry(entry, result)
            except Exception as exc:  # noqa: BLE001 - skip the entry, keep the feed
                log.error("source %s: entry normalize failed: %s", key, exc)
                continue
            for item in entry_items:
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)
                items.append(item)
                count += 1
        counts[key] = count
        log.info("source %s: %d items", key, count)

    items.sort(key=lambda i: i.published_at, reverse=True)
    return items, counts
