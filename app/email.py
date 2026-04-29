"""Email delivery via Resend, themed for Global Comfort Group.

FROM stays on the verified domain; sender identity is surfaced via display
name + reply-to, so recipients see "Lisa Smith via Global Comfort Group"
and replies route back to the admin's real inbox regardless of where it's
hosted.
"""
import os
import html as html_lib
import logging
from typing import Optional, List

import resend

logger = logging.getLogger(__name__)

resend.api_key = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "surveys@hotelsogo-ai.com")
APP_NAME = os.getenv("APP_NAME", "Global Comfort Group")
PUBLIC_APP_URL = os.getenv("PUBLIC_APP_URL", "http://localhost:3000").rstrip("/")


def _from_field(sender_name: Optional[str]) -> str:
    if sender_name:
        return f"{sender_name} via {APP_NAME} <{FROM_EMAIL}>"
    return f"{APP_NAME} <{FROM_EMAIL}>"


def _survey_url(survey_id: str, token: Optional[str] = None) -> str:
    base = f"{PUBLIC_APP_URL}/s/{survey_id}"
    return f"{base}?t={token}" if token else base


# ── GCG brand palette (mail-safe hex; OKLCH not supported in HTML email) ───
FONT_STACK = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
COLOR_BG = "#f5f6f8"
COLOR_CARD = "#ffffff"
COLOR_BORDER = "#e5e7eb"
COLOR_TEXT = "#111827"
COLOR_MUTED = "#6b7280"
COLOR_SUBTLE_BG = "#f9fafb"
GCG_RED = "#C8242E"
GCG_BLUE = "#1E40AF"
COLOR_ACCENT = GCG_RED       # primary CTA, headings
COLOR_ACCENT_2 = GCG_BLUE    # secondary accents (eyebrow, fallback link)


