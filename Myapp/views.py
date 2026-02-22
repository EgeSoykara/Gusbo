from django.contrib import messages
from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from math import asin, cos, radians, sin, sqrt
import json
from uuid import uuid4

from .constants import NC_CITY_DISTRICT_MAP
from .forms import CustomerLoginForm, CustomerSignupForm, ProviderRatingForm, ServiceRequestForm, ServiceSearchForm
from .models import CustomerProfile, Provider, ProviderOffer, ProviderRating, ServiceRequest
from .notifications import (
    is_valid_twilio_signature,
    normalize_phone_for_whatsapp,
    send_provider_offer_notification,
    strip_whatsapp_prefix,
)


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


def get_city_district_map_json():
    return json.dumps(NC_CITY_DISTRICT_MAP)


def generate_offer_token():
    token = uuid4().hex[:10].upper()
    while ProviderOffer.objects.filter(token=token).exists():
        token = uuid4().hex[:10].upper()
    return token


def build_provider_candidates(service_request):
    base_qs = Provider.objects.filter(
        is_available=True,
        service_type=service_request.service_type,
        city__iexact=service_request.city,
    )
    district_first = list(base_qs.filter(district__iexact=service_request.district).order_by("-rating", "full_name"))
    remaining_city = list(
        base_qs.exclude(id__in=[provider.id for provider in district_first]).order_by("-rating", "full_name")
    )
    return district_first + remaining_city


def set_other_pending_offers_expired(service_request, exclude_offer_id):
    pending_qs = service_request.provider_offers.filter(status="pending").exclude(id=exclude_offer_id)
    pending_qs.update(status="expired", responded_at=timezone.now())


def dispatch_next_provider_offer(service_request):
    candidates = build_provider_candidates(service_request)
    if not candidates:
        service_request.status = "new"
        service_request.matched_provider = None
        service_request.save(update_fields=["status", "matched_provider"])
        return {"result": "no-candidates"}

    offered_provider_ids = set(service_request.provider_offers.values_list("provider_id", flat=True))
    next_sequence = service_request.provider_offers.count() + 1
    now = timezone.now()

    for provider in candidates:
        if provider.id in offered_provider_ids:
            continue

        offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=provider,
            token=generate_offer_token(),
            sequence=next_sequence,
            status="pending",
            sent_at=now,
        )
        send_result = send_provider_offer_notification(provider, service_request, offer.token)
        offer.last_delivery_detail = send_result["detail"]

        if send_result["sent"]:
            offer.save(update_fields=["last_delivery_detail"])
            service_request.status = "pending_provider"
            service_request.matched_provider = None
            service_request.save(update_fields=["status", "matched_provider"])
            return {"result": "offer-sent", "offer": offer}

        offer.status = "failed"
        offer.responded_at = now
        offer.save(update_fields=["last_delivery_detail", "status", "responded_at"])
        next_sequence += 1

    service_request.status = "new"
    service_request.matched_provider = None
    service_request.save(update_fields=["status", "matched_provider"])
    return {"result": "delivery-failed"}


def parse_provider_reply(body_text):
    text = (body_text or "").strip().upper()
    if not text:
        return None, None

    parts = text.split()
    if len(parts) < 2:
        return None, None

    command = parts[0]
    token = parts[1].strip().upper()
    if command in {"KABUL", "ACCEPT", "ONAY"}:
        return "accept", token
    if command in {"RED", "REJECT", "RET"}:
        return "reject", token
    return None, token


def twiml_message(text):
    return HttpResponse(
        f"<Response><Message>{text}</Message></Response>",
        content_type="application/xml",
    )


def index(request):
    search_form = ServiceSearchForm(request.GET or None)
    providers_qs = (
        Provider.objects.filter(is_available=True)
        .select_related("service_type")
        .annotate(ratings_count=Count("ratings"))
    )
    location_used = False

    if search_form.is_valid():
        service_type = search_form.cleaned_data.get("service_type")
        city = (search_form.cleaned_data.get("city") or "").strip()
        district = (search_form.cleaned_data.get("district") or "").strip()
        user_latitude = search_form.cleaned_data.get("latitude")
        user_longitude = search_form.cleaned_data.get("longitude")

        if service_type:
            providers_qs = providers_qs.filter(service_type=service_type)
        if city:
            providers_qs = providers_qs.filter(city__icontains=city)
        if district:
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
    }
    return render(request, "Myapp/index.html", context)


