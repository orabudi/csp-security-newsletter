# Automated CSP Security News TLDR — Implementation Plan

**Scope:** Daily digest of **security-related** product news across AWS, Azure, Google Cloud and OCI — new security services, new security features, security-relevant changes to non-security services, and security deprecations.
**Constraint:** Entire stack must run on free tiers.
**Explicitly out of scope:** CVE advisories and vulnerability bulletins. Sources are parked in Appendix D as an optional toggle — the pipeline supports them without rework, but they are a different kind of content with a different cadence and urgency.

---

## 1. Goals & Non-Goals

### Goals
- One digest per weekday, readable in under 90 seconds.
- Covers **security posture-changing news**: new controls, new capabilities, changed defaults, retirements.
- Every item links to the authoritative source page.
- Security deprecations and breaking changes are never missed.
- Runs unattended; failures are visible, not silent.
- $0/month.

### Non-Goals
- Vulnerability/CVE alerting (see Appendix D).
- Real-time alerting — that's Service Health / Defender / Security Hub territory.
- Per-tenant data requiring cloud credentials.
- Comprehensive coverage. This is a TLDR; aggressive filtering is the product.

### Success criteria
- **Recall over precision.** A missed security feature announcement is a real cost; one extra irrelevant item is not. Target ≥90% recall on genuinely security-relevant items, ≥60% precision.
- Zero broken or hallucinated links.
- Every security deprecation for a service you run appears in the digest within 24h.

---

## 2. What Changed vs. a General Product Digest

This scope inverts the usual design, and it's worth being explicit because it drives every decision below:

| | General product digest | **This project** |
|---|---|---|
| Raw volume/day | 60–120 | 60–120 (same inputs) |
| Surviving items/day | 15–30 | **5–15** |
| Hardest problem | Cutting volume | **Deciding what counts as "security"** |
| Filtering method | Rules do ~80% of the work | **LLM classification does the real work** |
| Cost of a false negative | Low | **High** |
| Cost of a false positive | Medium | **Low** |

Because volume is low, you can afford to send **every raw item** to the LLM classifier and skip most rules-based pre-filtering. That is both simpler and higher-recall than trying to write a keyword lexicon that catches everything.

---

## 3. Architecture

```
                    ┌──────────────────────────────┐
                    │  GitHub Actions (cron, daily) │
                    └──────────────┬───────────────┘
                                   │
        ┌──────────────────────────▼──────────────────────────┐
        │  1. FETCH       parallel HTTP, per-source timeout     │
        │  2. NORMALIZE   → common item schema                  │
        │  3. DEDUPE      state.json (id + content hash)        │
        │  4. PRE-FILTER  drop obvious junk only (region exp.)  │
        │  5. CLASSIFY    ONE batched LLM call:                 │
        │                   is_security? category? impact?      │
        │  6. RANK        always-promote rules override         │
        │  7. RENDER      Markdown + HTML from templates         │
        │  8. DELIVER     Telegram / Slack / email               │
        │  9. PERSIST     commit state.json + archive page       │
        └───────────────────────────────────────────────────────┘
```

### Two structural rules

**One LLM call per run.** ~120 items × ~150 tokens ≈ 18k input tokens — one request. Rate limits become irrelevant on any free tier, and all retry/backoff complexity disappears. Per-item calls would mean ~120 requests/day and permanent quota friction.

**The model never emits URLs.** It receives `{id, provider, title, blurb}` and returns `{id, is_security, category, tldr, impact}`. URLs are joined back at render time from fetched data. This structurally eliminates hallucinated links.

---

## 4. Tech Stack (all free tier)

