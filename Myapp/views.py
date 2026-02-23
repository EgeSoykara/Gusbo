import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from math import asin, cos, radians, sin, sqrt
from uuid import uuid4

from django.contrib import messages
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .constants import NC_CITY_DISTRICT_MAP
from .forms import (
    ANY_DISTRICT_VALUE,
    AppointmentCreateForm,
    CustomerLoginForm,
    CustomerSignupForm,
    ProviderProfileForm,
    ProviderLoginForm,
    ProviderSignupForm,
    ProviderRatingForm,
    ServiceRequestForm,
    ServiceSearchForm,
    ServiceMessageForm,
)
from .models import (
    CustomerProfile,
    Provider,
    ProviderOffer,
    ProviderRating,
    ServiceAppointment,
    ServiceMessage,
    ServiceRequest,
    ServiceType,
)
from .sms import send_sms


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


def get_provider_for_user(user):
    if not user.is_authenticated:
        return None
    return Provider.objects.filter(user=user).first()


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

    if not expired_request_ids:
        return

    impacted_requests = list(ServiceRequest.objects.filter(id__in=expired_request_ids).select_related("service_type"))
    for service_request in impacted_requests:
        if service_request.status in {"matched", "completed", "cancelled"} or service_request.matched_provider_id:
            continue

        has_pending = service_request.provider_offers.filter(status="pending").exists()
        has_accepted = service_request.provider_offers.filter(status="accepted").exists()
        if has_accepted:
            if service_request.status != "pending_customer":
                service_request.status = "pending_customer"
                service_request.save(update_fields=["status"])
            continue
        if has_pending:
            if service_request.status != "pending_provider":
                service_request.status = "pending_provider"
                service_request.save(update_fields=["status"])
            continue
        dispatch_next_provider_offer(service_request)


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


def purge_request_messages(service_request_id):
    ServiceMessage.objects.filter(service_request_id=service_request_id).delete()


def get_provider_avg_quote_filter(service_type=None):
    quote_filter = Q(offers__status="accepted", offers__quote_amount__isnull=False)
    if service_type is not None:
        quote_filter &= Q(offers__service_request__service_type=service_type)
    return quote_filter


def score_accepted_offers(offers):
    if not offers:
        return []

    quote_values = [float(offer.quote_amount) for offer in offers if offer.quote_amount is not None]
    min_quote = min(quote_values) if quote_values else None
    max_quote = max(quote_values) if quote_values else None
    max_sequence = max((offer.sequence or 1) for offer in offers) or 1

    for offer in offers:
        price_score = 22.0
        if offer.quote_amount is not None and min_quote is not None and max_quote is not None:
            if max_quote == min_quote:
                price_score = 55.0
            else:
                normalized = (float(offer.quote_amount) - min_quote) / (max_quote - min_quote)
                price_score = max(0.0, min(55.0, 55.0 * (1.0 - normalized)))

        rating_score = max(0.0, min(35.0, (float(offer.provider.rating) / 5.0) * 35.0))
        if max_sequence <= 1:
            speed_score = 10.0
        else:
            speed_score = max(0.0, min(10.0, ((max_sequence - (offer.sequence or 1)) / (max_sequence - 1)) * 10.0))

        offer.price_score = round(price_score, 1)
        offer.rating_score = round(rating_score, 1)
        offer.speed_score = round(speed_score, 1)
        offer.comparison_score = round(offer.price_score + offer.rating_score + offer.speed_score, 1)

    return sorted(
        offers,
        key=lambda offer: (
            -(offer.comparison_score),
            float(offer.quote_amount) if offer.quote_amount is not None else float("inf"),
            -float(offer.provider.rating),
        ),
    )


def generate_offer_token():
    token = uuid4().hex[:10].upper()
    while ProviderOffer.objects.filter(token=token).exists():
        token = uuid4().hex[:10].upper()
    return token


