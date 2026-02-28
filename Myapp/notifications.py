from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import NotificationCursor, Provider, ServiceAppointment, ServiceMessage, ServiceRequest, WorkflowEvent


REQUEST_STATUS_LABELS = dict(ServiceRequest.STATUS_CHOICES)
APPOINTMENT_STATUS_LABELS = dict(ServiceAppointment.STATUS_CHOICES)


def _truncate(text, max_len=180):
    value = str(text or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def get_notification_retention_days():
    try:
        configured = int(getattr(settings, "NOTIFICATION_RETENTION_DAYS", 60))
    except (TypeError, ValueError):
        configured = 60
    return max(7, configured)


def get_notification_cutoff(now=None):
    reference = now or timezone.now()
    return reference - timedelta(days=get_notification_retention_days())


def get_provider_for_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return Provider.objects.filter(user=user).only("id").first()


def get_notification_cursor(user):
    cursor, _created = NotificationCursor.objects.get_or_create(user=user, defaults={"workflow_seen_at": None})
    return cursor


def get_incoming_message_queryset(user, provider=None, now=None):
    cutoff = get_notification_cutoff(now)
    if provider:
        return ServiceMessage.objects.filter(
            service_request__matched_provider=provider,
            service_request__matched_offer__isnull=False,
            service_request__matched_offer__provider=provider,
            service_request__status="matched",
            created_at__gte=cutoff,
        ).exclude(sender_role="provider")
    return ServiceMessage.objects.filter(
        service_request__customer=user,
        service_request__matched_offer__isnull=False,
        service_request__status="matched",
        created_at__gte=cutoff,
    ).exclude(sender_role="customer")


def get_workflow_event_queryset(user, provider=None, now=None):
    cutoff = get_notification_cutoff(now)
    if provider:
        return WorkflowEvent.objects.filter(
            Q(service_request__matched_provider=provider) | Q(appointment__provider=provider),
            created_at__gte=cutoff,
        )
    return WorkflowEvent.objects.filter(service_request__customer=user, created_at__gte=cutoff)


def get_total_unread_notifications_count(user):
    if not user or not getattr(user, "is_authenticated", False):
        return 0

    now = timezone.now()
    provider = get_provider_for_user(user)
    cursor = get_notification_cursor(user)

    unread_messages_count = get_incoming_message_queryset(user, provider=provider, now=now).filter(read_at__isnull=True).count()
    workflow_qs = get_workflow_event_queryset(user, provider=provider, now=now).exclude(actor_user=user)
    if cursor.workflow_seen_at:
        workflow_qs = workflow_qs.filter(created_at__gt=cursor.workflow_seen_at)
    unread_workflow_count = workflow_qs.count()
    return unread_messages_count + unread_workflow_count


def mark_all_notifications_read(user):
    if not user or not getattr(user, "is_authenticated", False):
        return

    provider = get_provider_for_user(user)
    now = timezone.now()

    get_incoming_message_queryset(user, provider=provider, now=now).filter(read_at__isnull=True).update(read_at=now)
    cursor = get_notification_cursor(user)
    cursor.workflow_seen_at = now
    cursor.save(update_fields=["workflow_seen_at", "updated_at"])


def _event_status_label(event, raw_status):
    if event.target_type == "appointment":
        return APPOINTMENT_STATUS_LABELS.get(raw_status, raw_status or "-")
    return REQUEST_STATUS_LABELS.get(raw_status, raw_status or "-")


def build_notification_entries(user, *, limit=180):
    if not user or not getattr(user, "is_authenticated", False):
        return []

    now = timezone.now()
    provider = get_provider_for_user(user)
    cursor = get_notification_cursor(user)
    workflow_seen_at = cursor.workflow_seen_at
    panel_url = reverse("provider_requests") if provider else reverse("my_requests")

    entries = []

    messages = list(
        get_incoming_message_queryset(user, provider=provider, now=now)
        .select_related("service_request")
        .order_by("-created_at")[:limit]
    )
    for item in messages:
        entries.append(
            {
                "entry_id": f"msg-{item.id}",
                "kind": "message",
                "category": "Mesaj",
                "title": f"Talep #{item.service_request_id} için yeni mesaj",
                "body": _truncate(item.body, 220),
                "link": reverse("request_messages", args=[item.service_request_id]),
                "created_at": item.created_at,
                "is_unread": item.read_at is None,
            }
        )

    events = list(
        get_workflow_event_queryset(user, provider=provider, now=now)
        .select_related("service_request", "appointment", "actor_user")
        .order_by("-created_at")[:limit]
    )
    for event in events:
        to_label = _event_status_label(event, event.to_status)
        from_label = _event_status_label(event, event.from_status)
        target_label = "Randevu" if event.target_type == "appointment" else "Talep"
        if event.note:
            body = _truncate(event.note, 220)
        else:
            body = f"{from_label} -> {to_label}"
        entries.append(
            {
                "entry_id": f"wf-{event.id}",
                "kind": "workflow",
                "category": target_label,
                "title": f"{target_label} durumu güncellendi: {to_label}",
                "body": body,
                "link": panel_url,
                "created_at": event.created_at,
                "is_unread": bool(event.actor_user_id != user.id and (not workflow_seen_at or event.created_at > workflow_seen_at)),
            }
        )

    entries.sort(key=lambda item: item["created_at"], reverse=True)
    return entries[:limit]