def _cta_button(url: str, label: str) -> str:
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0">
      <tr>
        <td align="center" bgcolor="{COLOR_ACCENT}" style="border-radius:8px;background:{COLOR_ACCENT}">
          <a href="{url}"
             style="display:inline-block;padding:14px 28px;font-family:{FONT_STACK};font-size:15px;font-weight:600;line-height:1;color:#ffffff;text-decoration:none;border-radius:8px">
            {label}&nbsp;&rarr;
          </a>
        </td>
      </tr>
    </table>""".strip()


def _brand_strip() -> str:
    """Top split-bar that mirrors the GCG navbar — half red, half blue."""
    return f"""
      <tr>
        <td style="padding:0;line-height:0;font-size:0">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td width="50%" height="4" bgcolor="{GCG_RED}" style="background:{GCG_RED};line-height:4px;font-size:0">&nbsp;</td>
              <td width="50%" height="4" bgcolor="{GCG_BLUE}" style="background:{GCG_BLUE};line-height:4px;font-size:0">&nbsp;</td>
            </tr>
          </table>
        </td>
      </tr>""".strip()


def _email_shell(*, preheader: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <title>{html_lib.escape(APP_NAME)}</title>
</head>
<body style="margin:0;padding:0;background:{COLOR_BG};-webkit-font-smoothing:antialiased">
  <div style="display:none!important;opacity:0;color:transparent;height:0;width:0;overflow:hidden;mso-hide:all;visibility:hidden">{html_lib.escape(preheader)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{COLOR_BG};padding:32px 12px">
    <tr>
      <td align="center">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;width:100%;background:{COLOR_CARD};border:1px solid {COLOR_BORDER};border-radius:12px;overflow:hidden">
          {_brand_strip()}
          <tr>
            <td style="padding:28px 32px 0;font-family:{FONT_STACK}">
              <div style="font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase">
                <span style="color:{COLOR_ACCENT}">{html_lib.escape(APP_NAME)}</span>
                <span style="color:{COLOR_MUTED};margin:0 6px">·</span>
                <span style="color:{COLOR_ACCENT_2}">customer survey</span>
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:12px 32px 32px;font-family:{FONT_STACK};color:{COLOR_TEXT};font-size:15px;line-height:1.55">
              {body_html}
            </td>
          </tr>
        </table>
        <div style="max-width:560px;margin:12px auto 0;padding:12px 24px;font-family:{FONT_STACK};font-size:12px;color:{COLOR_MUTED};text-align:center">
          Sent by {html_lib.escape(APP_NAME)}. If this wasn't meant for you, feel free to ignore it.
        </div>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _survey_card(survey_title: str) -> str:
    """Survey detail card — red left border, with a short blue accent rule above the title."""
    return f"""
      <div style="margin:0 0 24px;padding:16px 20px;background:{COLOR_SUBTLE_BG};border:1px solid {COLOR_BORDER};border-left:3px solid {COLOR_ACCENT};border-radius:6px">
        <div style="font-family:{FONT_STACK};font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:{COLOR_ACCENT_2};margin:0 0 4px">Survey</div>
        <div style="font-family:{FONT_STACK};font-size:17px;font-weight:600;color:{COLOR_TEXT};line-height:1.35">{html_lib.escape(survey_title)}</div>
      </div>""".strip()


def _fallback_link(url: str) -> str:
    return f"""
      <hr style="border:none;border-top:1px solid {COLOR_BORDER};margin:28px 0 18px">
      <p style="margin:0 0 6px;font-family:{FONT_STACK};font-size:12px;color:{COLOR_MUTED}">Button not working? Copy and paste this link:</p>
      <p style="margin:0;font-family:{FONT_STACK};font-size:12px;word-break:break-all">
        <a href="{url}" style="color:{COLOR_ACCENT_2};text-decoration:underline">{url}</a>
      </p>""".strip()


def _invite_text(survey_title: str, survey_url: str, sender_name: str) -> str:
    """Plain-text alternative — Gmail's classifier treats multipart messages
    with a real text body as transactional, not promotional. This is the
    single biggest factor in Primary vs Promotions routing."""
    return (
        f"Hi,\n\n"
        f"{sender_name} would love a minute of your time on a short survey:\n\n"
        f"  {survey_title}\n\n"
        f"Open the survey: {survey_url}\n\n"
        f"Takes about a minute. Every answer helps.\n\n"
        f"--\n"
        f"{APP_NAME}\n"
        f"Reply directly to this email if you have any questions.\n"
    )


def _reminder_text(survey_title: str, survey_url: str, sender_name: str) -> str:
    return (
        f"Hi,\n\n"
        f"Just a quick reminder — {sender_name} is still hoping to hear from "
        f"you on the survey:\n\n"
        f"  {survey_title}\n\n"
        f"Open the survey: {survey_url}\n\n"
        f"It only takes a minute. Thank you.\n\n"
        f"--\n"
        f"{APP_NAME}\n"
        f"Reply directly to this email if you have any questions.\n"
    )


def _invite_html(survey_title: str, survey_url: str, sender_name: str) -> str:
    safe_sender = html_lib.escape(sender_name)
    body = f"""
      <p style="margin:0 0 10px;font-size:22px;font-weight:700;color:{COLOR_TEXT};line-height:1.3">You're invited to share your feedback.</p>
      <p style="margin:0 0 24px;color:{COLOR_MUTED};font-size:15px;line-height:1.55">
        <strong style="color:{COLOR_TEXT};font-weight:600">{safe_sender}</strong> would love a minute of your time on a short survey.
      </p>
      {_survey_card(survey_title)}
      {_cta_button(survey_url, "Open survey")}
      <p style="margin:14px 0 0;font-size:13px;color:{COLOR_MUTED}">Takes about a minute — every answer helps.</p>
      {_fallback_link(survey_url)}
    """
    preheader = f"{sender_name} invited you to a short survey — about a minute of your time."
    return _email_shell(preheader=preheader, body_html=body)


def _reminder_html(survey_title: str, survey_url: str, sender_name: str) -> str:
    safe_sender = html_lib.escape(sender_name)
    body = f"""
      <p style="margin:0 0 10px;font-size:22px;font-weight:700;color:{COLOR_TEXT};line-height:1.3">A quick nudge &mdash; the survey's still open.</p>
      <p style="margin:0 0 24px;color:{COLOR_MUTED};font-size:15px;line-height:1.55">
        <strong style="color:{COLOR_TEXT};font-weight:600">{safe_sender}</strong> is still hoping to hear from you. It only takes a moment.
      </p>
      {_survey_card(survey_title)}
      {_cta_button(survey_url, "Open survey")}
      <p style="margin:14px 0 0;font-size:13px;color:{COLOR_MUTED}">One minute, and you're done. Thank you.</p>
      {_fallback_link(survey_url)}
    """
    preheader = f"Reminder from {sender_name} — the survey is still open."
    return _email_shell(preheader=preheader, body_html=body)


def _batch_send(*, payload: list, kind: str) -> dict:
    """Send each email individually via Resend.Emails.send so messages look
    like one-to-one correspondence instead of a bulk campaign. Gmail tab
    classifiers route batch-API sends and bulk-pattern emails to Promotions
    far more often; per-recipient single sends ride on transactional rails."""
    if not resend.api_key:
        msg = "RESEND_API_KEY is not configured"
        logger.warning("Resend: %s (skipping %d %s)", msg, len(payload), kind)
        return {"sent": 0, "failed": len(payload), "error": msg}
    if not payload:
        return {"sent": 0, "failed": 0, "error": None}

    print(f"[EMAIL] sending {len(payload)} {kind} via Resend (single sends)")
    sent, failed = 0, 0
    last_error: Optional[str] = None
    for item in payload:
        try:
            resend.Emails.send(item)
            sent += 1
        except Exception as e:
            logger.exception("Resend %s send failed for %s: %s", kind, item.get("to"), e)
            failed += 1
            last_error = str(e)
    return {"sent": sent, "failed": failed, "error": last_error}


def _first_name(full_name: str) -> str:
    return (full_name or "").strip().split(" ")[0] or APP_NAME


Recipient = dict  # {"email": str, "token": Optional[str]}


def _build_payload(
    *, kind: str, recipients: List[Recipient], survey_id: str, survey_title: str,
    sender_name: str, sender_email: str, subject: str,
    html_builder, text_builder,
) -> list:
    """Build per-recipient Resend payload entries, each with its own
    survey URL stamped with that recipient's invite token. Recipients
    without a token (e.g. CSV import use-case) get the bare survey URL."""
    out = []
    for r in recipients:
        addr = r["email"] if isinstance(r, dict) else r
        token = r.get("token") if isinstance(r, dict) else None
        url = _survey_url(survey_id, token=token)
        out.append({
            "from": _from_field(sender_name),
            "to": addr,
            "reply_to": sender_email if sender_email else FROM_EMAIL,
            "subject": subject,
            "html": html_builder(survey_title, url, sender_name),
            "text": text_builder(survey_title, url, sender_name),
        })
    return out


def send_survey_invites_batch(
    *, recipients: List[Recipient], survey_id: str, survey_title: str,
    sender_name: str, sender_email: str,
) -> dict:
    payload = _build_payload(
        kind="invite",
        recipients=recipients,
        survey_id=survey_id,
        survey_title=survey_title,
        sender_name=sender_name,
        sender_email=sender_email,
        subject=f"Quick favour from {_first_name(sender_name)}",
        html_builder=_invite_html,
        text_builder=_invite_text,
    )
    return _batch_send(payload=payload, kind="invites")


def send_survey_reminders_batch(
    *, recipients: List[Recipient], survey_id: str, survey_title: str,
    sender_name: str, sender_email: str,
) -> dict:
    payload = _build_payload(
        kind="reminder",
        recipients=recipients,
        survey_id=survey_id,
        survey_title=survey_title,
        sender_name=sender_name,
        sender_email=sender_email,
        subject=f"Following up — {_first_name(sender_name)}",
        html_builder=_reminder_html,
        text_builder=_reminder_text,
    )
    return _batch_send(payload=payload, kind="reminders")