def build_provider_candidate_groups(service_request):
    base_qs = Provider.objects.filter(
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


def dispatch_next_provider_offer(service_request):
    groups = build_provider_candidate_groups(service_request)
    if not groups:
        service_request.status = "new"
        service_request.matched_provider = None
        service_request.save(update_fields=["status", "matched_provider"])
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

        service_request.status = "pending_provider"
        service_request.matched_provider = None
        service_request.save(update_fields=["status", "matched_provider"])
        return {"result": "offers-created", "offers": created_offers}

    service_request.status = "new"
    service_request.matched_provider = None
    service_request.save(update_fields=["status", "matched_provider"])
    return {"result": "all-contacted"}


@never_cache
@ensure_csrf_cookie
def index(request):
    refresh_offer_lifecycle()
    is_provider_user = bool(get_provider_for_user(request.user)) if request.user.is_authenticated else False
    search_form = ServiceSearchForm(request.GET or None)
    providers_qs = (
        Provider.objects.filter(is_available=True)
        .prefetch_related("service_types")
        .annotate(ratings_count=Count("ratings", distinct=True))
    )
    location_used = False

    if search_form.is_valid():
        service_type = search_form.cleaned_data.get("service_type")
        city = (search_form.cleaned_data.get("city") or "").strip()
        district = (search_form.cleaned_data.get("district") or "").strip()
        user_latitude = search_form.cleaned_data.get("latitude")
        user_longitude = search_form.cleaned_data.get("longitude")

        if service_type:
            providers_qs = providers_qs.filter(service_types=service_type)
        if city:
            providers_qs = providers_qs.filter(city__icontains=city)
        if district and district != ANY_DISTRICT_VALUE:
            providers_qs = providers_qs.filter(district__icontains=district)

        providers_qs = providers_qs.annotate(
            avg_quote=Avg("offers__quote_amount", filter=get_provider_avg_quote_filter(service_type))
        )

        providers = list(providers_qs[:100])
        if user_latitude is not None and user_longitude is not None:
            location_used = True
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
                )
            )
        else:
            for provider in providers:
                provider.distance_km = None
    else:
        providers = list(providers_qs[:12])
        for provider in providers:
            provider.distance_km = None

    request_form = ServiceRequestForm(initial=build_request_form_initial(request))
    context = {
        "search_form": search_form,
        "request_form": request_form,
        "providers": providers[:12],
        "location_used": location_used,
        "city_district_map_json": get_city_district_map_json(),
        "is_provider_user": is_provider_user,
        "popular_service_types": get_popular_service_types(),
    }
    return render(request, "Myapp/index.html", context)


def create_request(request):
    if request.method != "POST":
        return redirect("index")

    provider_user = get_provider_for_user(request.user) if request.user.is_authenticated else None
    if provider_user:
        messages.error(request, "Usta hesabı ile talep oluşturamazsınız.")
        return redirect("provider_requests")

    request_form = ServiceRequestForm(request.POST)
    if not request_form.is_valid():
        search_form = ServiceSearchForm()
        providers = list(
            Provider.objects.filter(is_available=True)
            .prefetch_related("service_types")
            .annotate(ratings_count=Count("ratings", distinct=True))[:12]
        )
        for provider in providers:
            provider.distance_km = None
        return render(
            request,
            "Myapp/index.html",
            {
                "search_form": search_form,
                "request_form": request_form,
                "providers": providers,
                "location_used": False,
                "city_district_map_json": get_city_district_map_json(),
                "is_provider_user": False,
                "popular_service_types": get_popular_service_types(),
            },
        )

    service_request = request_form.save(commit=False)
    if request.user.is_authenticated:
        service_request.customer = request.user

    service_request.save()

    if request.user.is_authenticated:
        CustomerProfile.objects.update_or_create(
            user=request.user,
            defaults={
                "phone": service_request.customer_phone,
                "city": service_request.city,
                "district": service_request.district,
            },
        )

    dispatch_result = dispatch_next_provider_offer(service_request)
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
    if current_rating is not None:
        messages.warning(request, "Bu talep için puan zaten verildi. Güncelleme yapılamaz.")
        return redirect("my_requests")

    form = ProviderRatingForm(request.POST)
    if form.is_valid():
        rating = form.save(commit=False)
        rating.service_request = service_request
        rating.provider = service_request.matched_provider
        rating.customer = request.user
        rating.save()
        messages.success(request, f"{service_request.matched_provider.full_name} için puanınız kaydedildi.")
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
            user = form.save()
            login(request, user)
            request.session["role"] = "provider"
            messages.success(request, "Usta hesabı oluşturuldu ve giriş yapıldı.")
            return redirect("provider_requests")
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
        return redirect("provider_requests") if get_provider_for_user(request.user) else redirect("index")

    if request.method == "POST":
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
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesapları içindir.")
        return redirect("provider_login")

    if request.method == "POST":
        form = ProviderProfileForm(request.POST, instance=provider)
        if form.is_valid():
            form.save()
            messages.success(request, "Usta profilin güncellendi.")
            return redirect("provider_profile")
    else:
        form = ProviderProfileForm(instance=provider)

    return render(
        request,
        "Myapp/provider_profile.html",
        {
            "provider": provider,
            "form": form,
            "city_district_map_json": get_city_district_map_json(),
        },
    )


