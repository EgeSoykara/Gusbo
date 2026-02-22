import base64
import hashlib
import hmac
import logging
from urllib import error, parse, request

from django.conf import settings

logger = logging.getLogger(__name__)


def normalize_phone_for_whatsapp(raw_phone, default_country_code="+90"):
    if not raw_phone:
        return None

    phone = "".join(ch for ch in str(raw_phone) if ch.isdigit() or ch == "+")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    elif phone.startswith("0"):
        phone = f"{default_country_code}{phone[1:]}"
    elif not phone.startswith("+"):
        phone = f"{default_country_code}{phone}"

    if not phone.startswith("+") or len(phone) < 8:
        return None
    return phone


def send_whatsapp_via_twilio(to_phone, body):
    if not settings.WHATSAPP_NOTIFICATIONS_ENABLED:
        return {"attempted": False, "sent": False, "detail": "notifications-disabled"}

    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    from_whatsapp = settings.TWILIO_WHATSAPP_FROM
    if not account_sid or not auth_token or not from_whatsapp:
        return {"attempted": False, "sent": False, "detail": "missing-twilio-config"}

    to_e164 = normalize_phone_for_whatsapp(to_phone, settings.WHATSAPP_DEFAULT_COUNTRY_CODE)
    if not to_e164:
        return {"attempted": False, "sent": False, "detail": "invalid-phone"}

    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = parse.urlencode(
        {
            "From": f"whatsapp:{from_whatsapp}",
            "To": f"whatsapp:{to_e164}",
            "Body": body,
        }
    ).encode("utf-8")
    auth_header = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("utf-8")

    req = request.Request(endpoint, data=payload, method="POST")
    req.add_header("Authorization", f"Basic {auth_header}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with request.urlopen(req, timeout=settings.WHATSAPP_REQUEST_TIMEOUT_SEC) as resp:
            if 200 <= resp.status < 300:
                return {"attempted": True, "sent": True, "detail": "sent"}
            return {"attempted": True, "sent": False, "detail": f"http-{resp.status}"}
    except error.HTTPError as exc:
        logger.warning("Twilio WhatsApp HTTP error: %s", exc)
        return {"attempted": True, "sent": False, "detail": f"http-error-{exc.code}"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Twilio WhatsApp send failed: %s", exc)
        return {"attempted": True, "sent": False, "detail": "send-failed"}


def strip_whatsapp_prefix(value):
    if value and value.lower().startswith("whatsapp:"):
        return value.split(":", 1)[1]
    return value


def build_twilio_signature(url, payload_items, auth_token):
    signing_data = url
    for key in sorted(payload_items.keys()):
        values = payload_items.getlist(key) if hasattr(payload_items, "getlist") else [payload_items[key]]
        for value in values:
            signing_data += f"{key}{value}"

    digest = hmac.new(
        auth_token.encode("utf-8"),
        signing_data.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def is_valid_twilio_signature(request_obj):
    auth_token = settings.TWILIO_AUTH_TOKEN
    incoming_signature = request_obj.META.get("HTTP_X_TWILIO_SIGNATURE", "")
    if not auth_token or not incoming_signature:
        return False

    request_url = request_obj.build_absolute_uri()
    expected = build_twilio_signature(request_url, request_obj.POST, auth_token)
    return hmac.compare_digest(expected, incoming_signature)


def send_provider_offer_notification(provider, service_request, token):
    message = (
        f"Yeni is talebi var.\n"
        f"Hizmet: {service_request.service_type.name}\n"
        f"Konum: {service_request.city}/{service_request.district}\n"
        f"Musteri: {service_request.customer_name} - {service_request.customer_phone}\n"
        f"Detay: {service_request.details[:180]}\n\n"
        f"Kabul icin: KABUL {token}\n"
        f"Reddetmek icin: RED {token}"
    )
    return send_whatsapp_via_twilio(provider.phone, message)
