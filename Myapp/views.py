import json
import hashlib
import unicodedata
from datetime import timedelta
from math import asin, cos, radians, sin, sqrt
from uuid import uuid4

from django.contrib import messages
from django.conf import settings
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .constants import NC_CITY_DISTRICT_MAP
from .forms import (
    AccountIdentityForm,
    AccountPasswordChangeForm,
    ANY_DISTRICT_VALUE,
    AppointmentCreateForm,
    CustomerContactSettingsForm,
    CustomerLoginForm,
    CustomerSignupForm,
    ProviderAvailabilitySlotForm,
    ProviderProfileForm,
    ProviderLoginForm,
    ProviderSignupForm,
    ProviderRatingForm,
    ServiceRequestForm,
    ServiceSearchForm,
    ServiceMessageForm,
)
from .notifications import build_notification_entries, get_notification_retention_days, mark_all_notifications_read
from .models import (
    CustomerProfile,
    IdempotencyRecord,
    Provider,
    ProviderAvailabilitySlot,
    ProviderOffer,
    ProviderRating,
    SchedulerHeartbeat,
    ServiceAppointment,
    ServiceMessage,
    ServiceRequest,
    ServiceType,
    WorkflowEvent,
)
from .sms import send_sms

PROVIDER_PENDING_APPROVAL_MESSAGE = "Usta hesabınız admin onayı bekliyor."
PROVIDER_PENDING_APPROVAL_FLASH_FLAG = "_provider_pending_approval_warned"


def haversine_km(lat1, lon1, lat2, lon2):
    earth_radius_km = 6371
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * earth_radius_km * asin(sqrt(a))


def build_request_form_initial(request):
    if not request.user.is_authenticated:
        return {}

    profile = getattr(request.user, "customer_profile", None)
    return {
        "customer_name": request.user.get_full_name() or request.user.username,
        "customer_phone": profile.phone if profile else "",
        "city": profile.city if profile else "",
        "district": profile.district if profile else "",
    }


def paginate_items(request, items, *, per_page=12, page_param="page"):
    paginator = Paginator(items, per_page)
    return paginator.get_page(request.GET.get(page_param))


def get_provider_for_user(user):
    if not user.is_authenticated:
        return None
    return Provider.objects.filter(user=user).first()


def queue_provider_pending_approval_warning(request):
    request.session[PROVIDER_PENDING_APPROVAL_FLASH_FLAG] = True
    messages.warning(request, PROVIDER_PENDING_APPROVAL_MESSAGE)


def get_verified_provider_or_redirect(request, *, redirect_name="provider_login", api=False):
    provider = get_provider_for_user(request.user)
    if not provider:
        if api:
            return None, JsonResponse({"detail": "forbidden"}, status=403)
        messages.error(request, "Bu alan sadece usta hesapları içindir.")
        return None, redirect(redirect_name)
    if not provider.is_verified:
        if api:
            return None, JsonResponse({"detail": "pending-approval"}, status=403)
        queue_provider_pending_approval_warning(request)
        return None, redirect(redirect_name)
    return provider, None


def get_city_district_map_json():
    return json.dumps(NC_CITY_DISTRICT_MAP)


def get_popular_service_types(limit=6):
    return list(
        ServiceType.objects.annotate(request_count=Count("requests", distinct=True))
        .order_by("-request_count", "name")[:limit]
    )


def get_first_form_error(form):
    for field_errors in form.errors.values():
        if field_errors:
            return field_errors[0]
    return "Formdaki alanlari kontrol edip tekrar deneyin."


def get_offer_expiry_minutes():
    return max(1, int(getattr(settings, "OFFER_EXPIRY_MINUTES", 180)))


def get_offer_reminder_minutes():
    return max(1, int(getattr(settings, "OFFER_REMINDER_MINUTES", 60)))


def get_appointment_provider_confirm_minutes():
    return max(1, int(getattr(settings, "APPOINTMENT_PROVIDER_CONFIRM_MINUTES", 720)))


def get_appointment_customer_confirm_minutes():
    return max(1, int(getattr(settings, "APPOINTMENT_CUSTOMER_CONFIRM_MINUTES", 720)))




