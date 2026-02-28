from datetime import time, timedelta

from django.test import TestCase, override_settings
from django.core.management import call_command
from django.core.cache import cache
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone
from io import StringIO

from .models import (
    CustomerProfile,
    EscrowPayment,
    IdempotencyRecord,
    Provider,
    ProviderAvailabilitySlot,
    ProviderOffer,
    ProviderRating,
    SchedulerHeartbeat,
    SchedulerLock,
    ServiceAppointment,
    ServiceMessage,
    ServiceRequest,
    ServiceType,
    WorkflowEvent,
)
from .views import (
    refresh_offer_lifecycle,
    transition_appointment_status,
    transition_service_request_status,
)


class MarketplaceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.service = ServiceType.objects.create(name="Tesisat", slug="tesisat")
        self.provider_user_ali = User.objects.create_user(username="aliusta", password="GucluSifre123!")
        self.provider_ali = Provider.objects.create(
            user=self.provider_user_ali,
            full_name="Ali Usta",
            city="Lefkosa",
            district="Ortakoy",
            phone="05550000000",
            latitude=41.015000,
            longitude=29.020000,
            rating=4.8,
            is_verified=True,
            is_available=True,
        )
        self.provider_ali.service_types.add(self.service)
        self.provider_user_mehmet = User.objects.create_user(username="mehmetusta", password="GucluSifre123!")
        self.provider_mehmet = Provider.objects.create(
            user=self.provider_user_mehmet,
            full_name="Mehmet Usta",
            city="Girne",
            district="Karakum",
            phone="05551111111",
            latitude=40.980000,
            longitude=29.300000,
            rating=4.9,
            is_verified=True,
            is_available=True,
        )
        self.provider_mehmet.service_types.add(self.service)
        self.provider_user_hasan = User.objects.create_user(username="hasanusta", password="GucluSifre123!")
        self.provider_hasan = Provider.objects.create(
            user=self.provider_user_hasan,
            full_name="Hasan Usta",
            city="Lefkosa",
            district="Ortakoy",
            phone="05559998877",
            rating=4.0,
            is_verified=True,
            is_available=True,
        )
        self.provider_hasan.service_types.add(self.service)

    def _future_datetime_local(self, days=1):
        return timezone.localtime(timezone.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")

    def test_home_page_loads(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mahallendeki En")

    def test_anonymous_user_cannot_create_request(self):
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
        self.assertContains(response, "Talep oluşturmak için giriş yapmalısınız.")
        self.assertFalse(ServiceRequest.objects.exists())

    @override_settings(ACTION_RATE_LIMIT_MAX_ATTEMPTS=1, ACTION_RATE_LIMIT_WINDOW_SECONDS=120)
    def test_create_request_rate_limit_blocks_second_submission(self):
        customer = User.objects.create_user(username="ratelimitmusteri", password="GucluSifre123!")
        self.client.login(username="ratelimitmusteri", password="GucluSifre123!")

        first_payload = {
            "customer_name": "Rate Limit Musteri",
            "customer_phone": "05000000000",
            "service_type": self.service.id,
            "city": "Lefkosa",
            "district": "Ortakoy",
            "details": "Ilk talep",
        }
        second_payload = {
            "customer_name": "Rate Limit Musteri",
            "customer_phone": "05000000000",
            "service_type": self.service.id,
            "city": "Lefkosa",
            "district": "Ortakoy",
            "details": "Ikinci talep",
        }
        self.client.post(reverse("create_request"), data=first_payload, follow=True)
        response = self.client.post(reverse("create_request"), data=second_payload, follow=True)

        self.assertContains(response, "Çok kısa sürede çok fazla istek gönderdiniz")
        self.assertEqual(ServiceRequest.objects.filter(customer=customer).count(), 1)

    def test_service_request_creates_record(self):
        customer = User.objects.create_user(username="talepmusteri", password="GucluSifre123!")
        self.client.login(username="talepmusteri", password="GucluSifre123!")
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
        self.assertContains(response, "ustaya teklif vermesi için iletildi")
        latest = ServiceRequest.objects.latest("created_at")
        self.assertEqual(latest.customer, customer)
        self.assertEqual(latest.status, "pending_provider")
        self.assertEqual(ProviderOffer.objects.filter(service_request=latest, status="pending").count(), 2)

    def test_service_request_normalizes_phone_input(self):
        User.objects.create_user(username="formatmusteri", password="GucluSifre123!")
        self.client.login(username="formatmusteri", password="GucluSifre123!")
        response = self.client.post(
            reverse("create_request"),
            data={
                "customer_name": "Format Test",
                "customer_phone": "+90 500 123 45 67",
                "service_type": self.service.id,
                "city": "Lefkosa",
                "district": "Ortakoy",
                "details": "Format denemesi",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        created_request = ServiceRequest.objects.latest("created_at")
        self.assertEqual(created_request.customer_phone, "05001234567")

    def test_location_search_sorts_nearest_provider_first(self):
        response = self.client.get(
            reverse("index"),
            data={"latitude": 41.015, "longitude": 29.021},
        )
        self.assertEqual(response.status_code, 200)
        providers = response.context["providers"]
        self.assertGreaterEqual(len(providers), 2)
        self.assertEqual(providers[0].full_name, "Ali Usta")

    def test_index_provider_cards_do_not_render_verified_badge(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "provider-verify-badge")

    def test_search_with_any_district_does_not_filter_out_city_results(self):
        response = self.client.get(
            reverse("index"),
            data={"city": "Lefkosa", "district": "Herhangi"},
        )
        self.assertEqual(response.status_code, 200)
        providers = response.context["providers"]
        self.assertTrue(any(provider.city == "Lefkosa" for provider in providers))

    def test_search_by_service_type_shows_matching_providers(self):
        request_a = ServiceRequest.objects.create(
            customer_name="A",
            customer_phone="05000000001",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="A",
            status="pending_customer",
        )
        request_b = ServiceRequest.objects.create(
            customer_name="B",
            customer_phone="05000000002",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="B",
            status="pending_customer",
        )
        ProviderOffer.objects.create(
            service_request=request_a,
            provider=self.provider_ali,
            token="BUDGETA1",
            sequence=1,
            status="accepted",
            quote_amount=1500,
        )
        ProviderOffer.objects.create(
            service_request=request_b,
            provider=self.provider_mehmet,
            token="BUDGETB1",
            sequence=1,
            status="accepted",
            quote_amount=700,
        )

        response = self.client.get(
            reverse("index"),
            data={"service_type": self.service.id},
        )
        providers = response.context["providers"]
        provider_names = [provider.full_name for provider in providers]
        self.assertIn("Ali Usta", provider_names)
        self.assertIn("Mehmet Usta", provider_names)

    def test_provider_detail_page_loads(self):
        response = self.client.get(reverse("provider_detail", args=[self.provider_ali.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ali Usta")

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
        self.assertNotIn("phone_verify", self.client.session)

    def test_customer_signup_normalizes_phone_input(self):
        response = self.client.post(
            reverse("signup"),
            data={
                "username": "musteri_format",
                "first_name": "Telefon",
                "last_name": "Test",
                "email": "telefon@example.com",
                "phone": "+90 500 222 33 44",
                "city": "Lefkosa",
                "district": "Ortakoy",
                "password1": "GucluSifre123!",
                "password2": "GucluSifre123!",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        profile = CustomerProfile.objects.get(user__username="musteri_format")
        self.assertEqual(profile.phone, "05002223344")

    def test_customer_signup_does_not_require_phone_verification_step(self):
        user = User.objects.create_user(username="verifyme", password="GucluSifre123!")
        CustomerProfile.objects.create(user=user, phone="05009990000", city="Lefkosa", district="Ortakoy")
        response = self.client.post(
            reverse("customer_login"),
            data={"username": "verifyme", "password": "GucluSifre123!"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mahallendeki En")
        self.assertNotIn("phone_verify", self.client.session)

    def test_provider_can_signup(self):
        response = self.client.post(
            reverse("provider_signup"),
            data={
                "username": "yeniprofesyonel",
                "full_name": "Yeni Usta",
                "email": "usta@example.com",
                "phone": "05001234567",
                "city": "Lefkosa",
                "district": "Ortakoy",
                "service_types": [str(self.service.id)],
                "description": "10 yillik tecrube",
                "password1": "GucluSifre123!",
                "password2": "GucluSifre123!",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(username="yeniprofesyonel").exists())
        provider = Provider.objects.get(user__username="yeniprofesyonel")
        self.assertEqual(provider.full_name, "Yeni Usta")
        self.assertTrue(provider.service_types.filter(id=self.service.id).exists())
        self.assertFalse(provider.is_verified)
        self.assertNotEqual(self.client.session.get("role"), "provider")

    def test_unverified_provider_cannot_login_until_admin_approval(self):
        pending_user = User.objects.create_user(username="bekleyenusta", password="GucluSifre123!")
        Provider.objects.create(
            user=pending_user,
            full_name="Bekleyen Usta",
            city="Lefkosa",
            district="Ortakoy",
            phone="05558889900",
            is_verified=False,
            is_available=True,
        )

        response = self.client.post(
            reverse("provider_login"),
            data={"username": "bekleyenusta", "password": "GucluSifre123!"},
            follow=True,
        )
        self.assertContains(response, "admin onayı bekliyor")

    def test_unverified_provider_pending_warning_is_not_duplicated(self):
        pending_user = User.objects.create_user(username="bekleyenustapanel", password="GucluSifre123!")
        Provider.objects.create(
            user=pending_user,
            full_name="Bekleyen Usta Panel",
            city="Lefkosa",
            district="Ortakoy",
            phone="05558889901",
            is_verified=False,
            is_available=True,
        )

        self.client.login(username="bekleyenustapanel", password="GucluSifre123!")
        response = self.client.get(reverse("provider_requests"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Usta hesabınız admin onayı bekliyor.", count=1)

    def test_provider_can_update_profile(self):
        extra_service = ServiceType.objects.create(name="Elektrik", slug="elektrik")
        self.client.login(username="aliusta", password="GucluSifre123!")
        response = self.client.post(
            reverse("provider_profile"),
            data={
                "full_name": "Ali Usta Yeni",
                "phone": "05550009999",
                "city": "Lefkosa",
                "district": "Hamitkoy",
                "service_types": [str(self.service.id), str(extra_service.id)],
                "description": "Profil guncellendi",
                "is_available": "False",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.provider_ali.refresh_from_db()
        self.assertEqual(self.provider_ali.full_name, "Ali Usta Yeni")
        self.assertEqual(self.provider_ali.phone, "05550009999")
        self.assertEqual(self.provider_ali.district, "Hamitkoy")
        self.assertFalse(self.provider_ali.is_available)
        self.assertTrue(self.provider_ali.service_types.filter(id=extra_service.id).exists())

    def test_logged_in_customer_request_is_bound_to_user(self):
        user = User.objects.create_user(username="musteri2", password="GucluSifre123!")
        self.client.login(username="musteri2", password="GucluSifre123!")

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

    def test_customer_can_update_existing_rating(self):
        user = User.objects.create_user(username="degistiremez", password="GucluSifre123!")
        self.client.login(username="degistiremez", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Degistiremez Musteri",
            customer_phone="05007778899",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Degistirme testi",
            matched_provider=self.provider_ali,
            customer=user,
            status="completed",
        )

        self.client.post(
            reverse("rate_request", args=[service_request.id]),
            data={"score": 5, "comment": "Ilk oy"},
            follow=True,
        )
        response = self.client.post(
            reverse("rate_request", args=[service_request.id]),
            data={"score": 1, "comment": "Ikinci oy denemesi"},
            follow=True,
        )

        self.assertContains(response, "yorumunuz güncellendi")
        rating = ProviderRating.objects.get(service_request=service_request)
        self.assertEqual(rating.score, 1)
        self.assertEqual(rating.comment, "Ikinci oy denemesi")

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
        ServiceMessage.objects.create(
            service_request=service_request,
            sender_user=user,
            sender_role="customer",
            body="Is baslamadan once not.",
        )
        self.client.login(username="tamamlayan", password="GucluSifre123!")

        self.client.post(reverse("complete_request", args=[service_request.id]), follow=True)
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, "completed")
        self.assertEqual(ServiceMessage.objects.filter(service_request=service_request).count(), 0)

    def test_customer_can_create_appointment_for_matched_request(self):
        user = User.objects.create_user(username="randevulu", password="GucluSifre123!")
        self.client.login(username="randevulu", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Randevu Musteri",
            customer_phone="05005550000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Randevu olusturma testi",
            matched_provider=self.provider_ali,
            customer=user,
            status="matched",
        )

        self.client.post(
            reverse("create_appointment", args=[service_request.id]),
            data={
                "scheduled_for": self._future_datetime_local(days=2),
                "customer_note": "Aksam saatlerinde musaitim.",
            },
            follow=True,
        )

        appointment = ServiceAppointment.objects.get(service_request=service_request)
        self.assertEqual(appointment.status, "pending")
        self.assertEqual(appointment.provider, self.provider_ali)
        self.assertEqual(appointment.customer, user)

    def test_customer_can_create_appointment_with_quick_preset(self):
        user = User.objects.create_user(username="hizlirandevu", password="GucluSifre123!")
        self.client.login(username="hizlirandevu", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Hizli Randevu Musteri",
            customer_phone="05005550111",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Hizli randevu secimi testi",
            matched_provider=self.provider_ali,
            customer=user,
            status="matched",
        )

        self.client.post(
            reverse("create_appointment", args=[service_request.id]),
            data={
                "appointment_preset": "1h",
                "customer_note": "Hizli secim ile olusturuldu.",
            },
            follow=True,
        )

        appointment = ServiceAppointment.objects.get(service_request=service_request)
        minutes_to_appointment = (appointment.scheduled_for - timezone.now()).total_seconds() / 60
        self.assertEqual(appointment.status, "pending")
        self.assertGreaterEqual(minutes_to_appointment, 50)
        self.assertLessEqual(minutes_to_appointment, 70)

    def test_customer_create_appointment_requires_time_or_preset(self):
        user = User.objects.create_user(username="zamansizrandevu", password="GucluSifre123!")
        self.client.login(username="zamansizrandevu", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Zamansiz Randevu Musteri",
            customer_phone="05005550222",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Randevu zaman zorunlulugu testi",
            matched_provider=self.provider_ali,
            customer=user,
            status="matched",
        )

        response = self.client.post(
            reverse("create_appointment", args=[service_request.id]),
            data={"customer_note": "Saat secmeden deneme"},
            follow=True,
        )

        self.assertContains(response, "Randevu zamani secmelisiniz.")
        self.assertFalse(ServiceAppointment.objects.filter(service_request=service_request).exists())

    def test_provider_can_confirm_appointment_without_customer_reconfirm(self):
        customer = User.objects.create_user(username="randevumusteri", password="GucluSifre123!")
        appointment_request = ServiceRequest.objects.create(
            customer_name="Randevu Musteri",
            customer_phone="05001119999",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Onay testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        appointment = ServiceAppointment.objects.create(
            service_request=appointment_request,
            customer=customer,
            provider=self.provider_ali,
            scheduled_for=timezone.now() + timedelta(days=1),
            status="pending",
        )

        self.client.login(username="aliusta", password="GucluSifre123!")
        self.client.post(
            reverse("provider_confirm_appointment", args=[appointment.id]),
            data={"provider_note": "Saat uygundur."},
            follow=True,
        )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "confirmed")
        self.assertEqual(appointment.provider_note, "Saat uygundur.")

    @override_settings(APPOINTMENT_PROVIDER_CONFIRM_MINUTES=5)
    def test_pending_appointment_auto_cancels_after_provider_timeout(self):
        customer = User.objects.create_user(username="sureasimiusta", password="GucluSifre123!")
        appointment_request = ServiceRequest.objects.create(
            customer_name="Sure Asimi Musteri",
            customer_phone="05001119998",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Usta onay timeout testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        appointment = ServiceAppointment.objects.create(
            service_request=appointment_request,
            customer=customer,
            provider=self.provider_ali,
            scheduled_for=timezone.now() + timedelta(days=1),
            status="pending",
        )
        stale_time = timezone.now() - timedelta(minutes=20)
        ServiceAppointment.objects.filter(id=appointment.id).update(created_at=stale_time, updated_at=stale_time)

        self.client.login(username="sureasimiusta", password="GucluSifre123!")
        self.client.get(reverse("provider_requests"))

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "cancelled")

    def test_refresh_offer_lifecycle_detaches_unverified_matched_provider(self):
        customer = User.objects.create_user(username="eslesmebozulur", password="GucluSifre123!")
        unverified_user = User.objects.create_user(username="eslesmeonaysiz", password="GucluSifre123!")
        unverified_provider = Provider.objects.create(
            user=unverified_user,
            full_name="Eslesme Onaysiz Usta",
            city="Lefkosa",
            district="Ortakoy",
            phone="05550000002",
            is_verified=False,
            is_available=True,
        )
        unverified_provider.service_types.set([self.service])

        service_request = ServiceRequest.objects.create(
            customer_name="Eslesme Musteri",
            customer_phone="05001110000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Onaysiz eslesme testi",
            matched_provider=unverified_provider,
            customer=customer,
            status="matched",
        )
        blocked_offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=unverified_provider,
            token="DETACH1111",
            sequence=1,
            status="accepted",
            quote_amount=1000,
        )
        service_request.matched_offer = blocked_offer
        service_request.matched_at = timezone.now()
        service_request.save(update_fields=["matched_offer", "matched_at"])

        refresh_offer_lifecycle()

        service_request.refresh_from_db()
        blocked_offer.refresh_from_db()
        self.assertIsNone(service_request.matched_provider)
        self.assertIsNone(service_request.matched_offer)
        self.assertIn(service_request.status, {"new", "pending_provider", "pending_customer"})
        self.assertEqual(blocked_offer.status, "expired")

    def test_customer_can_confirm_provider_approved_appointment(self):
        customer = User.objects.create_user(username="sononaymusteri", password="GucluSifre123!")
        appointment_request = ServiceRequest.objects.create(
            customer_name="Son Onay Musteri",
            customer_phone="05001118888",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Iki tarafli onay testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        appointment = ServiceAppointment.objects.create(
            service_request=appointment_request,
            customer=customer,
            provider=self.provider_ali,
            scheduled_for=timezone.now() + timedelta(days=1),
            status="pending_customer",
        )
        self.client.login(username="sononaymusteri", password="GucluSifre123!")
        self.client.post(reverse("customer_confirm_appointment", args=[appointment_request.id]), follow=True)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "confirmed")

    @override_settings(APPOINTMENT_CUSTOMER_CONFIRM_MINUTES=5)
    def test_pending_customer_appointment_auto_cancels_after_customer_timeout(self):
        customer = User.objects.create_user(username="sureasimimusteri", password="GucluSifre123!")
        appointment_request = ServiceRequest.objects.create(
            customer_name="Sure Asimi Son Onay",
            customer_phone="05001118887",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Musteri onay timeout testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        appointment = ServiceAppointment.objects.create(
            service_request=appointment_request,
            customer=customer,
            provider=self.provider_ali,
            scheduled_for=timezone.now() + timedelta(days=1),
            status="pending_customer",
        )
        stale_time = timezone.now() - timedelta(minutes=20)
        ServiceAppointment.objects.filter(id=appointment.id).update(created_at=stale_time, updated_at=stale_time)

        self.client.login(username="sureasimimusteri", password="GucluSifre123!")
        self.client.get(reverse("my_requests"))

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "cancelled")

    @override_settings(APPOINTMENT_PROVIDER_CONFIRM_MINUTES=5)
    def test_marketplace_lifecycle_command_processes_stale_appointments(self):
        customer = User.objects.create_user(username="komuttestmusteri", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Komut Test",
            customer_phone="05003330000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Lifecycle command testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        appointment = ServiceAppointment.objects.create(
            service_request=service_request,
            customer=customer,
            provider=self.provider_ali,
            scheduled_for=timezone.now() + timedelta(days=1),
            status="pending",
        )
        stale_time = timezone.now() - timedelta(minutes=15)
        ServiceAppointment.objects.filter(id=appointment.id).update(created_at=stale_time, updated_at=stale_time)

        output = StringIO()
        call_command("marketplace_lifecycle", stdout=output)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "cancelled")
        self.assertIn("Marketplace lifecycle run #1 completed.", output.getvalue())
        heartbeat = SchedulerHeartbeat.objects.get(worker_name="marketplace_lifecycle")
        self.assertGreaterEqual(heartbeat.run_count, 1)
        self.assertIsNotNone(heartbeat.last_success_at)

    def test_marketplace_lifecycle_command_skips_when_lock_held_by_other_worker(self):
        SchedulerLock.objects.create(
            worker_name="marketplace_lifecycle",
            lock_owner="other-worker",
            locked_until=timezone.now() + timedelta(minutes=2),
            last_acquired_at=timezone.now(),
        )
        output = StringIO()
        call_command("marketplace_lifecycle", stdout=output)

        self.assertIn("another worker currently holds the lock", output.getvalue())
        self.assertFalse(SchedulerHeartbeat.objects.filter(worker_name="marketplace_lifecycle").exists())

    def test_marketplace_lifecycle_command_takes_over_expired_lock(self):
        expired_at = timezone.now() - timedelta(minutes=2)
        lock = SchedulerLock.objects.create(
            worker_name="marketplace_lifecycle",
            lock_owner="old-worker",
            locked_until=expired_at,
            last_acquired_at=expired_at,
        )
        output = StringIO()
        call_command("marketplace_lifecycle", "--loop", "--max-runs", "1", "--interval", "1", stdout=output)

        self.assertIn("Marketplace lifecycle run #1 completed.", output.getvalue())
        heartbeat = SchedulerHeartbeat.objects.get(worker_name="marketplace_lifecycle")
        self.assertGreaterEqual(heartbeat.run_count, 1)
        lock.refresh_from_db()
        self.assertNotEqual(lock.lock_owner, "old-worker")
        self.assertIsNotNone(lock.locked_until)
        self.assertGreater(lock.locked_until, timezone.now())

    def test_transition_creates_workflow_event_with_actor_metadata(self):
        customer = User.objects.create_user(username="eventcustomer", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Event Customer",
            customer_phone="05005556677",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Workflow event test",
            customer=customer,
            status="new",
        )

        transitioned = transition_service_request_status(
            service_request,
            "pending_provider",
            actor_user=customer,
            actor_role="customer",
            source="user",
            note="Event metadata",
        )
        self.assertTrue(transitioned)
        event = WorkflowEvent.objects.filter(service_request=service_request).latest("id")
        self.assertEqual(event.from_status, "new")
        self.assertEqual(event.to_status, "pending_provider")
        self.assertEqual(event.actor_user_id, customer.id)
        self.assertEqual(event.actor_role, "customer")
        self.assertEqual(event.source, "user")

    def test_duplicate_post_submission_is_blocked_by_idempotency(self):
        customer = User.objects.create_user(username="idempotent_customer", password="GucluSifre123!")
        self.client.login(username="idempotent_customer", password="GucluSifre123!")

        payload = {
            "customer_name": "Idempotent Customer",
            "customer_phone": "05001234567",
            "service_type": self.service.id,
            "city": "Lefkosa",
            "district": "Ortakoy",
            "details": "Ayni istek tekrar gonderiliyor",
        }
        self.client.post(reverse("create_request"), data=payload, follow=True)
        self.client.post(reverse("create_request"), data=payload, follow=True)

        self.assertEqual(ServiceRequest.objects.filter(customer=customer).count(), 1)
        self.assertEqual(IdempotencyRecord.objects.filter(scope="create-request").count(), 1)

    @override_settings(LIFECYCLE_HEARTBEAT_STALE_SECONDS=5)
    def test_lifecycle_health_endpoint_reports_healthy_and_stale(self):
        healthy = SchedulerHeartbeat.objects.create(
            worker_name="marketplace_lifecycle",
            run_count=4,
            last_started_at=timezone.now(),
            last_success_at=timezone.now(),
        )
        response = self.client.get(reverse("lifecycle_health"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        stale_at = timezone.now() - timedelta(seconds=30)
        SchedulerHeartbeat.objects.filter(pk=healthy.pk).update(last_success_at=stale_at, last_started_at=stale_at)
        response = self.client.get(reverse("lifecycle_health"))
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])

    def test_service_request_invalid_transition_is_rejected(self):
        request_item = ServiceRequest.objects.create(
            customer_name="Durum Test",
            customer_phone="05001230000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Durum akisi testi",
            status="completed",
        )

        transitioned = transition_service_request_status(request_item, "matched")
        request_item.refresh_from_db()
        self.assertFalse(transitioned)
        self.assertEqual(request_item.status, "completed")

    def test_appointment_invalid_transition_is_rejected(self):
        customer = User.objects.create_user(username="durumkontrol", password="GucluSifre123!")
        request_item = ServiceRequest.objects.create(
            customer_name="Randevu Durum Test",
            customer_phone="05003334444",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Randevu durum akisi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        appointment = ServiceAppointment.objects.create(
            service_request=request_item,
            customer=customer,
            provider=self.provider_ali,
            scheduled_for=timezone.now() + timedelta(days=1),
            status="completed",
        )

        transitioned = transition_appointment_status(appointment, "pending_customer")
        appointment.refresh_from_db()
        self.assertFalse(transitioned)
        self.assertEqual(appointment.status, "completed")

    def test_customer_can_cancel_appointment(self):
        customer = User.objects.create_user(username="iptalrandevu", password="GucluSifre123!")
        appointment_request = ServiceRequest.objects.create(
            customer_name="Iptal Randevu Musteri",
            customer_phone="05004448888",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Iptal randevu testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        appointment = ServiceAppointment.objects.create(
            service_request=appointment_request,
            customer=customer,
            provider=self.provider_ali,
            scheduled_for=timezone.now() + timedelta(days=1),
            status="confirmed",
        )

        self.client.login(username="iptalrandevu", password="GucluSifre123!")
        self.client.post(reverse("cancel_appointment", args=[appointment_request.id]), follow=True)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "cancelled")

    def test_customer_can_reschedule_active_appointment(self):
        customer = User.objects.create_user(username="guncellerandevu", password="GucluSifre123!")
        appointment_request = ServiceRequest.objects.create(
            customer_name="Guncel Randevu Musteri",
            customer_phone="05002223344",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Randevu guncelleme testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        old_time = timezone.now() + timedelta(days=1)
        appointment = ServiceAppointment.objects.create(
            service_request=appointment_request,
            customer=customer,
            provider=self.provider_ali,
            scheduled_for=old_time,
            customer_note="Eski not",
            status="confirmed",
        )

        new_local = self._future_datetime_local(days=3)
        self.client.login(username="guncellerandevu", password="GucluSifre123!")
        self.client.post(
            reverse("create_appointment", args=[appointment_request.id]),
            data={
                "scheduled_for": new_local,
                "customer_note": "Yeni saat rica ederim.",
            },
            follow=True,
        )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, "pending")
        self.assertEqual(appointment.customer_note, "Yeni saat rica ederim.")
        self.assertNotEqual(appointment.scheduled_for.replace(second=0, microsecond=0), old_time.replace(second=0, microsecond=0))

    def test_provider_can_complete_confirmed_appointment(self):
        customer = User.objects.create_user(username="tamamlarandevu", password="GucluSifre123!")
        appointment_request = ServiceRequest.objects.create(
            customer_name="Tamamla Randevu Musteri",
            customer_phone="05006667788",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Randevu tamamla testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        appointment = ServiceAppointment.objects.create(
            service_request=appointment_request,
            customer=customer,
            provider=self.provider_ali,
            scheduled_for=timezone.now() + timedelta(hours=2),
            status="confirmed",
        )
        ServiceMessage.objects.create(
            service_request=appointment_request,
            sender_user=customer,
            sender_role="customer",
            body="Islem sonrasi mesajlar silinecek mi",
        )

        self.client.login(username="aliusta", password="GucluSifre123!")
        self.client.post(reverse("provider_complete_appointment", args=[appointment.id]), follow=True)

        appointment.refresh_from_db()
        appointment_request.refresh_from_db()
        self.assertEqual(appointment.status, "completed")
        self.assertEqual(appointment_request.status, "completed")
        self.assertEqual(ServiceMessage.objects.filter(service_request=appointment_request).count(), 0)

    def test_customer_can_cancel_request_before_match(self):
        user = User.objects.create_user(username="iptaleden", password="GucluSifre123!")
        self.client.login(username="iptaleden", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Iptal Eden Musteri",
            customer_phone="05001110000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Iptal testi",
            customer=user,
            status="pending_provider",
        )
        offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="CANCEL1234",
            sequence=1,
            status="pending",
        )

        self.client.post(reverse("cancel_request", args=[service_request.id]), follow=True)
        service_request.refresh_from_db()
        offer.refresh_from_db()
        self.assertEqual(service_request.status, "cancelled")
        self.assertEqual(offer.status, "expired")

    def test_customer_cancel_clears_stale_offer_match_metadata(self):
        user = User.objects.create_user(username="iptaltemiz", password="GucluSifre123!")
        self.client.login(username="iptaltemiz", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Iptal Temizleme Musteri",
            customer_phone="05001112223",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Eski eslesme verisi temizleme testi",
            customer=user,
            status="pending_customer",
            matched_at=timezone.now(),
        )
        stale_offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="CANCELMETA1",
            sequence=1,
            status="accepted",
            quote_amount=1000,
        )
        service_request.matched_offer = stale_offer
        service_request.save(update_fields=["matched_offer", "matched_at"])

        self.client.post(reverse("cancel_request", args=[service_request.id]), follow=True)
        service_request.refresh_from_db()
        stale_offer.refresh_from_db()
        self.assertEqual(service_request.status, "cancelled")
        self.assertIsNone(service_request.matched_offer)
        self.assertIsNone(service_request.matched_at)
        self.assertEqual(stale_offer.status, "expired")

    def test_customer_cannot_cancel_after_match(self):
        user = User.objects.create_user(username="iptalolmaz", password="GucluSifre123!")
        self.client.login(username="iptalolmaz", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Iptal Olamaz Musteri",
            customer_phone="05001110001",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Iptal olmaz testi",
            customer=user,
            status="matched",
            matched_provider=self.provider_ali,
        )

        self.client.post(reverse("cancel_request", args=[service_request.id]), follow=True)
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, "matched")

    def test_customer_can_delete_cancelled_request(self):
        user = User.objects.create_user(username="silici", password="GucluSifre123!")
        self.client.login(username="silici", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Silinecek Musteri",
            customer_phone="05002220000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Silme testi",
            customer=user,
            status="cancelled",
        )

        self.client.post(reverse("delete_cancelled_request", args=[service_request.id]), follow=True)
        self.assertFalse(ServiceRequest.objects.filter(id=service_request.id).exists())

    def test_customer_can_delete_all_cancelled_requests(self):
        user = User.objects.create_user(username="toplusil", password="GucluSifre123!")
        self.client.login(username="toplusil", password="GucluSifre123!")
        cancelled_1 = ServiceRequest.objects.create(
            customer_name="Toplu Sil 1",
            customer_phone="05003330000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Toplu silme 1",
            customer=user,
            status="cancelled",
        )
        cancelled_2 = ServiceRequest.objects.create(
            customer_name="Toplu Sil 2",
            customer_phone="05003330001",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Toplu silme 2",
            customer=user,
            status="cancelled",
        )
        active_request = ServiceRequest.objects.create(
            customer_name="Toplu Sil Aktif",
            customer_phone="05003330002",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Toplu silme aktif",
            customer=user,
            status="new",
        )

        self.client.post(reverse("delete_all_cancelled_requests"), follow=True)
        self.assertFalse(ServiceRequest.objects.filter(id=cancelled_1.id).exists())
        self.assertFalse(ServiceRequest.objects.filter(id=cancelled_2.id).exists())
        self.assertTrue(ServiceRequest.objects.filter(id=active_request.id).exists())

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

    def test_provider_can_accept_offer_from_panel(self):
        User.objects.create_user(username="panelmusteri", password="GucluSifre123!")
        self.client.login(username="panelmusteri", password="GucluSifre123!")
        self.client.post(
            reverse("create_request"),
            data={
                "customer_name": "Panel Musteri",
                "customer_phone": "05000000000",
                "service_type": self.service.id,
                "city": "Lefkosa",
                "district": "Ortakoy",
                "details": "Panel kabul testi",
            },
            follow=True,
        )

        service_request = ServiceRequest.objects.latest("created_at")
        offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_ali)
        self.client.logout()
        self.client.login(username="aliusta", password="GucluSifre123!")
        self.client.post(
            reverse("provider_accept_offer", args=[offer.id]),
            data={"quote_amount": "1500", "quote_note": "Ayni gun gelebilirim."},
            follow=True,
        )

        service_request.refresh_from_db()
        offer.refresh_from_db()
        sibling_offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_hasan)
        sibling_offer.refresh_from_db()
        self.assertEqual(service_request.status, "pending_customer")
        self.assertIsNone(service_request.matched_provider)
        self.assertEqual(offer.status, "accepted")
        self.assertEqual(float(offer.quote_amount), 1500.0)
        self.assertEqual(sibling_offer.status, "pending")

    def test_offer_is_auto_expired_when_time_passes(self):
        request_item = ServiceRequest.objects.create(
            customer_name="Timeout Musteri",
            customer_phone="05000000077",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Timeout testi",
            status="pending_provider",
        )
        offer = ProviderOffer.objects.create(
            service_request=request_item,
            provider=self.provider_ali,
            token="EXPIRE001",
            sequence=1,
            status="pending",
            sent_at=timezone.now() - timedelta(hours=4),
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.client.get(reverse("index"))
        offer.refresh_from_db()
        self.assertEqual(offer.status, "expired")

    def test_matched_customer_and_provider_can_exchange_messages(self):
        customer = User.objects.create_user(username="chatcustomer", password="GucluSifre123!")
        matched_request = ServiceRequest.objects.create(
            customer_name="Chat Musteri",
            customer_phone="05006660000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Mesajlasma testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="matched",
        )
        selected_offer = ProviderOffer.objects.create(
            service_request=matched_request,
            provider=self.provider_ali,
            token="CHATMATCH01",
            sequence=1,
            status="accepted",
        )
        matched_request.matched_offer = selected_offer
        matched_request.save(update_fields=["matched_offer"])

        self.client.login(username="chatcustomer", password="GucluSifre123!")
        self.client.post(
            reverse("request_messages", args=[matched_request.id]),
            data={"body": "Merhaba, yarin musait misiniz"},
            follow=True,
        )
        self.client.logout()

        self.client.login(username="aliusta", password="GucluSifre123!")
        response = self.client.get(reverse("request_messages", args=[matched_request.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            ServiceMessage.objects.filter(
                service_request=matched_request,
                sender_user=customer,
                sender_role="customer",
            ).exists()
        )

    def test_provider_cannot_message_before_customer_selects_provider(self):
        customer = User.objects.create_user(username="chatnoselect", password="GucluSifre123!")
        pending_request = ServiceRequest.objects.create(
            customer_name="Secim Bekleyen Musteri",
            customer_phone="05006660002",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Secim oncesi mesaj testi",
            matched_provider=self.provider_ali,
            customer=customer,
            status="pending_customer",
        )
        ProviderOffer.objects.create(
            service_request=pending_request,
            provider=self.provider_ali,
            token="CHATPEND01",
            sequence=1,
            status="accepted",
        )

        self.client.login(username="aliusta", password="GucluSifre123!")
        get_response = self.client.get(
            reverse("request_messages", args=[pending_request.id]),
            follow=True,
        )
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "Musteri sizi henuz secmedigi icin mesajlasma acilmadi.")

        post_response = self.client.post(
            reverse("request_messages", args=[pending_request.id]),
            data={"body": "Secim olmadan mesaj denemesi"},
            follow=True,
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertContains(post_response, "Musteri sizi henuz secmedigi icin mesajlasma acilmadi.")
        self.assertEqual(ServiceMessage.objects.filter(service_request=pending_request).count(), 0)

    def test_completed_request_messages_page_is_closed(self):
        customer = User.objects.create_user(username="chatclosed", password="GucluSifre123!")
        completed_request = ServiceRequest.objects.create(
            customer_name="Kapali Mesaj Musteri",
            customer_phone="05006660001",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Tamamlanmis is",
            matched_provider=self.provider_ali,
            customer=customer,
            status="completed",
        )

        self.client.login(username="chatclosed", password="GucluSifre123!")
        get_response = self.client.get(
            reverse("request_messages", args=[completed_request.id]),
            follow=True,
        )
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "Tamamlanan veya kapalı taleplerde mesajlaşma açık değildir.")

        post_response = self.client.post(
            reverse("request_messages", args=[completed_request.id]),
            data={"body": "Yeni mesaj denemesi"},
            follow=True,
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertContains(post_response, "Tamamlanan veya kapalı taleplerde mesajlaşma açık değildir.")
        self.assertEqual(ServiceMessage.objects.filter(service_request=completed_request).count(), 0)

    def test_customer_can_select_provider_after_offers(self):
        customer = User.objects.create_user(username="teklifsecen", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Teklif Secen Musteri",
            customer_phone="05001112222",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Teklif secim testi",
            customer=customer,
            status="pending_customer",
        )
        offer_1 = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="SELECT1111",
            sequence=1,
            status="accepted",
            quote_amount=1200,
        )
        offer_2 = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_hasan,
            token="SELECT2222",
            sequence=2,
            status="accepted",
            quote_amount=1100,
        )

        self.client.login(username="teklifsecen", password="GucluSifre123!")
        self.client.post(reverse("select_provider_offer", args=[service_request.id, offer_2.id]), follow=True)

        service_request.refresh_from_db()
        offer_1.refresh_from_db()
        offer_2.refresh_from_db()
        self.assertEqual(service_request.status, "matched")
        self.assertEqual(service_request.matched_provider, self.provider_hasan)
        self.assertEqual(service_request.matched_offer, offer_2)
        self.assertIsNotNone(service_request.matched_at)
        self.assertEqual(offer_2.status, "accepted")
        self.assertEqual(offer_1.status, "expired")

    def test_customer_cannot_select_offer_from_unverified_provider(self):
        customer = User.objects.create_user(username="onaysizsecim", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Onaysiz Secim Musteri",
            customer_phone="05001112221",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Onaysiz teklif secim testi",
            customer=customer,
            status="pending_customer",
        )
        unverified_user = User.objects.create_user(username="onaysizusta", password="GucluSifre123!")
        unverified_provider = Provider.objects.create(
            user=unverified_user,
            full_name="Onaysiz Usta",
            city="Lefkosa",
            district="Ortakoy",
            phone="05550000001",
            is_verified=False,
            is_available=True,
        )
        unverified_provider.service_types.set([self.service])
        blocked_offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=unverified_provider,
            token="UNVER1111",
            sequence=1,
            status="accepted",
            quote_amount=900,
        )

        self.client.login(username="onaysizsecim", password="GucluSifre123!")
        response = self.client.post(
            reverse("select_provider_offer", args=[service_request.id, blocked_offer.id]),
            follow=True,
        )

        self.assertContains(response, "usta henüz admin onaylı değil")
        service_request.refresh_from_db()
        blocked_offer.refresh_from_db()
        self.assertIsNone(service_request.matched_provider)
        self.assertIn(service_request.status, {"pending_provider", "pending_customer", "new"})
        self.assertIn(blocked_offer.status, {"accepted", "expired"})

    def test_offer_comparison_marks_best_offer(self):
        customer = User.objects.create_user(username="karsilastirma", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Karsilastirma Musteri",
            customer_phone="05001234567",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Karsilastirma",
            customer=customer,
            status="pending_customer",
        )
        ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="CMPA1111",
            sequence=1,
            status="accepted",
            quote_amount=1800,
        )
        best_offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_hasan,
            token="CMPB2222",
            sequence=2,
            status="accepted",
            quote_amount=950,
        )

        self.client.login(username="karsilastirma", password="GucluSifre123!")
        response = self.client.get(reverse("my_requests"))
        requests = response.context["requests"]
        target = next(item for item in requests if item.id == service_request.id)
        self.assertEqual(target.recommended_offer_id, best_offer.id)
        self.assertGreaterEqual(target.accepted_offers[0].comparison_score, target.accepted_offers[1].comparison_score)

    def test_provider_reject_keeps_request_if_other_pending_offers_exist(self):
        User.objects.create_user(username="panelredmusteri", password="GucluSifre123!")
        self.client.login(username="panelredmusteri", password="GucluSifre123!")
        self.client.post(
            reverse("create_request"),
            data={
                "customer_name": "Panel Red Musteri",
                "customer_phone": "05000000000",
                "service_type": self.service.id,
                "city": "Lefkosa",
                "district": "Ortakoy",
                "details": "Panel red testi",
            },
            follow=True,
        )
        service_request = ServiceRequest.objects.latest("created_at")
        first_offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_ali)
        self.client.logout()
        self.client.login(username="aliusta", password="GucluSifre123!")
        self.client.post(reverse("provider_reject_offer", args=[first_offer.id]), follow=True)

        service_request.refresh_from_db()
        first_offer.refresh_from_db()
        second_offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_hasan)
        self.assertEqual(first_offer.status, "rejected")
        self.assertEqual(second_offer.status, "pending")
        self.assertEqual(service_request.status, "pending_provider")

    def test_provider_reject_redispatch_clears_stale_match_metadata(self):
        stale_time = timezone.now()
        service_request = ServiceRequest.objects.create(
            customer_name="Yeniden Dagitim Musteri",
            customer_phone="05000000021",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Yeniden dagitim temizleme testi",
            status="pending_provider",
            matched_provider=self.provider_hasan,
            matched_at=stale_time,
        )
        first_offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="REDISPATCH1",
            sequence=1,
            status="pending",
        )
        service_request.matched_offer = first_offer
        service_request.save(update_fields=["matched_offer"])

        self.client.login(username="aliusta", password="GucluSifre123!")
        self.client.post(reverse("provider_reject_offer", args=[first_offer.id]), follow=True)

        service_request.refresh_from_db()
        first_offer.refresh_from_db()
        next_offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_hasan)
        self.assertEqual(first_offer.status, "rejected")
        self.assertEqual(next_offer.status, "pending")
        self.assertEqual(service_request.status, "pending_provider")
        self.assertIsNone(service_request.matched_provider)
        self.assertIsNone(service_request.matched_offer)
        self.assertIsNone(service_request.matched_at)

    def test_provider_reject_deletes_request_when_no_provider_left(self):
        User.objects.create_user(username="tekredmusteri", password="GucluSifre123!")
        self.client.login(username="tekredmusteri", password="GucluSifre123!")
        self.client.post(
            reverse("create_request"),
            data={
                "customer_name": "Tek Usta Red Musteri",
                "customer_phone": "05000000000",
                "service_type": self.service.id,
                "city": "Girne",
                "district": "Karakum",
                "details": "Tek usta red testi",
            },
            follow=True,
        )
        service_request = ServiceRequest.objects.latest("created_at")
        only_offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_mehmet)
        self.client.logout()
        self.client.login(username="mehmetusta", password="GucluSifre123!")
        self.client.post(reverse("provider_reject_offer", args=[only_offer.id]), follow=True)

        self.assertFalse(ServiceRequest.objects.filter(id=service_request.id).exists())

    def test_customer_login_rejects_provider_account(self):
        response = self.client.post(
            reverse("customer_login"),
            data={"username": "aliusta", "password": "GucluSifre123!"},
            follow=True,
        )
        self.assertContains(response, "Bu hesap usta hesab")

    @override_settings(LOGIN_RATE_LIMIT_MAX_ATTEMPTS=1, LOGIN_RATE_LIMIT_WINDOW_SECONDS=120)
    def test_customer_login_rate_limit_blocks_second_attempt(self):
        User.objects.create_user(username="ratelogin", password="GucluSifre123!")

        self.client.post(
            reverse("customer_login"),
            data={"username": "ratelogin", "password": "hatali-sifre"},
            follow=True,
        )
        response = self.client.post(
            reverse("customer_login"),
            data={"username": "ratelogin", "password": "hatali-sifre"},
            follow=True,
        )

        self.assertContains(response, "Çok kısa sürede çok fazla istek gönderdiniz")

    def test_provider_login_rejects_customer_account(self):
        User.objects.create_user(username="normalmusteri", password="GucluSifre123!")
        response = self.client.post(
            reverse("provider_login"),
            data={"username": "normalmusteri", "password": "GucluSifre123!"},
            follow=True,
        )
        self.assertContains(response, "Bu hesap usta olarak")

    def test_provider_panel_snapshot_returns_pending_state(self):
        customer = User.objects.create_user(username="snapshotprovidercustomer", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Canli Takip",
            customer_phone="05009990000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Panel snapshot testi",
            status="pending_provider",
        )
        pending_offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="SNAPSHOT1",
            sequence=1,
            status="pending",
        )
        matched_request = ServiceRequest.objects.create(
            customer_name="Mesaj Musterisi",
            customer_phone="05001239876",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Okunmamis mesaj testi",
            customer=customer,
            matched_provider=self.provider_ali,
            status="matched",
        )
        ServiceMessage.objects.create(
            service_request=matched_request,
            sender_user=customer,
            sender_role="customer",
            body="Yeni mesaj",
        )

        self.client.login(username="aliusta", password="GucluSifre123!")
        response = self.client.get(reverse("provider_panel_snapshot"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pending_offers_count"], 1)
        self.assertEqual(payload["latest_pending_offer_id"], pending_offer.id)
        self.assertEqual(payload["unread_messages_count"], 1)

    def test_provider_panel_snapshot_forbidden_for_non_provider(self):
        User.objects.create_user(username="normaluser", password="GucluSifre123!")
        self.client.login(username="normaluser", password="GucluSifre123!")
        response = self.client.get(reverse("provider_panel_snapshot"))
        self.assertEqual(response.status_code, 403)

    def test_customer_requests_snapshot_returns_signature(self):
        customer = User.objects.create_user(username="snapshotcustomer", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Snapshot Musteri",
            customer_phone="05001234567",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Snapshot test",
            customer=customer,
            status="pending_customer",
        )
        ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="SNAPSHOT2",
            sequence=1,
            status="accepted",
            quote_amount=1350,
        )
        ServiceMessage.objects.create(
            service_request=service_request,
            sender_user=self.provider_user_ali,
            sender_role="provider",
            body="Teklif detaylarini paylastim.",
        )

        self.client.login(username="snapshotcustomer", password="GucluSifre123!")
        response = self.client.get(reverse("customer_requests_snapshot"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("signature", payload)
        self.assertTrue(payload["signature"])
        self.assertEqual(payload["pending_customer_requests_count"], 1)
        self.assertEqual(payload["accepted_offers_count"], 1)
        self.assertEqual(payload["unread_messages_count"], 1)

    def test_customer_requests_snapshot_forbidden_for_provider(self):
        self.client.login(username="aliusta", password="GucluSifre123!")
        response = self.client.get(reverse("customer_requests_snapshot"))
        self.assertEqual(response.status_code, 403)

    def test_customer_can_view_agreement_history(self):
        customer = User.objects.create_user(username="anlasmamusteri", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Anlasma Musteri",
            customer_phone="05001230000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Anlasma gecmisi test",
            customer=customer,
            matched_provider=self.provider_ali,
            status="matched",
            matched_at=timezone.now(),
        )
        offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="HISTORY001",
            sequence=1,
            status="accepted",
            quote_amount=1750,
        )
        service_request.matched_offer = offer
        service_request.save(update_fields=["matched_offer"])

        self.client.login(username="anlasmamusteri", password="GucluSifre123!")
        response = self.client.get(reverse("agreement_history"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Anlaşma Geçmişi")
        self.assertContains(response, "Ali Usta")
        self.assertContains(response, "1750")

    def test_provider_can_view_agreement_history(self):
        customer = User.objects.create_user(username="anlasmaprovmusteri", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Anlasma Provider Musteri",
            customer_phone="05007770000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Provider anlasma gecmisi test",
            customer=customer,
            matched_provider=self.provider_ali,
            status="completed",
            matched_at=timezone.now(),
        )
        offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="HISTORY002",
            sequence=1,
            status="accepted",
            quote_amount=2100,
        )
        service_request.matched_offer = offer
        service_request.save(update_fields=["matched_offer"])

        self.client.login(username="aliusta", password="GucluSifre123!")
        response = self.client.get(reverse("agreement_history"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Anlasma Provider Musteri")
        self.assertContains(response, "2100")

    def test_customer_can_delete_account(self):
        customer = User.objects.create_user(username="silinenmusteri", password="GucluSifre123!")
        ServiceRequest.objects.create(
            customer_name="Silinen Musteri",
            customer_phone="05009990001",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Hesap silme testi",
            customer=customer,
            status="new",
        )
        self.client.login(username="silinenmusteri", password="GucluSifre123!")

        response = self.client.post(
            reverse("delete_account"),
            data={"confirmation_text": "HESABIMI SIL", "password": "GucluSifre123!"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="silinenmusteri").exists())
        self.assertEqual(ServiceRequest.objects.filter(customer_name="Silinen Musteri").count(), 0)

    def test_provider_can_delete_account(self):
        customer = User.objects.create_user(username="ustaesleme", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Usta Eslesme",
            customer_phone="05009990002",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Usta hesap silme testi",
            customer=customer,
            matched_provider=self.provider_ali,
            status="matched",
        )
        self.client.login(username="aliusta", password="GucluSifre123!")

        response = self.client.post(
            reverse("delete_account"),
            data={"confirmation_text": "HESABIMI SIL", "password": "GucluSifre123!"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="aliusta").exists())
        self.assertFalse(Provider.objects.filter(full_name="Ali Usta").exists())
        service_request.refresh_from_db()
        self.assertIsNone(service_request.matched_provider)

    def test_delete_account_requires_confirmation_phrase(self):
        customer = User.objects.create_user(username="onaysizsilme", password="GucluSifre123!")
        self.client.login(username="onaysizsilme", password="GucluSifre123!")

        response = self.client.post(
            reverse("delete_account"),
            data={"confirmation_text": "SIL", "password": "GucluSifre123!"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(username="onaysizsilme").exists())

    def test_customer_account_settings_page_loads_with_tabs(self):
        customer = User.objects.create_user(username="ayarli_musteri", password="GucluSifre123!")
        CustomerProfile.objects.create(user=customer, phone="05001112233", city="Lefkosa", district="Ortakoy")
        self.client.login(username="ayarli_musteri", password="GucluSifre123!")

        response = self.client.get(reverse("account_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hesap Bilgileri")
        self.assertContains(response, "İletişim ve Konum Bilgileri")
        self.assertContains(response, "Güvenlik")

    def test_provider_account_settings_page_hides_contact_section(self):
        self.client.login(username="aliusta", password="GucluSifre123!")
        response = self.client.get(reverse("account_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "İletişim ve Konum Bilgileri")
        self.assertContains(response, "Usta Profili")

    def test_customer_can_update_identity_from_account_settings(self):
        customer = User.objects.create_user(username="kimlikeski", password="GucluSifre123!")
        CustomerProfile.objects.create(user=customer, phone="05001112233", city="Lefkosa", district="Ortakoy")
        self.client.login(username="kimlikeski", password="GucluSifre123!")

        self.client.post(
            reverse("account_settings"),
            data={
                "form_action": "identity",
                "identity-username": "kimlikyeni",
                "identity-first_name": "Ayse",
                "identity-last_name": "Demir",
                "identity-email": "ayse@example.com",
            },
            follow=True,
        )

        customer.refresh_from_db()
        self.assertEqual(customer.username, "kimlikyeni")
        self.assertEqual(customer.first_name, "Ayse")
        self.assertEqual(customer.last_name, "Demir")
        self.assertEqual(customer.email, "ayse@example.com")

    def test_customer_can_update_contact_from_account_settings(self):
        customer = User.objects.create_user(username="iletisim_musteri", password="GucluSifre123!")
        CustomerProfile.objects.create(user=customer, phone="05001112233", city="Lefkosa", district="Ortakoy")
        self.client.login(username="iletisim_musteri", password="GucluSifre123!")

        self.client.post(
            reverse("account_settings"),
            data={
                "form_action": "contact",
                "contact-phone": "+90 555 333 22 11",
                "contact-city": "Girne",
                "contact-district": "Karakum",
            },
            follow=True,
        )

        profile = CustomerProfile.objects.get(user=customer)
        self.assertEqual(profile.phone, "05553332211")
        self.assertEqual(profile.city, "Girne")
        self.assertEqual(profile.district, "Karakum")

    def test_customer_can_fund_escrow_for_matched_request(self):
        customer = User.objects.create_user(username="escrow_musteri", password="GucluSifre123!")
        self.client.login(username="escrow_musteri", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Escrow Musteri",
            customer_phone="05001230000",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Escrow test",
            customer=customer,
            matched_provider=self.provider_ali,
            status="matched",
        )
        offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="ESCROW001",
            sequence=1,
            status="accepted",
            quote_amount=1250,
        )
        service_request.matched_offer = offer
        service_request.save(update_fields=["matched_offer"])

        response = self.client.post(reverse("fund_escrow", args=[service_request.id]), follow=True)
        self.assertEqual(response.status_code, 200)
        escrow = EscrowPayment.objects.get(service_request=service_request)
        self.assertEqual(escrow.status, "funded")
        self.assertEqual(float(escrow.funded_amount), 1250.0)

    def test_customer_appointment_respects_provider_availability_slots(self):
        customer = User.objects.create_user(username="slot_musteri", password="GucluSifre123!")
        self.client.login(username="slot_musteri", password="GucluSifre123!")
        service_request = ServiceRequest.objects.create(
            customer_name="Slot Musteri",
            customer_phone="05002223344",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Slot kontrol testi",
            customer=customer,
            matched_provider=self.provider_ali,
            status="matched",
        )

        ProviderAvailabilitySlot.objects.create(
            provider=self.provider_ali,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(12, 0),
            is_active=True,
        )

        local_candidate = timezone.localtime(timezone.now() + timedelta(days=1)).replace(
            hour=15,
            minute=0,
            second=0,
            microsecond=0,
        )
        while local_candidate.weekday() != 0:
            local_candidate += timedelta(days=1)
        if local_candidate <= timezone.localtime(timezone.now()):
            local_candidate += timedelta(days=7)

        response = self.client.post(
            reverse("create_appointment", args=[service_request.id]),
            data={
                "scheduled_for": local_candidate.strftime("%Y-%m-%dT%H:%M"),
                "customer_note": "Slot disi deneme",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "musaitlik araliginda")
        self.assertFalse(ServiceAppointment.objects.filter(service_request=service_request).exists())

    def test_provider_contact_update_redirects_to_provider_profile(self):
        self.client.login(username="aliusta", password="GucluSifre123!")
        response = self.client.post(
            reverse("account_settings"),
            data={
                "form_action": "contact",
                "contact-full_name": "Ali Usta Yeni",
                "contact-phone": "05557778899",
                "contact-city": "Girne",
                "contact-district": "Karakum",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.provider_ali.refresh_from_db()
        self.assertEqual(self.provider_ali.full_name, "Ali Usta")
        self.assertEqual(self.provider_ali.phone, "05550000000")
        self.assertEqual(self.provider_ali.city, "Lefkosa")
        self.assertEqual(self.provider_ali.district, "Ortakoy")

    def test_customer_can_change_password_from_account_settings(self):
        customer = User.objects.create_user(username="sifredegis", password="GucluSifre123!")
        CustomerProfile.objects.create(user=customer, phone="05004445566", city="Lefkosa", district="Ortakoy")
        self.client.login(username="sifredegis", password="GucluSifre123!")

        response = self.client.post(
            reverse("account_settings"),
            data={
                "form_action": "security",
                "password-old_password": "GucluSifre123!",
                "password-new_password1": "YeniGucluSifre123!",
                "password-new_password2": "YeniGucluSifre123!",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        self.client.logout()
        self.assertTrue(self.client.login(username="sifredegis", password="YeniGucluSifre123!"))


    def test_provider_accept_offer_succeeds_without_credit_requirement(self):
        service_request = ServiceRequest.objects.create(
            customer_name="Kredi Yok",
            customer_phone="05000000222",
            city="Lefkosa",
            district="Ortakoy",
            service_type=self.service,
            details="Kredi zorunlu test",
            status="pending_provider",
        )
        offer = ProviderOffer.objects.create(
            service_request=service_request,
            provider=self.provider_ali,
            token="CREDIT0001",
            sequence=1,
            status="pending",
        )

        self.client.login(username="aliusta", password="GucluSifre123!")
        response = self.client.post(
            reverse("provider_accept_offer", args=[offer.id]),
            data={"quote_amount": "1200", "quote_note": "Teklif"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        offer.refresh_from_db()
        service_request.refresh_from_db()
        self.assertEqual(offer.status, "accepted")
        self.assertEqual(service_request.status, "pending_customer")




