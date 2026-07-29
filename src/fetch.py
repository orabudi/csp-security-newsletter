"""Parallel feed fetch with per-source timeout and per-source isolation.

One bad source must never fail the run (§8). Every source produces a
FetchResult; failures are carried through to the digest footer.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import httpx

from .util import env_int, load_yaml, log

USER_AGENT = (
    "csp-sec-tldr/1.0 (+https://github.com/; daily cloud security digest; "
    "contact: repo owner)"
)


@dataclass
class Source:
    provider: str
    name: str
    url: str
    tier: int = 2
    enabled: bool = True
    service: str | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.name}"


@dataclass
class FetchResult:
    source: Source
    ok: bool
    status: int | None = None
    body: bytes = b""
    error: str | None = None
    elapsed_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def load_sources() -> list[Source]:
    raw = load_yaml("sources.yaml") or []
    sources: list[Source] = []
    seen_keys: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            log.error("sources.yaml: skipping non-mapping entry %r", entry)
            continue
        missing = [k for k in ("provider", "name", "url") if not entry.get(k)]
        if missing:
            log.error("sources.yaml: entry missing %s: %r", missing, entry)
            continue
        source = Source(
            provider=str(entry["provider"]).lower().strip(),
            name=str(entry["name"]).strip(),
            url=str(entry["url"]).strip(),
            tier=int(entry.get("tier", 2)),
            enabled=bool(entry.get("enabled", True)),
            service=entry.get("service"),
        )
        if source.key in seen_keys:
            log.error("sources.yaml: duplicate source key %s, skipping", source.key)
            continue
        seen_keys.add(source.key)
        if not source.enabled:
            log.info("source %s disabled, skipping", source.key)
            continue
        sources.append(source)
    return sources


def _fetch_one(client: httpx.Client, source: Source, timeout: float) -> FetchResult:
    """Fetch one source, retrying transient errors.

    Some feeds are large (GCP release notes is ~550 KB) and occasionally time
    out. Without a retry, a blip looks identical to feed rot and raises a false
    alarm, which is the fastest way to make the canary worth ignoring.
    """
    attempts = max(1, env_int("FETCH_ATTEMPTS", 2))
    last_error = "unknown error"

    for attempt in range(1, attempts + 1):
        try:
            response = client.get(source.url, timeout=timeout)
            elapsed = int(response.elapsed.total_seconds() * 1000)
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}"
                log.warning("source %s HTTP %s (%d ms)", source.key,
                            response.status_code, elapsed)
                # 4xx is a config problem, not a blip — do not retry.
                if response.status_code < 500:
                    return FetchResult(source=source, ok=False,
                                       status=response.status_code,
                                       error=last_error, elapsed_ms=elapsed)
                continue
            log.info("source %s ok: %d bytes (%d ms)", source.key,
                     len(response.content), elapsed)
            return FetchResult(source=source, ok=True, status=response.status_code,
                               body=response.content, elapsed_ms=elapsed)
        except Exception as exc:  # noqa: BLE001 - per-source isolation is the point
            last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("source %s attempt %d/%d failed: %s", source.key, attempt,
                        attempts, last_error)

    return FetchResult(source=source, ok=False, error=last_error)


def fetch_all(sources: list[Source]) -> list[FetchResult]:
    """Fetch every source in parallel. Never raises."""
    if not sources:
        return []

    timeout = float(env_int("FETCH_TIMEOUT_SECONDS", 30))
    workers = min(env_int("FETCH_CONCURRENCY", 8), max(1, len(sources)))
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, "
        "text/xml;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }

    results: list[FetchResult] = []
    with httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=timeout,
        limits=httpx.Limits(max_connections=workers),
    ) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_fetch_one, client, source, timeout) for source in sources
            ]
            for future, source in zip(futures, sources):
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - belt and braces
                    log.error("source %s worker crashed: %s", source.key, exc)
                    results.append(
                        FetchResult(source=source, ok=False, error=str(exc)[:200])
                    )

    failed = [r.source.key for r in results if not r.ok]
    log.info("fetched %d/%d sources ok", len(results) - len(failed), len(results))
    if failed:
        log.warning("failed sources: %s", ", ".join(failed))
    return results
