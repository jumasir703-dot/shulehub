"""
msg_gateway.py — delivery for messaging.py: text messages (SMS) and email.

messaging.py decides WHO gets WHAT; this file only knows HOW to get one message out.
It never touches the database: every function takes the settings dict `cfg`
(the msg_settings rows, see messaging.load_settings) and returns a Result.

SMS providers
    simulated        TEST MODE. Nothing leaves the computer; every message "succeeds".
    africastalking   Africa's Talking (https://api.africastalking.com/version1/messaging).
                     Tick "sandbox" to use their free test environment (username must be "sandbox").
    generic          Any gateway with a web address. You give the URL, the method (POST/GET),
                     whether the body is a form or JSON, a body template, optional headers and
                     the text a successful reply must contain. Placeholders (use them in the
                     URL, body and headers):
                         {to}        +254712345678          {to_plain}  254712345678
                         {message}   the text               {sender}    sender id / short code
                         {api_key}   the API key            {username}  the account username

Email providers
    simulated        TEST MODE.
    smtp             Any mail server (Gmail, Outlook, your host, ...): host, port, security
                     (STARTTLS / SSL / none), user and password.
    brevo            Brevo's web API (formerly Sendinblue), using an API key. Works where
                     SMTP ports are blocked.

Every send returns a Result:
    ok       True when the provider accepted the message
    error    plain-English reason when not ok
    retry    True only for a passing problem (network down, timeout, server busy); a wrong
             password or an invalid number is NOT retried
    ref      the provider's message id (for tracing)
    cost     e.g. "KES 0.8000" when the provider says
    parts    SMS parts billed (1 for email)
"""

import json
import re
import smtplib
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

SMS_PROVIDERS = [
    ("simulated", "Test mode (nothing is sent)"),
    ("africastalking", "Africa's Talking"),
    ("generic", "Other gateway (web address)"),
]
EMAIL_PROVIDERS = [
    ("simulated", "Test mode (nothing is sent)"),
    ("smtp", "Mail server (SMTP)"),
    ("brevo", "Brevo"),
]
_SMS_LABEL = dict(SMS_PROVIDERS)
_EMAIL_LABEL = dict(EMAIL_PROVIDERS)

AT_LIVE_URL = "https://api.africastalking.com/version1/messaging"
AT_SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/messaging"
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
TIMEOUT = 20


@dataclass
class Result:
    ok: bool
    error: str = ""
    retry: bool = False
    ref: str = ""
    cost: str = ""
    parts: int = 1


def _ok(ref="", cost="", parts=1):
    return Result(True, "", False, ref or "", cost or "", parts)


def _fail(error, retry=False):
    return Result(False, error, retry)


# ----------------------------------------------------------------------------
# Phone numbers and email addresses
# ----------------------------------------------------------------------------
def normalize_phone(raw, country="254"):
    """'0712 345 678', '+254712345678', '254712345678', '712345678' -> '+254712345678'.
    Returns None when it can't be a real mobile number. If several numbers were typed
    ('0712345678 / 0722345678') the first one is used. Kenyan numbers (254) must be
    mobile numbers (07xx / 01xx); other countries accept 8-15 digits."""
    text = str(raw or "").strip()
    if not text:
        return None
    text = re.split(r"[/,;]|\bor\b|\band\b", text, maxsplit=1)[0].strip()
    country = re.sub(r"\D", "", str(country or "254")) or "254"
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):
        digits, plus = digits[2:], True
    if not digits:
        return None
    if plus:
        num = digits
    elif digits.startswith("0"):
        num = country + digits[1:]
    elif digits.startswith(country) and len(digits) >= len(country) + 8:
        num = digits
    else:
        num = country + digits
    if num.startswith("254"):
        if not re.fullmatch(r"254[17]\d{8}", num):
            return None
    elif not 8 <= len(num) <= 15:
        return None
    return "+" + num


_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")


def valid_email(raw):
    """The cleaned address, or None. If several were typed, the first valid one is used."""
    for part in re.split(r"[;,]+", str(raw or "").strip()):
        part = part.strip().strip("<>")
        if part and len(part) <= 254 and _EMAIL_RE.match(part):
            return part
    return None