| Layer | Choice | Free allowance | Notes |
|---|---|---|---|
| Scheduler + compute | GitHub Actions cron | Unlimited (public repo); 2,000 min/mo (private) | Run is ~1 min/day |
| Language | Python 3.11 | — | `feedparser`, `httpx`, `PyYAML`, `jinja2` |
| State store | `state.json` committed by the workflow | — | Versioned, diffable; SQLite is overkill |
| Secrets | GitHub Actions secrets | — | |
| LLM | Gemini Flash / Flash-Lite (AI Studio key) | No credit card, no expiry; ~15 RPM, several hundred–1,500 RPD depending on model | Quotas were cut in Dec 2025 — check current limits. At 1–3 req/day it's irrelevant |
| LLM alternates | Groq, GitHub Models, Cloudflare Workers AI | — | Keep the provider behind an interface; swapping should be a one-file change |
| Delivery (primary) | Telegram Bot API | Unlimited, free | 10 minutes to set up, no domain/DNS |
| Delivery (optional) | Slack incoming webhook | Free | |
| Delivery (email) | Brevo / Resend free tier, or Gmail SMTP app password | ~100–300/day | Requires sender verification |
| Archive | GitHub Pages from `/docs` | Free | Powers "+N more" links, doubles as debug record |
| Monitoring | GitHub workflow-failure notifications | Free | |

### Free-tier caveats
- **Gemini free tier may use prompts for model training.** Fine for public release notes; becomes a blocker the moment tenant-specific data is added.
- **GitHub disables scheduled workflows after ~60 days of repo inactivity.** The daily state commit normally prevents this — verify after month two.
- **GitHub Actions cron is best-effort**, can be 5–30 min late. Irrelevant here.
- **Free LLM quotas change without warning.** The degraded fallback (§8) is what makes this safe to depend on.

---

## 5. Sources

Two tiers. Dedicated security blogs are **100% in by definition** and bypass classification. General product feeds are the ones that need classifying.

### Tier 1 — dedicated security sources (no classification needed)

| Provider | Feed | Status |
|---|---|---|
| AWS | `https://aws.amazon.com/blogs/security/feed/` | verified |
| Microsoft | `https://www.microsoft.com/en-us/security/blog/feed/` | verify in Phase 1 |
| Google Cloud | `https://cloudblog.withgoogle.com/products/identity-security/rss/` | **verify in Phase 1** — Google Cloud blog feed paths have moved before |
| Oracle | `https://blogs.oracle.com/cloudsecurity/rss` | **verify in Phase 1** |

These are narrative/announcement content — 1–3 items/day combined. High value, zero filtering cost.

### Tier 2 — general product feeds (require classification)

| Provider | Feed | Raw/day | Notes |
|---|---|---|---|
| AWS | `https://aws.amazon.com/about-aws/whats-new/recent/feed/` | 15–30 | No taxonomy; classification does everything |
| Azure | `https://www.microsoft.com/releasecommunications/api/v2/azure/rss` | 20–40 | Current URL; older `azurecomcdn` / TechCommunity URLs are dead. Feed supports filtering by product ID only |
| Google Cloud | `https://cloud.google.com/feeds/gcp-release-notes.xml` | 30–60 | Rolling ~60-day window; no backfill possible |
| OCI | `https://docs.oracle.com/en-us/iaas/releasenotes/services/<service>/feed` | 5–15 | Per-service; subscribe to security services (Appendix B) plus the compute/network/identity services you run |

**Expected output:** ~10–25 security-relevant raw items/day → **5–15 in the final digest**. Some days will be genuinely thin — handle that gracefully (§9) rather than padding.

---

## 6. Classification

### 6.1 Pre-filter (rules — deliberately minimal)
Only drop what is unambiguously noise:
- Region-expansion boilerplate: `now available in|expanded to|now supported in` + a region token, **unless** the item also matches a security term (a security service landing in your region *is* news).
- GCP `Libraries` and `Fixed` release-note categories.
- Azure `In development` status (optional — keep if you want early warning).

Everything else goes to the classifier. Resist the urge to pre-filter harder; recall is the priority.

### 6.2 LLM classifier (the core of this project)

**Input:** JSON array of `{id, provider, service, title, blurb}`.

**Output:**
```json
{
  "lede": "2-3 lines naming the day's most consequential security change",
  "items": [
    {
      "id": "...",
      "is_security": true,
      "category": "identity | data | network | detection | governance | workload",
      "tldr": "<=25 words",
      "impact": "high | medium | low",
      "action_required": false
    }
  ]
}
```

