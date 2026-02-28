from __future__ import annotations

import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _parse_recipients(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        item = (v or "").strip()
        if item:
            out.append(item)
    return out


def send_email(subject: str, body_text: str, to_list: list[str], pdf_path: str | Path | None = None) -> None:
    """
    Send UTF-8 plain text email with optional PDF attachment via SMTP.
    Required env:
      - SMTP_HOST
      - SMTP_PORT
      - SMTP_USER
      - SMTP_PASS
      - SMTP_FROM (fallback: SMTP_USER)
    """
    smtp_host = _env("SMTP_HOST")
    smtp_port_raw = _env("SMTP_PORT", "587")
    smtp_user = _env("SMTP_USER")
    smtp_pass = _env("SMTP_PASS")
    smtp_from = _env("SMTP_FROM") or smtp_user

    try:
        smtp_port = int(smtp_port_raw)
    except Exception as e:
        raise RuntimeError(f"SMTP_PORT is invalid: {smtp_port_raw!r}") from e

    if not smtp_host:
        raise RuntimeError("SMTP_HOST is not set")
    if not smtp_user:
        raise RuntimeError("SMTP_USER is not set")
    if not smtp_pass:
        raise RuntimeError("SMTP_PASS is not set")
    if not smtp_from:
        raise RuntimeError("SMTP_FROM/SMTP_USER is not set")

    recipients = _parse_recipients(to_list or [])
    if not recipients:
        raise RuntimeError("to_list is empty")

    msg = MIMEMultipart()
    msg["From"] = smtp_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text or "", _subtype="plain", _charset="utf-8"))

    if pdf_path:
        p = Path(pdf_path)
        if not p.exists():
            raise RuntimeError(f"Attachment not found: {p}")
        data = p.read_bytes()
        part = MIMEApplication(data, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=p.name)
        msg.attach(part)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except Exception:
                # Some servers may not require/allow STARTTLS on configured port.
                pass
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, recipients, msg.as_string())
    except Exception as e:
        raise RuntimeError(f"SMTP send failed: {e}") from e
