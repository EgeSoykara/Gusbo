import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


def send_sms(phone, text):
    phone_value = (phone or "").strip()
    text_value = (text or "").strip()
    webhook_url = getattr(settings, "SMS_WEBHOOK_URL", "").strip()
    webhook_token = getattr(settings, "SMS_WEBHOOK_TOKEN", "").strip()
    debug_fallback = bool(getattr(settings, "SMS_DEBUG_FALLBACK", True))

    if not phone_value or not text_value:
        return {"sent": False, "detail": "missing-phone-or-text"}

    if webhook_url:
        payload = {"to": phone_value, "text": text_value}
        if webhook_token:
            payload["token"] = webhook_token
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                status_code = getattr(response, "status", 200)
                if 200 <= status_code < 300:
                    return {"sent": True, "detail": f"webhook-{status_code}"}
                return {"sent": False, "detail": f"webhook-status-{status_code}"}
        except urllib.error.URLError as error:
            logger.warning("SMS webhook failed: %s", error)
            if not debug_fallback:
                return {"sent": False, "detail": "webhook-error"}

    logger.info("SMS DEBUG -> %s | %s", phone_value, text_value)
    if debug_fallback:
        return {"sent": True, "detail": "debug-fallback"}
    return {"sent": False, "detail": "no-provider-configured"}
