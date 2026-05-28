"""Change-request email notifications via SMTP (Office 365).

Replaces the old notify-change-request.yml GitHub Action. Sends to the
configured recipient when a change request is submitted from the public
dashboard. Credentials come from Key Vault (smtp-username / smtp-password);
host/port/recipient come from environment (non-secret config).

DECISION (see STATUS.md): the platform-setup doc mentioned replacing this with
a Microsoft Graph call, but SMTP credentials already exist in Key Vault and the
old workflow used SMTP, so this preserves identical behavior with no new Graph
permissions. Swap to Graph later if desired.
"""

import os
import smtplib
from email.message import EmailMessage

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.office365.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
CHANGE_REQUEST_RECIPIENT = os.environ.get("CHANGE_REQUEST_RECIPIENT", "nacolvin@davidson.edu")


def send_change_request_email(payload: dict, secrets: dict) -> bool:
    """Email a submitted change request. Returns True on success."""
    username = secrets.get("smtp-username", "")
    password = secrets.get("smtp-password", "")
    if not username or not password:
        print("  email: smtp-username/smtp-password not set — skipping change-request email")
        return False

    title = payload.get("title") or payload.get("summary") or "Dashboard change request"
    submitted_by = payload.get("submitted_by", "anonymous")
    details = payload.get("details", "")

    msg = EmailMessage()
    msg["Subject"] = f"Dashboard Change Request: {title}"
    msg["From"] = username
    msg["To"] = CHANGE_REQUEST_RECIPIENT
    msg.set_content(
        f"New dashboard change request\n\n"
        f"Title: {title}\n"
        f"Submitted by: {submitted_by}\n\n"
        f"Details:\n{details}\n"
    )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(msg)
        print(f"  email: change-request notification sent to {CHANGE_REQUEST_RECIPIENT}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  email: failed to send change-request notification: {exc}")
        return False