@login_required
@never_cache
@ensure_csrf_cookie
def request_messages(request, request_id):
    service_request = get_object_or_404(
        ServiceRequest.objects.select_related("service_type", "customer", "matched_provider"),
        id=request_id,
    )
    provider = get_provider_for_user(request.user)
    if provider:
        if service_request.matched_provider_id != provider.id:
            messages.error(request, "Bu mesajlasmaya erisiminiz yok.")
            return redirect("provider_requests")
        viewer_role = "provider"
        back_url = "provider_requests"
    else:
        if service_request.customer_id != request.user.id:
            messages.error(request, "Bu mesajlasmaya erisiminiz yok.")
            return redirect("index")
        viewer_role = "customer"
        back_url = "my_requests"

    if service_request.status not in {"matched", "completed"}:
        messages.warning(request, "Mesajlaşma sadece eşleşen talepler için açıktır.")
        return redirect(back_url)

    if request.method == "POST":
        form = ServiceMessageForm(request.POST)
        if form.is_valid():
            message_item = form.save(commit=False)
            message_item.service_request = service_request
            message_item.sender_user = request.user
            message_item.sender_role = viewer_role
            message_item.save()
            return redirect("request_messages", request_id=service_request.id)
    else:
        form = ServiceMessageForm()

    ServiceMessage.objects.filter(service_request=service_request, read_at__isnull=True).exclude(
        sender_role=viewer_role
    ).update(read_at=timezone.now())
    thread_messages = list(service_request.messages.select_related("sender_user").all())

    return render(
        request,
        "Myapp/request_messages.html",
        {
            "service_request": service_request,
            "viewer_role": viewer_role,
            "messages_list": thread_messages,
            "form": form,
            "back_url": back_url,
        },
    )


@login_required
def my_requests(request):
    refresh_offer_lifecycle()
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    requests = list(
        request.user.service_requests.select_related("service_type", "matched_provider").prefetch_related(
            "provider_offers",
            "provider_offers__provider",
        )
    )
    rating_map = {
        rating.service_request_id: rating
        for rating in ProviderRating.objects.filter(service_request_id__in=[item.id for item in requests])
    }
    appointment_map = {
        appointment.service_request_id: appointment
        for appointment in ServiceAppointment.objects.filter(service_request_id__in=[item.id for item in requests])
    }
    unread_message_map = build_unread_message_map([item.id for item in requests], "customer")
    for item in requests:
        item.rating_entry = rating_map.get(item.id)
        item.appointment_entry = appointment_map.get(item.id)
        item.pending_offer = next((offer for offer in item.provider_offers.all() if offer.status == "pending"), None)
        accepted_offers = [offer for offer in item.provider_offers.all() if offer.status == "accepted"]
        item.accepted_offers = score_accepted_offers(accepted_offers)
        item.recommended_offer_id = item.accepted_offers[0].id if item.accepted_offers else None
        item.unread_messages = unread_message_map.get(item.id, 0)
    cancelled_count = sum(1 for item in requests if item.status == "cancelled")
    return render(
        request,
        "Myapp/my_requests.html",
        {
            "requests": requests,
            "cancelled_count": cancelled_count,
        },
    )


