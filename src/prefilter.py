"""§6.1 Pre-filter — rules that drop only unambiguous noise.

Everything that survives goes to the classifier. Volume is low enough that
aggressive rule-based filtering buys nothing and costs recall, which is the
expensive direction to be wrong in (§2).

Tier-1 (dedicated security blog) items are never pre-filtered.
"""

from __future__ import annotations

from .normalize import Item
from .util import any_match, compile_patterns, contains_any, load_yaml, log


class Prefilter:
    def __init__(self) -> None:
        rules = load_yaml("rules.yaml") or {}
        regions = load_yaml("regions.yaml") or {}
        cfg = rules.get("prefilter") or {}

        self.region_phrases = compile_patterns(cfg.get("region_expansion_phrases"))
        self.drop_titles = compile_patterns(cfg.get("drop_title_patterns"))
        self.drop_gcp_categories = {
            c.strip().lower() for c in (cfg.get("drop_gcp_categories") or [])
        }
        self.drop_gcp_category_patterns = compile_patterns(
            cfg.get("drop_gcp_category_patterns")
        )
        self.drop_azure_in_dev = bool(cfg.get("drop_azure_in_development", True))

        # Appendix D — out of scope unless explicitly toggled on.
        self.include_vuln = bool(cfg.get("include_vulnerability_bulletins", False))
        vuln = cfg.get("vulnerability_bulletin") or {}
        self.vuln_services = {s.strip().lower() for s in (vuln.get("services") or [])}
        self.vuln_patterns = compile_patterns(vuln.get("text_patterns"))
        self.security_terms = compile_patterns(
            rules.get("security_terms_for_prefilter")
        )
        self.region_tokens = regions.get("region_tokens") or []
        self.my_regions = regions.get("mine") or []

    def _is_region_expansion(self, item: Item) -> bool:
        text = item.text
        if not any_match(self.region_phrases, text):
            return False
        return contains_any(self.region_tokens, text) is not None

    def _is_vulnerability_bulletin(self, item: Item) -> bool:
        if (item.service or "").strip().lower() in self.vuln_services:
            return True
        # Checked separately so that ^-anchored patterns can match the start of
        # the note body, which is where these bulletins announce themselves.
        return any_match(self.vuln_patterns, item.title) or any_match(
            self.vuln_patterns, item.blurb
        )

    def check(self, item: Item) -> str | None:
        """Return a drop reason, or None to keep."""
        if item.source_tier == 1:
            return None

        if any_match(self.drop_titles, item.title):
            return "empty or malformed title"

        if not self.include_vuln and self._is_vulnerability_bulletin(item):
            return "CVE/patch bulletin (out of scope — Appendix D)"

        if item.provider == "gcp" and item.raw_category:
            category = item.raw_category.strip().lower()
            if category in self.drop_gcp_categories:
                return f"gcp category: {item.raw_category}"
            if any_match(self.drop_gcp_category_patterns, item.raw_category):
                return f"gcp version/channel note: {item.raw_category}"

        if (
            item.provider == "azure"
            and self.drop_azure_in_dev
            and (item.status or "").strip().lower() == "in development"
        ):
            return "azure status: In development"

        if self._is_region_expansion(item):
            # A security service landing anywhere, or anything landing in one of
            # your regions, is news. Everything else is boilerplate.
            if any_match(self.security_terms, item.text):
                return None
            if contains_any(self.my_regions, item.text):
                return None
            return "region expansion, no security signal"

        return None


def prefilter(items: list[Item]) -> tuple[list[Item], list[Item]]:
    """Split into (kept, dropped). Dropped items carry `drop_reason`."""
    rules = Prefilter()
    kept: list[Item] = []
    dropped: list[Item] = []

    for item in items:
        reason = rules.check(item)
        if reason:
            item.drop_reason = reason
            dropped.append(item)
        else:
            kept.append(item)

    log.info("prefilter: %d kept, %d dropped", len(kept), len(dropped))
    for item in dropped:
        log.debug("prefilter drop [%s] %s — %s", item.provider, item.title[:80],
                  item.drop_reason)
    return kept, dropped
