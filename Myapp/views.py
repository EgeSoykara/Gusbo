import json
import hashlib
import unicodedata
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from math import asin, cos, radians, sin, sqrt
from uuid import uuid4

from django.contrib import messages
from django.conf import settings
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Avg, Count, Q, Sum
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
    ProviderProfileForm,
    ProviderLoginForm,
    ProviderSignupForm,
    ProviderRatingForm,
    ServiceRequestForm,
    ServiceSearchForm,
    ServiceMessageForm,
)
from .models import (
    CreditTransaction,
    CustomerProfile,
    Provider,
    ProviderWallet,
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


def get_initial_provider_credits():
    return max(0, int(getattr(settings, "INITIAL_PROVIDER_CREDITS", 10)))


def get_quote_credit_cost():
    return max(1, int(getattr(settings, "QUOTE_CREDIT_COST", 1)))


def get_provider_package_catalog():
    configured_packages = getattr(settings, "PROVIDER_PACKAGES", None)
    if configured_packages:
        normalized_packages = []
        for package in configured_packages:
            key = str(package.get("key", "")).strip()
            if not key:
                continue
            normalized_packages.append(
                {
                    "key": key,
                    "name": str(package.get("name", key)).strip() or key,
                    "credits": max(1, int(package.get("credits", 1))),
                    "price": max(0, int(package.get("price", 0))),
                    "description": str(package.get("description", "")).strip(),
                }
            )
        if normalized_packages:
            return normalized_packages

    return [
        {
            "key": "basic",
            "name": "Basic",
            "credits": 25,
            "price": 199,
            "description": "Yeni balayan ustalar iÃ§in uygun kredi paketi.",
        },
        {
            "key": "pro",
            "name": "Pro",
            "credits": 80,
            "price": 499,
            "description": "Youn Ã§alÄ±an ustalar iÃ§in yÃ¼ksek kredi paketi.",
        },
    ]


def get_or_create_provider_wallet(provider, for_update=False):
    initial_credits = get_initial_provider_credits()
    wallet, created = ProviderWallet.objects.get_or_create(
        provider=provider,
        defaults={"balance": initial_credits},
    )
    if for_update:
        wallet = ProviderWallet.objects.select_for_update().get(pk=wallet.pk)
    if created and initial_credits > 0:
        CreditTransaction.objects.create(
            provider=provider,
            wallet=wallet,
            transaction_type="welcome",
            amount=initial_credits,
            balance_after=wallet.balance,
            note="Ho geldin kredisi yÃ¼klendi.",
        )
    return wallet


def apply_wallet_transaction(provider, amount, transaction_type, note="", reference_offer=None):
    with transaction.atomic():
        wallet = get_or_create_provider_wallet(provider, for_update=True)
        next_balance = wallet.balance + int(amount)
        if next_balance < 0:
            return None
        wallet.balance = next_balance
        wallet.save(update_fields=["balance", "updated_at"])
        CreditTransaction.objects.create(
            provider=provider,
            wallet=wallet,
            transaction_type=transaction_type,
            amount=int(amount),
            balance_after=wallet.balance,
            note=note[:240],
            reference_offer=reference_offer,
        )
        return wallet


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


def build_customer_requests_signature(user):
    request_rows = list(
        user.service_requests.values_list("id", "status", "matched_provider_id", "matched_offer_id", "matched_at").order_by("id")
    )
    request_ids = [row[0] for row in request_rows]
    if not request_ids:
        return "empty"

    offer_rows = list(
        ProviderOffer.objects.filter(service_request_id__in=request_ids)
        .values_list("service_request_id", "provider_id", "status", "responded_at", "quote_amount")
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
        service_request.matched_offer = None
        service_request.matched_at = None
        service_request.save(update_fields=["status", "matched_provider", "matched_offer", "matched_at"])
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
        service_request.matched_offer = None
        service_request.matched_at = None
        service_request.save(update_fields=["status", "matched_provider", "matched_offer", "matched_at"])
        return {"result": "offers-created", "offers": created_offers}

    service_request.status = "new"
    service_request.matched_provider = None
    service_request.matched_offer = None
    service_request.matched_at = None
    service_request.save(update_fields=["status", "matched_provider", "matched_offer", "matched_at"])
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

    if not request.user.is_authenticated:
        messages.error(request, "Talep oluÅŸturmak iÃ§in giriÅŸ yapmalÄ±sÄ±nÄ±z.")
        return redirect("customer_login")

    provider_user = get_provider_for_user(request.user)
    if provider_user:
        messages.error(request, "Usta hesabÄ± ile talep oluturamazsÄ±nÄ±z.")
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
            f"Talebiniz alÄ±ndÄ±. {offer_count} ustaya teklif vermesi iÃ§in iletildi.",
        )
    elif dispatch_result["result"] == "no-candidates":
        messages.info(
            request,
            "Talebiniz alÄ±ndÄ± ancak u an ehir/ilÃ§e kriterlerinde mÃ¼sait usta bulunamadÄ±.",
        )
    else:
        messages.warning(
            request,
            "Talebiniz kaydedildi fakat u an sÄ±radaki uygun usta bulunamadÄ±.",
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
        messages.error(request, "Bu alan sadece mÃ¼teri hesaplarÄ± iÃ§indir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "completed" or service_request.matched_provider is None:
        messages.error(request, "Puanlama sadece tamamlanmÄ± ve elemi talepler iÃ§in yapÄ±labilir.")
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
            messages.success(request, f"{service_request.matched_provider.full_name} iÃ§in puanÄ±nÄ±z kaydedildi.")
        else:
            messages.success(request, f"{service_request.matched_provider.full_name} iÃ§in yorumunuz gÃ¼ncellendi.")
    else:
        messages.error(request, "Puan kaydedilemedi. LÃ¼tfen geÃ§erli bir puan seÃ§in.")

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
            messages.success(request, "HesabÄ±nÄ±z oluturuldu ve giri yapÄ±ldÄ±.")
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
            messages.success(request, "Usta hesabÄ± oluturuldu ve giri yapÄ±ldÄ±.")
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
            messages.success(request, "Giri baarÄ±lÄ±.")
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
            messages.success(request, "Usta girii baarÄ±lÄ±.")
            return redirect("provider_requests")
    else:
        form = ProviderLoginForm(request)

    return render(request, "Myapp/provider_login.html", {"form": form})


def logout_view(request):
    if request.method == "POST":
        logout(request)
        request.session.pop("role", None)
        messages.info(request, "Ä±kÄ± yapÄ±ldÄ±.")
    return redirect("index")


@login_required
@never_cache
@ensure_csrf_cookie
def provider_profile_view(request):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesaplarÄ± iÃ§indir.")
        return redirect("provider_login")

    if request.method == "POST":
        form = ProviderProfileForm(request.POST, instance=provider)
        if form.is_valid():
            form.save()
            messages.success(request, "Usta profilin gÃ¼ncellendi.")
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

    if service_request.status != "matched":
        messages.warning(request, "Tamamlanan veya kapalÄ± taleplerde mesajlaÅŸma aÃ§Ä±k deÄŸildir.")
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
        messages.error(request, "Bu alan sadece mÃ¼teri hesaplarÄ± iÃ§indir.")
        return redirect("provider_requests")

    requests = list(
        request.user.service_requests.select_related(
            "service_type",
            "matched_provider",
            "matched_offer",
            "matched_offer__provider",
        ).prefetch_related(
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
            "customer_requests_signature": build_customer_requests_signature(request.user),
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

    agreements = list(agreements_qs)
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
        total_quote=Sum("matched_offer__quote_amount"),
    )
    return render(
        request,
        "Myapp/agreement_history.html",
        {
            "agreements": agreements,
            "is_provider_user": bool(provider),
            "summary_total_count": summary.get("total_count", 0) or 0,
            "summary_completed_count": summary.get("completed_count", 0) or 0,
            "summary_matched_count": summary.get("matched_count", 0) or 0,
            "summary_total_quote": summary.get("total_quote"),
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
        messages.error(request, 'Hesap silme onayÄ± iÃ§in "HESABIMI SÄ°L" yazmalÄ±sÄ±nÄ±z.')
        return redirect(f"{reverse('account_settings')}?tab=danger")

    if not request.user.check_password(password):
        messages.error(request, "Åifre doÄŸrulamasÄ± baÅŸarÄ±sÄ±z.")
        return redirect(f"{reverse('account_settings')}?tab=danger")

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
    messages.success(request, "HesabÄ±nÄ±z kalÄ±cÄ± olarak silindi.")
    return redirect("index")


@login_required
@never_cache
def customer_requests_snapshot(request):
    if get_provider_for_user(request.user):
        return JsonResponse({"detail": "forbidden"}, status=403)
    response = JsonResponse({"signature": build_customer_requests_signature(request.user)})
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@login_required
def complete_request(request, request_id):
    if request.method != "POST":
        return redirect("my_requests")
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece mÃ¼teri hesaplarÄ± iÃ§indir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "matched":
        messages.warning(request, "Sadece eleen talepler tamamlandÄ± olarak iaretlenebilir.")
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
    messages.success(request, "Talep tamamlandÄ± olarak gÃ¼ncellendi.")
    return redirect("my_requests")


@login_required
@require_POST
def create_appointment(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece mÃ¼teri hesaplarÄ± iÃ§indir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "matched" or service_request.matched_provider is None:
        messages.warning(request, "Randevu sadece eleen talepler iÃ§in oluturulabilir.")
        return redirect("my_requests")

    form = AppointmentCreateForm(request.POST)
    if not form.is_valid():
        messages.error(request, get_first_form_error(form))
        return redirect("my_requests")

    existing = ServiceAppointment.objects.filter(service_request=service_request).first()
    if existing and existing.status == "completed":
        messages.warning(request, "Tamamlanan bir talep iÃ§in yeni randevu oluturulamaz.")
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
        messages.success(request, "Randevu talebiniz gÃ¼ncellendi ve ustaya iletildi.")
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
        messages.error(request, "Bu alan sadece mÃ¼teri hesaplarÄ± iÃ§indir.")
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
        messages.error(request, "Bu alan sadece mÃ¼teri hesaplarÄ± iÃ§indir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    appointment = get_object_or_404(ServiceAppointment, service_request=service_request)
    if appointment.status != "pending_customer":
        messages.warning(request, "Onay bekleyen bir randevu bulunamadÄ±.")
        return redirect("my_requests")

    appointment.status = "confirmed"
    appointment.save(update_fields=["status", "updated_at"])
    send_sms(
        appointment.provider.phone,
        (
            f"UstaBul randevu: MÃ¼teri Talep #{service_request.id} randevusunu onayladÄ±. "
            f"Tarih: {timezone.localtime(appointment.scheduled_for).strftime('%d.%m %H:%M')}"
        ),
    )
    messages.success(request, "Randevuyu onayladÄ±nÄ±z.")
    return redirect("my_requests")


@login_required
@require_POST
def cancel_request(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece mÃ¼teri hesaplarÄ± iÃ§indir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status not in {"new", "pending_provider", "pending_customer"} or service_request.matched_provider is not None:
        messages.warning(request, "Bu talep artik iptal edilemez.")
        return redirect("my_requests")

    now = timezone.now()
    service_request.provider_offers.filter(status__in=["pending", "accepted"]).update(status="expired", responded_at=now)
    service_request.status = "cancelled"
    service_request.matched_provider = None
    service_request.matched_offer = None
    service_request.matched_at = None
    service_request.save(update_fields=["status", "matched_provider", "matched_offer", "matched_at"])
    messages.success(request, "Talep aramasi iptal edildi.")
    return redirect("my_requests")


@login_required
@require_POST
def delete_cancelled_request(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece mÃ¼teri hesaplarÄ± iÃ§indir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "cancelled":
        messages.warning(request, "Sadece iptal edilen talepler silinebilir.")
        return redirect("my_requests")

    service_request.delete()
    messages.success(request, "Ä°ptal edilen talep silindi.")
    return redirect("my_requests")


@login_required
@require_POST
def delete_all_cancelled_requests(request):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece mÃ¼teri hesaplarÄ± iÃ§indir.")
        return redirect("provider_requests")

    deleted_count, _ = request.user.service_requests.filter(status="cancelled").delete()
    if deleted_count:
        messages.success(request, "Ä°ptal edilen talepler silindi.")
    else:
        messages.info(request, "Silinecek iptal edilen talep bulunamadÄ±.")
    return redirect("my_requests")


@login_required
@require_POST
def select_provider_offer(request, request_id, offer_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece mÃ¼ÅŸteri hesaplarÄ± iÃ§indir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status not in {"pending_provider", "pending_customer"} or service_request.matched_provider is not None:
        messages.warning(request, "Bu talep iÃ§in usta seÃ§imi artÄ±k yapÄ±lamaz.")
        return redirect("my_requests")

    with transaction.atomic():
        service_request = ServiceRequest.objects.select_for_update().filter(id=service_request.id).first()
        if not service_request:
            messages.warning(request, "Talep bulunamadÄ±.")
            return redirect("my_requests")
        if service_request.status not in {"pending_provider", "pending_customer"} or service_request.matched_provider_id is not None:
            messages.warning(request, "Bu talep zaten eÅŸleÅŸtirilmiÅŸ.")
            return redirect("my_requests")

        selected_offer = (
            ProviderOffer.objects.select_for_update()
            .select_related("provider")
            .filter(
                id=offer_id,
                service_request=service_request,
                status="accepted",
            )
            .first()
        )
        if not selected_offer:
            messages.warning(request, "Bu teklif artÄ±k seÃ§ilemez.")
            return redirect("my_requests")

        now = timezone.now()
        ProviderOffer.objects.filter(service_request=service_request).exclude(id=selected_offer.id).filter(
            status__in=["pending", "accepted"]
        ).update(status="expired", responded_at=now)
        service_request.matched_provider = selected_offer.provider
        service_request.matched_offer = selected_offer
        service_request.matched_at = now
        service_request.status = "matched"
        service_request.save(update_fields=["matched_provider", "matched_offer", "matched_at", "status"])

    messages.success(request, f"Talep #{service_request.id} iÃ§in {selected_offer.provider.full_name} seÃ§ildi.")
    return redirect("my_requests")

@login_required
def provider_requests(request):
    refresh_offer_lifecycle()
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesaplarÄ± iÃ§indir.")
        return redirect("provider_login")
    wallet = get_or_create_provider_wallet(provider)

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
        provider.service_requests.filter(status="matched")
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
            "wallet": wallet,
            "quote_credit_cost": get_quote_credit_cost(),
            "pending_offers": pending_offers,
            "recent_offers": recent_offers,
            "pending_appointments": pending_appointments,
            "waiting_customer_appointments": waiting_customer_appointments,
            "confirmed_appointments": confirmed_appointments,
            "recent_appointments": recent_appointments,
            "active_threads": active_threads,
        },
    )


@login_required
@never_cache
def provider_panel_snapshot(request):
    provider = get_provider_for_user(request.user)
    if not provider:
        return JsonResponse({"detail": "forbidden"}, status=403)

    refresh_offer_lifecycle()
    wallet = get_or_create_provider_wallet(provider)
    pending_offers_qs = provider.offers.filter(status="pending").order_by("-sent_at")
    latest_pending_offer = pending_offers_qs.values("id").first()

    payload = {
        "wallet_balance": wallet.balance,
        "pending_offers_count": pending_offers_qs.count(),
        "latest_pending_offer_id": latest_pending_offer["id"] if latest_pending_offer else 0,
        "pending_appointments_count": provider.appointments.filter(status="pending").count(),
        "waiting_customer_appointments_count": provider.appointments.filter(status="pending_customer").count(),
    }
    response = JsonResponse(payload)
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@login_required
@never_cache
@ensure_csrf_cookie
def provider_packages(request):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesaplarÄ± iÃ§indir.")
        return redirect("provider_login")

    packages = get_provider_package_catalog()
    package_map = {package["key"]: package for package in packages}
    quote_credit_cost = get_quote_credit_cost()

    if request.method == "POST":
        package_key = (request.POST.get("package_key") or "").strip()
        selected_package = package_map.get(package_key)
        if not selected_package:
            messages.warning(request, "GeÃ§ersiz paket seÃ§imi.")
            return redirect("provider_packages")

        wallet = apply_wallet_transaction(
            provider=provider,
            amount=selected_package["credits"],
            transaction_type="package_purchase",
            note=f"{selected_package['name']} paketi satÄ±n alÄ±ndÄ± ({selected_package['price']} TL).",
        )
        if wallet is None:
            messages.error(request, "Paket satÄ±n alma sÄ±rasÄ±nda bir sorun olutu.")
            return redirect("provider_packages")

        messages.success(
            request,
            f"{selected_package['name']} paketi aktif edildi. +{selected_package['credits']} kredi yÃ¼klendi.",
        )
        return redirect("provider_packages")

    wallet = get_or_create_provider_wallet(provider)
    transactions = list(provider.credit_transactions.select_related("reference_offer").order_by("-created_at")[:30])
    return render(
        request,
        "Myapp/provider_packages.html",
        {
            "provider": provider,
            "wallet": wallet,
            "packages": packages,
            "transactions": transactions,
            "quote_credit_cost": quote_credit_cost,
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
        messages.error(request, "Bu alan sadece usta hesaplarÄ± iÃ§indir.")
        return redirect("provider_login")

    appointment = get_object_or_404(
        ServiceAppointment.objects.select_related("service_request"),
        id=appointment_id,
        provider=provider,
    )
    if appointment.status != "pending":
        messages.warning(request, "Bu randevu talebi artÄ±k aÃ§Ä±k deil.")
        return redirect("provider_requests")

    provider_note = (request.POST.get("provider_note") or "").strip()
    appointment.status = "pending_customer"
    appointment.provider_note = provider_note
    appointment.save(update_fields=["status", "provider_note", "updated_at"])
    send_sms(
        appointment.service_request.customer_phone,
        (
            f"UstaBul randevu: Talep #{appointment.service_request_id} iÃ§in usta onayÄ± verildi. "
            "MÃ¼teri panelinden son onayÄ± tamamlayÄ±n."
        ),
    )
    messages.success(request, f"Talep #{appointment.service_request_id} iÃ§in usta onayÄ± verildi. MÃ¼teri onayÄ± bekleniyor.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_complete_appointment(request, appointment_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesaplarÄ± iÃ§indir.")
        return redirect("provider_login")

    appointment = get_object_or_404(
        ServiceAppointment.objects.select_related("service_request"),
        id=appointment_id,
        provider=provider,
    )
    if appointment.status != "confirmed":
        messages.warning(request, "Sadece onaylÄ± randevular tamamlanabilir.")
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
    messages.success(request, f"Talep #{service_request.id} randevusu tamamlandÄ± olarak iaretlendi.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_reject_appointment(request, appointment_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesaplarÄ± iÃ§indir.")
        return redirect("provider_login")

    appointment = get_object_or_404(
        ServiceAppointment.objects.select_related("service_request"),
        id=appointment_id,
        provider=provider,
    )
    if appointment.status != "pending":
        messages.warning(request, "Bu randevu talebi artÄ±k aÃ§Ä±k deil.")
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
        messages.error(request, "Bu alan sadece usta hesaplarÄ± iÃ§indir.")
        return redirect("provider_login")

    with transaction.atomic():
        offer = (
            ProviderOffer.objects.select_for_update()
            .select_related("service_request")
            .filter(id=offer_id, provider=provider)
            .first()
        )
        if not offer:
            messages.warning(request, "Teklif bulunamadÄ±.")
            return redirect("provider_requests")

        service_request = ServiceRequest.objects.select_for_update().filter(id=offer.service_request_id).first()
        if not service_request:
            messages.warning(request, "Talep artÄ±k mevcut deil.")
            return redirect("provider_requests")

        if offer.status != "pending":
            messages.warning(request, "Bu teklif artÄ±k aÃ§Ä±k deil.")
            return redirect("provider_requests")

        if service_request.status in {"matched", "completed", "cancelled"}:
            offer.status = "expired"
            offer.responded_at = timezone.now()
            offer.save(update_fields=["status", "responded_at"])
            messages.warning(request, "Bu talep artÄ±k aÃ§Ä±k deil.")
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
        quote_credit_cost = get_quote_credit_cost()
        wallet = get_or_create_provider_wallet(provider, for_update=True)
        if wallet.balance < quote_credit_cost:
            messages.warning(
                request,
                f"Teklif gÃ¶ndermek iÃ§in en az {quote_credit_cost} kredi gerekli. Mevcut kredi: {wallet.balance}.",
            )
            return redirect("provider_packages")

        now = timezone.now()
        offer.status = "accepted"
        offer.responded_at = now
        offer.quote_amount = quote_amount
        offer.quote_note = quote_note
        offer.save(update_fields=["status", "responded_at", "quote_amount", "quote_note"])

        wallet.balance -= quote_credit_cost
        wallet.save(update_fields=["balance", "updated_at"])
        CreditTransaction.objects.create(
            provider=provider,
            wallet=wallet,
            transaction_type="quote_fee",
            amount=-quote_credit_cost,
            balance_after=wallet.balance,
            note=f"Talep #{service_request.id} iÃ§in teklif kredisi dÃ¼Ã¼ldÃ¼.",
            reference_offer=offer,
        )

        service_request.status = "pending_customer"
        service_request.save(update_fields=["status"])

    messages.success(request, f"Talep #{service_request.id} iÃ§in teklifiniz mÃ¼teriye gÃ¶nderildi.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_reject_offer(request, offer_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesaplarÄ± iÃ§indir.")
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
            f"Talep #{service_request.id} reddedildi. Dier ustalardan gelecek onay bekleniyor.",
        )
        return redirect("provider_requests")

    if has_accepted_offer:
        service_request.status = "pending_customer"
        service_request.save(update_fields=["status"])
        messages.info(request, f"Talep #{service_request.id} reddedildi. MÃ¼terinin teklif seÃ§imi bekleniyor.")
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
            f"Talep #{request_id} iÃ§in kabul eden usta bulunamadÄ±, talep silindi.",
        )
    return redirect("provider_requests")
