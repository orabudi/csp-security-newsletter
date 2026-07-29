"""Phase 1 tool: prove every configured feed actually returns items.

    python -m src.verify_feeds

Two Tier-1 blog feeds ship unverified (§14). Exits non-zero if any Tier-1 feed
returns nothing, so this doubles as a periodic feed-rot check.
"""

from __future__ import annotations

import sys
from collections import Counter

from .fetch import fetch_all, load_sources
from .normalize import normalize
from .util import log, now_utc, setup_logging


def main() -> int:
    setup_logging()
    sources = load_sources()
    if not sources:
        print("no sources configured")
        return 1

    results = fetch_all(sources)
    items, counts = normalize(results)
    by_source: dict[str, list] = {}
    for item in items:
        by_source.setdefault(item.source, []).append(item)

    now = now_utc()
    failures: list[str] = []
    print()
    print(f"{'source':<34} {'tier':<5} {'items':<6} {'newest':<10} status")
    print("-" * 92)

    for result in results:
        source = result.source
        source_items = by_source.get(source.key, [])
        count = counts.get(source.key, 0)
        if source_items:
            newest = max(i.published_at for i in source_items)
            age = f"{(now - newest).days}d ago"
        else:
            age = "—"
        status = "OK" if result.ok and count else (result.error or "0 items")
        print(f"{source.key:<34} {source.tier:<5} {count:<6} {age:<10} {status}")
        if source_items:
            print(f"{'':<34} └─ {source_items[0].title[:70]}")
        if not count:
            failures.append(f"{source.key} (tier {source.tier}): {status}")

    print()
    print(f"total items: {len(items)}")
    print("by provider:", dict(Counter(i.provider for i in items)))

    tier1_failures = [f for f in failures if "tier 1" in f]
    if failures:
        print()
        print("PROBLEM FEEDS:")
        for failure in failures:
            print(f"  - {failure}")
    if tier1_failures:
        log.error("%d tier-1 feed(s) returned nothing — fix before building on them",
                  len(tier1_failures))
        return 1
    if failures:
        log.warning("%d tier-2 feed(s) returned nothing", len(failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