def create_request(request):
    if request.method != "POST":
        return redirect("index")

    request_form = ServiceRequestForm(request.POST)
    if not request_form.is_valid():
        search_form = ServiceSearchForm()
        providers = list(
            Provider.objects.filter(is_available=True)
            .select_related("service_type")
            .annotate(ratings_count=Count("ratings"))[:12]
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
    if dispatch_result["result"] == "offer-sent":
        provider_name = dispatch_result["offer"].provider.full_name
        messages.success(
            request,
            f"Talebiniz alindi. {provider_name} ustasina WhatsApp ile musaitlik soruldu.",
        )
    elif dispatch_result["result"] == "no-candidates":
        messages.info(
            request,
            "Talebiniz alindi ancak su an sehir/ilce kriterlerinde musait usta bulunamadi.",
        )
    else:
        messages.warning(
            request,
            "Talebiniz kaydedildi fakat WhatsApp bildirimi gonderilemedigi icin ustaya iletilemedi.",
        )

    return redirect("index")


def contact(request):
    return render(request, "Myapp/Contact.html")


@csrf_exempt
def whatsapp_webhook(request):
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)
    if settings.TWILIO_VALIDATE_WEBHOOK_SIGNATURE and not is_valid_twilio_signature(request):
        return HttpResponse("Invalid signature", status=403)

    incoming_from = normalize_phone_for_whatsapp(strip_whatsapp_prefix(request.POST.get("From", "")))
    action, token = parse_provider_reply(request.POST.get("Body", ""))
    if not action or not token:
        return twiml_message("Mesaj anlasilamadi. Ornek: KABUL ABC123")

    offer = (
        ProviderOffer.objects.select_related("provider", "service_request")
        .filter(token=token, status="pending")
        .first()
    )
    if not offer:
        return twiml_message("Gecersiz veya kapanmis teklif kodu.")

    provider_phone = normalize_phone_for_whatsapp(offer.provider.phone)
    if incoming_from != provider_phone:
        return twiml_message("Bu teklif bu numara icin degil.")

    now = timezone.now()
    service_request = offer.service_request

    if action == "accept":
        offer.status = "accepted"
        offer.responded_at = now
        offer.save(update_fields=["status", "responded_at"])
        set_other_pending_offers_expired(service_request, exclude_offer_id=offer.id)
        service_request.matched_provider = offer.provider
        service_request.status = "matched"
        service_request.save(update_fields=["matched_provider", "status"])
        return twiml_message("Talep kabul edildi. Musteri ile iletisime gecebilirsiniz.")

    offer.status = "rejected"
    offer.responded_at = now
    offer.save(update_fields=["status", "responded_at"])
    set_other_pending_offers_expired(service_request, exclude_offer_id=offer.id)
    next_dispatch = dispatch_next_provider_offer(service_request)
    if next_dispatch["result"] == "offer-sent":
        return twiml_message("Talep reddedildi. Tesekkurler.")
    return twiml_message("Talep reddedildi. Siradaki uygun usta bulunamadi.")


@login_required
def rate_request(request, request_id):
    if request.method != "POST":
        return redirect("my_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "completed" or service_request.matched_provider is None:
        messages.error(request, "Puanlama sadece tamamlanmis ve eslesmis talepler icin yapilabilir.")
        return redirect("my_requests")

    current_rating = getattr(service_request, "provider_rating", None)
    form = ProviderRatingForm(request.POST, instance=current_rating)
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


def signup_view(request):
    if request.user.is_authenticated:
        return redirect("index")

    if request.method == "POST":
        form = CustomerSignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
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


def login_view(request):
    if request.user.is_authenticated:
        return redirect("index")

    if request.method == "POST":
        form = CustomerLoginForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            messages.success(request, "Giris basarili.")
            return redirect("index")
    else:
        form = CustomerLoginForm(request)

    return render(request, "Myapp/login.html", {"form": form})


def logout_view(request):
    if request.method == "POST":
        logout(request)
        messages.info(request, "Cikis yapildi.")
    return redirect("index")


@login_required
def my_requests(request):
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
    return render(request, "Myapp/my_requests.html", {"requests": requests})


@login_required
def complete_request(request, request_id):
    if request.method != "POST":
        return redirect("my_requests")

    service_request = get_object_or_404(ServiceRequest, id=request_id, customer=request.user)
    if service_request.status != "matched":
        messages.warning(request, "Sadece eslesen talepler tamamlandi olarak isaretlenebilir.")
        return redirect("my_requests")

    service_request.status = "completed"
    service_request.save(update_fields=["status"])
    messages.success(request, "Talep tamamlandi olarak guncellendi.")
    return redirect("my_requests")
