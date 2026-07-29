"""§6.2 Classification — one batched LLM call, with a keyword fallback.

Two structural rules from §3 are enforced here:

  1. ONE call per run. The model sees every surviving item at once, so rate
     limits and retry/backoff complexity stop mattering.
  2. The model never sees or emits URLs. It receives {id, provider, service,
     title, blurb} and returns verdicts keyed by id. URLs are rejoined at
     render time, which structurally rules out hallucinated links.

The provider sits behind `LLMProvider`; swapping vendors is a one-class change.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx

from .normalize import Item
from .util import (
    any_match,
    clip_words,
    compile_patterns,
    env_int,
    load_yaml,
    log,
    truncate,
)

CATEGORIES = ("identity", "data", "network", "detection", "governance", "workload")
IMPACTS = ("high", "medium", "low")
MAX_ITEMS_PER_CALL = 120
TLDR_MAX_WORDS = 25

RUBRIC = """\
Count as security-relevant:
- Any change to a dedicated security service (IAM/identity, key management,
  firewall/WAF/DDoS, threat detection, posture management, compliance tooling,
  audit/logging, secrets, certificates, confidential computing).
- New authn/authz, encryption, key management, network isolation, audit/logging,
  or compliance capability on ANY service, security service or not.
- Changes to DEFAULTS that alter security posture (public access blocked by
  default, TLS minimum raised, encryption on by default, MFA enforced).
- Deprecation or retirement of a security feature, protocol, cipher, or auth
  method — including anything with a migration deadline.
- New compliance certifications, data-residency or sovereignty controls.
- Confidential computing, attestation, supply-chain/signing, isolation features.

Do NOT count as security-relevant:
- Generic performance, pricing, capacity, or quota announcements.
- "Enterprise-grade security" or similar marketing filler with no specific
  capability named.
- Region expansions of non-security services.
- Pure UI/console refreshes with no change to what can be enforced or observed.

When genuinely unsure, answer is_security: true. A missed security announcement
costs far more than one extra line in the digest."""

SYSTEM_PROMPT = f"""\
You classify cloud provider product announcements for a daily security digest \
read by cloud security engineers running AWS, Azure, Google Cloud and OCI.

{RUBRIC}

For every input item return exactly one object with these fields:
  id             the id string from the input, copied verbatim
  is_security    boolean, per the rubric above
  category       one of: {" | ".join(CATEGORIES)}
  tldr           <= {TLDR_MAX_WORDS} words, one plain declarative sentence saying
                 what changed and who it affects. No marketing adjectives, no
                 "this announcement", no restating the provider name twice.
  impact         high | medium | low. high = changes what you must do or what is
                 enforced by default; medium = new capability worth adopting;
                 low = incremental or niche.
  action_required  true only if there is a deadline, retirement, forced
                 migration, or a default that will change.

Category definitions:
  identity   IAM, federation, MFA, conditional access, PIM, workload identity, permissions
  data       encryption, KMS/HSM, secrets, DLP, sensitive data, backup immutability, residency
  network    firewall, WAF, DDoS, private link/endpoints, segmentation, zero trust
  detection  CSPM/CNAPP, SIEM, threat intel, malware scanning, incident response
  governance policy, guardrails, audit, certifications, sovereignty, access transparency
  workload   confidential computing, attestation, image signing, SBOM, admission control

Also write a "lede": 2-3 short lines naming the single most consequential
security change in this batch and why it matters. If nothing stands out, say so
plainly in one line. Never invent an item that is not in the input.

Rules:
- Return ONLY the ids given to you. Never invent an id. Never omit an item.
- Return valid JSON. No code fences, no commentary, no trailing text.
- Never output URLs.

