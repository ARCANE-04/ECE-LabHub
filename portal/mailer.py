"""Outgoing mail.

Production: set MAIL_SERVER (and MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, MAIL_USE_TLS,
MAIL_SENDER) and messages go out over SMTP.

Development (no MAIL_SERVER): messages are written as .eml files to instance/outbox/ and
logged to the console. Nothing is ever auto-verified - the link in the message must still be
opened, so the verification flow is identical to production.
"""
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

from flask import current_app


def send_mail(to, subject, body):
    cfg = current_app.config
    msg = EmailMessage()
    msg["From"] = cfg["MAIL_SENDER"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    if cfg.get("MAIL_SERVER"):
        smtp_cls = smtplib.SMTP_SSL if cfg.get("MAIL_USE_SSL") else smtplib.SMTP
        with smtp_cls(cfg["MAIL_SERVER"], cfg.get("MAIL_PORT", 587), timeout=20) as smtp:
            if cfg.get("MAIL_USE_TLS") and not cfg.get("MAIL_USE_SSL"):
                smtp.starttls()
            if cfg.get("MAIL_USERNAME"):
                smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            smtp.send_message(msg)
        return "smtp"

    outbox = os.path.join(current_app.instance_path, "outbox")
    os.makedirs(outbox, exist_ok=True)
    safe_to = "".join(ch if ch.isalnum() or ch in "@._-" else "_" for ch in to)
    path = os.path.join(outbox, f"{datetime.now():%Y%m%d-%H%M%S-%f}-{safe_to}.eml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(msg.as_string())
    current_app.logger.warning("MAIL_SERVER not configured - email to %s saved to %s", to, path)
    outbox_log = current_app.config.get("_TEST_OUTBOX")
    if outbox_log is not None:
        outbox_log.append({"to": to, "subject": subject, "body": body})
    return "file"
