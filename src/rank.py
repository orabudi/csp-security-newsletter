"""§6.3/§6.4 Ranking: promote rules override the classifier, estate boosts impact.

The service allowlist is used to *rank*, never to filter (§6.4): a security
change on a service you run is high impact; the same change on one you don't is
still worth a line.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import Item
from .util import compile_patterns, contains_any, env_int, first_match, load_yaml, log

# §7 taxonomy. Order here is the order in the digest.
TAXONOMY: list[tuple[str, str, str]] = [
    ("action", "⚠️", "Action Needed"),
    ("identity", "🔑", "Identity & Access"),
    ("data", "🔒", "Data Protection"),
    ("network", "🌐", "Network Security"),
    ("detection", "🛡️", "Detection & Response"),
    ("governance", "📋", "Governance & Compliance"),
    ("workload", "📦", "Workload & Supply Chain"),
]

PROVIDER_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "GCP", "oci": "OCI"}

IMPACT_SCORE = {"high": 100.0, "medium": 50.0, "low": 20.0}

PROMOTE_BONUS = 200.0
TIER1_BONUS = 15.0
SECURITY_SERVICE_BONUS = 20.0
ESTATE_BONUS = 30.0


@dataclass
class Section:
    key: str
    emoji: str
    title: str
    items: list[Item] = field(default_factory=list)

    @property
    def is_action(self) -> bool:
        return self.key == "action"


@dataclass
class Digest:
    lede: str
    sections: list[Section]
    overflow: list[Item]
    dropped: list[Item]
    kept_count: int = 0

    @property
    def total_shown(self) -> int:
        return sum(len(section.items) for section in self.sections)

    @property
    def is_empty(self) -> bool:
        return self.total_shown == 0

    @property
    def is_thin(self) -> bool:
        return 0 < self.total_shown <= env_int("THIN_DAY_THRESHOLD", 3)


class Ranker:
    def __init__(self) -> None:
        rules = load_yaml("rules.yaml") or {}
        services = load_yaml("services.yaml") or {}
        self.promote = compile_patterns(rules.get("promote_patterns"))
        self.security_services: dict[str, list[str]] = services.get(
            "security_services"
        ) or {}
        self.estate: dict[str, list[str]] = services.get("estate") or {}

    def annotate(self, item: Item) -> None:
        text = item.text

        # §6.3 — always-promote overrides whatever the classifier decided.
        match = first_match(self.promote, text)
        if match:
            item.promote_reason = match
            item.action_required = True
            item.is_security = True
            if item.impact != "high":
                item.impact = "high"

        item.security_service_hit = contains_any(
            self.security_services.get(item.provider, []), text
        )
        item.estate_hit = contains_any(self.estate.get(item.provider, []), text)

        score = IMPACT_SCORE.get(item.impact, 50.0)
        if item.action_required:
            score += PROMOTE_BONUS
        if item.source_tier == 1:
            score += TIER1_BONUS
        if item.security_service_hit:
            score += SECURITY_SERVICE_BONUS
        if item.estate_hit:
            score += ESTATE_BONUS
            # A security change on something you actually run is high impact,
            # whatever the model said.
            if item.impact == "low":
                item.impact = "medium"
        item.score = score


def rank(items: list[Item], lede: str) -> Digest:
    ranker = Ranker()
    kept: list[Item] = []
    dropped: list[Item] = []

    for item in items:
        ranker.annotate(item)
        if item.is_security:
            kept.append(item)
        else:
            item.drop_reason = "classified not security-relevant"
            dropped.append(item)

    kept.sort(key=lambda i: (-i.score, -i.published_at.timestamp()))

    cap = env_int("CATEGORY_CAP", 4)
    buckets: dict[str, list[Item]] = {key: [] for key, _, _ in TAXONOMY}
    for item in kept:
        key = "action" if item.action_required else item.category
        buckets.setdefault(key, []).append(item)

    sections: list[Section] = []
    overflow: list[Item] = []
    for key, emoji, title in TAXONOMY:
        bucket = buckets.get(key, [])
        if not bucket:
            continue
        # Action Needed is never truncated — deadlines are the whole point (§6.3).
        limit = len(bucket) if key == "action" else cap
        sections.append(Section(key=key, emoji=emoji, title=title,
                                items=bucket[:limit]))
        overflow.extend(bucket[limit:])

    log.info(
        "rank: %d security items across %d sections, %d overflow, %d not security",
        len(kept), len(sections), len(overflow), len(dropped),
    )
    for section in sections:
        log.info("  %s %s: %d", section.emoji, section.title, len(section.items))

    return Digest(
        lede=lede,
        sections=sections,
        overflow=overflow,
        dropped=dropped,
        kept_count=len(kept),
    )
