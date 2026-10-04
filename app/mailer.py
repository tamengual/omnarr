"""Outgoing email (optional), for sign-up invitations. Uses the "email" connection
(SMTP host, port, security, username, password, from address)."""
import smtplib
import ssl
from email.message import EmailMessage


def _connect(s, timeout=20):
    host, port = s["host"].strip(), int(s.get("port") or 587)
    security = (s.get("security") or "starttls").lower()
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(host, port, timeout=timeout)
        if security == "starttls":
            smtp.starttls(context=ssl.create_default_context())
    if s.get("username"):
        smtp.login(s["username"], s.get("password") or "")
    return smtp


def test(s):
    """Connect and sign in without sending anything."""
    try:
        with _connect(s):
            pass
    except smtplib.SMTPAuthenticationError:
        return False, "Sign-in rejected: check the username and password (Gmail needs an app password)"
    return True, f"Connected to {s['host']}"


def send(s, to, subject, body):
    msg = EmailMessage()
    msg["From"] = s.get("from_address") or s.get("username")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with _connect(s) as smtp:
        smtp.send_message(msg)
