# Cloud Security TLDR

A daily digest of **security-relevant product news** across AWS, Azure, Google
Cloud and OCI — new security services, new security features, security-relevant
changes to non-security services, and security deprecations.

Runs on GitHub Actions, costs $0/month, and degrades instead of dying when a
feed moves or an LLM quota runs out.

**Out of scope:** CVE advisories and vulnerability bulletins. They are a
different kind of content with a different cadence and urgency. The pipeline
supports them behind a one-line toggle — see
`prefilter.include_vulnerability_bulletins` in [config/rules.yaml](config/rules.yaml).

---

## How it works

```
FETCH → NORMALIZE → DEDUPE → PRE-FILTER → CLASSIFY → RANK → RENDER → DELIVER → PERSIST
```

Two rules shape the whole design:

**One LLM call per run.** Every surviving item goes to the classifier in a
single batched request, so rate limits and retry/backoff complexity stop
mattering. At ~50 items/day this is roughly 10k input tokens.

**The model never sees a URL.** It receives `{id, provider, service, title,
blurb}` and returns `{id, is_security, category, tldr, impact}`. URLs are
rejoined at render time from the fetched item, which structurally eliminates
hallucinated links.

Recall is prioritised over precision: a missed security announcement costs far
more than one extra line, so pre-filtering is deliberately minimal and the
classifier is told to answer "yes" when unsure.

---

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1. Confirm every feed still returns items (do this first, and after any
#    "feed rot" alert)
.venv/bin/python -m src.verify_feeds

# 2. See what the pipeline produces without sending anything
.venv/bin/python -m src.main --dry-run --no-state

# 3. Real run
LLM_API_KEY=... TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... .venv/bin/python -m src.main
```

### CLI flags

| Flag | Effect |
|---|---|
| `--dump-raw` | Fetch + normalize only, dump JSON to stdout |
| `--dry-run` | Print the digest, deliver nothing |
| `--no-deliver` | Run everything, skip delivery |
| `--no-state` | Do not write `state.json`, `docs/` or the audit log |
| `--stdout` | Also print the markdown digest |

---

## Configuration

### Secrets (GitHub → Settings → Secrets and variables → Actions)

| Name | Required | Notes |
|---|---|---|
| `LLM_API_KEY` | for the LLM path | Without it the run silently degrades to keyword classification |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | for Telegram | Primary channel; 10 minutes to set up via [@BotFather](https://t.me/botfather) |
| `SLACK_WEBHOOK_URL` | optional | Incoming webhook |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `EMAIL_FROM` / `EMAIL_TO` | optional | Works with Gmail app passwords, Brevo, Resend SMTP |

Every configured channel is used; unconfigured ones are skipped. A delivery
failure never kills the run.

### Variables and env knobs

| Name | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini`, `groq`, or `openai-compat` |
| `LLM_MODEL` | `gemini-2.5-flash` | Provider-specific model id |
| `LLM_BASE_URL` | — | For `openai-compat` (GitHub Models, Cloudflare Workers AI, …) |
| `SITE_BASE_URL` | derived from `GITHUB_REPOSITORY` | Base URL for archive links |
| `CATEGORY_CAP` | `4` | Max items per category; the rest overflow to the archive |
| `MAX_ITEM_AGE_HOURS` | `24` | Freshness window (`FIRST_RUN_LOOKBACK_HOURS`, default `48`, on the first run) |
| `THIN_DAY_THRESHOLD` | `3` | At or below this, the digest is labelled a quiet day |
| `SEND_ON_EMPTY` | `0` | Send a digest even with zero items |
| `RESURFACE_UPDATED` | `0` | Re-surface items a feed republished with edited content |
| `DRY_RUN` | `0` | Skip delivery |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every pre-filter drop |

**⚠️ Action Needed is never truncated** by `CATEGORY_CAP` — deprecations carry
deadlines and are the reason this digest exists.

### Config files

| File | Purpose |
|---|---|
| [config/sources.yaml](config/sources.yaml) | Feed URLs, tier, per-source service pinning |
| [config/services.yaml](config/services.yaml) | **Edit `estate`** — services you run get an impact boost |
| [config/regions.yaml](config/regions.yaml) | **Edit `mine`** — your regions survive the region-expansion filter |
| [config/rules.yaml](config/rules.yaml) | Pre-filter drops, always-promote patterns, keyword lexicon |

---

## Sources

**Tier 1 — dedicated security blogs.** Security-relevant by definition; they
bypass the `is_security` decision and are only summarised and categorised.

**Tier 2 — general product feeds.** These are what the classifier is for.

All feeds were verified on 2026-07-28 (`python -m src.verify_feeds`). Two notes:

- **Oracle's cloud security blog is disabled.** `blogs.oracle.com` sits behind
  bot protection and returns HTTP 403 to every non-browser client. The entry is
  left in `sources.yaml`, disabled, so it is not silently forgotten. OCI
  security coverage comes from the per-service release-note feeds instead.
- **GCP release notes publish one entry per day** containing every product's
  notes. `normalize.py` splits each entry into one item per product section —
  without that, ~40 daily GCP changes collapse into a single line.

---

## Failure handling

| Failure | Behaviour |
|---|---|
| One feed unreachable | Retried once, then skipped; listed in the digest footer |
| Feed reachable but empty 3 runs running | Feed-rot alert in the digest and a failed workflow run |
| LLM fails or quota exhausted | Falls back to keyword classification (Appendix C lexicon). Lower precision, still shipped |
| LLM returns invalid JSON | One retry with a stricter instruction, then keyword fallback |
| Delivery fails | State and archive still committed; next run retries |
| Zero security items | Delivery suppressed (the run is still logged and archived) |

A bad day degrades the digest; it never kills it and never loses state.

Exit codes: `0` fine · `1` hard failure · `3` shipped but needs attention
(feed rot, delivery failure, degraded classifier). The workflow surfaces `3` as
a failed run so it reaches you via GitHub's notifications.

---

## Tuning (the part that actually determines quality)

Every run writes `docs/audit/<date>.json` containing **every item with its
verdict** — shown, overflow, classified-not-security, and pre-filtered, each
with its reason.

For the first two weeks, spend ~15 minutes a day:

1. Read `dropped_not_security` and look for anything genuinely security-relevant.
   Every false negative becomes a rubric fix in `RUBRIC` in
   [src/classify.py](src/classify.py).
2. Spot-check TLDRs against their source links.
3. Adjust `estate` in `services.yaml` as your ranking intuition sharpens.

Do **not** fix thin days by loosening the classifier — ship a three-item digest
instead. Aggressive filtering is the product.

---

## Layout

```
.github/workflows/daily.yml   cron + workflow_dispatch, commits state back
config/                       sources, services, regions, rules
src/fetch.py                  parallel HTTP, per-source timeout and isolation
src/normalize.py              → common item schema; all per-provider quirks
src/dedupe.py                 state.json, content hashing, feed-rot canary
src/prefilter.py              minimal rules
src/classify.py               batched LLM call, validation, keyword fallback
src/rank.py                   promote rules, impact boosting, ordering
src/render.py                 jinja2 → markdown / html / telegram
src/deliver.py                telegram, slack, email
src/main.py                   orchestration
src/verify_feeds.py           feed verification tool
templates/                    digest.md.j2, digest.html.j2, digest.telegram.j2
docs/                         GitHub Pages archive + audit logs
state.json                    seen ids, feed health, run history
```

To publish the archive: **Settings → Pages → Source: Deploy from a branch →
`main` / `/docs`**.