# ----------------------------------------------------------------------------
# SMS text: cleaning and length
# ----------------------------------------------------------------------------
# Smart punctuation pushes a message into UCS-2 (70 chars a part instead of 160), which
# more than doubles what it costs. Swap it for plain look-alikes.
_PUNCT = {"\u2018": "'", "\u2019": "'", "\u201a": ",", "\u201c": '"', "\u201d": '"', "\u201e": '"',
          "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2026": "...", "\u00a0": " ", "\u2022": "-",
          "\u200b": "", "\ufeff": ""}
_GSM_BASIC = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
              "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM_EXT = "^{}\\[~]|€\f"


def sms_clean(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(_PUNCT.get(ch, ch) for ch in text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def sms_info(text):
    """{'encoding': 'GSM-7'|'UCS-2', 'length': n, 'parts': n, 'per_part': n}.
    GSM-7: 160 characters in one part, 153 per part when split. UCS-2 (emoji, accents the
    GSM set lacks): 70 and 67. A few GSM characters (€ { } [ ] ~ ^ | \\) count double."""
    text = str(text or "")
    if all(ch in _GSM_BASIC or ch in _GSM_EXT for ch in text):
        length = sum(2 if ch in _GSM_EXT else 1 for ch in text)
        single, multi, enc = 160, 153, "GSM-7"
    else:
        length = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)       # emoji are 2 UTF-16 units
        single, multi, enc = 70, 67, "UCS-2"
    parts = 0 if length == 0 else 1 if length <= single else -(-length // multi)
    return {"encoding": enc, "length": length, "parts": parts, "per_part": single if parts <= 1 else multi}


# ----------------------------------------------------------------------------
# Is it set up?  -> (ok, label, problem)
# ----------------------------------------------------------------------------
def _url_ok(url):
    p = urllib.parse.urlparse(url or "")
    return p.scheme in ("http", "https") and bool(p.netloc)


def sms_ready(cfg):
    provider = cfg.get("sms_provider") or "simulated"
    label = _SMS_LABEL.get(provider, provider)
    if provider == "simulated":
        return True, label, ""
    if provider == "africastalking":
        missing = [n for k, n in (("at_username", "username"), ("at_api_key", "API key")) if not (cfg.get(k) or "").strip()]
        if missing:
            return False, label, "Africa's Talking " + " and ".join(missing) + " missing"
        if cfg.get("at_sandbox") == "1" and (cfg.get("at_username") or "").strip().lower() != "sandbox":
            return False, label, "sandbox mode needs the username to be \"sandbox\""
        return True, label + (" (sandbox)" if cfg.get("at_sandbox") == "1" else ""), ""
    if provider == "generic":
        if not _url_ok((cfg.get("generic_url") or "").strip()):
            return False, label, "the gateway web address is missing or is not a web address (http:// or https://)"
        return True, label, ""
    return False, label, "unknown text message provider"


def email_ready(cfg):
    provider = cfg.get("email_provider") or "simulated"
    label = _EMAIL_LABEL.get(provider, provider)
    if provider == "simulated":
        return True, label, ""
    if not valid_email(cfg.get("email_from_address")):
        return False, label, "the \"from\" email address is missing or invalid"
    if provider == "smtp":
        if not (cfg.get("smtp_host") or "").strip():
            return False, label, "the mail server address is missing"
        if bool((cfg.get("smtp_user") or "").strip()) != bool(cfg.get("smtp_password")):
            return False, label, "enter both the mail username and password, or leave both empty"
        return True, label, ""
    if provider == "brevo":
        if not (cfg.get("brevo_api_key") or "").strip():
            return False, label, "the Brevo API key is missing"
        return True, label, ""
    return False, label, "unknown email provider"


# ----------------------------------------------------------------------------
# HTTP (one place, so it is easy to test and to keep safe)
# ----------------------------------------------------------------------------
class _NetError(Exception):
    def __init__(self, message, retry=True):
        super().__init__(message)
        self.retry = retry


def _http(method, url, headers=None, data=None, timeout=TIMEOUT):
    """-> (status_code, response_text). Returns error statuses too (so their message can
    be read); raises _NetError when the server could not be reached at all."""
    if not _url_ok(url):
        raise _NetError("The gateway address is not a valid web address.", retry=False)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except (socket.timeout, TimeoutError):
        raise _NetError("The gateway did not answer in time.")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise _NetError("The gateway did not answer in time.")
        if isinstance(reason, ssl.SSLError):
            raise _NetError(f"Secure connection to the gateway failed ({reason}).", retry=False)
        raise _NetError(f"Could not reach the gateway ({reason}).")
    except (ConnectionError, OSError) as e:
        raise _NetError(f"Could not reach the gateway ({e}).")


def _snippet(text, n=160):
    return re.sub(r"\s+", " ", text or "").strip()[:n]


# ----------------------------------------------------------------------------
# SMS
# ----------------------------------------------------------------------------
def send_sms(cfg, to, text):
    """Send one text message. `to` should already be normalize_phone()'d."""
    text = sms_clean(text)
    if not text:
        return _fail("The message is empty.")
    if not normalize_phone(to, cfg.get("country_code") or "254"):
        return _fail("That is not a valid phone number.")
    parts = max(sms_info(text)["parts"], 1)
    provider = cfg.get("sms_provider") or "simulated"
    if provider == "simulated":
        return _ok("TEST-" + uuid.uuid4().hex[:10], "", parts)
    ready, _, problem = sms_ready(cfg)
    if not ready:
        return _fail(f"Text messages are not set up: {problem}.")
    if provider == "africastalking":
        return _send_sms_at(cfg, to, text, parts)
    if provider == "generic":
        return _send_sms_generic(cfg, to, text, parts)
    return _fail("Unknown text message provider.")


# Africa's Talking status codes: https://developers.africastalking.com/docs/sms/sending/bulk
_AT_OK = {100, 101, 102}                       # Processed, Sent, Queued
_AT_RETRY = {500, 501}                         # their server trouble: worth another go
_AT_TEXT = {
    401: "held for review by the provider (risk hold)",
    402: "the sender ID is not valid or not approved",
    403: "the phone number is not valid",
    404: "that kind of number cannot receive texts",
    405: "the Africa's Talking account has no balance left",
    406: "the person has blocked messages from this sender",
    407: "the message could not be routed to that network",
    500: "Africa's Talking had an internal error",
    501: "the mobile network gateway had an error",
    502: "the message was rejected by the mobile network",
}


def _send_sms_at(cfg, to, text, parts):
    sandbox = cfg.get("at_sandbox") == "1"
    form = {"username": cfg["at_username"].strip(), "to": to, "message": text}
    sender = (cfg.get("at_sender_id") or "").strip()
    if sender and not sandbox:
        form["from"] = sender
    headers = {"apiKey": cfg["at_api_key"].strip(), "Accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded"}
    try:
        status, body = _http("POST", AT_SANDBOX_URL if sandbox else AT_LIVE_URL, headers,
                             urllib.parse.urlencode(form).encode())
    except _NetError as e:
        return _fail(str(e), e.retry)
    if status in (401, 403) and "recipients" not in body.lower():
        return _fail("Africa's Talking refused the username or API key. Check them in Messages > Settings.")
    if status >= 500:
        return _fail(f"Africa's Talking is having trouble (HTTP {status}).", retry=True)
    try:
        data = json.loads(body)
        recipients = data["SMSMessageData"]["Recipients"]
        summary = data["SMSMessageData"].get("Message", "")
    except (ValueError, KeyError, TypeError):
        return _fail(f"Africa's Talking sent an unexpected reply (HTTP {status}): {_snippet(body)}")
    if not recipients:
        return _fail(f"Africa's Talking did not accept the message: {_snippet(summary) or 'no recipient was accepted'}.")
    r = recipients[0]
    code = r.get("statusCode")
    if code in _AT_OK:
        return _ok(r.get("messageId", ""), r.get("cost", ""), parts)
    reason = _AT_TEXT.get(code) or (r.get("status") or "rejected")
    return _fail(f"Not sent: {reason}.", retry=code in _AT_RETRY)


_PLACEHOLDER = re.compile(r"\{(to|to_plain|message|sender|api_key|username)\}")


def _fill(template, values, mode):
    """Swap {placeholders} for values, escaped for where they land: 'raw' (form lines,
    headers), 'json' (inside a JSON string) or 'url' (inside a web address)."""
    def esc(v):
        if mode == "json":
            return json.dumps(v)[1:-1]
        if mode == "url":
            return urllib.parse.quote(v, safe="")
        return v
    return _PLACEHOLDER.sub(lambda m: esc(values.get(m.group(1), "")), template or "")


def _send_sms_generic(cfg, to, text, parts):
    values = {"to": to, "to_plain": to.lstrip("+"), "message": text,
              "sender": (cfg.get("generic_sender") or "").strip(),
              "api_key": (cfg.get("generic_api_key") or "").strip(),
              "username": (cfg.get("generic_username") or "").strip()}
    method = "GET" if (cfg.get("generic_method") or "POST").upper() == "GET" else "POST"
    as_json = (cfg.get("generic_content") or "form") == "json"
    url = _fill((cfg.get("generic_url") or "").strip(), values, "url")

    headers = {"Accept": "*/*", "User-Agent": "ShuleHub"}
    for line in (cfg.get("generic_headers") or "").splitlines():
        if ":" in line:
            name, _, val = line.partition(":")
            name, val = name.strip(), _fill(val.strip(), values, "raw")
            if name and "\n" not in val and "\r" not in val:
                headers[name] = val

    template = cfg.get("generic_body") or ""
    data = None
    if method == "GET":
        pairs = _form_pairs(template or _default_form(values), values)
        if pairs:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(pairs)
    elif as_json:
        body = _fill(template or '{"to": "{to}", "message": "{message}"}', values, "json")
        try:
            json.loads(body)
        except ValueError:
            return _fail("The gateway body template is not valid JSON once filled in. Check it in Messages > Settings.")
        data = body.encode()
        headers.setdefault("Content-Type", "application/json")
    else:
        data = urllib.parse.urlencode(_form_pairs(template or _default_form(values), values)).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    try:
        status, body = _http(method, url, headers, data)
    except _NetError as e:
        return _fail(str(e), e.retry)
    if status >= 500 or status == 429:
        return _fail(f"The gateway is busy or having trouble (HTTP {status}).", retry=True)
    if not 200 <= status < 300:
        return _fail(f"The gateway refused it (HTTP {status}): {_snippet(body)}")
    expect = (cfg.get("generic_ok_text") or "").strip()
    if expect and expect.lower() not in body.lower():
        return _fail(f"The gateway replied, but not with the expected success text \"{expect}\": {_snippet(body)}")
    return _ok("", "", parts)


def _default_form(values):
    lines = ["to={to}", "message={message}"]
    if values["sender"]:
        lines.append("from={sender}")
    return "\n".join(lines)


def _form_pairs(template, values):
    """'key=value' lines (or &-joined) -> [(key, value)] with placeholders filled in."""
    pairs = []
    for chunk in re.split(r"[\r\n]+", template or ""):
        # allow "a=1&b=2" on one line, but never split inside a {placeholder} value
        for piece in (chunk.split("&") if "{message}" not in chunk else [chunk]):
            if "=" in piece:
                k, _, v = piece.partition("=")
                if k.strip():
                    pairs.append((k.strip(), _fill(v.strip(), values, "raw")))
    return pairs


# ----------------------------------------------------------------------------
# Email
# ----------------------------------------------------------------------------
def send_email(cfg, to, subject, body, to_name=""):
    """Send one plain-text email."""
    address = valid_email(to)
    if not address:
        return _fail("That is not a valid email address.")
    subject = re.sub(r"[\r\n]+", " ", subject or "").strip() or "(no subject)"
    provider = cfg.get("email_provider") or "simulated"
    if provider == "simulated":
        return _ok("TEST-" + uuid.uuid4().hex[:10])
    ready, _, problem = email_ready(cfg)
    if not ready:
        return _fail(f"Email is not set up: {problem}.")
    to_name = _clean_name(to_name)
    if provider == "smtp":
        return _send_email_smtp(cfg, address, to_name, subject, body or "")
    if provider == "brevo":
        return _send_email_brevo(cfg, address, to_name, subject, body or "")
    return _fail("Unknown email provider.")


def _clean_name(name):
    """A display name that is safe in a header: no line breaks, quotes or angle brackets."""
    return re.sub(r"\s+", " ", re.sub(r"[\r\n<>\"]+", " ", name or "")).strip()


def _from_header(cfg):
    return _clean_name(cfg.get("email_from_name")), valid_email(cfg.get("email_from_address"))


def _send_email_smtp(cfg, to, to_name, subject, body):
    name, from_addr = _from_header(cfg)
    msg = EmailMessage()
    msg["From"] = formataddr((name, from_addr))
    msg["To"] = formataddr((to_name, to))
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    reply_to = valid_email(cfg.get("email_reply_to"))
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)

    host = cfg["smtp_host"].strip()
    try:
        port = int(cfg.get("smtp_port") or 587)
    except ValueError:
        return _fail("The mail server port must be a number (usually 587 or 465).")
    security = cfg.get("smtp_security") or "starttls"
    context = ssl.create_default_context()
    server = None
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
        server.ehlo()
        if security == "starttls":
            server.starttls(context=context)
            server.ehlo()
        user = (cfg.get("smtp_user") or "").strip()
        if user:
            server.login(user, cfg.get("smtp_password") or "")
        server.send_message(msg)
        return _ok(msg["Message-ID"])
    except smtplib.SMTPAuthenticationError:
        return _fail("The mail server refused the username or password. For Gmail or Outlook use an app password.")
    except smtplib.SMTPRecipientsRefused:
        return _fail("The mail server rejected that email address.")
    except smtplib.SMTPSenderRefused:
        return _fail("The mail server rejected the \"from\" address. Use an address your mail account is allowed to send from.")
    except smtplib.SMTPResponseException as e:           # includes SMTPDataError, SMTPConnectError
        text = (e.smtp_error.decode("utf-8", "replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error))
        return _fail(f"The mail server said {e.smtp_code}: {_snippet(text)}", retry=400 <= e.smtp_code < 500)
    except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError):
        return _fail("The mail server did not answer or hung up.", retry=True)
    except ssl.SSLError as e:
        return _fail(f"Secure connection to the mail server failed ({e}). Check the port and the security setting.")
    except (socket.gaierror, ConnectionError, OSError) as e:
        return _fail(f"Could not reach the mail server ({e}).", retry=True)
    except smtplib.SMTPException as e:
        return _fail(f"Email failed: {e}")
    except ValueError as e:                               # e.g. a bad header value
        return _fail(f"The email could not be built: {e}")
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def _send_email_brevo(cfg, to, to_name, subject, body):
    name, from_addr = _from_header(cfg)
    payload = {"sender": {"email": from_addr, **({"name": name} if name else {})},
               "to": [{"email": to, **({"name": to_name} if to_name else {})}],
               "subject": subject, "textContent": body}
    reply_to = valid_email(cfg.get("email_reply_to"))
    if reply_to:
        payload["replyTo"] = {"email": reply_to}
    headers = {"api-key": cfg["brevo_api_key"].strip(), "accept": "application/json",
               "content-type": "application/json"}
    try:
        status, text = _http("POST", BREVO_URL, headers, json.dumps(payload).encode())
    except _NetError as e:
        return _fail(str(e), e.retry)
    if status in (200, 201, 202):
        try:
            ref = json.loads(text).get("messageId", "")
        except (ValueError, AttributeError):
            ref = ""
        return _ok(ref)
    try:
        detail = json.loads(text).get("message", "")
    except (ValueError, AttributeError):
        detail = _snippet(text)
    if status in (401, 403):
        return _fail(f"Brevo refused the API key or the sender address ({detail or 'HTTP ' + str(status)}). "
                     f"The \"from\" address must be a verified sender in Brevo.")
    if status == 429 or status >= 500:
        return _fail(f"Brevo is busy or having trouble (HTTP {status}).", retry=True)
    return _fail(f"Brevo rejected the email (HTTP {status}): {detail}")
