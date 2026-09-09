"""
Sends one email via SMTP. Builds a compliant plain-text message (physical
address + opt-out line -- required by CAN-SPAM for commercial email, not
optional) and threads follow-up steps as replies to email 1 via
In-Reply-To/References, so a sequence shows up as one thread, not four
separate cold emails.

Never raises on a send failure -- returns (False, error) so a batch run
can log it, mark that one lead failed, and keep going.
"""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, formataddr


def build_message(from_name: str, from_addr: str, to_addr: str, subject: str, body: str,
                   reply_to: str, physical_address: str, unsubscribe_text: str,
                   in_reply_to: str = None, references: str = None,
                   unsubscribe_mailto: str = None, unsubscribe_url: str = None) -> tuple:
    """Returns (EmailMessage, message_id).

    unsubscribe_mailto/unsubscribe_url add a List-Unsubscribe header (RFC
    2369) -- Gmail and Yahoo have required this since Feb 2024 for anyone
    sending meaningful bulk volume, and it's a strong deliverability signal
    regardless of volume (a one-click unsubscribe reduces spam complaints,
    which is what actually damages sender reputation). The one-click
    List-Unsubscribe-Post header (RFC 8058) is only added when a URL is
    given -- it's meaningless for a mailto-only target, which has no HTTP
    endpoint to POST to."""
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to_addr
    msg["Reply-To"] = reply_to or from_addr
    msg["Subject"] = subject
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
        # threaded follow-ups conventionally keep the original subject with Re:
        if not subject.lower().startswith("re:"):
            msg.replace_header("Subject", f"Re: {subject}")

    list_unsub_targets = []
    if unsubscribe_url:
        list_unsub_targets.append(f"<{unsubscribe_url}>")
    if unsubscribe_mailto:
        list_unsub_targets.append(f"<mailto:{unsubscribe_mailto}?subject=unsubscribe>")
    if list_unsub_targets:
        msg["List-Unsubscribe"] = ", ".join(list_unsub_targets)
        if unsubscribe_url:
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    footer_lines = ["", "--", physical_address]
    if unsubscribe_text:
        footer_lines.append(unsubscribe_text)
    full_body = body.rstrip() + "\n" + "\n".join(footer_lines)
    msg.set_content(full_body)
    return msg, message_id


def send_email(smtp_host: str, smtp_port: int, smtp_user: str, smtp_password: str,
               from_name: str, to_addr: str, subject: str, body: str, reply_to: str,
               physical_address: str, unsubscribe_text: str,
               in_reply_to: str = None, references: str = None,
               unsubscribe_mailto: str = None, unsubscribe_url: str = None,
               use_starttls: bool = True) -> tuple:
    """Returns (True, message_id) on success, (False, error_message) on failure.
    Never raises -- a batch sender must be able to log-and-continue past one
    bad address without crashing the rest of the run."""
    try:
        msg, message_id = build_message(
            from_name, smtp_user, to_addr, subject, body, reply_to,
            physical_address, unsubscribe_text, in_reply_to, references,
            unsubscribe_mailto, unsubscribe_url,
        )
    except Exception as e:
        return False, f"failed to build message: {e}"

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
            if use_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
        return True, message_id
    except smtplib.SMTPAuthenticationError as e:
        return False, f"auth failed -- check SMTP_USER/SMTP_PASSWORD (app password?): {e}"
    except smtplib.SMTPRecipientsRefused as e:
        return False, f"recipient refused (likely invalid address): {e}"
    except Exception as e:
        return False, f"send failed: {e}"