@login_required
def complete_request(request, request_id):
    if request.method != "POST":
        return redirect("my_requests")
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

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

    service_request.status = "completed"
    service_request.save(update_fields=["status"])

    if appointment and appointment.status in {"pending", "pending_customer", "confirmed"}:
        appointment.status = "completed"
        appointment.save(update_fields=["status", "updated_at"])

    purge_request_messages(service_request.id)
    messages.success(request, "Talep tamamlandı olarak güncellendi.")
    return redirect("my_requests")


@login_required
@require_POST
def create_appointment(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "matched" or service_request.matched_provider is None:
        messages.warning(request, "Randevu sadece eşleşen talepler için oluşturulabilir.")
        return redirect("my_requests")

    form = AppointmentCreateForm(request.POST)
    if not form.is_valid():
        messages.error(request, get_first_form_error(form))
        return redirect("my_requests")

    existing = ServiceAppointment.objects.filter(service_request=service_request).first()
    if existing and existing.status == "completed":
        messages.warning(request, "Tamamlanan bir talep için yeni randevu oluşturulamaz.")
        return redirect("my_requests")

    scheduled_for = form.cleaned_data["scheduled_for"]
    customer_note = form.cleaned_data.get("customer_note", "")
    if existing:
        existing.provider = service_request.matched_provider
        existing.customer = request.user
        existing.scheduled_for = scheduled_for
        existing.customer_note = customer_note
        existing.provider_note = ""
        existing.status = "pending"
        existing.save(
            update_fields=[
                "provider",
                "customer",
                "scheduled_for",
                "customer_note",
                "provider_note",
                "status",
                "updated_at",
            ]
        )
        send_sms(
            service_request.matched_provider.phone,
            (
                f"UstaBul randevu: Talep #{service_request.id} icin yeni randevu talebi var. "
                f"Tarih: {timezone.localtime(scheduled_for).strftime('%d.%m %H:%M')}"
            ),
        )
        messages.success(request, "Randevu talebiniz güncellendi ve ustaya iletildi.")
        return redirect("my_requests")

    ServiceAppointment.objects.create(
        service_request=service_request,
        customer=request.user,
        provider=service_request.matched_provider,
        scheduled_for=scheduled_for,
        customer_note=customer_note,
        status="pending",
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

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    appointment = get_object_or_404(ServiceAppointment, service_request=service_request)
    if appointment.status not in {"pending", "pending_customer", "confirmed"}:
        messages.warning(request, "Bu randevu artik iptal edilemez.")
        return redirect("my_requests")

    appointment.status = "cancelled"
    appointment.save(update_fields=["status", "updated_at"])
    messages.success(request, "Randevu iptal edildi.")
    return redirect("my_requests")


@login_required
@require_POST
def customer_confirm_appointment(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece müşteri hesapları içindir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    appointment = get_object_or_404(ServiceAppointment, service_request=service_request)
    if appointment.status != "pending_customer":
        messages.warning(request, "Onay bekleyen bir randevu bulunamadı.")
        return redirect("my_requests")

    appointment.status = "confirmed"
    appointment.save(update_fields=["status", "updated_at"])
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

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status not in {"new", "pending_provider", "pending_customer"} or service_request.matched_provider is not None:
        messages.warning(request, "Bu talep artik iptal edilemez.")
        return redirect("my_requests")

    now = timezone.now()
    service_request.provider_offers.filter(status__in=["pending", "accepted"]).update(status="expired", responded_at=now)
    service_request.status = "cancelled"
    service_request.matched_provider = None
    service_request.save(update_fields=["status", "matched_provider"])
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

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status not in {"pending_provider", "pending_customer"} or service_request.matched_provider is not None:
        messages.warning(request, "Bu talep için usta seçimi artık yapılamaz.")
        return redirect("my_requests")

    selected_offer = get_object_or_404(
        ProviderOffer.objects.select_related("provider"),
        id=offer_id,
        service_request=service_request,
        status="accepted",
    )

    with transaction.atomic():
        service_request = ServiceRequest.objects.select_for_update().filter(id=service_request.id).first()
        if not service_request:
            messages.warning(request, "Talep bulunamadı.")
            return redirect("my_requests")
        if service_request.matched_provider_id is not None or service_request.status == "matched":
            messages.warning(request, "Bu talep zaten eşleşmiş.")
            return redirect("my_requests")

        ProviderOffer.objects.filter(service_request=service_request).exclude(id=selected_offer.id).filter(
            status__in=["pending", "accepted"]
        ).update(status="expired", responded_at=timezone.now())
        service_request.matched_provider = selected_offer.provider
        service_request.status = "matched"
        service_request.save(update_fields=["matched_provider", "status"])

    messages.success(request, f"Talep #{service_request.id} için {selected_offer.provider.full_name} seçildi.")
    return redirect("my_requests")


@login_required
def provider_requests(request):
    refresh_offer_lifecycle()
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesapları içindir.")
        return redirect("provider_login")

    pending_offers = list(
        provider.offers.filter(status="pending")
        .select_related("service_request", "service_request__service_type")
        .order_by("-sent_at")
    )
    recent_offers = list(
        provider.offers.exclude(status="pending")
        .select_related("service_request", "service_request__service_type")
        .order_by("-responded_at", "-sent_at")[:20]
    )
    pending_appointments = list(
        provider.appointments.filter(status="pending")
        .select_related("service_request", "service_request__service_type")
        .order_by("scheduled_for")
    )
    waiting_customer_appointments = list(
        provider.appointments.filter(status="pending_customer")
        .select_related("service_request", "service_request__service_type")
        .order_by("scheduled_for")[:20]
    )
    confirmed_appointments = list(
        provider.appointments.filter(status="confirmed")
        .select_related("service_request", "service_request__service_type")
        .order_by("scheduled_for")[:20]
    )
    recent_appointments = list(
        provider.appointments.exclude(status__in=["pending", "pending_customer", "confirmed"])
        .select_related("service_request", "service_request__service_type")
        .order_by("-updated_at")[:20]
    )
    active_threads = list(
        provider.service_requests.filter(status__in=["matched", "completed"])
        .select_related("service_type", "customer")
        .order_by("-created_at")[:30]
    )
    unread_map = build_unread_message_map([item.id for item in active_threads], "provider")
    for thread in active_threads:
        thread.unread_messages = unread_map.get(thread.id, 0)

    return render(
        request,
        "Myapp/provider_requests.html",
        {
            "provider": provider,
            "pending_offers": pending_offers,
            "recent_offers": recent_offers,
            "pending_appointments": pending_appointments,
            "waiting_customer_appointments": waiting_customer_appointments,
            "confirmed_appointments": confirmed_appointments,
            "recent_appointments": recent_appointments,
            "active_threads": active_threads,
        },
    )


def provider_detail(request, provider_id):
    provider = get_object_or_404(
        Provider.objects.prefetch_related("service_types").annotate(ratings_count=Count("ratings", distinct=True)),
        id=provider_id,
    )
    recent_ratings = list(provider.ratings.select_related("customer").order_by("-updated_at")[:10])
    completed_jobs = provider.service_requests.filter(status="completed").count()
    successful_quotes = provider.offers.filter(status="accepted").count()
    avg_quote = (
        provider.offers.filter(status="accepted", quote_amount__isnull=False)
        .aggregate(avg_value=Avg("quote_amount"))
        .get("avg_value")
    )
    return render(
        request,
        "Myapp/provider_detail.html",
        {
            "provider": provider,
            "recent_ratings": recent_ratings,
            "completed_jobs": completed_jobs,
            "successful_quotes": successful_quotes,
            "avg_quote": avg_quote,
        },
    )


@login_required
@require_POST
def provider_confirm_appointment(request, appointment_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesapları içindir.")
        return redirect("provider_login")

    appointment = get_object_or_404(
        ServiceAppointment.objects.select_related("service_request"),
        id=appointment_id,
        provider=provider,
    )
    if appointment.status != "pending":
        messages.warning(request, "Bu randevu talebi artık açık değil.")
        return redirect("provider_requests")

    provider_note = (request.POST.get("provider_note") or "").strip()
    appointment.status = "pending_customer"
    appointment.provider_note = provider_note
    appointment.save(update_fields=["status", "provider_note", "updated_at"])
    send_sms(
        appointment.service_request.customer_phone,
        (
            f"UstaBul randevu: Talep #{appointment.service_request_id} için usta onayı verildi. "
            "Müşteri panelinden son onayı tamamlayın."
        ),
    )
    messages.success(request, f"Talep #{appointment.service_request_id} için usta onayı verildi. Müşteri onayı bekleniyor.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_complete_appointment(request, appointment_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesapları içindir.")
        return redirect("provider_login")

    appointment = get_object_or_404(
        ServiceAppointment.objects.select_related("service_request"),
        id=appointment_id,
        provider=provider,
    )
    if appointment.status != "confirmed":
        messages.warning(request, "Sadece onaylı randevular tamamlanabilir.")
        return redirect("provider_requests")

    appointment.status = "completed"
    appointment.save(update_fields=["status", "updated_at"])

    service_request = appointment.service_request
    if service_request.status != "completed":
        service_request.status = "completed"
        if service_request.matched_provider_id is None:
            service_request.matched_provider = provider
            service_request.save(update_fields=["status", "matched_provider"])
        else:
            service_request.save(update_fields=["status"])

    purge_request_messages(service_request.id)
    messages.success(request, f"Talep #{service_request.id} randevusu tamamlandı olarak işaretlendi.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_reject_appointment(request, appointment_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesapları içindir.")
        return redirect("provider_login")

    appointment = get_object_or_404(
        ServiceAppointment.objects.select_related("service_request"),
        id=appointment_id,
        provider=provider,
    )
    if appointment.status != "pending":
        messages.warning(request, "Bu randevu talebi artık açık değil.")
        return redirect("provider_requests")

    provider_note = (request.POST.get("provider_note") or "").strip()
    appointment.status = "rejected"
    appointment.provider_note = provider_note
    appointment.save(update_fields=["status", "provider_note", "updated_at"])
    messages.info(request, f"Talep #{appointment.service_request_id} randevusu reddedildi.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_accept_offer(request, offer_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesapları içindir.")
        return redirect("provider_login")

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

        raw_quote_amount = (request.POST.get("quote_amount") or "").strip().replace(",", ".")
        if not raw_quote_amount:
            messages.warning(request, "Teklif tutari zorunludur.")
            return redirect("provider_requests")
        try:
            quote_amount = Decimal(raw_quote_amount)
        except InvalidOperation:
            messages.warning(request, "Teklif tutari gecerli bir sayi olmali.")
            return redirect("provider_requests")
        if quote_amount <= 0:
            messages.warning(request, "Teklif tutari sifirdan buyuk olmali.")
            return redirect("provider_requests")

        quote_note = (request.POST.get("quote_note") or "").strip()[:240]
        now = timezone.now()
        offer.status = "accepted"
        offer.responded_at = now
        offer.quote_amount = quote_amount
        offer.quote_note = quote_note
        offer.save(update_fields=["status", "responded_at", "quote_amount", "quote_note"])
        service_request.status = "pending_customer"
        service_request.save(update_fields=["status"])

    messages.success(request, f"Talep #{service_request.id} için teklifiniz müşteriye gönderildi.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_reject_offer(request, offer_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesapları içindir.")
        return redirect("provider_login")

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
            service_request.status = "pending_customer"
            service_request.save(update_fields=["status"])
        messages.info(
            request,
            f"Talep #{service_request.id} reddedildi. Diğer ustalardan gelecek onay bekleniyor.",
        )
        return redirect("provider_requests")

    if has_accepted_offer:
        service_request.status = "pending_customer"
        service_request.save(update_fields=["status"])
        messages.info(request, f"Talep #{service_request.id} reddedildi. Müşterinin teklif seçimi bekleniyor.")
        return redirect("provider_requests")

    dispatch_result = dispatch_next_provider_offer(service_request)
    if dispatch_result["result"] == "offers-created":
        offer_count = len(dispatch_result["offers"])
        messages.info(request, f"Talep #{service_request.id} reddedildi. {offer_count} yeni ustaya teklif acildi.")
    else:
        request_id = service_request.id
        service_request.delete()
        messages.warning(
            request,
            f"Talep #{request_id} için kabul eden usta bulunamadı, talep silindi.",
        )
    return redirect("provider_requests")
