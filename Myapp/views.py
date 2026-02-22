import json
from math import asin, cos, radians, sin, sqrt
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .constants import NC_CITY_DISTRICT_MAP
from .forms import (
    ANY_DISTRICT_VALUE,
    CustomerLoginForm,
    CustomerSignupForm,
    ProviderLoginForm,
    ProviderRatingForm,
    ServiceRequestForm,
    ServiceSearchForm,
)
from .models import CustomerProfile, Provider, ProviderOffer, ProviderRating, ServiceRequest


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
    pending_qs = service_request.provider_offers.filter(status="pending").exclude(id=exclude_offer_id)
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
    }
    return render(request, "Myapp/index.html", context)


def create_request(request):
    if request.method != "POST":
        return redirect("index")

    provider_user = get_provider_for_user(request.user) if request.user.is_authenticated else None
    if provider_user:
        messages.error(request, "Usta hesabi ile talep olusturamazsiniz.")
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
        provider_name = dispatch_result["offers"][0].provider.full_name
        messages.success(
            request,
            f"Talebiniz alindi. {offer_count} ustaya panelde onay icin iletildi. Ilk onaylayan: {provider_name} ve digerleri.",
        )
    elif dispatch_result["result"] == "no-candidates":
        messages.info(
            request,
            "Talebiniz alindi ancak su an sehir/ilce kriterlerinde musait usta bulunamadi.",
        )
    else:
        messages.warning(
            request,
            "Talebiniz kaydedildi fakat su an siradaki uygun usta bulunamadi.",
        )

    return redirect("index")


def contact(request):
    return render(request, "Myapp/Contact.html")


@login_required
def rate_request(request, request_id):
    if request.method != "POST":
        return redirect("my_requests")
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece musteri hesaplari icindir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "completed" or service_request.matched_provider is None:
        messages.error(request, "Puanlama sadece tamamlanmis ve eslesmis talepler icin yapilabilir.")
        return redirect("my_requests")

    current_rating = getattr(service_request, "provider_rating", None)
    if current_rating is not None:
        messages.warning(request, "Bu talep icin puan zaten verildi. Guncelleme yapilamaz.")
        return redirect("my_requests")

    form = ProviderRatingForm(request.POST)
    if form.is_valid():
        rating = form.save(commit=False)
        rating.service_request = service_request
        rating.provider = service_request.matched_provider
        rating.customer = request.user
        rating.save()
        messages.success(request, f"{service_request.matched_provider.full_name} icin puaniniz kaydedildi.")
    else:
        messages.error(request, "Puan kaydedilemedi. Lutfen gecerli bir puan secin.")

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
            messages.success(request, "Hesabiniz olusturuldu ve giris yapildi.")
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
def login_view(request):
    if request.user.is_authenticated:
        return redirect("provider_requests") if get_provider_for_user(request.user) else redirect("index")

    if request.method == "POST":
        form = CustomerLoginForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            request.session["role"] = "customer"
            messages.success(request, "Giris basarili.")
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
            messages.success(request, "Usta girisi basarili.")
            return redirect("provider_requests")
    else:
        form = ProviderLoginForm(request)

    return render(request, "Myapp/provider_login.html", {"form": form})


def logout_view(request):
    if request.method == "POST":
        logout(request)
        request.session.pop("role", None)
        messages.info(request, "Cikis yapildi.")
    return redirect("index")


