"""Delivery: Telegram (primary), Slack, email. Each channel is isolated.

A delivery failure never kills the run — state and the archive page are still
committed, and the next run retries (§8).
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from .render import Rendered
from .util import env_bool, env_int, log

TELEGRAM_LIMIT = 4096
SLACK_LIMIT = 39000


@dataclass
class DeliveryResult:
    channel: str
    ok: bool
    detail: str = ""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _split_message(text: str, limit: int) -> list[str]:
    """Split on paragraph, then line, then hard boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(block) <= limit:
            current = block
            continue
        # A single oversized block: fall back to line, then hard, splitting.
        current = ""
        for line in block.split("\n"):
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = line
    if current:
        chunks.append(current)
    return chunks


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def markdown_to_slack(markdown: str) -> str:
    text = _MD_LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", markdown)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.M)
    text = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"_\1_", text)
    text = text.replace("<sub>", "").replace("</sub>", "")
    return text.strip()


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #


def deliver_telegram(rendered: Rendered) -> DeliveryResult | None:
    token = (os.getenv("TELEGRAM_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return None

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = _split_message(rendered.telegram, TELEGRAM_LIMIT)
    try:
        for index, chunk in enumerate(chunks, 1):
            response = httpx.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": env_bool("TELEGRAM_SILENT", False),
                },
                timeout=float(env_int("DELIVERY_TIMEOUT_SECONDS", 30)),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            log.info("telegram: sent part %d/%d", index, len(chunks))
        return DeliveryResult("telegram", True, f"{len(chunks)} message(s)")
    except Exception as exc:  # noqa: BLE001 - delivery must not kill the run
        log.error("telegram delivery failed: %s", exc)
        return DeliveryResult("telegram", False, str(exc)[:300])


def deliver_slack(rendered: Rendered) -> DeliveryResult | None:
    webhook = (os.getenv("SLACK_WEBHOOK_URL") or "").strip()
    if not webhook:
        return None

    text = markdown_to_slack(rendered.markdown)[:SLACK_LIMIT]
    try:
        response = httpx.post(
            webhook,
            json={"text": text, "unfurl_links": False, "unfurl_media": False},
            timeout=float(env_int("DELIVERY_TIMEOUT_SECONDS", 30)),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
        log.info("slack: delivered")
        return DeliveryResult("slack", True)
    except Exception as exc:  # noqa: BLE001
        log.error("slack delivery failed: %s", exc)
        return DeliveryResult("slack", False, str(exc)[:300])


def deliver_email(rendered: Rendered) -> DeliveryResult | None:
    host = (os.getenv("SMTP_HOST") or "").strip()
    recipients = [
        addr.strip()
        for addr in (os.getenv("EMAIL_TO") or "").split(",")
        if addr.strip()
    ]
    if not host or not recipients:
        return None

    port = env_int("SMTP_PORT", 587)
    user = os.getenv("SMTP_USER") or ""
    password = os.getenv("SMTP_PASSWORD") or ""
    sender = os.getenv("EMAIL_FROM") or user

    message = EmailMessage()
    message["Subject"] = rendered.subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(rendered.markdown)
    message.add_alternative(rendered.html, subtype="html")

    try:
        timeout = float(env_int("DELIVERY_TIMEOUT_SECONDS", 30))
        if port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                host, port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        with server:
            if port != 465:
                server.starttls(context=ssl.create_default_context())
            if user:
                server.login(user, password)
            server.send_message(message)
        log.info("email: delivered to %d recipient(s)", len(recipients))
        return DeliveryResult("email", True, f"{len(recipients)} recipient(s)")
    except Exception as exc:  # noqa: BLE001
        log.error("email delivery failed: %s", exc)
        return DeliveryResult("email", False, str(exc)[:300])


def deliver(rendered: Rendered) -> list[DeliveryResult]:
    """Send on every configured channel. Unconfigured channels are skipped."""
    if env_bool("DRY_RUN", False):
        log.warning("DRY_RUN set — skipping all delivery")
        return [DeliveryResult("dry-run", True, "delivery skipped")]

    results: list[DeliveryResult] = []
    for channel in (deliver_telegram, deliver_slack, deliver_email):
        result = channel(rendered)
        if result is not None:
            results.append(result)

    if not results:
        log.warning(
            "no delivery channel configured — set TELEGRAM_TOKEN/TELEGRAM_CHAT_ID, "
            "SLACK_WEBHOOK_URL or SMTP_HOST+EMAIL_TO"
        )
    return results
