from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.contrib.auth.models import User
from unittest.mock import patch

from .models import CustomerProfile, Provider, ProviderOffer, ProviderRating, ServiceRequest, ServiceType
from .notifications import normalize_phone_for_whatsapp, send_whatsapp_via_twilio


class MarketplaceTests(TestCase):
    def setUp(self):
        self.service = ServiceType.objects.create(name="Tesisat", slug="tesisat")
        self.provider_ali = Provider.objects.create(
            full_name="Ali Usta",
            service_type=self.service,
            city="Lefkosa",
            district="Ortakoy",
            phone="05550000000",
            latitude=41.015000,
            longitude=29.020000,
            rating=4.8,
            is_available=True,
        )
        self.provider_mehmet = Provider.objects.create(
            full_name="Mehmet Usta",
            service_type=self.service,
            city="Girne",
            district="Karakum",
            phone="05551111111",
            latitude=40.980000,
            longitude=29.300000,
            rating=4.9,
            is_available=True,
        )

    def test_home_page_loads(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mahallendeki En Iyi Ustalari")

    def test_service_request_creates_record(self):
        with patch("Myapp.views.send_provider_offer_notification", return_value={"attempted": True, "sent": True, "detail": "sent"}):
            response = self.client.post(
                reverse("create_request"),
                data={
                    "customer_name": "Ayse Yilmaz",
                    "customer_phone": "05000000000",
                    "service_type": self.service.id,
                    "city": "Lefkosa",
                    "district": "Ortakoy",
                    "details": "Mutfakta su kacagi var.",
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "musaitlik soruldu")
        latest = ServiceRequest.objects.latest("created_at")
        self.assertEqual(latest.status, "pending_provider")
        self.assertTrue(ProviderOffer.objects.filter(service_request=latest, status="pending").exists())

    def test_location_search_sorts_nearest_provider_first(self):
        response = self.client.get(
            reverse("index"),
            data={"latitude": 41.015, "longitude": 29.021},
        )
        self.assertEqual(response.status_code, 200)
        providers = response.context["providers"]
        self.assertGreaterEqual(len(providers), 2)
        self.assertEqual(providers[0].full_name, "Ali Usta")

    def test_customer_can_signup(self):
        response = self.client.post(
            reverse("signup"),
            data={
                "username": "musteri1",
                "first_name": "Ayse",
                "last_name": "Yilmaz",
                "email": "ayse@example.com",
                "phone": "05000000000",
                "city": "Lefkosa",
                "district": "Ortakoy",
                "password1": "GucluSifre123!",
                "password2": "GucluSifre123!",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(username="musteri1").exists())
        self.assertTrue(CustomerProfile.objects.filter(user__username="musteri1").exists())

    def test_logged_in_customer_request_is_bound_to_user(self):
        user = User.objects.create_user(username="musteri2", password="GucluSifre123!")
        self.client.login(username="musteri2", password="GucluSifre123!")

        with patch("Myapp.views.send_provider_offer_notification", return_value={"attempted": True, "sent": True, "detail": "sent"}):
            self.client.post(
                reverse("create_request"),
                data={
                    "customer_name": "Musteri Iki",
                    "customer_phone": "05001112233",
                    "service_type": self.service.id,
                    "city": "Girne",
                    "district": "Karakum",
                    "details": "Banyo tesisatinda sorun var.",
                },
                follow=True,
            )

        service_request = ServiceRequest.objects.latest("created_at")
        self.assertEqual(service_request.customer, user)
        self.assertEqual(service_request.status, "pending_provider")

    def test_customer_can_rate_matched_provider(self):
        user = User.objects.create_user(username="puanlayan", password="GucluSifre123!")
        self.client.login(username="puanlayan", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Puanlayan Musteri",
            customer_phone="05001231234",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Test talebi",
            matched_provider=self.provider_ali,
            customer=user,
            status="completed",
        )

        self.client.post(
            reverse("rate_request", args=[service_request.id]),
            data={"score": 5, "comment": "Cok hizli cozum sagladi."},
            follow=True,
        )

        self.assertTrue(
            ProviderRating.objects.filter(provider=self.provider_ali, customer=user, score=5).exists()
        )
        self.provider_ali.refresh_from_db()
        self.assertEqual(float(self.provider_ali.rating), 5.0)

    def test_customer_cannot_rate_without_match(self):
        user = User.objects.create_user(username="eslesmesiz", password="GucluSifre123!")
        self.client.login(username="eslesmesiz", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Eslesmesiz Musteri",
            customer_phone="05009998877",
            city="Girne",
            district="Karakum",
            service_type=self.service,
            details="Deneme",
            matched_provider=self.provider_mehmet,
            customer=user,
            status="matched",
        )
        self.client.post(
            reverse("rate_request", args=[service_request.id]),
            data={"score": 3, "comment": "Deneme"},
            follow=True,
        )
        self.assertFalse(
            ProviderRating.objects.filter(provider=self.provider_mehmet, customer=user).exists()
        )

    def test_customer_can_complete_matched_request(self):
        user = User.objects.create_user(username="tamamlayan", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Tamamlayan Musteri",
            customer_phone="05000001122",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Tamamlama testi",
            matched_provider=self.provider_ali,
            customer=user,
            status="matched",
        )
        self.client.login(username="tamamlayan", password="GucluSifre123!")

        self.client.post(reverse("complete_request", args=[service_request.id]), follow=True)
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, "completed")

    def test_customer_can_rate_same_provider_for_different_requests(self):
        user = User.objects.create_user(username="coklu", password="GucluSifre123!")
        self.client.login(username="coklu", password="GucluSifre123!")

        req1 = ServiceRequest.objects.create(
            customer_name="Coklu Musteri",
            customer_phone="05000000001",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Ilk is",
            matched_provider=self.provider_ali,
            customer=user,
            status="completed",
        )
        req2 = ServiceRequest.objects.create(
            customer_name="Coklu Musteri",
            customer_phone="05000000001",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Ikinci is",
            matched_provider=self.provider_ali,
            customer=user,
            status="completed",
        )

        self.client.post(reverse("rate_request", args=[req1.id]), data={"score": 5, "comment": "Ilk puan"}, follow=True)
        self.client.post(reverse("rate_request", args=[req2.id]), data={"score": 3, "comment": "Ikinci puan"}, follow=True)

        self.assertEqual(
            ProviderRating.objects.filter(provider=self.provider_ali, customer=user).count(),
            2,
        )

    @override_settings(TWILIO_VALIDATE_WEBHOOK_SIGNATURE=False)
    def test_provider_can_accept_offer_from_whatsapp_webhook(self):
        with patch("Myapp.views.send_provider_offer_notification", return_value={"attempted": True, "sent": True, "detail": "sent"}):
            self.client.post(
                reverse("create_request"),
                data={
                    "customer_name": "Webhook Musteri",
                    "customer_phone": "05000000000",
                    "service_type": self.service.id,
                    "city": "Lefkosa",
                    "district": "Ortakoy",
                    "details": "Webhook kabul testi",
                },
                follow=True,
            )

        service_request = ServiceRequest.objects.latest("created_at")
        offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_ali)
        self.client.post(
            reverse("whatsapp_webhook"),
            data={"From": "whatsapp:+905550000000", "Body": f"KABUL {offer.token}"},
        )

        service_request.refresh_from_db()
        offer.refresh_from_db()
        self.assertEqual(service_request.status, "matched")
        self.assertEqual(service_request.matched_provider, self.provider_ali)
        self.assertEqual(offer.status, "accepted")

    @override_settings(TWILIO_VALIDATE_WEBHOOK_SIGNATURE=False)
    def test_provider_reject_moves_offer_to_next_provider(self):
        second_provider = Provider.objects.create(
            full_name="Hasan Usta",
            service_type=self.service,
            city="Lefkosa",
            district="Ortakoy",
            phone="05559998877",
            rating=4.0,
            is_available=True,
        )

        with patch("Myapp.views.send_provider_offer_notification", return_value={"attempted": True, "sent": True, "detail": "sent"}):
            self.client.post(
                reverse("create_request"),
                data={
                    "customer_name": "Webhook Red Musteri",
                    "customer_phone": "05000000000",
                    "service_type": self.service.id,
                    "city": "Lefkosa",
                    "district": "Ortakoy",
                    "details": "Webhook red testi",
                },
                follow=True,
            )
            service_request = ServiceRequest.objects.latest("created_at")
            first_offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_ali)
            self.client.post(
                reverse("whatsapp_webhook"),
                data={"From": "whatsapp:+905550000000", "Body": f"RED {first_offer.token}"},
            )

        service_request.refresh_from_db()
        first_offer.refresh_from_db()
        second_offer = ProviderOffer.objects.get(service_request=service_request, provider=second_provider)
        self.assertEqual(first_offer.status, "rejected")
        self.assertEqual(second_offer.status, "pending")
        self.assertEqual(service_request.status, "pending_provider")

    def test_webhook_rejects_invalid_signature_by_default(self):
        response = self.client.post(
            reverse("whatsapp_webhook"),
            data={"From": "whatsapp:+905550000000", "Body": "KABUL TESTTOKEN"},
        )
        self.assertEqual(response.status_code, 403)

    def test_phone_normalization_for_whatsapp(self):
        self.assertEqual(normalize_phone_for_whatsapp("0555 000 00 00"), "+905550000000")
        self.assertEqual(normalize_phone_for_whatsapp("+905551112233"), "+905551112233")

    @override_settings(
        WHATSAPP_NOTIFICATIONS_ENABLED=True,
        TWILIO_ACCOUNT_SID="",
        TWILIO_AUTH_TOKEN="",
        TWILIO_WHATSAPP_FROM="",
    )
    def test_whatsapp_send_skips_when_config_missing(self):
        result = send_whatsapp_via_twilio("05550000000", "test")
        self.assertFalse(result["sent"])
        self.assertFalse(result["attempted"])