**Prompt must define "security-relevant" explicitly**, because this is where accuracy lives. Include it as an in-prompt rubric:

> Count as security-relevant:
> - Any change to a dedicated security service (see list)
> - New authn/authz, encryption, key management, network isolation, audit/logging, or compliance capability on **any** service
> - Changes to **defaults** that alter security posture (e.g. public access blocked by default, TLS minimum raised)
> - Deprecation or retirement of a security feature, protocol, cipher, or auth method
> - New compliance certifications, data-residency or sovereignty controls
> - Confidential computing, attestation, supply-chain/signing, and isolation features
>
> Do NOT count as security-relevant:
> - Generic performance, pricing, or capacity announcements
> - "Enterprise-grade security" used as marketing filler with no specific capability
> - Region expansions of non-security services

**Also require:** return item IDs only, never invent IDs, ≤25 words per TLDR, plain declarative sentences, valid JSON with no code fences.

**Post-processing:** validate every returned ID exists in the input; drop unknown IDs; for any input item the model omitted, default to `is_security: false` but log it.

### 6.3 Always-promote rules (override the classifier)
Regardless of what the model says, promote to a pinned **⚠️ Action Needed** section:

```
deprecat | retire | retirement | end of life | end-of-support | EOL
breaking change | mandatory | will be disabled | must migrate | action required
TLS 1.0 | TLS 1.1 | legacy auth | basic authentication | certificate expiry
default will change | enabled by default | disabled by default
```

These carry deadlines. They are the single highest-value content in the digest and the main reason it exists.

### 6.4 Service allowlist — used for ranking, not filtering
Unlike a general product digest, **do not** use the service allowlist to drop items. Use it to boost impact: a security feature on a service you run is high impact; the same feature on a service you don't is still worth one line. Store as `config/services.yaml` (Appendix B).

---

## 7. Digest Taxonomy

| Category | Covers |
|---|---|
| ⚠️ **Action Needed** (pinned) | Deprecations, retirements, default changes, breaking changes |
| 🔑 **Identity & Access** | IAM, federation, MFA, conditional access, PIM, workload identity, permissions |
| 🔒 **Data Protection** | Encryption, KMS/HSM, secrets, DLP/sensitive data, backup immutability, residency |
| 🌐 **Network Security** | Firewall, WAF, DDoS, private link/endpoints, segmentation, zero trust |
| 🛡️ **Detection & Response** | CSPM/CNAPP, SIEM, threat intel, malware scanning, incident response |
| 📋 **Governance & Compliance** | Policy, guardrails, audit, certifications, sovereignty, access transparency |
| 📦 **Workload & Supply Chain** | Confidential computing, attestation, image signing, SBOM, admission control |

---

## 8. Failure Handling & Fallbacks

| Failure | Behaviour |
|---|---|
| One feed unreachable | Continue; note in digest footer as "sources unavailable" |
| Feed returns 0 items 3 days running | Alert — the feed-rot canary |
| LLM call fails / quota exhausted | Fall back to **keyword-only classification** using the lexicon in Appendix C. Lower precision, acceptable recall, still shipped |
| LLM returns invalid JSON | One retry with a stricter instruction, then keyword fallback |
| Delivery fails | Still commit state + archive page; retry next run |

**Rule:** a bad day degrades the digest, never kills it, and never loses state.

---

## 9. Handling Thin Days

Security-only scope means some weekdays will yield 2–3 items, and occasionally zero. Options, in order of preference:

1. **Ship it short.** A 3-item digest is a feature — it signals a quiet day. Add a one-line "quiet day" note.
2. **Suppress on zero.** Send nothing rather than an empty email; log the run so you know it executed.
3. **Roll up.** If you find yourself under ~3 items/day consistently, switch to a Mon/Thu cadence rather than padding daily editions with filler.

Do **not** solve thin days by loosening the classifier. That trades the digest's entire value proposition for volume.

---

## 10. Repository Layout

