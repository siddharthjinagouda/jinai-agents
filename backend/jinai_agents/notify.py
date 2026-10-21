"""Email delivery for notify() and owner alerts. Without SMTP settings, messages stay in the run record."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from . import config

outbox: list[dict] = []  # last messages, for tests and local use


def send(to: str, subject: str, body: str) -> str:
    st = config.settings
    outbox.append({"to": to, "subject": subject, "body": body})
    del outbox[:-200]
    if not to:
        return "kept (no recipient set)"
    if not st.smtp_host:
        return "kept (email not configured)"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = st.smtp_from, to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(st.smtp_host, st.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            if st.smtp_user:
                smtp.login(st.smtp_user, st.smtp_password)
            smtp.send_message(msg)
        return "sent"
    except Exception as e:  # noqa: BLE001
        return f"email failed: {e}"[:200]


def alert_owner(agent, subject: str, body: str) -> str:
    to = agent.alert_email or config.settings.owner_email
    link = f"{config.settings.public_url}/agent/?id={agent.id}"
    return send(to, f"[Jinai] {agent.name}: {subject}", f"{body}\n\nOpen the agent: {link}")
