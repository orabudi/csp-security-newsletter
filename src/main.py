"""Orchestration: fetch → normalize → dedupe → prefilter → classify → rank →
render → deliver → persist (§3).

Exit codes:
  0  digest produced (or suppressed on a genuinely empty day)
  1  hard failure — nothing fetched, or an unhandled error
  3  digest shipped but something needs attention (feed rot, delivery failure,
     degraded classifier). The workflow surfaces this as a failed run.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from typing import Any

from .classify import classify
from .dedupe import dedupe, load_state, record_run, save_state, update_feed_health
from .deliver import deliver
from .fetch import fetch_all, load_sources
from .normalize import Item, normalize
from .prefilter import prefilter
from .rank import rank
from .render import build_context, render
from .util import DOCS_DIR, env_bool, iso, log, now_utc, setup_logging

EXIT_OK = 0
EXIT_HARD_FAIL = 1
EXIT_ATTENTION = 3


def archive_base_url() -> str | None:
    base = (os.getenv("SITE_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    repo = (os.getenv("GITHUB_REPOSITORY") or "").strip()
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    return None


def write_archive(date_iso: str, html_body: str, base_url: str | None) -> str | None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    page = DOCS_DIR / f"{date_iso}.html"
    page.write_text(html_body, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").touch()
    log.info("archive page written: %s", page.relative_to(DOCS_DIR.parent))
    write_archive_index(base_url)
    return f"{base_url}/{date_iso}.html" if base_url else None


def write_archive_index(base_url: str | None) -> None:
    pages = sorted(
        (p.stem for p in DOCS_DIR.glob("*.html") if p.name != "index.html"),
        reverse=True,
    )
    rows = "\n".join(
        f'    <li><a href="{name}.html">{html.escape(name)}</a></li>' for name in pages
    )
    (DOCS_DIR / "index.html").write_text(
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Cloud Security TLDR — archive</title>\n"
        "<style>:root{color-scheme:light dark}"
        "body{font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif;max-width:640px;margin:40px auto;padding:0 16px}"
        "h1{font-size:20px}ul{list-style:none;padding:0}li{margin:6px 0}"
        "a{color:#2a49c4;text-decoration:none}a:hover{text-decoration:underline}"
        "@media(prefers-color-scheme:dark){body{background:#0e1013;color:#e6e8ec}"
        "a{color:#93a5ff}}</style>\n"
        "</head><body>\n"
        "  <h1>🔐 Cloud Security TLDR — archive</h1>\n"
        f"  <ul>\n{rows}\n  </ul>\n"
        "</body></html>\n",
        encoding="utf-8",
    )


def write_audit(date_iso: str, payload: dict[str, Any]) -> None:
    """Every item with its verdict, kept and dropped (Phase 2/5 tuning loop)."""
    audit_dir = DOCS_DIR / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / f"{date_iso}.json"
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    log.info("audit log written: %s", path.relative_to(DOCS_DIR.parent))


def step_summary(lines: list[str]) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.warning("could not write step summary: %s", exc)


def dump_raw(items: list[Item]) -> None:
    json.dump([item.to_dict() for item in items], sys.stdout, indent=2,
              ensure_ascii=False)
    sys.stdout.write("\n")


def run(args: argparse.Namespace) -> int:
    started = now_utc()
    date_iso = started.strftime("%Y-%m-%d")

    # 1. FETCH
    sources = load_sources()
    if not sources:
        log.error("no sources configured — check config/sources.yaml")
        return EXIT_HARD_FAIL
    results = fetch_all(sources)

    # 2. NORMALIZE
    items, counts = normalize(results)
    failures = {r.source.key: (r.error or "unknown") for r in results if not r.ok}
    unavailable = sorted(failures)
    if not items and failures:
        log.error("every source failed — aborting before touching state")
        step_summary([f"## ❌ Cloud Security TLDR {date_iso}",
                      "", "Every source failed:", ""] +
                     [f"- `{k}` — {v}" for k, v in failures.items()])
        return EXIT_HARD_FAIL
    log.info("normalized %d raw items from %d sources", len(items), len(sources))

    if args.dump_raw:
        dump_raw(items)
        return EXIT_OK

    # 3. DEDUPE
    state = load_state()
    fresh, seen_before = dedupe(items, state)
    alerts = update_feed_health(state, counts, failures)

    # 4. PRE-FILTER
    kept, prefiltered = prefilter(fresh)

    # 5. CLASSIFY (one batched call)
    lede, diagnostics = classify(kept)

    # 6. RANK
    digest = rank(kept, lede)

    # 7. RENDER
    base_url = archive_base_url()
    archive_url = f"{base_url}/{date_iso}.html" if base_url else None
    context = build_context(
        digest,
        unavailable=unavailable,
        alerts=alerts,
        archive_url=archive_url,
        classifier_path=diagnostics["path"],
        run_date=started,
    )
    rendered = render(context)

    if args.stdout or args.dry_run:
        print(rendered.markdown)

    # 8. DELIVER — §9: ship short, but suppress an empty digest rather than
    # sending a hollow one. The run is still logged and archived either way.
    deliveries = []
    suppressed = digest.is_empty and not env_bool("SEND_ON_EMPTY", False)
    if args.no_deliver or args.dry_run:
        log.info("delivery skipped (--no-deliver/--dry-run)")
    elif suppressed:
        log.info("empty digest — suppressing delivery (set SEND_ON_EMPTY=1 to override)")
    else:
        deliveries = deliver(rendered)

    # 9. PERSIST
    if not args.no_state:
        write_archive(date_iso, rendered.html, base_url)
        write_audit(
            date_iso,
            {
                "run_at": iso(started),
                "classifier": diagnostics,
                "counts": {
                    "raw": len(items),
                    "fresh": len(fresh),
                    "already_seen": len(seen_before),
                    "prefiltered": len(prefiltered),
                    "classified": len(kept),
                    "security": digest.kept_count,
                    "shown": digest.total_shown,
                    "overflow": len(digest.overflow),
                },
                "per_source": counts,
                "unavailable": failures,
                "alerts": alerts,
                "shown": [
                    item.to_dict()
                    for section in digest.sections
                    for item in section.items
                ],
                "overflow": [item.to_dict() for item in digest.overflow],
                "dropped_not_security": [item.to_dict() for item in digest.dropped],
                "dropped_prefilter": [item.to_dict() for item in prefiltered],
            },
        )
        record_run(
            state,
            {
                "raw": len(items),
                "fresh": len(fresh),
                "shown": digest.total_shown,
                "overflow": len(digest.overflow),
                "classifier": diagnostics["path"],
                "delivered": [d.channel for d in deliveries if d.ok],
                "failed_delivery": [d.channel for d in deliveries if not d.ok],
                "unavailable": unavailable,
            },
        )
        save_state(state)
    else:
        log.info("state/archive writes skipped (--no-state)")

    # Summary
    failed_delivery = [d for d in deliveries if not d.ok]
    log.info(
        "done: %d raw → %d fresh → %d classified → %d security → %d shown",
        len(items), len(fresh), len(kept), digest.kept_count, digest.total_shown,
    )
    step_summary(
        [
            f"## 🔐 Cloud Security TLDR — {date_iso}",
            "",
            f"- **{digest.total_shown}** items shown, {len(digest.overflow)} overflow",
            f"- {len(items)} raw → {len(fresh)} fresh → {len(kept)} classified "
            f"→ {digest.kept_count} security-relevant",
            f"- classifier: `{diagnostics['path']}` ({diagnostics['calls']} call(s))",
            f"- delivered: {', '.join(d.channel for d in deliveries if d.ok) or 'none'}",
            f"- sources unavailable: {', '.join(unavailable) or 'none'}",
        ]
        + ([""] + [f"- 🚨 {a}" for a in alerts] if alerts else [])
        + ["", "```", rendered.markdown[:4000], "```"]
    )

    if alerts or failed_delivery or diagnostics["path"] not in ("llm", "none"):
        return EXIT_ATTENTION
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(prog="csp-sec-tldr",
                                     description="Daily cloud security TLDR")
    parser.add_argument("--dump-raw", action="store_true",
                        help="fetch + normalize only, dump JSON to stdout (Phase 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the digest, deliver nothing")
    parser.add_argument("--no-deliver", action="store_true",
                        help="run everything but skip delivery")
    parser.add_argument("--no-state", action="store_true",
                        help="do not write state.json, docs/ or the audit log")
    parser.add_argument("--stdout", action="store_true",
                        help="also print the markdown digest")
    args = parser.parse_args()

    setup_logging()
    try:
        return run(args)
    except KeyboardInterrupt:
        return EXIT_HARD_FAIL
    except Exception:  # noqa: BLE001 - a crash must be loud, not silent
        log.exception("run failed")
        return EXIT_HARD_FAIL


if __name__ == "__main__":
    sys.exit(main())
