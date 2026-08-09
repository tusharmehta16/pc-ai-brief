"""Send the brief. Two backends, chosen by EMAIL_BACKEND.

Distribution modes, set with EMAIL_MODE:
  bcc         one send, everyone hidden from everyone else. The default.
  to          one send, all addresses visible in the To line.
  individual  a separate message per address. Slowest, best deliverability,
              and the only mode where one bad address cannot affect the rest.
"""

from __future__ import annotations

import logging
import os
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import requests

log = logging.getLogger(__name__)


def recipients() -> list[str]:
    """EMAIL_TO accepts commas, semicolons, or newlines."""
    raw = os.getenv("EMAIL_TO", "")
    for separator in (";", "\n"):
        raw = raw.replace(separator, ",")
    seen, ordered = set(), []
    for address in (a.strip() for a in raw.split(",")):
        key = address.lower()
        if address and "@" in address and key not in seen:
            seen.add(key)
            ordered.append(address)
    return ordered


def mode() -> str:
    return os.getenv("EMAIL_MODE", "bcc").lower()


def unsubscribe_target() -> str:
    return os.getenv("EMAIL_REPLY_TO") or os.getenv("EMAIL_FROM", "")


def build_message(subject: str, html: str, text: str, sender: str,
                  to_line: list[str], bcc_line: list[str]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("P&C AI Brief", sender))
    message["To"] = ", ".join(to_line)
    if bcc_line:
        message["Bcc"] = ", ".join(bcc_line)
    if os.getenv("EMAIL_REPLY_TO"):
        message["Reply-To"] = os.environ["EMAIL_REPLY_TO"]
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    # Gives every reader a one click way out, and improves inbox placement.
    if unsubscribe_target():
        message["List-Unsubscribe"] = (
            f"<mailto:{unsubscribe_target()}?subject=Unsubscribe%20PC%20AI%20Brief>")
        message["List-Id"] = "P&C AI Brief <pc-ai-brief.local>"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def smtp_connection():
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    server.login(user, password)
    return server


def send_smtp(subject: str, html: str, text: str) -> None:
    sender = os.getenv("EMAIL_FROM", os.environ["SMTP_USER"])
    people = recipients()
    delivery = mode()

    with smtp_connection() as server:
        if delivery == "individual":
            failures = []
            for address in people:
                message = build_message(subject, html, text, sender, [address], [])
                try:
                    server.send_message(message)
                    log.info("sent to %s", address)
                except smtplib.SMTPException as exc:
                    failures.append(address)
                    log.error("failed for %s: %s", address, exc)
                time.sleep(1)  # stay well under Gmail's per minute throttle
            if failures:
                log.warning("%d of %d recipients failed", len(failures), len(people))
            return

        if delivery == "to":
            message = build_message(subject, html, text, sender, people, [])
        else:  # bcc, the default
            message = build_message(subject, html, text, sender, [sender], people)
        server.send_message(message)

    log.info("sent via SMTP in %s mode to %d recipient(s)", delivery, len(people))


def send_resend(subject: str, html: str, text: str) -> None:
    sender = os.getenv("EMAIL_FROM", "brief@resend.dev")
    people = recipients()
    delivery = mode()

    def post(payload: dict) -> None:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                     "Content-Type": "application/json"},
            json=payload, timeout=30)
        response.raise_for_status()
        log.info("Resend accepted %s", response.json().get("id"))

    base = {"from": sender, "subject": subject, "html": html, "text": text}
    if os.getenv("EMAIL_REPLY_TO"):
        base["reply_to"] = os.environ["EMAIL_REPLY_TO"]

    if delivery == "individual":
        for address in people:
            post({**base, "to": [address]})
        return
    if delivery == "to":
        post({**base, "to": people})
        return
    post({**base, "to": [sender], "bcc": people})


def send(subject: str, html: str, text: str) -> None:
    people = recipients()
    if not people:
        raise RuntimeError("EMAIL_TO is empty, nowhere to send")
    log.info("distribution list has %d recipient(s), mode %s", len(people), mode())
    if os.getenv("EMAIL_BACKEND", "smtp").lower() == "resend":
        send_resend(subject, html, text)
    else:
        send_smtp(subject, html, text)