```
csp-sec-tldr/
├── .github/workflows/daily.yml
├── config/
│   ├── sources.yaml          # feed URLs, tier, parser hints
│   ├── services.yaml         # security services + your estate (for ranking)
│   ├── regions.yaml          # your regions
│   └── rules.yaml            # promote patterns, pre-filter drops, keyword lexicon
├── src/
│   ├── fetch.py              # parallel HTTP, timeouts, per-source isolation
│   ├── normalize.py          # → common item schema
│   ├── dedupe.py             # state.json, content hashing
│   ├── prefilter.py          # minimal rules (§6.1)
│   ├── classify.py           # batched LLM call, validation, keyword fallback
│   ├── rank.py               # promote rules, impact boosting, ordering
│   ├── render.py             # jinja2 → markdown + html
│   ├── deliver.py            # telegram / slack / email
│   └── main.py               # orchestration
├── templates/
│   ├── digest.md.j2
│   └── digest.html.j2
├── docs/                     # GitHub Pages archive
├── state.json
├── requirements.txt
└── README.md
```

### Item schema
```python
{
  "id": str,              # feed GUID or url hash
  "provider": str,        # aws | azure | gcp | oci
  "source_tier": int,     # 1 = dedicated security source, 2 = general feed
  "service": str | None,
  "title": str,
  "url": str,
  "published_at": datetime,
  "blurb": str,           # HTML-stripped feed description
  "content_hash": str,
  # populated by classify.py:
  "is_security": bool,
  "category": str,
  "tldr": str,
  "impact": str,
  "action_required": bool,
}
```

---

## 11. Implementation Phases

### Phase 1 — Ingest & verify feeds (1 evening)
- [ ] Repo scaffold, `requirements.txt`, `sources.yaml`
- [ ] **Verify all four Tier-1 blog feeds return items** — two are unconfirmed
- [ ] `fetch.py` — parallel, per-source timeout, one bad source never fails the run
- [ ] `normalize.py` for all sources
- [ ] `dedupe.py` + `state.json`
- [ ] `main.py` dumps raw JSON to stdout

**Exit criteria:** 3 consecutive days of local runs, with actual per-provider volume recorded. Don't set any threshold before you have this data.

### Phase 2 — Classification (1–2 evenings, the real work)
- [ ] `prefilter.py` (minimal)
- [ ] `classify.py` — batched call, rubric prompt, JSON schema, ID validation
- [ ] Keyword fallback path using Appendix C lexicon
- [ ] `rank.py` — promote rules + impact boosting from `services.yaml`
- [ ] **Log every item with its verdict**, kept and dropped, for audit

**Exit criteria:** manually review 3 days of dropped items and find no security-relevant item wrongly excluded. Iterate the rubric until that holds.

### Phase 3 — Render & deliver (1 evening)
- [ ] `render.py` + Markdown template with the §7 taxonomy
- [ ] `deliver.py` — Telegram first
- [ ] Thin-day handling (§9)

**Exit criteria:** a digest lands on your phone that you'd actually read.

### Phase 4 — Automate & harden (1 evening)
- [ ] `daily.yml` with cron + `workflow_dispatch`
- [ ] Secrets wired; state committed back by the workflow
- [ ] HTML template + email delivery (if wanted)
- [ ] GitHub Pages archive + "+N more" links
- [ ] Feed-rot canary

### Phase 5 — Tune (2 weeks, ~15 min/day)
- [ ] Daily: read the dropped-items log; every false negative becomes a rubric fix
- [ ] Weekly: adjust impact thresholds and category assignment
- [ ] Spot-check every TLDR against source for the first week

**Not optional.** Untuned, the classifier is a coin flip on edge cases. Tuned, this becomes invisible infrastructure — which is exactly when a silent regression is most expensive.

---

## 12. Workflow Sketch

```yaml
name: daily-csp-security-tldr
on:
  schedule:
    - cron: '0 5 * * 1-5'   # 05:00 UTC, weekdays
  workflow_dispatch:

permissions:
  contents: write

jobs:
  digest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python -m src.main
        env:
          LLM_API_KEY:      ${{ secrets.LLM_API_KEY }}
          TELEGRAM_TOKEN:   ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      - run: |
          git config user.name  "sec-tldr-bot"
          git config user.email "actions@github.com"
          git add state.json docs/
          git diff --staged --quiet || git commit -m "digest $(date -u +%F)"
          git push
```