def get_login_rate_limit_max_attempts():
    return max(1, int(getattr(settings, "LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 15)))


def get_login_rate_limit_window_seconds():
    return max(10, int(getattr(settings, "LOGIN_RATE_LIMIT_WINDOW_SECONDS", 60)))


def get_action_rate_limit_max_attempts():
    return max(1, int(getattr(settings, "ACTION_RATE_LIMIT_MAX_ATTEMPTS", 40)))


def get_action_rate_limit_window_seconds():
    return max(10, int(getattr(settings, "ACTION_RATE_LIMIT_WINDOW_SECONDS", 60)))


def get_post_idempotency_ttl_seconds():
    return max(3, int(getattr(settings, "POST_IDEMPOTENCY_TTL_SECONDS", 10)))


def get_lifecycle_heartbeat_stale_seconds():
    return max(10, int(getattr(settings, "LIFECYCLE_HEARTBEAT_STALE_SECONDS", 180)))


def infer_actor_role(user):
    if not user or not getattr(user, "is_authenticated", False):
        return "system"
    return "provider" if get_provider_for_user(user) else "customer"


def get_client_ip(request):
    forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip() or "unknown"
    return (request.META.get("REMOTE_ADDR") or "").strip() or "unknown"


def reject_rate_limited_request(request, scope, redirect_name, *, max_attempts, window_seconds, identity=""):
    if request.method != "POST":
        return None

    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key or "no-session"
    ip_address = get_client_ip(request)
    user_marker = f"user:{request.user.id}" if request.user.is_authenticated else "anon"
    normalized_identity = (identity or "").strip().lower()
    raw_key = json.dumps(
        {
            "scope": scope,
            "path": request.path,
            "ip": ip_address,
            "session": session_key,
            "user": user_marker,
            "identity": normalized_identity,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cache_key = f"rl:{hashlib.sha256(raw_key.encode('utf-8')).hexdigest()}"

    try:
        is_new = cache.add(cache_key, 1, timeout=window_seconds)
        if is_new:
            hit_count = 1
        else:
            try:
                hit_count = cache.incr(cache_key)
            except ValueError:
                previous = int(cache.get(cache_key) or 0)
                hit_count = previous + 1
                cache.set(cache_key, hit_count, timeout=window_seconds)
    except Exception:
        return None

    if hit_count > max_attempts:
        messages.warning(
            request,
            f"Çok kısa sürede çok fazla istek gönderdiniz. Lütfen {window_seconds} saniye sonra tekrar deneyin.",
        )
        return redirect(redirect_name)
    return None


def create_workflow_event(
    instance,
    *,
    from_status,
    to_status,
    actor_user=None,
    actor_role="system",
    source="system",
    note="",
):
    if isinstance(instance, ServiceRequest):
        WorkflowEvent.objects.create(
            target_type="request",
            service_request=instance,
            appointment=None,
            from_status=from_status,
            to_status=to_status,
            actor_user=actor_user,
            actor_role=actor_role,
            source=source,
            note=note[:240],
        )
        return

    if isinstance(instance, ServiceAppointment):
        WorkflowEvent.objects.create(
            target_type="appointment",
            service_request=instance.service_request if instance.service_request_id else None,
            appointment=instance,
            from_status=from_status,
            to_status=to_status,
            actor_user=actor_user,
            actor_role=actor_role,
            source=source,
            note=note[:240],
        )


def reject_duplicate_submission(request, scope, redirect_name):
    if request.method != "POST":
        return None

    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key or "no-session"
    user_id = request.user.id if request.user.is_authenticated else None
    identity = f"user:{user_id}" if user_id else f"session:{session_key}"

    payload = {}
    for key in sorted(request.POST.keys()):
        if key in {"csrfmiddlewaretoken", "idempotency_key"}:
            continue
        payload[key] = request.POST.getlist(key)
    explicit_key = (request.POST.get("idempotency_key") or request.headers.get("X-Idempotency-Key") or "").strip()
    ttl_seconds = get_post_idempotency_ttl_seconds()
    window = int(timezone.now().timestamp() // ttl_seconds)
    raw_key = json.dumps(
        {
            "scope": scope,
            "endpoint": request.path,
            "identity": identity,
            "window": window,
            "payload": payload,
            "explicit": explicit_key,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    dedupe_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    now = timezone.now()
    try:
        with transaction.atomic():
            IdempotencyRecord.objects.create(
                key=dedupe_key,
                scope=scope[:80],
                endpoint=request.path[:200],
                user=request.user if request.user.is_authenticated else None,
            )
    except IntegrityError:
        messages.info(request, "Aynı işlem kısa aralıkta tekrar gönderildiği için tek sefer işlendi.")
        return redirect(redirect_name)

    if now.second < 2:
        IdempotencyRecord.objects.filter(created_at__lt=now - timedelta(days=2)).delete()
    return None


SERVICE_REQUEST_ALLOWED_TRANSITIONS = {
    "new": {"pending_provider", "cancelled"},
    "pending_provider": {"pending_customer", "matched", "new", "cancelled"},
    "pending_customer": {"pending_provider", "matched", "new", "cancelled"},
    "matched": {"completed", "pending_provider", "pending_customer", "new"},
    "completed": set(),
    "cancelled": set(),
}

SERVICE_APPOINTMENT_ALLOWED_TRANSITIONS = {
    "pending": {"pending_customer", "confirmed", "rejected", "cancelled", "completed"},
    "pending_customer": {"confirmed", "cancelled", "pending", "completed"},
    "confirmed": {"completed", "cancelled", "pending"},
    "rejected": {"pending"},
    "cancelled": {"pending"},
    "completed": set(),
}


def transition_model_status(
    instance,
    next_status,
    allowed_transitions,
    extra_update_fields=None,
    *,
    actor_user=None,
    actor_role="system",
    source="system",
    note="",
):
    current_status = instance.status
    changed = current_status != next_status
    if current_status != next_status:
        allowed_targets = allowed_transitions.get(current_status, set())
        if next_status not in allowed_targets:
            return False
        instance.status = next_status

    if extra_update_fields is None:
        if changed:
            instance.save(update_fields=["status"])
        if changed:
            create_workflow_event(
                instance,
                from_status=current_status,
                to_status=next_status,
                actor_user=actor_user,
                actor_role=actor_role,
                source=source,
                note=note,
            )
        return True

    update_fields = list(extra_update_fields)
    if changed:
        update_fields = ["status", *update_fields]
    update_fields = list(dict.fromkeys(update_fields))
    if update_fields:
        instance.save(update_fields=update_fields)
    if changed:
        create_workflow_event(
            instance,
            from_status=current_status,
            to_status=next_status,
            actor_user=actor_user,
            actor_role=actor_role,
            source=source,
            note=note,
        )
    return True


def transition_service_request_status(
    service_request,
    next_status,
    extra_update_fields=None,
    *,
    actor_user=None,
    actor_role="system",
    source="system",
    note="",
):
    return transition_model_status(
        service_request,
        next_status,
        SERVICE_REQUEST_ALLOWED_TRANSITIONS,
        extra_update_fields=extra_update_fields,
        actor_user=actor_user,
        actor_role=actor_role,
        source=source,
        note=note,
    )


def transition_appointment_status(
    appointment,
    next_status,
    extra_update_fields=None,
    *,
    actor_user=None,
    actor_role="system",
    source="system",
    note="",
):
    return transition_model_status(
        appointment,
        next_status,
        SERVICE_APPOINTMENT_ALLOWED_TRANSITIONS,
        extra_update_fields=extra_update_fields,
        actor_user=actor_user,
        actor_role=actor_role,
        source=source,
        note=note,
    )




def refresh_offer_lifecycle():
    now = timezone.now()
    expired_request_ids = set()
    expiry_minutes = get_offer_expiry_minutes()

    pending_without_expiry = list(
        ProviderOffer.objects.filter(status="pending", expires_at__isnull=True).only("id", "sent_at")
    )
    for offer in pending_without_expiry:
        base_time = offer.sent_at or now
        offer.expires_at = base_time + timedelta(minutes=expiry_minutes)
        offer.save(update_fields=["expires_at"])

    expired_qs = ProviderOffer.objects.filter(status="pending", expires_at__isnull=False, expires_at__lte=now)
    expired_request_ids.update(expired_qs.values_list("service_request_id", flat=True))
    expired_qs.update(status="expired", responded_at=now)

    # Unverified providers must never stay in customer offer flow.
    unverified_offer_qs = ProviderOffer.objects.filter(
        status__in=["pending", "accepted"],
        provider__is_verified=False,
    )
    expired_request_ids.update(unverified_offer_qs.values_list("service_request_id", flat=True))
    unverified_offer_qs.update(status="expired", responded_at=now)

    reminder_deadline = now + timedelta(minutes=get_offer_reminder_minutes())
    reminder_qs = list(
        ProviderOffer.objects.filter(
            status="pending",
            expires_at__isnull=False,
            expires_at__gt=now,
            expires_at__lte=reminder_deadline,
            reminder_sent_at__isnull=True,
        ).select_related("provider", "service_request", "service_request__service_type")
    )
    for offer in reminder_qs:
        send_sms(
            offer.provider.phone,
            (
                f"UstaBul hatirlatma: Talep #{offer.service_request_id} icin teklif bekleniyor. "
                f"Sure sonu: {timezone.localtime(offer.expires_at).strftime('%d.%m %H:%M')}"
            ),
        )
        offer.reminder_sent_at = now
        offer.save(update_fields=["reminder_sent_at"])

    matched_unverified_requests = list(
        ServiceRequest.objects.filter(status="matched", matched_provider__is_verified=False).select_related("service_type")
    )
    for service_request in matched_unverified_requests:
        open_appointments = ServiceAppointment.objects.filter(
            service_request=service_request,
            status__in=["pending", "pending_customer", "confirmed"],
        )
        for appointment in open_appointments:
            transition_appointment_status(
                appointment,
                "cancelled",
                extra_update_fields=["updated_at"],
                actor_role="system",
                source="scheduler",
                note="Usta admin onaylı olmadığı için randevu kapatıldı",
            )

        service_request.matched_provider = None
        service_request.matched_offer = None
        service_request.matched_at = None

        has_accepted = service_request.provider_offers.filter(status="accepted", provider__is_verified=True).exists()
        has_pending = service_request.provider_offers.filter(status="pending", provider__is_verified=True).exists()
        next_status = "new"
        if has_accepted:
            next_status = "pending_customer"
        elif has_pending:
            next_status = "pending_provider"

        if transition_service_request_status(
            service_request,
            next_status,
            extra_update_fields=["matched_provider", "matched_offer", "matched_at"],
            actor_role="system",
            source="scheduler",
            note="Usta admin onaylı olmadığı için eşleşme kaldırıldı",
        ) and next_status == "new":
            dispatch_next_provider_offer(
                service_request,
                actor_role="system",
                source="scheduler",
                note="Onaysız usta eşleşmesi kaldırıldı, yeni aday arandı",
            )

    if not expired_request_ids:
        return

    impacted_requests = list(ServiceRequest.objects.filter(id__in=expired_request_ids).select_related("service_type"))
    for service_request in impacted_requests:
        if service_request.status in {"matched", "completed", "cancelled"} or service_request.matched_provider_id:
            continue

        has_pending = service_request.provider_offers.filter(status="pending", provider__is_verified=True).exists()
        has_accepted = service_request.provider_offers.filter(status="accepted", provider__is_verified=True).exists()
        if has_accepted:
            if service_request.status != "pending_customer":
                transition_service_request_status(
                    service_request,
                    "pending_customer",
                    actor_role="system",
                    source="scheduler",
                    note="Teklif süre sonu kontrolü",
                )
            continue
        if has_pending:
            if service_request.status != "pending_provider":
                transition_service_request_status(
                    service_request,
                    "pending_provider",
                    actor_role="system",
                    source="scheduler",
                    note="Teklif süre sonu kontrolü",
                )
            continue
        dispatch_next_provider_offer(
            service_request,
            actor_role="system",
            source="scheduler",
            note="Tüm bekleyen teklifler süre doldu",
        )


def refresh_appointment_lifecycle():
    now = timezone.now()
    provider_deadline = now - timedelta(minutes=get_appointment_provider_confirm_minutes())
    customer_deadline = now - timedelta(minutes=get_appointment_customer_confirm_minutes())

    stale_provider_appointments = list(
        ServiceAppointment.objects.filter(status="pending", created_at__lte=provider_deadline).select_related(
            "service_request",
            "provider",
        )
    )
    for appointment in stale_provider_appointments:
        if not transition_appointment_status(
            appointment,
            "cancelled",
            extra_update_fields=["updated_at"],
            actor_role="system",
            source="scheduler",
            note="Usta onay süresi doldu",
        ):
            continue
        send_sms(
            appointment.service_request.customer_phone,
            f"UstaBul: Talep #{appointment.service_request_id} randevusu, usta onay suresi asildigi icin iptal edildi.",
        )

    stale_customer_appointments = list(
        ServiceAppointment.objects.filter(status="pending_customer", updated_at__lte=customer_deadline).select_related(
            "service_request",
            "provider",
        )
    )
    for appointment in stale_customer_appointments:
        if not transition_appointment_status(
            appointment,
            "cancelled",
            extra_update_fields=["updated_at"],
            actor_role="system",
            source="scheduler",
            note="Müşteri onay süresi doldu",
        ):
            continue
        send_sms(
            appointment.provider.phone,
            f"UstaBul: Talep #{appointment.service_request_id} randevusu, musteri onay suresi asildigi icin iptal edildi.",
        )


def refresh_marketplace_lifecycle():
    refresh_offer_lifecycle()
    refresh_appointment_lifecycle()


def build_unread_message_map(service_request_ids, viewer_role):
    if not service_request_ids:
        return {}
    unread_rows = (
        ServiceMessage.objects.filter(service_request_id__in=service_request_ids, read_at__isnull=True)
        .exclude(sender_role=viewer_role)
        .values("service_request_id")
        .annotate(total=Count("id"))
    )
    return {row["service_request_id"]: row["total"] for row in unread_rows}


def build_customer_requests_signature(user):
    request_rows = list(
        user.service_requests.values_list("id", "status", "matched_provider_id", "matched_offer_id", "matched_at").order_by("id")
    )
    request_ids = [row[0] for row in request_rows]
    if not request_ids:
        return "empty"

    offer_rows = list(
        ProviderOffer.objects.filter(service_request_id__in=request_ids, provider__is_verified=True)
        .values_list("service_request_id", "provider_id", "status", "responded_at")
        .order_by("service_request_id", "provider_id")
    )
    appointment_rows = list(
        ServiceAppointment.objects.filter(service_request_id__in=request_ids)
        .values_list("service_request_id", "status", "scheduled_for", "updated_at")
        .order_by("service_request_id")
    )
    rating_rows = list(
        ProviderRating.objects.filter(service_request_id__in=request_ids)
        .values_list("service_request_id", "score", "updated_at")
        .order_by("service_request_id")
    )
    unread_rows = list(
        ServiceMessage.objects.filter(service_request_id__in=request_ids, read_at__isnull=True)
        .exclude(sender_role="customer")
        .values("service_request_id")
        .annotate(total=Count("id"))
        .order_by("service_request_id")
    )
    payload = {
        "requests": request_rows,
        "offers": offer_rows,
        "appointments": appointment_rows,
        "ratings": rating_rows,
        "unread": unread_rows,
    }
    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def build_customer_snapshot_payload(user):
    request_ids = list(user.service_requests.values_list("id", flat=True))
    snapshot = {
        "signature": build_customer_requests_signature(user),
        "pending_customer_requests_count": 0,
        "matched_requests_count": 0,
        "pending_customer_appointments_count": 0,
        "confirmed_appointments_count": 0,
        "accepted_offers_count": 0,
        "unread_messages_count": 0,
    }
    if not request_ids:
        return snapshot

    snapshot["pending_customer_requests_count"] = ServiceRequest.objects.filter(
        id__in=request_ids,
        status="pending_customer",
    ).count()
    snapshot["matched_requests_count"] = ServiceRequest.objects.filter(
        id__in=request_ids,
        status="matched",
    ).count()
    snapshot["pending_customer_appointments_count"] = ServiceAppointment.objects.filter(
        service_request_id__in=request_ids,
        status="pending_customer",
    ).count()
    snapshot["confirmed_appointments_count"] = ServiceAppointment.objects.filter(
        service_request_id__in=request_ids,
        status="confirmed",
    ).count()
    snapshot["accepted_offers_count"] = ProviderOffer.objects.filter(
        service_request_id__in=request_ids,
        status="accepted",
        provider__is_verified=True,
    ).count()
    snapshot["unread_messages_count"] = ServiceMessage.objects.filter(
        service_request_id__in=request_ids,
        read_at__isnull=True,
    ).exclude(sender_role="customer").count()
    return snapshot


def build_customer_flow_state(service_request, appointment, *, has_accepted_offers=False, now=None):
    reference_time = now or timezone.now()
    flow = {
        "step": "Adım 1/4",
        "title": "Usta yanıtı bekleniyor",
        "hint": "Talebiniz uygun ustalara iletildi. Usta dönüşlerini bekleyin.",
        "next_action": "Şimdilik bekleyin veya isterseniz talebi iptal edin.",
        "tone": "waiting",
    }
    status = service_request.status

    if status == "pending_customer":
        flow.update(
            {
                "step": "Adım 2/4",
                "title": "Usta seçimi sizde",
                "hint": "Teklifler geldiyse bir ustayı seçip randevu aşamasına geçin.",
                "next_action": "Listeden bir ustayı seçin.",
                "tone": "action",
            }
        )
        if not has_accepted_offers:
            flow.update(
                {
                    "title": "Teklifler hazırlanıyor",
                    "hint": "Henüz seçilebilir teklif oluşmadı.",
                    "next_action": "Ustalardan yeni teklif gelmesini bekleyin.",
                    "tone": "waiting",
                }
            )
        return flow

    if status == "matched":
        flow.update(
            {
                "step": "Adım 3/4",
                "title": "Usta seçildi",
                "hint": "Şimdi randevu zamanını belirleyin.",
                "next_action": "Randevu saati seçip ustaya gönderin.",
                "tone": "action",
            }
        )
        if not appointment:
            return flow

        appointment_status = appointment.status
        if appointment_status == "pending":
            flow.update(
                {
                    "title": "Randevu usta onayında",
                    "hint": "Randevu talebiniz ustaya iletildi.",
                    "next_action": "Ustanın randevu onayını bekleyin.",
                    "tone": "waiting",
                }
            )
        elif appointment_status == "pending_customer":
            flow.update(
                {
                    "title": "Randevu son onay aşamasında",
                    "hint": "Randevu kaydı müşteri onayı bekliyor.",
                    "next_action": "Randevu bölümünden onaylayın veya güncelleyin.",
                    "tone": "action",
                }
            )
        elif appointment_status == "confirmed":
            is_future = bool(appointment.scheduled_for and appointment.scheduled_for > reference_time)
            if is_future:
                flow.update(
                    {
                        "title": "Randevu onaylandı",
                        "hint": "Randevu tarihi netleşti. Saatinde hazır olmanız yeterli.",
                        "next_action": "Randevu sonrası işi tamamlandı olarak işaretleyin.",
                        "tone": "success",
                    }
                )
            else:
                flow.update(
                    {
                        "title": "Randevu saati geldi",
                        "hint": "İş tamamlandıysa talebi kapatabilirsiniz.",
                        "next_action": "İş bittiyse Tamamlandı butonunu kullanın.",
                        "tone": "action",
                    }
                )
        elif appointment_status in {"rejected", "cancelled"}:
            flow.update(
                {
                    "title": "Randevu yeniden planlanmalı",
                    "hint": "Mevcut randevu aktif değil.",
                    "next_action": "Yeni bir randevu saati belirleyin.",
                    "tone": "danger",
                }
            )
        elif appointment_status == "completed":
            flow.update(
                {
                    "title": "Randevu tamamlandı",
                    "hint": "İş kapatma aşamasına geçebilirsiniz.",
                    "next_action": "Talep tamamlandıysa puanlama yapabilirsiniz.",
                    "tone": "success",
                }
            )
        return flow

    if status == "completed":
        return {
            "step": "Adım 4/4",
            "title": "İş tamamlandı",
            "hint": "Talep başarıyla tamamlandı.",
            "next_action": "Ustayı puanlayarak süreci bitirebilirsiniz.",
            "tone": "success",
        }

    if status == "cancelled":
        return {
            "step": "Kapalı",
            "title": "Talep iptal edildi",
            "hint": "Bu talep artık aktif değil.",
            "next_action": "Gerekirse yeni bir talep oluşturun.",
            "tone": "muted",
        }

    return flow


def purge_request_messages(service_request_id):
    ServiceMessage.objects.filter(service_request_id=service_request_id).delete()


def score_accepted_offers(offers):
    if not offers:
        return []

    max_sequence = max((offer.sequence or 1) for offer in offers) or 1

    for offer in offers:
        rating_score = max(0.0, min(70.0, (float(offer.provider.rating) / 5.0) * 70.0))
        if max_sequence <= 1:
            speed_score = 30.0
        else:
            speed_score = max(0.0, min(30.0, ((max_sequence - (offer.sequence or 1)) / (max_sequence - 1)) * 30.0))

        offer.rating_score = round(rating_score, 1)
        offer.speed_score = round(speed_score, 1)
        offer.comparison_score = round(offer.rating_score + offer.speed_score, 1)

    return sorted(
        offers,
        key=lambda offer: (
            -(offer.comparison_score),
            -float(offer.provider.rating),
            offer.sequence or 1,
        ),
    )


def generate_offer_token():
    token = uuid4().hex[:10].upper()
    while ProviderOffer.objects.filter(token=token).exists():
        token = uuid4().hex[:10].upper()
    return token


def build_provider_candidate_groups(service_request):
    base_qs = Provider.objects.filter(
        is_verified=True,
        is_available=True,
        service_types=service_request.service_type,
        city__iexact=service_request.city,
    ).prefetch_related("service_types")

    if service_request.district == ANY_DISTRICT_VALUE:
        return [list(base_qs.order_by("-rating", "full_name"))]

    district_first = list(base_qs.filter(district__iexact=service_request.district).order_by("-rating", "full_name"))
    remaining_city = list(
        base_qs.exclude(id__in=[provider.id for provider in district_first]).order_by("-rating", "full_name")
    )

    groups = []
    if district_first:
        groups.append(district_first)
    if remaining_city:
        groups.append(remaining_city)
    return groups


def set_other_pending_offers_expired(service_request, exclude_offer_id):
    pending_qs = service_request.provider_offers.filter(status__in=["pending", "accepted"]).exclude(id=exclude_offer_id)
    pending_qs.update(status="expired", responded_at=timezone.now())


def dispatch_next_provider_offer(service_request, actor_user=None, actor_role="system", source="system", note=""):
    groups = build_provider_candidate_groups(service_request)
    if not groups:
        service_request.matched_provider = None
        service_request.matched_offer = None
        service_request.matched_at = None
        if not transition_service_request_status(
            service_request,
            "new",
            extra_update_fields=["matched_provider", "matched_offer", "matched_at"],
            actor_user=actor_user,
            actor_role=actor_role,
            source=source,
            note=note,
        ):
            return {"result": "invalid-state"}
        return {"result": "no-candidates"}

    offered_provider_ids = set(service_request.provider_offers.values_list("provider_id", flat=True))
    now = timezone.now()

    for group in groups:
        next_providers = [provider for provider in group if provider.id not in offered_provider_ids]
        if not next_providers:
            continue

        next_sequence = service_request.provider_offers.count() + 1
        created_offers = []
        expiry_minutes = get_offer_expiry_minutes()
        expires_at = now + timedelta(minutes=expiry_minutes)
        for provider in next_providers:
            created_offers.append(
                ProviderOffer.objects.create(
                    service_request=service_request,
                    provider=provider,
                    token=generate_offer_token(),
                    sequence=next_sequence,
                    status="pending",
                    last_delivery_detail="in-app-queue",
                    sent_at=now,
                    expires_at=expires_at,
                    reminder_sent_at=None,
                )
            )
            next_sequence += 1

        service_request.matched_provider = None
        service_request.matched_offer = None
        service_request.matched_at = None
        if not transition_service_request_status(
            service_request,
            "pending_provider",
            extra_update_fields=["matched_provider", "matched_offer", "matched_at"],
            actor_user=actor_user,
            actor_role=actor_role,
            source=source,
            note=note,
        ):
            return {"result": "invalid-state"}
        return {"result": "offers-created", "offers": created_offers}

    service_request.matched_provider = None
    service_request.matched_offer = None
    service_request.matched_at = None
    if not transition_service_request_status(
        service_request,
        "new",
        extra_update_fields=["matched_provider", "matched_offer", "matched_at"],
        actor_user=actor_user,
        actor_role=actor_role,
        source=source,
        note=note,
    ):
        return {"result": "invalid-state"}
    return {"result": "all-contacted"}


@never_cache
@ensure_csrf_cookie
def index(request):
    refresh_marketplace_lifecycle()
    is_provider_user = bool(get_provider_for_user(request.user)) if request.user.is_authenticated else False
    search_form = ServiceSearchForm(request.GET or None)
    provider_page_size_options = [12, 24, 48, 96]
    provider_page_size_raw = (request.GET.get("provider_page_size") or "").strip()
    if provider_page_size_raw.isdigit():
        provider_page_size = int(provider_page_size_raw)
        if provider_page_size not in provider_page_size_options:
            provider_page_size = 24
    else:
        provider_page_size = 24
    providers_qs = (
        Provider.objects.filter(is_verified=True, is_available=True)
        .prefetch_related("service_types")
        .annotate(
            ratings_count=Count("ratings", distinct=True),
            active_slot_count=Count("availability_slots", filter=Q(availability_slots__is_active=True), distinct=True),
        )
    )
    location_used = False
    location_sorted = False
    selected_sort_label = "Önerilen"

    if search_form.is_valid():
        query_text = (search_form.cleaned_data.get("query") or "").strip()
        service_type = search_form.cleaned_data.get("service_type")
        city = (search_form.cleaned_data.get("city") or "").strip()
        district = (search_form.cleaned_data.get("district") or "").strip()
        sort_by = (search_form.cleaned_data.get("sort_by") or "relevance").strip() or "relevance"
        min_rating = search_form.cleaned_data.get("min_rating")
        min_reviews = search_form.cleaned_data.get("min_reviews")
        has_schedule = bool(search_form.cleaned_data.get("has_schedule"))
        user_latitude = search_form.cleaned_data.get("latitude")
        user_longitude = search_form.cleaned_data.get("longitude")
        requires_distinct = False

        if service_type:
            providers_qs = providers_qs.filter(service_types=service_type)
        if query_text:
            providers_qs = providers_qs.filter(
                Q(full_name__icontains=query_text)
                | Q(description__icontains=query_text)
                | Q(service_types__name__icontains=query_text)
            )
            requires_distinct = True
        if city:
            providers_qs = providers_qs.filter(city__icontains=city)
        if district and district != ANY_DISTRICT_VALUE:
            providers_qs = providers_qs.filter(district__icontains=district)
        if min_rating is not None:
            providers_qs = providers_qs.filter(rating__gte=min_rating)
        if min_reviews is not None:
            providers_qs = providers_qs.filter(ratings_count__gte=min_reviews)
        if has_schedule:
            providers_qs = providers_qs.filter(active_slot_count__gt=0)
        if requires_distinct:
            providers_qs = providers_qs.distinct()

        if sort_by not in {"relevance", "distance", "rating_desc", "reviews_desc", "newest", "name_asc"}:
            sort_by = "relevance"
        if sort_by == "distance" and (user_latitude is None or user_longitude is None):
            sort_by = "relevance"

        selected_sort_label = dict(search_form.fields["sort_by"].choices).get(sort_by, "Önerilen")
        if user_latitude is not None and user_longitude is not None:
            location_used = True

        if location_used and sort_by in {"relevance", "distance"}:
            location_sorted = True
            providers = list(providers_qs[:2000])
            for provider in providers:
                if provider.latitude is not None and provider.longitude is not None:
                    provider.distance_km = round(
                        haversine_km(
                            float(user_latitude),
                            float(user_longitude),
                            float(provider.latitude),
                            float(provider.longitude),
                        ),
                        1,
                    )
                else:
                    provider.distance_km = None
            providers.sort(
                key=lambda p: (
                    p.distance_km is None,
                    p.distance_km if p.distance_km is not None else 10**9,
                    -float(p.rating),
                    -int(getattr(p, "ratings_count", 0) or 0),
                    (p.full_name or "").lower(),
                )
            )
            provider_page_obj = paginate_items(
                request,
                providers,
                per_page=provider_page_size,
                page_param="provider_page",
            )
            providers = list(provider_page_obj.object_list)
        else:
            if sort_by == "reviews_desc":
                providers_qs = providers_qs.order_by("-ratings_count", "-rating", "full_name", "id")
            elif sort_by == "newest":
                providers_qs = providers_qs.order_by("-created_at", "-rating", "full_name", "id")
            elif sort_by == "name_asc":
                providers_qs = providers_qs.order_by("full_name", "-rating", "id")
            else:
                providers_qs = providers_qs.order_by("-rating", "-ratings_count", "full_name", "id")

            provider_page_obj = paginate_items(
                request,
                providers_qs,
                per_page=provider_page_size,
                page_param="provider_page",
            )
            providers = list(provider_page_obj.object_list)
            for provider in providers:
                if location_used and provider.latitude is not None and provider.longitude is not None:
                    provider.distance_km = round(
                        haversine_km(
                            float(user_latitude),
                            float(user_longitude),
                            float(provider.latitude),
                            float(provider.longitude),
                        ),
                        1,
                    )
                else:
                    provider.distance_km = None
    else:
        selected_sort_label = "Önerilen"
        providers_qs = providers_qs.order_by("-rating", "-ratings_count", "full_name", "id")
        provider_page_obj = paginate_items(
            request,
            providers_qs,
            per_page=provider_page_size,
            page_param="provider_page",
        )
        providers = list(provider_page_obj.object_list)
        for provider in providers:
            provider.distance_km = None

    query_without_provider_page = request.GET.copy()
    query_without_provider_page.pop("provider_page", None)
    provider_page_query = query_without_provider_page.urlencode()

    request_form = ServiceRequestForm(initial=build_request_form_initial(request))
    context = {
        "search_form": search_form,
        "request_form": request_form,
        "providers": providers,
        "provider_page_obj": provider_page_obj,
        "provider_total_count": provider_page_obj.paginator.count,
        "provider_page_query": provider_page_query,
        "provider_page_size": provider_page_size,
        "provider_page_size_options": provider_page_size_options,
        "location_used": location_used,
        "location_sorted": location_sorted,
        "selected_sort_label": selected_sort_label,
        "city_district_map_json": get_city_district_map_json(),
        "is_provider_user": is_provider_user,
        "popular_service_types": get_popular_service_types(),
    }
    return render(request, "Myapp/index.html", context)


def create_request(request):
    if request.method != "POST":
        return redirect("index")

    if not request.user.is_authenticated:
        messages.error(request, "Talep oluşturmak için giriş yapmalısınız.")
        return redirect("customer_login")

    provider_user = get_provider_for_user(request.user)
    if provider_user:
        messages.error(request, "Usta hesabı ile talep oluşturamazsınız.")
        return redirect("provider_requests")

    rate_limit_response = reject_rate_limited_request(
        request,
        "create-request",
        "index",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "create-request", "index")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    request_form = ServiceRequestForm(request.POST)
    if not request_form.is_valid():
        search_form = ServiceSearchForm()
        provider_page_size_options = [12, 24, 48, 96]
        provider_page_size = 24
        providers_qs = (
            Provider.objects.filter(is_verified=True, is_available=True)
            .prefetch_related("service_types")
            .annotate(ratings_count=Count("ratings", distinct=True))
            .order_by("-rating", "full_name", "id")
        )
        provider_page_obj = paginate_items(
            request,
            providers_qs,
            per_page=provider_page_size,
            page_param="provider_page",
        )
        providers = list(provider_page_obj.object_list)
        for provider in providers:
            provider.distance_km = None
        return render(
            request,
            "Myapp/index.html",
            {
                "search_form": search_form,
                "request_form": request_form,
                "providers": providers,
                "provider_page_obj": provider_page_obj,
                "provider_total_count": provider_page_obj.paginator.count,
                "provider_page_query": "",
                "provider_page_size": provider_page_size,
                "provider_page_size_options": provider_page_size_options,
                "location_used": False,
                "location_sorted": False,
                "selected_sort_label": "Önerilen",
                "city_district_map_json": get_city_district_map_json(),
                "is_provider_user": False,
                "popular_service_types": get_popular_service_types(),
            },
        )

    service_request = request_form.save(commit=False)
    if request.user.is_authenticated:
        service_request.customer = request.user

    service_request.save()
    create_workflow_event(
        service_request,
        from_status="created",
        to_status=service_request.status,
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Müşteri talebi oluşturuldu",
    )

    if request.user.is_authenticated:
        customer_profile, _ = CustomerProfile.objects.get_or_create(user=request.user)
        customer_profile.phone = service_request.customer_phone
        customer_profile.city = service_request.city
        customer_profile.district = service_request.district
        customer_profile.save(update_fields=["phone", "city", "district"])

    dispatch_result = dispatch_next_provider_offer(
        service_request,
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Talep için uygun ustalara teklif gönderildi",
    )
    if dispatch_result["result"] == "offers-created":
        offer_count = len(dispatch_result["offers"])
        messages.success(
            request,
            f"Talebiniz alındı. {offer_count} ustaya teklif vermesi için iletildi.",
        )
    elif dispatch_result["result"] == "no-candidates":
        messages.info(
            request,
            "Talebiniz alındı ancak şu an şehir/ilçe kriterlerinde müsait usta bulunamadı.",
        )
    else:
        messages.warning(
            request,
            "Talebiniz kaydedildi fakat şu an sıradaki uygun usta bulunamadı.",
        )

    return redirect("index")


def contact(request):
    return render(request, "Myapp/Contact.html")


def offline(request):
    return render(request, "Myapp/offline.html")


@never_cache
def service_worker(request):
    response = render(request, "service-worker.js", content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@login_required
def rate_request(request, request_id):
    if request.method != "POST":
        return redirect("my_requests")
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "completed" or service_request.matched_provider is None:
        messages.error(request, "Puanlama sadece tamamlanmış ve eşleşmiş talepler için yapılabilir.")
        return redirect("my_requests")

    current_rating = getattr(service_request, "provider_rating", None)
    form = ProviderRatingForm(request.POST, instance=current_rating)
    if form.is_valid():
        rating = form.save(commit=False)
        rating.service_request = service_request
        rating.provider = service_request.matched_provider
        rating.customer = request.user
        rating.save()
        if current_rating is None:
            messages.success(request, f"{service_request.matched_provider.full_name} için puanınız kaydedildi.")
        else:
            messages.success(request, f"{service_request.matched_provider.full_name} için yorumunuz güncellendi.")
    else:
        messages.error(request, "Puan kaydedilemedi. Lütfen geçerli bir puan seçin.")

    return redirect("my_requests")


@never_cache
@ensure_csrf_cookie
def signup_view(request):
    if request.user.is_authenticated:
        return redirect("provider_requests") if get_provider_for_user(request.user) else redirect("index")

    if request.method == "POST":
        form = CustomerSignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            request.session["role"] = "customer"
            messages.success(request, "Hesabınız oluşturuldu ve giriş yapıldı.")
            return redirect("index")
    else:
        form = CustomerSignupForm()

    return render(
        request,
        "Myapp/signup.html",
        {
            "form": form,
            "city_district_map_json": get_city_district_map_json(),
        },
    )


@never_cache
@ensure_csrf_cookie
def provider_signup_view(request):
    if request.user.is_authenticated:
        return redirect("provider_requests") if get_provider_for_user(request.user) else redirect("index")

    if request.method == "POST":
        form = ProviderSignupForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Usta hesabınız oluşturuldu. Admin onayı sonrası giriş yapabilirsiniz.")
            return redirect("provider_login")
    else:
        form = ProviderSignupForm()

    return render(
        request,
        "Myapp/provider_signup.html",
        {
            "form": form,
            "city_district_map_json": get_city_district_map_json(),
        },
    )


@never_cache
@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect("provider_requests") if get_provider_for_user(request.user) else redirect("index")

    if request.method == "POST":
        rate_limit_response = reject_rate_limited_request(
            request,
            "customer-login",
            "customer_login",
            max_attempts=get_login_rate_limit_max_attempts(),
            window_seconds=get_login_rate_limit_window_seconds(),
            identity=(request.POST.get("username") or "").strip(),
        )
        if rate_limit_response:
            return rate_limit_response

        form = CustomerLoginForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            request.session["role"] = "customer"
            messages.success(request, "Giriş başarılı.")
            return redirect("index")
    else:
        form = CustomerLoginForm(request)

    return render(request, "Myapp/login.html", {"form": form})


@never_cache
@ensure_csrf_cookie
def provider_login_view(request):
    if request.user.is_authenticated:
        provider = get_provider_for_user(request.user)
        if provider:
            if provider.is_verified:
                return redirect("provider_requests")
            if not request.session.pop(PROVIDER_PENDING_APPROVAL_FLASH_FLAG, False):
                messages.warning(request, PROVIDER_PENDING_APPROVAL_MESSAGE)
            return redirect("index")
        return redirect("index")

    if request.method == "POST":
        rate_limit_response = reject_rate_limited_request(
            request,
            "provider-login",
            "provider_login",
            max_attempts=get_login_rate_limit_max_attempts(),
            window_seconds=get_login_rate_limit_window_seconds(),
            identity=(request.POST.get("username") or "").strip(),
        )
        if rate_limit_response:
            return rate_limit_response

        form = ProviderLoginForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            request.session["role"] = "provider"
            messages.success(request, "Usta girişi başarılı.")
            return redirect("provider_requests")
    else:
        form = ProviderLoginForm(request)

    return render(request, "Myapp/provider_login.html", {"form": form})


def logout_view(request):
    if request.method == "POST":
        logout(request)
        request.session.pop("role", None)
        messages.info(request, "Çıkış yapıldı.")
    return redirect("index")


@login_required
@never_cache
@ensure_csrf_cookie
def provider_profile_view(request):
    provider, blocked_response = get_verified_provider_or_redirect(request)
    if blocked_response:
        return blocked_response

    availability_form = ProviderAvailabilitySlotForm(provider=provider)
    if request.method == "POST":
        slot_action = (request.POST.get("slot_action") or "").strip()
        if slot_action == "add":
            form = ProviderProfileForm(instance=provider)
            availability_form = ProviderAvailabilitySlotForm(request.POST, provider=provider)
            if availability_form.is_valid():
                slot = availability_form.save(commit=False)
                slot.provider = provider
                slot.save()
                messages.success(request, "Musaitlik araligi eklendi.")
                return redirect("provider_profile")
        elif slot_action == "delete":
            form = ProviderProfileForm(instance=provider)
            try:
                slot_id = int(request.POST.get("slot_id") or 0)
            except (TypeError, ValueError):
                slot_id = 0
            slot = get_object_or_404(ProviderAvailabilitySlot, id=slot_id, provider=provider)
            slot.delete()
            messages.success(request, "Musaitlik araligi silindi.")
            return redirect("provider_profile")
        else:
            form = ProviderProfileForm(request.POST, instance=provider)
            if form.is_valid():
                form.save()
                messages.success(request, "Usta profiliniz güncellendi.")
                return redirect("provider_profile")
    else:
        form = ProviderProfileForm(instance=provider)

    availability_slots = list(provider.availability_slots.order_by("weekday", "start_time", "end_time"))

    return render(
        request,
        "Myapp/provider_profile.html",
        {
            "provider": provider,
            "form": form,
            "availability_form": availability_form,
            "availability_slots": availability_slots,
            "city_district_map_json": get_city_district_map_json(),
        },
    )


def get_message_quick_replies(viewer_role):
    if viewer_role == "provider":
        return [
            "Merhaba, talebinizi aldım.",
            "Müsaitim, uygun saati yazarsanız randevu planlayabiliriz.",
            "Yola çıktım, kısa süre içinde ulaşacağım.",
            "Fotoğraf paylaşırsanız daha net yönlendirebilirim.",
        ]
    return [
        "Merhaba, müsait misiniz?",
        "Randevu saatini netleştirelim.",
        "Adres detayını paylaşıyorum.",
        "Teşekkürler, onaylıyorum.",
    ]


def serialize_service_message(message_item, viewer_role):
    return {
        "id": message_item.id,
        "body": message_item.body,
        "sender_role": message_item.sender_role,
        "sender_label": message_item.get_sender_role_display(),
        "mine": message_item.sender_role == viewer_role,
        "created_at": timezone.localtime(message_item.created_at).strftime("%d.%m.%Y %H:%M"),
    }


def resolve_request_message_access(request, request_id, *, api=False):
    service_request = get_object_or_404(
        ServiceRequest.objects.select_related(
            "service_type",
            "customer",
            "matched_provider",
            "matched_offer",
            "matched_offer__provider",
        ),
        id=request_id,
    )
    provider = get_provider_for_user(request.user)
    if provider:
        if not provider.is_verified:
            if api:
                return None, None, None, JsonResponse({"detail": "pending-approval"}, status=403)
            queue_provider_pending_approval_warning(request)
            return None, None, None, redirect("provider_login")
        if service_request.matched_provider_id != provider.id:
            if api:
                return None, None, None, JsonResponse({"detail": "forbidden"}, status=403)
            messages.error(request, "Bu mesajlaşmaya erişiminiz yok.")
            return None, None, None, redirect("provider_requests")
        if service_request.matched_offer_id is None or service_request.matched_offer.provider_id != provider.id:
            if api:
                return None, None, None, JsonResponse({"detail": "not-selected-by-customer"}, status=403)
            messages.warning(request, "Müşteri sizi henüz seçmediği için mesajlaşma açılmadı.")
            return None, None, None, redirect("provider_requests")
        viewer_role = "provider"
        back_url = "provider_requests"
    else:
        if service_request.customer_id != request.user.id:
            if api:
                return None, None, None, JsonResponse({"detail": "forbidden"}, status=403)
            messages.error(request, "Bu mesajlaşmaya erişiminiz yok.")
            return None, None, None, redirect("index")
        if service_request.matched_provider and not service_request.matched_provider.is_verified:
            if api:
                return None, None, None, JsonResponse({"detail": "provider-not-verified"}, status=403)
            messages.warning(request, "Bu usta henüz admin onaylı olmadığı için mesajlaşma kapalı.")
            return None, None, None, redirect("my_requests")
        if service_request.matched_offer_id is None:
            if api:
                return None, None, None, JsonResponse({"detail": "provider-not-selected"}, status=403)
            messages.warning(request, "Usta seçimi tamamlanmadan mesajlaşma açılmaz.")
            return None, None, None, redirect("my_requests")
        viewer_role = "customer"
        back_url = "my_requests"

    if service_request.status != "matched":
        if api:
            return (
                None,
                None,
                None,
                JsonResponse({"detail": "thread-closed", "request_status": service_request.status}, status=409),
            )
        messages.warning(request, "Tamamlanan veya kapalı taleplerde mesajlaşma açık değildir.")
        return None, None, None, redirect(back_url)

    return service_request, viewer_role, back_url, None


@login_required
@never_cache
@ensure_csrf_cookie
def request_messages(request, request_id):
    service_request, viewer_role, back_url, blocked_response = resolve_request_message_access(request, request_id)
    if blocked_response:
        return blocked_response

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if request.method == "POST":
        form = ServiceMessageForm(request.POST)
        if form.is_valid():
            message_item = form.save(commit=False)
            message_item.service_request = service_request
            message_item.sender_user = request.user
            message_item.sender_role = viewer_role
            message_item.save()
            if is_ajax:
                return JsonResponse(
                    {
                        "ok": True,
                        "message": serialize_service_message(message_item, viewer_role),
                    }
                )
            return redirect("request_messages", request_id=service_request.id)
        if is_ajax:
            return JsonResponse({"ok": False, "error": get_first_form_error(form)}, status=400)
    else:
        form = ServiceMessageForm()

    ServiceMessage.objects.filter(service_request=service_request, read_at__isnull=True).exclude(
        sender_role=viewer_role
    ).update(read_at=timezone.now())
    thread_messages = list(service_request.messages.select_related("sender_user").order_by("id"))
    latest_message_id = thread_messages[-1].id if thread_messages else 0

    return render(
        request,
        "Myapp/request_messages.html",
        {
            "service_request": service_request,
            "viewer_role": viewer_role,
            "messages_list": thread_messages,
            "latest_message_id": latest_message_id,
            "form": form,
            "back_url": back_url,
            "quick_reply_options": get_message_quick_replies(viewer_role),
        },
    )


@login_required
@never_cache
def request_messages_snapshot(request, request_id):
    service_request, viewer_role, _back_url, blocked_response = resolve_request_message_access(
        request, request_id, api=True
    )
    if blocked_response:
        return blocked_response

    after_id_raw = (request.GET.get("after_id") or "").strip()
    if after_id_raw.isdigit():
        after_id = int(after_id_raw)
    else:
        after_id = 0

    ServiceMessage.objects.filter(service_request=service_request, read_at__isnull=True).exclude(
        sender_role=viewer_role
    ).update(read_at=timezone.now())

    thread_qs = service_request.messages.select_related("sender_user").order_by("id")
    if after_id > 0:
        thread_qs = thread_qs.filter(id__gt=after_id)
    thread_messages = list(thread_qs[:100])

    if thread_messages:
        latest_id = thread_messages[-1].id
    else:
        latest_id = service_request.messages.order_by("-id").values_list("id", flat=True).first() or 0

    return JsonResponse(
        {
            "messages": [serialize_service_message(item, viewer_role) for item in thread_messages],
            "latest_id": latest_id,
            "thread_closed": False,
        }
    )


@login_required
@never_cache
def notifications_view(request):
    entries = build_notification_entries(request.user, limit=250)
    page_obj = paginate_items(request, entries, per_page=20, page_param="page")
    page_entries = list(page_obj.object_list)
    unread_count = sum(1 for item in entries if item.get("is_unread"))

    return render(
        request,
        "Myapp/notifications.html",
        {
            "notifications_page_obj": page_obj,
            "notification_entries": page_entries,
            "notifications_unread_count": unread_count,
            "notifications_retention_days": get_notification_retention_days(),
        },
    )


@login_required
@require_POST
def notifications_mark_all_read(request):
    mark_all_notifications_read(request.user)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True})
    return redirect("notifications")


@login_required
def my_requests(request):
    refresh_marketplace_lifecycle()
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    requests_qs = request.user.service_requests.select_related(
        "service_type",
        "matched_provider",
        "matched_offer",
        "matched_offer__provider",
    ).prefetch_related(
        "provider_offers",
        "provider_offers__provider",
        "matched_provider__availability_slots",
    )
    requests_page_obj = paginate_items(request, requests_qs, per_page=10, page_param="page")
    requests = list(requests_page_obj.object_list)
    request_ids = [item.id for item in requests]
    rating_map = {
        rating.service_request_id: rating
        for rating in ProviderRating.objects.filter(service_request_id__in=request_ids)
    }
    appointment_map = {
        appointment.service_request_id: appointment
        for appointment in ServiceAppointment.objects.filter(service_request_id__in=request_ids)
    }
    unread_message_map = build_unread_message_map(request_ids, "customer")
    now = timezone.now()
    for item in requests:
        item.rating_entry = rating_map.get(item.id)
        item.appointment_entry = appointment_map.get(item.id)
        verified_offers = [
            offer for offer in item.provider_offers.all() if offer.provider_id and getattr(offer.provider, "is_verified", False)
        ]
        item.pending_offer = next((offer for offer in verified_offers if offer.status == "pending"), None)
        accepted_offers = [offer for offer in verified_offers if offer.status == "accepted"]
        item.accepted_offers = score_accepted_offers(accepted_offers)
        item.recommended_offer_id = item.accepted_offers[0].id if item.accepted_offers else None
        item.unread_messages = unread_message_map.get(item.id, 0)
        item.can_complete_now = False
        item.complete_block_reason = ""

        if item.status == "matched":
            appointment = item.appointment_entry
            if appointment and appointment.status in {"pending", "pending_customer"}:
                item.complete_block_reason = "Bekleyen randevu talebi varken tamamlanamaz."
            elif (
                appointment
                and appointment.status == "confirmed"
                and appointment.scheduled_for
                and appointment.scheduled_for > now
            ):
                item.complete_block_reason = "Onaylı randevu zamanı gelmeden tamamlanamaz."
            else:
                item.can_complete_now = True
        item.provider_availability_slots = []
        if item.matched_provider:
            item.provider_availability_slots = list(
                item.matched_provider.availability_slots.filter(is_active=True).order_by("weekday", "start_time")
            )
        flow_state = build_customer_flow_state(
            item,
            item.appointment_entry,
            has_accepted_offers=bool(item.accepted_offers),
            now=now,
        )
        item.flow_step = flow_state["step"]
        item.flow_title = flow_state["title"]
        item.flow_hint = flow_state["hint"]
        item.flow_next_action = flow_state["next_action"]
        item.flow_tone = flow_state["tone"]
    cancelled_count = requests_qs.filter(status="cancelled").count()
    all_request_ids = list(requests_qs.values_list("id", flat=True))
    waiting_provider_appointment_count = 0
    if all_request_ids:
        waiting_provider_appointment_count = ServiceAppointment.objects.filter(
            service_request_id__in=all_request_ids,
            status="pending",
        ).count()
    customer_flow_summary = {
        "waiting_provider_count": requests_qs.filter(status__in=["new", "pending_provider"]).count(),
        "waiting_customer_selection_count": requests_qs.filter(status="pending_customer").count(),
        "active_matched_count": requests_qs.filter(status="matched").count(),
        "waiting_provider_appointment_count": waiting_provider_appointment_count,
    }
    customer_snapshot = build_customer_snapshot_payload(request.user)
    return render(
        request,
        "Myapp/my_requests.html",
        {
            "requests": requests,
            "requests_page_obj": requests_page_obj,
            "cancelled_count": cancelled_count,
            "customer_requests_signature": customer_snapshot["signature"],
            "customer_snapshot": customer_snapshot,
            "customer_flow_summary": customer_flow_summary,
        },
    )


@login_required
def agreement_history(request):
    provider = get_provider_for_user(request.user)
    if provider:
        agreements_qs = (
            ServiceRequest.objects.filter(
                matched_provider=provider,
                matched_offer__isnull=False,
            )
            .select_related(
                "service_type",
                "customer",
                "matched_provider",
                "matched_offer",
                "matched_offer__provider",
            )
            .order_by("-matched_at", "-created_at")
        )
    else:
        agreements_qs = (
            request.user.service_requests.filter(matched_offer__isnull=False)
            .select_related(
                "service_type",
                "customer",
                "matched_provider",
                "matched_offer",
                "matched_offer__provider",
            )
            .order_by("-matched_at", "-created_at")
        )

    agreements_page_obj = paginate_items(request, agreements_qs, per_page=12, page_param="page")
    agreements = list(agreements_page_obj.object_list)
    appointment_map = {
        appointment.service_request_id: appointment
        for appointment in ServiceAppointment.objects.filter(service_request_id__in=[item.id for item in agreements])
    }
    for item in agreements:
        item.appointment_entry = appointment_map.get(item.id)

    summary = agreements_qs.aggregate(
        total_count=Count("id"),
        completed_count=Count("id", filter=Q(status="completed")),
        matched_count=Count("id", filter=Q(status="matched")),
    )
    return render(
        request,
        "Myapp/agreement_history.html",
        {
            "agreements": agreements,
            "agreements_page_obj": agreements_page_obj,
            "is_provider_user": bool(provider),
            "summary_total_count": summary.get("total_count", 0) or 0,
            "summary_completed_count": summary.get("completed_count", 0) or 0,
            "summary_matched_count": summary.get("matched_count", 0) or 0,
        },
    )


@login_required
@never_cache
@ensure_csrf_cookie
def account_settings(request):
    provider = get_provider_for_user(request.user)
    customer_profile = None
    if not provider:
        customer_profile, _ = CustomerProfile.objects.get_or_create(user=request.user)

    allow_contact_tab = not bool(provider)
    active_tab = request.GET.get("tab") or "identity"
    allowed_tabs = {"identity", "security", "danger"}
    if allow_contact_tab:
        allowed_tabs.add("contact")
    if active_tab not in allowed_tabs:
        active_tab = "identity"

    identity_form = AccountIdentityForm(instance=request.user, prefix="identity")
    contact_form = CustomerContactSettingsForm(instance=customer_profile, prefix="contact") if allow_contact_tab else None
    password_form = AccountPasswordChangeForm(user=request.user, prefix="password")

    if request.method == "POST":
        action = (request.POST.get("form_action") or "").strip()
        if action == "identity":
            active_tab = "identity"
            identity_form = AccountIdentityForm(request.POST, instance=request.user, prefix="identity")
            if identity_form.is_valid():
                identity_form.save()
                messages.success(request, "Hesap bilgileriniz güncellendi.")
                return redirect("account_settings")
        elif action == "contact":
            if provider:
                messages.info(request, "Usta profil ve iletişim bilgileri Usta Profili sekmesinden güncellenir.")
                return redirect("provider_profile")
            active_tab = "contact"
            contact_form = CustomerContactSettingsForm(request.POST, instance=customer_profile, prefix="contact")
            if contact_form.is_valid():
                contact_form.save()
                messages.success(request, "İletişim bilgileriniz güncellendi.")
                return redirect("account_settings")
        elif action == "security":
            active_tab = "security"
            password_form = AccountPasswordChangeForm(user=request.user, data=request.POST, prefix="password")
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Şifreniz güncellendi.")
                return redirect("account_settings")
        elif action == "danger":
            active_tab = "danger"

    return render(
        request,
        "Myapp/account_settings.html",
        {
            "is_provider_user": bool(provider),
            "identity_form": identity_form,
            "contact_form": contact_form,
            "password_form": password_form,
            "active_tab": active_tab,
            "allow_contact_tab": allow_contact_tab,
            "city_district_map_json": get_city_district_map_json(),
        },
    )



@login_required
@require_POST
def delete_account(request):
    expected_phrase = "HESABIMI SIL"
    confirm_phrase = " ".join(((request.POST.get("confirmation_text") or "").strip().upper()).split())
    confirm_phrase = unicodedata.normalize("NFKD", confirm_phrase).encode("ascii", "ignore").decode("ascii")
    password = request.POST.get("password") or ""

    if confirm_phrase != expected_phrase:
        messages.error(request, 'Hesap silme onayı için "HESABIMI SİL" yazmalısınız.')
        return redirect(f"{reverse('account_settings')}tab=danger")

    if not request.user.check_password(password):
        messages.error(request, "Şifre doğrulaması başarısız.")
        return redirect(f"{reverse('account_settings')}tab=danger")

    user = request.user
    provider = get_provider_for_user(user)
    with transaction.atomic():
        if provider:
            provider.delete()
        else:
            user.service_requests.all().delete()

        user.delete()

    logout(request)
    request.session.pop("role", None)
    messages.success(request, "Hesabınız kalıcı olarak silindi.")
    return redirect("index")


@login_required
@never_cache
def customer_requests_snapshot(request):
    if get_provider_for_user(request.user):
        return JsonResponse({"detail": "forbidden"}, status=403)
    refresh_marketplace_lifecycle()
    response = JsonResponse(build_customer_snapshot_payload(request.user))
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@never_cache
def lifecycle_health(request):
    worker_name = (request.GET.get("worker") or "marketplace_lifecycle").strip()[:80] or "marketplace_lifecycle"
    stale_after_seconds = get_lifecycle_heartbeat_stale_seconds()
    now = timezone.now()
    heartbeat = SchedulerHeartbeat.objects.filter(worker_name=worker_name).first()

    if heartbeat is None:
        payload = {
            "ok": False,
            "worker_name": worker_name,
            "status": "missing",
            "stale_after_seconds": stale_after_seconds,
            "message": "Scheduler heartbeat kaydı bulunamadı.",
        }
        response = JsonResponse(payload, status=503)
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    reference_at = heartbeat.last_success_at or heartbeat.last_started_at or heartbeat.updated_at
    age_seconds = None
    is_stale = True
    if reference_at is not None:
        age_seconds = max(0, int((now - reference_at).total_seconds()))
        is_stale = age_seconds > stale_after_seconds

    payload = {
        "ok": not is_stale,
        "worker_name": worker_name,
        "status": "stale" if is_stale else "healthy",
        "stale_after_seconds": stale_after_seconds,
        "age_seconds": age_seconds,
        "run_count": heartbeat.run_count,
        "last_started_at": heartbeat.last_started_at.isoformat() if heartbeat.last_started_at else None,
        "last_success_at": heartbeat.last_success_at.isoformat() if heartbeat.last_success_at else None,
        "last_error_at": heartbeat.last_error_at.isoformat() if heartbeat.last_error_at else None,
        "last_error": heartbeat.last_error,
    }
    response = JsonResponse(payload, status=503 if is_stale else 200)
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@login_required
def complete_request(request, request_id):
    if request.method != "POST":
        return redirect("my_requests")
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    rate_limit_response = reject_rate_limited_request(
        request,
        "complete-request",
        "my_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "complete-request", "my_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "matched":
        messages.warning(request, "Sadece eşleşen talepler tamamlandı olarak işaretlenebilir.")
        return redirect("my_requests")

    appointment = ServiceAppointment.objects.filter(service_request=service_request).first()
    if appointment and appointment.status in {"pending", "pending_customer"}:
        messages.warning(request, "Bekleyen randevu talebi varken talep tamamlanamaz.")
        return redirect("my_requests")
    if appointment and appointment.status == "confirmed" and appointment.scheduled_for > timezone.now():
        messages.warning(request, "Onayli randevu zamani gelmeden talep tamamlanamaz.")
        return redirect("my_requests")

    if not transition_service_request_status(
        service_request,
        "completed",
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Müşteri talebi tamamladı",
    ):
        messages.warning(request, "Talep durumu güncellenemedi.")
        return redirect("my_requests")

    if appointment and appointment.status == "confirmed":
        transition_appointment_status(
            appointment,
            "completed",
            extra_update_fields=["updated_at"],
            actor_user=request.user,
            actor_role=actor_role,
            source="user",
            note="Talep tamamlandı, randevu da tamamlandı",
        )

    purge_request_messages(service_request.id)
    messages.success(request, "Talep tamamlandı olarak güncellendi.")
    return redirect("my_requests")

@login_required
@require_POST
def create_appointment(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    rate_limit_response = reject_rate_limited_request(
        request,
        "create-appointment",
        "my_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "create-appointment", "my_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    refresh_appointment_lifecycle()
    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "matched" or service_request.matched_provider is None:
        messages.warning(request, "Randevu sadece eşleşen talepler için oluşturulabilir.")
        return redirect("my_requests")
    if not service_request.matched_provider.is_verified:
        messages.warning(request, "Bu usta henüz admin onaylı olmadığı için randevu oluşturulamaz.")
        return redirect("my_requests")

    existing = ServiceAppointment.objects.filter(service_request=service_request).first()
    if existing and existing.status == "completed":
        messages.warning(request, "Tamamlanan bir talep için yeni randevu oluşturulamaz.")
        return redirect("my_requests")

    form = AppointmentCreateForm(
        request.POST,
        provider=service_request.matched_provider,
        current_appointment_id=existing.id if existing else None,
    )
    if not form.is_valid():
        messages.error(request, get_first_form_error(form))
        return redirect("my_requests")

    scheduled_for = form.cleaned_data["scheduled_for"]
    customer_note = form.cleaned_data.get("customer_note", "")
    if existing:
        existing.provider = service_request.matched_provider
        existing.customer = request.user
        existing.scheduled_for = scheduled_for
        existing.customer_note = customer_note
        existing.provider_note = ""
        if not transition_appointment_status(
            existing,
            "pending",
            extra_update_fields=[
                "provider",
                "customer",
                "scheduled_for",
                "customer_note",
                "provider_note",
                "updated_at",
            ],
            actor_user=request.user,
            actor_role=actor_role,
            source="user",
            note="Müşteri randevuyu yeniden planladı",
        ):
            messages.warning(request, "Bu randevu durumu yeniden planlama için uygun değil.")
            return redirect("my_requests")
        send_sms(
            service_request.matched_provider.phone,
            (
                f"UstaBul randevu: Talep #{service_request.id} icin yeni randevu talebi var. "
                f"Tarih: {timezone.localtime(scheduled_for).strftime('%d.%m %H:%M')}"
            ),
        )
        messages.success(request, "Randevu talebiniz güncellendi ve ustaya iletildi.")
        return redirect("my_requests")

    new_appointment = ServiceAppointment.objects.create(
        service_request=service_request,
        customer=request.user,
        provider=service_request.matched_provider,
        scheduled_for=scheduled_for,
        customer_note=customer_note,
        status="pending",
    )
    create_workflow_event(
        new_appointment,
        from_status="created",
        to_status=new_appointment.status,
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Müşteri yeni randevu talebi oluşturdu",
    )
    send_sms(
        service_request.matched_provider.phone,
        (
            f"UstaBul randevu: Talep #{service_request.id} icin randevu talebi var. "
            f"Tarih: {timezone.localtime(scheduled_for).strftime('%d.%m %H:%M')}"
        ),
    )
    messages.success(request, "Randevu talebiniz ustaya iletildi.")
    return redirect("my_requests")


@login_required
@require_POST
def cancel_appointment(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    rate_limit_response = reject_rate_limited_request(
        request,
        "cancel-appointment",
        "my_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "cancel-appointment", "my_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    refresh_appointment_lifecycle()
    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    appointment = get_object_or_404(ServiceAppointment, service_request=service_request)
    if appointment.status not in {"pending", "pending_customer", "confirmed"}:
        messages.warning(request, "Bu randevu artik iptal edilemez.")
        return redirect("my_requests")

    transition_appointment_status(
        appointment,
        "cancelled",
        extra_update_fields=["updated_at"],
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Müşteri randevuyu iptal etti",
    )
    messages.success(request, "Randevu iptal edildi.")
    return redirect("my_requests")


@login_required
@require_POST
def customer_confirm_appointment(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    rate_limit_response = reject_rate_limited_request(
        request,
        "confirm-appointment",
        "my_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "confirm-appointment", "my_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    refresh_appointment_lifecycle()
    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    appointment = get_object_or_404(ServiceAppointment, service_request=service_request)
    if not appointment.provider.is_verified:
        messages.warning(request, "Bu usta henüz admin onaylı olmadığı için randevu onaylanamaz.")
        return redirect("my_requests")
    if appointment.status != "pending_customer":
        messages.warning(request, "Onay bekleyen bir randevu bulunamadı.")
        return redirect("my_requests")

    transition_appointment_status(
        appointment,
        "confirmed",
        extra_update_fields=["updated_at"],
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Müşteri randevuyu onayladı",
    )
    send_sms(
        appointment.provider.phone,
        (
            f"UstaBul randevu: Müşteri Talep #{service_request.id} randevusunu onayladı. "
            f"Tarih: {timezone.localtime(appointment.scheduled_for).strftime('%d.%m %H:%M')}"
        ),
    )
    messages.success(request, "Randevuyu onayladınız.")
    return redirect("my_requests")


@login_required
@require_POST
def cancel_request(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    rate_limit_response = reject_rate_limited_request(
        request,
        "cancel-request",
        "my_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "cancel-request", "my_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status not in {"new", "pending_provider", "pending_customer"} or service_request.matched_provider is not None:
        messages.warning(request, "Bu talep artik iptal edilemez.")
        return redirect("my_requests")

    now = timezone.now()
    service_request.provider_offers.filter(status__in=["pending", "accepted"]).update(status="expired", responded_at=now)
    service_request.matched_provider = None
    service_request.matched_offer = None
    service_request.matched_at = None
    transition_service_request_status(
        service_request,
        "cancelled",
        extra_update_fields=["matched_provider", "matched_offer", "matched_at"],
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Müşteri talebi iptal etti",
    )
    messages.success(request, "Talep aramasi iptal edildi.")
    return redirect("my_requests")


@login_required
@require_POST
def delete_cancelled_request(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "cancelled":
        messages.warning(request, "Sadece iptal edilen talepler silinebilir.")
        return redirect("my_requests")

    service_request.delete()
    messages.success(request, "İptal edilen talep silindi.")
    return redirect("my_requests")


@login_required
@require_POST
def delete_all_cancelled_requests(request):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    deleted_count, _ = request.user.service_requests.filter(status="cancelled").delete()
    if deleted_count:
        messages.success(request, "İptal edilen talepler silindi.")
    else:
        messages.info(request, "Silinecek iptal edilen talep bulunamadı.")
    return redirect("my_requests")


@login_required
@require_POST
def select_provider_offer(request, request_id, offer_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    rate_limit_response = reject_rate_limited_request(
        request,
        "select-provider-offer",
        "my_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "select-provider-offer", "my_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status not in {"pending_provider", "pending_customer"} or service_request.matched_provider is not None:
        messages.warning(request, "Bu talep için usta seçimi artık yapılamaz.")
        return redirect("my_requests")

    with transaction.atomic():
        service_request = ServiceRequest.objects.select_for_update().filter(id=service_request.id).first()
        if not service_request:
            messages.warning(request, "Talep bulunamadı.")
            return redirect("my_requests")
        if service_request.status not in {"pending_provider", "pending_customer"} or service_request.matched_provider_id is not None:
            messages.warning(request, "Bu talep zaten eşleştirilmiş.")
            return redirect("my_requests")

        selected_offer = (
            ProviderOffer.objects.select_for_update()
            .select_related("provider")
            .filter(
                id=offer_id,
                service_request=service_request,
                status="accepted",
                provider__is_verified=True,
            )
            .first()
        )
        if not selected_offer:
            messages.warning(request, "Bu teklif artık seçilemez veya usta henüz admin onaylı değil.")
            return redirect("my_requests")

        now = timezone.now()
        ProviderOffer.objects.filter(service_request=service_request).exclude(id=selected_offer.id).filter(
            status__in=["pending", "accepted"]
        ).update(status="expired", responded_at=now)
        service_request.matched_provider = selected_offer.provider
        service_request.matched_offer = selected_offer
        service_request.matched_at = now
        if not transition_service_request_status(
            service_request,
            "matched",
            extra_update_fields=["matched_provider", "matched_offer", "matched_at"],
            actor_user=request.user,
            actor_role=actor_role,
            source="user",
            note="Müşteri teklif seçti ve usta eşleşti",
        ):
            messages.warning(request, "Talep durumu eşleştirme için uygun değil.")
            return redirect("my_requests")

    messages.success(request, f"Talep #{service_request.id} için {selected_offer.provider.full_name} seçildi.")
    return redirect("my_requests")

@login_required
def provider_requests(request):
    refresh_marketplace_lifecycle()
    provider, blocked_response = get_verified_provider_or_redirect(request)
    if blocked_response:
        return blocked_response

    pending_offers = list(
        provider.offers.filter(status="pending")
        .select_related("service_request", "service_request__service_type")
        .order_by("-sent_at")
    )
    recent_offers_qs = (
        provider.offers.exclude(status="pending")
        .select_related("service_request", "service_request__service_type")
        .order_by("-responded_at", "-sent_at")
    )
    recent_offers_page_obj = paginate_items(request, recent_offers_qs, per_page=10, page_param="recent_offer_page")
    recent_offers = list(recent_offers_page_obj.object_list)
    pending_appointments = list(
        provider.appointments.filter(status="pending")
        .select_related("service_request", "service_request__service_type")
        .order_by("scheduled_for")
    )
    confirmed_appointments = list(
        provider.appointments.filter(status__in=["confirmed", "pending_customer"])
        .select_related("service_request", "service_request__service_type")
        .order_by("scheduled_for")[:20]
    )
    recent_appointments_qs = (
        provider.appointments.exclude(status__in=["pending", "pending_customer", "confirmed"])
        .select_related("service_request", "service_request__service_type")
        .order_by("-updated_at")
    )
    recent_appointments_page_obj = paginate_items(
        request,
        recent_appointments_qs,
        per_page=10,
        page_param="recent_appointment_page",
    )
    recent_appointments = list(recent_appointments_page_obj.object_list)
    active_threads = list(
        provider.service_requests.filter(
            status="matched",
            matched_offer__isnull=False,
            matched_offer__provider=provider,
        )
        .select_related("service_type", "customer")
        .order_by("-created_at")[:30]
    )
    active_thread_ids = [item.id for item in active_threads]
    unread_map = build_unread_message_map(active_thread_ids, "provider")
    appointment_map = {
        appointment.service_request_id: appointment
        for appointment in ServiceAppointment.objects.filter(service_request_id__in=active_thread_ids)
    }
    waiting_schedule_count = 0
    for thread in active_threads:
        thread.unread_messages = unread_map.get(thread.id, 0)
        thread.appointment_entry = appointment_map.get(thread.id)
        thread.appointment_feedback_tone = "neutral"
        thread.appointment_feedback_label = "Durum güncelleniyor"
        thread.appointment_feedback_note = "Randevu bilgisi kontrol ediliyor."

        appointment = thread.appointment_entry
        if appointment is None:
            waiting_schedule_count += 1
            thread.appointment_feedback_tone = "warning"
            thread.appointment_feedback_label = "Randevu saati bekleniyor"
            thread.appointment_feedback_note = "Müşterinin randevu saati seçmesi bekleniyor."
            continue

        appointment_status = appointment.status
        if appointment_status in {"rejected", "cancelled"}:
            waiting_schedule_count += 1
            thread.appointment_feedback_tone = "warning"
            thread.appointment_feedback_label = "Yeni randevu saati bekleniyor"
            thread.appointment_feedback_note = "Müşterinin yeni bir randevu oluşturması gerekiyor."
        elif appointment_status == "pending":
            thread.appointment_feedback_tone = "action"
            thread.appointment_feedback_label = "Randevu onayınız bekleniyor"
            thread.appointment_feedback_note = "Müşteri saat seçimini yaptı. Bekleyen Randevu Talepleri bölümünü kontrol edin."
        elif appointment_status == "pending_customer":
            thread.appointment_feedback_tone = "info"
            thread.appointment_feedback_label = "Müşteri son onayı bekleniyor"
            thread.appointment_feedback_note = "Randevu ustadan onaylandı, son adım müşteri onayı."
        elif appointment_status == "confirmed":
            thread.appointment_feedback_tone = "success"
            thread.appointment_feedback_label = "Randevu onaylandı"
            thread.appointment_feedback_note = "Planlanan saat: " + timezone.localtime(appointment.scheduled_for).strftime(
                "%d.%m.%Y %H:%M"
            )
        elif appointment_status == "completed":
            thread.appointment_feedback_tone = "success"
            thread.appointment_feedback_label = "Randevu tamamlandı"
            thread.appointment_feedback_note = "Bu randevu kapatıldı."
    total_unread_messages = sum(thread.unread_messages for thread in active_threads)

    return render(
        request,
        "Myapp/provider_requests.html",
        {
            "provider": provider,
            "pending_offers": pending_offers,
            "recent_offers": recent_offers,
            "pending_appointments": pending_appointments,
            "confirmed_appointments": confirmed_appointments,
            "recent_appointments": recent_appointments,
            "recent_appointments_page_obj": recent_appointments_page_obj,
            "active_threads": active_threads,
            "total_unread_messages": total_unread_messages,
            "waiting_schedule_count": waiting_schedule_count,
            "recent_offers_page_obj": recent_offers_page_obj,
        },
    )


@login_required
@never_cache
def provider_panel_snapshot(request):
    provider, blocked_response = get_verified_provider_or_redirect(request, api=True)
    if blocked_response:
        return blocked_response

    refresh_marketplace_lifecycle()
    pending_offers_qs = provider.offers.filter(status="pending").order_by("-sent_at")
    latest_pending_offer = pending_offers_qs.values("id").first()

    payload = {
        "pending_offers_count": pending_offers_qs.count(),
        "latest_pending_offer_id": latest_pending_offer["id"] if latest_pending_offer else 0,
        "pending_appointments_count": provider.appointments.filter(status="pending").count(),
        "unread_messages_count": ServiceMessage.objects.filter(
            service_request__matched_provider=provider,
            service_request__status="matched",
            service_request__matched_offer__isnull=False,
            service_request__matched_offer__provider=provider,
            read_at__isnull=True,
        ).exclude(sender_role="provider").count(),
    }
    response = JsonResponse(payload)
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response



def provider_detail(request, provider_id):
    provider = get_object_or_404(
        Provider.objects.prefetch_related("service_types").annotate(ratings_count=Count("ratings", distinct=True)),
        id=provider_id,
        is_verified=True,
    )
    recent_ratings = list(provider.ratings.select_related("customer").order_by("-updated_at")[:10])
    completed_jobs = provider.service_requests.filter(status="completed").count()
    successful_quotes = provider.offers.filter(status="accepted").count()
    return render(
        request,
        "Myapp/provider_detail.html",
        {
            "provider": provider,
            "recent_ratings": recent_ratings,
            "completed_jobs": completed_jobs,
            "successful_quotes": successful_quotes,
        },
    )


@login_required
@require_POST
def provider_confirm_appointment(request, appointment_id):
    provider, blocked_response = get_verified_provider_or_redirect(request)
    if blocked_response:
        return blocked_response

    rate_limit_response = reject_rate_limited_request(
        request,
        "provider-confirm-appointment",
        "provider_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "provider-confirm-appointment", "provider_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    refresh_appointment_lifecycle()
    appointment = get_object_or_404(
        ServiceAppointment.objects.select_related("service_request"),
        id=appointment_id,
        provider=provider,
    )
    if appointment.status != "pending":
        messages.warning(request, "Bu randevu talebi artık açık değil.")
        return redirect("provider_requests")

    provider_note = (request.POST.get("provider_note") or "").strip()
    appointment.provider_note = provider_note
    if not transition_appointment_status(
        appointment,
        "confirmed",
        extra_update_fields=["provider_note", "updated_at"],
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Usta randevuyu onayladı",
    ):
        messages.warning(request, "Randevu durumu usta onayı için uygun değil.")
        return redirect("provider_requests")
    send_sms(
        appointment.service_request.customer_phone,
        (
            f"UstaBul randevu: Talep #{appointment.service_request_id} için usta onayı verildi. "
            f"Randevu onaylandı. Tarih: {timezone.localtime(appointment.scheduled_for).strftime('%d.%m %H:%M')}"
        ),
    )
    messages.success(request, f"Talep #{appointment.service_request_id} için randevu onaylandı.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_complete_appointment(request, appointment_id):
    provider, blocked_response = get_verified_provider_or_redirect(request)
    if blocked_response:
        return blocked_response

    rate_limit_response = reject_rate_limited_request(
        request,
        "provider-complete-appointment",
        "provider_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "provider-complete-appointment", "provider_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    appointment = get_object_or_404(
        ServiceAppointment.objects.select_related("service_request"),
        id=appointment_id,
        provider=provider,
    )
    if appointment.status not in {"confirmed", "pending_customer"}:
        messages.warning(request, "Sadece onaylı randevular tamamlanabilir.")
        return redirect("provider_requests")

    if not transition_appointment_status(
        appointment,
        "completed",
        extra_update_fields=["updated_at"],
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Usta randevuyu tamamladı",
    ):
        messages.warning(request, "Randevu durumu tamamlamaya uygun değil.")
        return redirect("provider_requests")

    service_request = appointment.service_request
    if service_request.status != "completed":
        if service_request.matched_provider_id is None:
            service_request.matched_provider = provider
            transition_service_request_status(
                service_request,
                "completed",
                extra_update_fields=["matched_provider"],
                actor_user=request.user,
                actor_role=actor_role,
                source="user",
                note="Randevu tamamlandı, talep kapatıldı",
            )
        else:
            transition_service_request_status(
                service_request,
                "completed",
                actor_user=request.user,
                actor_role=actor_role,
                source="user",
                note="Randevu tamamlandı, talep kapatıldı",
            )

    purge_request_messages(service_request.id)
    messages.success(request, f"Talep #{service_request.id} randevusu tamamlandı olarak işaretlendi.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_reject_appointment(request, appointment_id):
    provider, blocked_response = get_verified_provider_or_redirect(request)
    if blocked_response:
        return blocked_response

    rate_limit_response = reject_rate_limited_request(
        request,
        "provider-reject-appointment",
        "provider_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "provider-reject-appointment", "provider_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    refresh_appointment_lifecycle()
    appointment = get_object_or_404(
        ServiceAppointment.objects.select_related("service_request"),
        id=appointment_id,
        provider=provider,
    )
    if appointment.status != "pending":
        messages.warning(request, "Bu randevu talebi artık açık değil.")
        return redirect("provider_requests")

    provider_note = (request.POST.get("provider_note") or "").strip()
    appointment.provider_note = provider_note
    if not transition_appointment_status(
        appointment,
        "rejected",
        extra_update_fields=["provider_note", "updated_at"],
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Usta randevu talebini reddetti",
    ):
        messages.warning(request, "Randevu durumu red için uygun değil.")
        return redirect("provider_requests")
    messages.info(request, f"Talep #{appointment.service_request_id} randevusu reddedildi.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_accept_offer(request, offer_id):
    provider, blocked_response = get_verified_provider_or_redirect(request)
    if blocked_response:
        return blocked_response

    rate_limit_response = reject_rate_limited_request(
        request,
        "provider-accept-offer",
        "provider_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "provider-accept-offer", "provider_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    with transaction.atomic():
        offer = (
            ProviderOffer.objects.select_for_update()
            .select_related("service_request")
            .filter(id=offer_id, provider=provider)
            .first()
        )
        if not offer:
            messages.warning(request, "Teklif bulunamadı.")
            return redirect("provider_requests")

        service_request = ServiceRequest.objects.select_for_update().filter(id=offer.service_request_id).first()
        if not service_request:
            messages.warning(request, "Talep artık mevcut değil.")
            return redirect("provider_requests")

        if offer.status != "pending":
            messages.warning(request, "Bu teklif artık açık değil.")
            return redirect("provider_requests")

        if service_request.status in {"matched", "completed", "cancelled"}:
            offer.status = "expired"
            offer.responded_at = timezone.now()
            offer.save(update_fields=["status", "responded_at"])
            messages.warning(request, "Bu talep artık açık değil.")
            return redirect("provider_requests")

        quote_note = (request.POST.get("quote_note") or "").strip()[:240]

        now = timezone.now()
        offer.status = "accepted"
        offer.responded_at = now
        offer.quote_note = quote_note
        offer.save(update_fields=["status", "responded_at", "quote_note"])

        if not transition_service_request_status(
            service_request,
            "pending_customer",
            actor_user=request.user,
            actor_role=actor_role,
            source="user",
            note="Usta teklif verdi, müşteri seçimi bekleniyor",
        ):
            messages.warning(request, "Talep durumu teklif sonrası güncellenemedi.")
            return redirect("provider_requests")

    messages.success(request, f"Talep #{service_request.id} iş teklifiniz müşteriye gönderildi.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_reject_offer(request, offer_id):
    provider, blocked_response = get_verified_provider_or_redirect(request)
    if blocked_response:
        return blocked_response

    rate_limit_response = reject_rate_limited_request(
        request,
        "provider-reject-offer",
        "provider_requests",
        max_attempts=get_action_rate_limit_max_attempts(),
        window_seconds=get_action_rate_limit_window_seconds(),
    )
    if rate_limit_response:
        return rate_limit_response

    duplicate_response = reject_duplicate_submission(request, "provider-reject-offer", "provider_requests")
    if duplicate_response:
        return duplicate_response

    actor_role = infer_actor_role(request.user)
    offer = get_object_or_404(
        ProviderOffer.objects.select_related("service_request"),
        id=offer_id,
        provider=provider,
        status="pending",
    )
    now = timezone.now()
    service_request = offer.service_request

    offer.status = "rejected"
    offer.responded_at = now
    offer.save(update_fields=["status", "responded_at"])

    has_accepted_offer = service_request.provider_offers.filter(status="accepted").exists()
    if service_request.provider_offers.filter(status="pending").exists():
        if has_accepted_offer:
            transition_service_request_status(
                service_request,
                "pending_customer",
                actor_user=request.user,
                actor_role=actor_role,
                source="user",
                note="Reddedilen teklif sonrası müşteri seçimi bekleniyor",
            )
        messages.info(
            request,
            f"Talep #{service_request.id} reddedildi. Diğer ustalardan gelecek onay bekleniyor.",
        )
        return redirect("provider_requests")

    if has_accepted_offer:
        transition_service_request_status(
            service_request,
            "pending_customer",
            actor_user=request.user,
            actor_role=actor_role,
            source="user",
            note="Reddedilen teklif sonrası müşteri seçimi bekleniyor",
        )
        messages.info(request, f"Talep #{service_request.id} reddedildi. Müşterinin teklif seçimi bekleniyor.")
        return redirect("provider_requests")

    dispatch_result = dispatch_next_provider_offer(
        service_request,
        actor_user=request.user,
        actor_role=actor_role,
        source="user",
        note="Usta teklifi reddetti, sıradaki adaylara geçildi",
    )
    if dispatch_result["result"] == "offers-created":
        offer_count = len(dispatch_result["offers"])
        messages.info(request, f"Talep #{service_request.id} reddedildi. {offer_count} yeni ustaya teklif açıldı.")
    else:
        request_id = service_request.id
        service_request.delete()
        messages.warning(
            request,
            f"Talep #{request_id} için kabul eden usta bulunamadı, talep silindi.",
        )
    return redirect("provider_requests")




