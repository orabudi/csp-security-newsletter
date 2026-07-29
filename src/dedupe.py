"""state.json: dedupe by id + content hash, freshness window, feed-rot canary.

The window is a fixed UTC lookback (now - MAX_ITEM_AGE_HOURS), never a calendar
day (§14). ID dedupe is what actually prevents repeats; the window only stops a
first run — or a feed that suddenly republishes its whole archive — from
flooding the digest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .normalize import Item
from .util import STATE_PATH, env_bool, env_int, iso, log, now_utc

STATE_VERSION = 2
SEEN_RETENTION_DAYS = 90
RUN_HISTORY = 60
ZERO_ITEM_ALERT_RUNS = 3

# `seen` holds thousands of ids and is committed on every run, so each entry is
# a single "<content-hash>|<first-seen date>" string: one line per id, and an
# unchanged item produces no diff at all. Retention is measured from first_seen
# so records never need rewriting.


def _pack(content_hash: str, first_seen: str) -> str:
    return f"{content_hash}|{first_seen}"


def _unpack(record: Any) -> tuple[str, str]:
    """(content_hash, first_seen_date). Tolerates the older dict form."""
    if isinstance(record, dict):  # state written by version 1
        return record.get("hash", ""), (record.get("first_seen", "") or "")[:10]
    content_hash, _, first_seen = str(record).partition("|")
    return content_hash, first_seen


@dataclass
class State:
    version: int = STATE_VERSION
    last_run: str | None = None
    seen: dict[str, Any] = field(default_factory=dict)
    feeds: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "last_run": self.last_run,
            "feeds": self.feeds,
            "runs": self.runs,
            "seen": self.seen,
        }


def load_state() -> State:
    if not STATE_PATH.exists():
        log.info("no state.json — first run, bootstrapping")
        return State()
    try:
        with STATE_PATH.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.error("state.json unreadable (%s) — starting fresh, expect duplicates", exc)
        return State()
    return State(
        version=raw.get("version", STATE_VERSION),
        last_run=raw.get("last_run"),
        seen=raw.get("seen") or {},
        feeds=raw.get("feeds") or {},
        runs=raw.get("runs") or [],
    )


def save_state(state: State) -> None:
    _prune_seen(state)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state.to_dict(), fh, indent=1, sort_keys=False)
        fh.write("\n")
    tmp.replace(STATE_PATH)
    log.info("state saved: %d seen ids, %d feeds", len(state.seen), len(state.feeds))


def _prune_seen(state: State) -> None:
    cutoff = (now_utc() - timedelta(days=SEEN_RETENTION_DAYS)).strftime("%Y-%m-%d")
    before = len(state.seen)
    state.seen = {
        key: value
        for key, value in state.seen.items()
        if _unpack(value)[1] >= cutoff
    }
    if before != len(state.seen):
        log.info("pruned %d seen ids older than %dd", before - len(state.seen),
                 SEEN_RETENTION_DAYS)


def dedupe(items: list[Item], state: State) -> tuple[list[Item], list[Item]]:
    """Split items into (new, skipped). Marks every item as seen either way."""
    first_run = not state.seen
    max_age_hours = env_int(
        "FIRST_RUN_LOOKBACK_HOURS" if first_run else "MAX_ITEM_AGE_HOURS",
        48 if first_run else 24,
    )
    window_start = now_utc() - timedelta(hours=max_age_hours)
    resurface_updated = env_bool("RESURFACE_UPDATED", False)
    today = now_utc().strftime("%Y-%m-%d")

    fresh: list[Item] = []
    skipped: list[Item] = []

    for item in items:
        record = state.seen.get(item.id)
        first_seen = today
        if record is None:
            if item.published_at < window_start:
                item.drop_reason = f"older than {max_age_hours}h window"
                skipped.append(item)
            else:
                fresh.append(item)
        else:
            known_hash, first_seen = _unpack(record)
            first_seen = first_seen or today
            if known_hash != item.content_hash:
                # Feeds republish edited items; content hashing tells an edit
                # apart from a genuinely new item (§14).
                if resurface_updated:
                    item.extra["updated"] = True
                    fresh.append(item)
                else:
                    item.drop_reason = "already seen (content edited upstream)"
                    skipped.append(item)
                    log.debug("item %s republished with new content: %s", item.id,
                              item.title[:80])
            else:
                item.drop_reason = "already seen"
                skipped.append(item)

        state.seen[item.id] = _pack(item.content_hash, first_seen)

    log.info(
        "dedupe: %d new, %d skipped (window %dh%s)",
        len(fresh),
        len(skipped),
        max_age_hours,
        ", first run" if first_run else "",
    )
    return fresh, skipped


def update_feed_health(state: State, counts: dict[str, int],
                       failures: dict[str, str]) -> list[str]:
    """Track per-feed item counts. Returns feed-rot alerts (§8)."""
    stamp = iso(now_utc())
    alerts: list[str] = []

    for key, count in counts.items():
        health = state.feeds.setdefault(
            key, {"consecutive_zero": 0, "consecutive_fail": 0}
        )
        health["last_count"] = count
        health["last_run"] = stamp
        if key in failures:
            health["consecutive_fail"] = health.get("consecutive_fail", 0) + 1
            health["last_error"] = failures[key]
        else:
            health["consecutive_fail"] = 0
            health["last_ok"] = stamp
            health.pop("last_error", None)

        # "Reachable but empty" is the feed-rot signal; "unreachable" is tracked
        # separately above so a broken feed raises one alert, not two.
        if count == 0 and key not in failures:
            health["consecutive_zero"] = health.get("consecutive_zero", 0) + 1
        elif count:
            health["consecutive_zero"] = 0

        if health["consecutive_zero"] >= ZERO_ITEM_ALERT_RUNS:
            alerts.append(
                f"{key}: 0 items for {health['consecutive_zero']} runs — feed may have moved"
            )
        if health["consecutive_fail"] >= ZERO_ITEM_ALERT_RUNS:
            alerts.append(
                f"{key}: unreachable for {health['consecutive_fail']} runs "
                f"({health.get('last_error', 'unknown error')})"
            )

    for alert in alerts:
        log.error("FEED ROT: %s", alert)
    return alerts


def record_run(state: State, summary: dict[str, Any]) -> None:
    state.last_run = iso(now_utc())
    state.runs.append({"at": state.last_run, **summary})
    del state.runs[:-RUN_HISTORY]