---

## 13. Digest Layout

```
🔐 Cloud Security TLDR — Mon 27 Jul 2026

<2-3 line lede: the one security change that matters today>

⚠️ ACTION NEEDED
• [Azure] Legacy auth for X disabled 15 Oct — migrate to Y          [link]
• [AWS]   TLS 1.0/1.1 endpoints retired for Z on 1 Sep              [link]

🔑 IDENTITY & ACCESS
• <=25 word tldr                                                    [link]

🔒 DATA PROTECTION
• ...

🌐 NETWORK SECURITY
• ...

🛡️ DETECTION & RESPONSE
• ...

📋 GOVERNANCE & COMPLIANCE
• ...

📦 WORKLOAD & SUPPLY CHAIN
• ...

→ 4 lower-impact items in the archive
⚠️ sources unavailable today: none
```

Group by category, not provider — security work is organised by domain, and cross-provider patterns ("all three added X this month") become visible. Tag each line with its provider. Cap at ~4 per category; overflow to archive.

---

## 14. Known Pitfalls

| Pitfall | Mitigation |
|---|---|
| **Classifier drifts toward marketing language** — "enterprise-grade security" filler | Explicit negative examples in the rubric; review dropped/kept logs weekly |
| **Feed rot.** Azure's RSS URL has moved twice already | Zero-items-for-3-days canary; check footer daily |
| **Two Tier-1 blog feeds are unverified** | Confirm in Phase 1 before building on them |
| **GCP feed is a rolling ~60-day window** | Fine for daily runs; no backfill is possible |
| **Timezone/cutoff drift** | Fixed window (last-run → now) in UTC, never "today" |
| **Feeds republish edited items** | Content hashing (§10 schema) |
| **Thin days tempt you to loosen the filter** | See §9 — ship short instead |
| **Trusting summaries too early** | Spot-check for two weeks before it fades into the background |
| **Free LLM quota changes** | Provider behind an interface + keyword fallback |

---

## Appendix A — Config Example

`config/sources.yaml`
```yaml
# Tier 1 — dedicated security sources, no classification needed
- provider: aws
  name: security-blog
  url: https://aws.amazon.com/blogs/security/feed/
  tier: 1
- provider: azure
  name: ms-security-blog
  url: https://www.microsoft.com/en-us/security/blog/feed/
  tier: 1
- provider: gcp
  name: gcp-security-blog
  url: https://cloudblog.withgoogle.com/products/identity-security/rss/
  tier: 1        # VERIFY
- provider: oci
  name: oracle-cloud-security-blog
  url: https://blogs.oracle.com/cloudsecurity/rss
  tier: 1        # VERIFY

# Tier 2 — general product feeds, require classification
- provider: aws
  name: whats-new
  url: https://aws.amazon.com/about-aws/whats-new/recent/feed/
  tier: 2
- provider: azure
  name: service-updates
  url: https://www.microsoft.com/releasecommunications/api/v2/azure/rss
  tier: 2
- provider: gcp
  name: release-notes
  url: https://cloud.google.com/feeds/gcp-release-notes.xml
  tier: 2
- provider: oci
  name: cloud-guard
  url: https://docs.oracle.com/en-us/iaas/releasenotes/services/cloud-guard/feed
  tier: 2
```

---

## Appendix B — Security Service Reference (for ranking & rubric)

**AWS** — IAM, IAM Identity Center, Organizations/SCPs, GuardDuty, Security Hub, Inspector, Macie, Detective, Security Lake, WAF, Shield, Network Firewall, Firewall Manager, KMS, CloudHSM, Certificate Manager, Secrets Manager, Verified Access, Verified Permissions, Cognito, Config, CloudTrail, Audit Manager, Artifact, PrivateLink, Nitro Enclaves, Payment Cryptography, RAM

**Azure** — Microsoft Defender for Cloud, Microsoft Sentinel, Defender XDR, Entra ID (Conditional Access, PIM, ID Protection, Permissions Management, Verified ID, Global Secure Access), Key Vault, Managed HSM, Azure Firewall, Front Door WAF, DDoS Protection, Private Link, Bastion, Azure Policy, Microsoft Purview, Confidential Computing, Attestation