@login_required
def my_requests(request):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece musteri hesaplari icindir.")
        return redirect("provider_requests")

    requests = list(
        request.user.service_requests.select_related("service_type", "matched_provider").prefetch_related("provider_offers")
    )
    rating_map = {
        rating.service_request_id: rating
        for rating in ProviderRating.objects.filter(service_request_id__in=[item.id for item in requests])
    }
    for item in requests:
        item.rating_entry = rating_map.get(item.id)
        item.pending_offer = next((offer for offer in item.provider_offers.all() if offer.status == "pending"), None)
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
        messages.error(request, "Bu alan sadece musteri hesaplari icindir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "matched":
        messages.warning(request, "Sadece eslesen talepler tamamlandi olarak isaretlenebilir.")
        return redirect("my_requests")

    service_request.status = "completed"
    service_request.save(update_fields=["status"])
    messages.success(request, "Talep tamamlandi olarak guncellendi.")
    return redirect("my_requests")


@login_required
@require_POST
def cancel_request(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece musteri hesaplari icindir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status not in {"new", "pending_provider"} or service_request.matched_provider is not None:
        messages.warning(request, "Bu talep artik iptal edilemez.")
        return redirect("my_requests")

    now = timezone.now()
    service_request.provider_offers.filter(status="pending").update(status="expired", responded_at=now)
    service_request.status = "cancelled"
    service_request.matched_provider = None
    service_request.save(update_fields=["status", "matched_provider"])
    messages.success(request, "Talep aramasi iptal edildi.")
    return redirect("my_requests")


@login_required
@require_POST
def delete_cancelled_request(request, request_id):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece musteri hesaplari icindir.")
        return redirect("provider_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "cancelled":
        messages.warning(request, "Sadece iptal edilen talepler silinebilir.")
        return redirect("my_requests")

    service_request.delete()
    messages.success(request, "Iptal edilen talep silindi.")
    return redirect("my_requests")


@login_required
@require_POST
def delete_all_cancelled_requests(request):
    if get_provider_for_user(request.user):
        messages.error(request, "Bu alan sadece musteri hesaplari icindir.")
        return redirect("provider_requests")

    deleted_count, _ = request.user.service_requests.filter(status="cancelled").delete()
    if deleted_count:
        messages.success(request, "Iptal edilen talepler silindi.")
    else:
        messages.info(request, "Silinecek iptal edilen talep bulunamadi.")
    return redirect("my_requests")


@login_required
def provider_requests(request):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesaplari icindir.")
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
    return render(
        request,
        "Myapp/provider_requests.html",
        {
            "provider": provider,
            "pending_offers": pending_offers,
            "recent_offers": recent_offers,
        },
    )


@login_required
@require_POST
def provider_accept_offer(request, offer_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesaplari icindir.")
        return redirect("provider_login")

    with transaction.atomic():
        offer = (
            ProviderOffer.objects.select_for_update()
            .select_related("service_request")
            .filter(id=offer_id, provider=provider)
            .first()
        )
        if not offer:
            messages.warning(request, "Teklif bulunamadi.")
            return redirect("provider_requests")

        service_request = ServiceRequest.objects.select_for_update().filter(id=offer.service_request_id).first()
        if not service_request:
            messages.warning(request, "Talep artik mevcut degil.")
            return redirect("provider_requests")

        if offer.status != "pending":
            messages.warning(request, "Bu teklif artik acik degil.")
            return redirect("provider_requests")

        if service_request.status == "matched" and service_request.matched_provider_id != provider.id:
            offer.status = "expired"
            offer.responded_at = timezone.now()
            offer.save(update_fields=["status", "responded_at"])
            messages.warning(request, "Bu talep baska bir usta tarafindan kabul edildi.")
            return redirect("provider_requests")

        now = timezone.now()
        offer.status = "accepted"
        offer.responded_at = now
        offer.save(update_fields=["status", "responded_at"])
        set_other_pending_offers_expired(service_request, exclude_offer_id=offer.id)
        service_request.matched_provider = provider
        service_request.status = "matched"
        service_request.save(update_fields=["matched_provider", "status"])

    messages.success(request, f"Talep #{service_request.id} kabul edildi.")
    return redirect("provider_requests")


@login_required
@require_POST
def provider_reject_offer(request, offer_id):
    provider = get_provider_for_user(request.user)
    if not provider:
        messages.error(request, "Bu alan sadece usta hesaplari icindir.")
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

    if service_request.provider_offers.filter(status="pending").exists():
        messages.info(
            request,
            f"Talep #{service_request.id} reddedildi. Diger ustalardan gelecek onay bekleniyor.",
        )
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
            f"Talep #{request_id} icin kabul eden usta bulunamadi, talep silindi.",
        )
    return redirect("provider_requests")
