"""Deliver queued reviews to a configured internal mailbox through SMTP."""
from email.message import EmailMessage
import os
import smtplib
import ssl


def deliver_pending(store, cfg):
    if not cfg.has_section("mail") or not cfg.getboolean("mail", "enabled", fallback=False):
        return
    section = cfg["mail"]
    sender, recipient = section["sender"].strip(), section["recipient"].strip()
    host = section["host"].strip()
    if not all((host, sender, recipient)):
        raise ValueError("메일 서버·발신 주소·수신 주소가 필요합니다.")
    pending = store.pending_mail()
    if not pending:
        return
    with smtplib.SMTP(host, section.getint("port", 587), timeout=30) as smtp:
        if section.getboolean("starttls", fallback=True):
            smtp.starttls(context=ssl.create_default_context())
        if section.get("username"):
            smtp.login(section["username"], os.environ[section["password_env"]])
        for rid, subject, body in pending:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = sender, recipient, subject
            msg.set_content(body)
            if not store.change_mail_state(rid, "pending", "sending"):
                continue
            # After SMTP accepts DATA, a disconnect can obscure delivery. Retain sending for reconciliation.
            smtp.send_message(msg)
            store.change_mail_state(rid, "sending", "sent")