**Google Cloud** — Security Command Center, Cloud IAM, Access Context Manager / BeyondCorp, VPC Service Controls, Cloud KMS, Cloud HSM, Secret Manager, Cloud Armor, Cloud NGFW, Certificate Authority Service, Certificate Manager, Binary Authorization, Assured Workloads, Confidential Computing, Google SecOps (Chronicle), Mandiant, Sensitive Data Protection, Policy Intelligence, Access Transparency/Approval, Identity Platform, reCAPTCHA, Shielded VM, Workload Identity

**OCI** — Cloud Guard, Security Zones, Security Advisor, Vault/KMS, WAF, Network Firewall, Vulnerability Scanning, Threat Intelligence, Data Safe, Access Governance, Bastion, Certificates, Audit, Zero Trust Packet Routing, Identity Domains, Shielded Instances, Confidential Computing

---

## Appendix C — Keyword Lexicon (LLM fallback path)

```
identity, IAM, RBAC, ABAC, permission, privilege, role, policy, principal,
authentication, authorisation, MFA, passwordless, passkey, SSO, federation,
conditional access, credential, token, session, workload identity,
encryption, encrypt, key, KMS, HSM, TLS, mTLS, cipher, certificate, PKI,
secret, vault, rotation, envelope encryption, BYOK, CMEK, HYOK,
firewall, WAF, DDoS, private link, private endpoint, egress, segmentation,
zero trust, network isolation, perimeter, service control,
threat, detection, malware, ransomware, anomaly, SIEM, SOAR, XDR, CNAPP,
CSPM, CIEM, posture, vulnerability, exposure, attack path,
audit, logging, CloudTrail, access transparency, compliance, attestation,
certification, FedRAMP, SOC 2, ISO 27001, PCI, HIPAA, GDPR, sovereignty,
data residency, DLP, sensitive data, PII, classification, redaction,
confidential computing, enclave, TEE, secure boot, measured boot,
signing, SBOM, provenance, supply chain, admission control, image scanning,
deprecat, retire, end of life, breaking change, default
```

Precision on this lexicon alone is roughly 40–50% — acceptable as a degraded fallback, not as the primary path.

---

## Appendix D — Optional CVE / Advisory Track

Currently out of scope. If added later, it reuses fetch/normalize/dedupe/render unchanged; only classification differs (advisories bypass the classifier entirely — surface all, never cap).

**AWS**
- Security bulletins: `https://aws.amazon.com/security/security-bulletins/rss/feed/`
- Amazon Linux advisories: per-version RSS at `https://alas.aws.amazon.com/`

**Azure**
- MSRC Security Update Guide, CVRF API — public and unauthenticated, send `Accept: application/json`:
  - release list: `https://api.msrc.microsoft.com/cvrf/v3.0/updates`
  - document: `https://api.msrc.microsoft.com/cvrf/v3.0/cvrf/{ID}`
  - No server-side time filter — fetch the list in full, filter client-side

**Google Cloud**
- Per-product bulletins: `https://cloud.google.com/feeds/<product>-security-bulletins.xml`
  (e.g. `kubernetes-engine-`, `compute-engine-`, `cloudbuild-`)
- Consolidated page: `docs.cloud.google.com/support/bulletins`

**OCI**
- Critical Patch Updates / Security Alerts: `https://www.oracle.com/security-alerts/` — quarterly cadence, no clean RSS, requires scraping the index

**Enrichment**
- CISA KEV catalog — flag actively exploited CVEs
- cloudvulndb.org — cloud-specific vulnerability tracking

---

## Appendix E — Effort Estimate

| Component | Lines (approx.) |
|---|---|
| fetch + normalize + dedupe | 150 |
| prefilter + rank + config | 100 |
| classify (prompt, validation, fallback) | 150 |
| render | 90 |
| deliver | 60 |
| workflow | 30 |
| **Total** | **~580** |

A weekend to build, then two weeks of rubric tuning — which is where the quality actually comes from.