Response shape:
{{"lede": "...", "items": [{{"id": "...", "is_security": true, \
"category": "identity", "tldr": "...", "impact": "high", \
"action_required": false}}]}}"""

STRICTER_SUFFIX = (
    "\n\nYour previous response was not valid JSON. Return ONLY a single JSON "
    "object. Start your response with { and end it with }. No markdown, no code "
    "fences, no explanation."
)


# --------------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------------- #


class LLMError(RuntimeError):
    pass


class LLMProvider:
    """Minimal text-in/text-out interface. Add a vendor by adding a subclass."""

    name = "base"

    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def _post(self, url: str, headers: dict[str, str], payload: dict) -> dict:
        attempts = env_int("LLM_MAX_ATTEMPTS", 3)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.post(
                    url, headers=headers, json=payload, timeout=self.timeout
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise LLMError(
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                if response.status_code >= 400:
                    raise LLMError(
                        f"HTTP {response.status_code}: {response.text[:300]}"
                    )
                return response.json()
            except Exception as exc:  # noqa: BLE001 - retried, then surfaced
                last_error = exc
                if attempt < attempts:
                    backoff = 2**attempt
                    log.warning(
                        "llm attempt %d/%d failed (%s), retrying in %ds",
                        attempt, attempts, exc, backoff,
                    )
                    time.sleep(backoff)
        raise LLMError(str(last_error))


class GeminiProvider(LLMProvider):
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def complete(self, system: str, user: str) -> str:
        url = f"{self.BASE}/{self.model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "maxOutputTokens": env_int("LLM_MAX_OUTPUT_TOKENS", 8192),
                # Newer "-latest" aliases resolve to thinking models that spend
                # the output-token budget on hidden reasoning before ever
                # writing JSON, silently truncating large batches. 0 is
                # rejected by some models as below their minimum; 128 is the
                # smallest budget accepted across the current model line.
                "thinkingConfig": {"thinkingBudget": 128},
            },
        }
        data = self._post(
            url,
            {"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            payload,
        )
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError(f"no candidates: {json.dumps(data)[:300]}")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        usage = data.get("usageMetadata") or {}
        log.info(
            "gemini usage: %s in / %s out tokens",
            usage.get("promptTokenCount", "?"),
            usage.get("candidatesTokenCount", "?"),
        )
        if not text.strip():
            raise LLMError(
                f"empty completion (finishReason={candidates[0].get('finishReason')})"
            )
        return text


class OpenAICompatProvider(LLMProvider):
    """Groq, GitHub Models, Cloudflare Workers AI, Together, vLLM, ..."""

    name = "openai-compat"
    default_base = "https://api.groq.com/openai/v1"

    def __init__(self, api_key: str, model: str, timeout: float,
                 base_url: str | None = None) -> None:
        super().__init__(api_key, model, timeout)
        self.base_url = (base_url or self.default_base).rstrip("/")

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": env_int("LLM_MAX_OUTPUT_TOKENS", 8192),
        }
        data = self._post(
            f"{self.base_url}/chat/completions",
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload,
        )
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"no choices: {json.dumps(data)[:300]}")
        usage = data.get("usage") or {}
        log.info(
            "llm usage: %s in / %s out tokens",
            usage.get("prompt_tokens", "?"),
            usage.get("completion_tokens", "?"),
        )
        return choices[0].get("message", {}).get("content", "") or ""


class GroqProvider(OpenAICompatProvider):
    name = "groq"
    default_base = "https://api.groq.com/openai/v1"


DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "groq": "llama-3.3-70b-versatile",
    "openai-compat": "gpt-4o-mini",
}


def get_provider() -> LLMProvider | None:
    """Build the configured provider, or None if no key is set."""
    name = (os.getenv("LLM_PROVIDER") or "gemini").strip().lower()
    api_key = (os.getenv("LLM_API_KEY") or "").strip()
    if not api_key:
        log.warning("LLM_API_KEY not set — using keyword fallback")
        return None

    model = (os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(name, "")).strip()
    timeout = float(env_int("LLM_TIMEOUT_SECONDS", 120))
    base_url = os.getenv("LLM_BASE_URL")

    if name == "gemini":
        return GeminiProvider(api_key, model, timeout)
    if name == "groq":
        return GroqProvider(api_key, model, timeout, base_url)
    if name in ("openai", "openai-compat", "github", "cloudflare"):
        return OpenAICompatProvider(api_key, model, timeout, base_url)

    log.error("unknown LLM_PROVIDER %r — using keyword fallback", name)
    return None


# --------------------------------------------------------------------------- #
# Prompt / response handling
# --------------------------------------------------------------------------- #


def build_payload(items: list[Item]) -> str:
    """Model input. Note the deliberate absence of any URL field."""
    return json.dumps(
        [
            {
                "id": item.id,
                "provider": item.provider,
                "service": item.service or "",
                "title": item.title,
                "blurb": truncate(item.blurb, 320),
            }
            for item in items
        ],
        ensure_ascii=False,
        indent=None,
    )


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_response(text: str) -> dict[str, Any]:
    """Tolerate fences and surrounding prose; anything else is a parse failure."""
    cleaned = _FENCE_RE.sub("", (text or "").strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return json.loads(cleaned[start : end + 1])
    raise json.JSONDecodeError("no JSON object found", cleaned or "", 0)


def apply_verdicts(items: list[Item], data: dict[str, Any]) -> tuple[int, list[str]]:
    """Merge model verdicts into items. Returns (applied, unmatched_ids)."""
    by_id = {item.id: item for item in items}
    unmatched: list[str] = []
    applied = 0

    for verdict in data.get("items") or []:
        if not isinstance(verdict, dict):
            continue
        item_id = str(verdict.get("id", "")).strip()
        item = by_id.get(item_id)
        if item is None:
            unmatched.append(item_id)
            continue

        category = str(verdict.get("category", "")).strip().lower()
        impact = str(verdict.get("impact", "")).strip().lower()
        tldr = str(verdict.get("tldr", "") or "").strip()

        item.is_security = bool(verdict.get("is_security", False))
        item.category = category if category in CATEGORIES else "governance"
        item.impact = impact if impact in IMPACTS else "medium"
        item.tldr = clip_words(tldr, TLDR_MAX_WORDS) if tldr else truncate(item.title, 160)
        item.action_required = bool(verdict.get("action_required", False))
        item.classified_by = "llm"
        applied += 1

    return applied, unmatched


# --------------------------------------------------------------------------- #
# Keyword fallback (Appendix C)
# --------------------------------------------------------------------------- #


class KeywordClassifier:
    """Degraded path (§8). ~40-50% precision — acceptable, never primary."""

    def __init__(self) -> None:
        rules = load_yaml("rules.yaml") or {}
        lexicon = rules.get("lexicon") or {}
        self.lexicon = {
            category: compile_patterns(patterns)
            for category, patterns in lexicon.items()
            if category in CATEGORIES
        }
        self.promote = compile_patterns(rules.get("promote_patterns"))

    def classify(self, item: Item) -> None:
        text = item.text
        scores = {
            category: sum(1 for p in patterns if p.search(text))
            for category, patterns in self.lexicon.items()
        }
        best = max(scores, key=lambda c: scores[c]) if scores else "governance"
        hits = scores.get(best, 0)

        item.is_security = item.source_tier == 1 or hits > 0
        item.category = best if hits else "governance"
        item.tldr = clip_words(item.blurb or item.title, TLDR_MAX_WORDS) or item.title
        item.action_required = any_match(self.promote, text)
        item.impact = (
            "high" if item.action_required else ("medium" if hits >= 2 else "low")
        )
        item.classified_by = "keyword"


def keyword_classify(items: list[Item]) -> None:
    classifier = KeywordClassifier()
    for item in items:
        classifier.classify(item)
    kept = sum(1 for item in items if item.is_security)
    log.warning("keyword fallback classified %d items, %d security-relevant",
                len(items), kept)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def classify(items: list[Item]) -> tuple[str, dict[str, Any]]:
    """Classify in place. Returns (lede, diagnostics)."""
    diagnostics: dict[str, Any] = {
        "path": "none",
        "calls": 0,
        "unmatched_ids": [],
        "omitted_ids": [],
        "errors": [],
    }
    if not items:
        return "", diagnostics

    provider = get_provider()
    if provider is None:
        keyword_classify(items)
        diagnostics["path"] = "keyword"
        diagnostics["errors"].append("no LLM provider configured")
        return "", diagnostics

    batches = [
        items[i : i + MAX_ITEMS_PER_CALL]
        for i in range(0, len(items), MAX_ITEMS_PER_CALL)
    ]
    if len(batches) > 1:
        log.info("splitting %d items into %d calls", len(items), len(batches))

    ledes: list[str] = []
    classified_ids: set[str] = set()
    failed = False

    for batch in batches:
        payload = build_payload(batch)
        log.info(
            "classifying %d items via %s/%s (~%d chars of input)",
            len(batch), provider.name, provider.model, len(payload),
        )
        data = None
        for attempt in (1, 2):
            system = SYSTEM_PROMPT + (STRICTER_SUFFIX if attempt == 2 else "")
            try:
                raw = provider.complete(system, payload)
                diagnostics["calls"] += 1
                data = parse_response(raw)
                break
            except json.JSONDecodeError as exc:
                log.error("llm returned invalid JSON (attempt %d): %s", attempt, exc)
                diagnostics["errors"].append(f"invalid JSON (attempt {attempt})")
            except LLMError as exc:
                log.error("llm call failed: %s", exc)
                diagnostics["errors"].append(str(exc)[:200])
                break
            except Exception as exc:  # noqa: BLE001 - never kill the run
                log.error("llm call raised %s: %s", type(exc).__name__, exc)
                diagnostics["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
                break

        if data is None:
            failed = True
            break

        applied, unmatched = apply_verdicts(batch, data)
        classified_ids.update(
            item.id for item in batch if item.classified_by == "llm"
        )
        diagnostics["unmatched_ids"].extend(unmatched)
        if unmatched:
            log.warning("llm returned %d unknown ids, dropped: %s",
                        len(unmatched), unmatched[:5])
        lede = str(data.get("lede", "") or "").strip()
        if lede:
            ledes.append(lede)
        log.info("llm classified %d/%d items in batch", applied, len(batch))

    if failed:
        # Degrade the whole run rather than shipping a half-LLM, half-nothing
        # digest with inconsistent verdicts.
        log.error("LLM path failed — falling back to keyword classification")
        keyword_classify(items)
        diagnostics["path"] = "keyword-fallback"
        return "", diagnostics

    omitted = [item for item in items if item.id not in classified_ids]
    if omitted:
        # §6.2 post-processing: default to not-security, but log every one so a
        # systematic omission shows up in the audit trail.
        classifier = KeywordClassifier()
        for item in omitted:
            classifier.classify(item)
            if item.source_tier != 1:
                item.is_security = False
            item.classified_by = "omitted-by-llm"
        diagnostics["omitted_ids"] = [item.id for item in omitted]
        log.warning("llm omitted %d items: %s", len(omitted),
                    [item.title[:60] for item in omitted[:5]])

    # Tier 1 sources are security by definition (§5) — the model's is_security
    # verdict does not apply to them.
    for item in items:
        if item.source_tier == 1:
            item.is_security = True

    diagnostics["path"] = "llm"
    return ledes[0] if ledes else "", diagnostics
