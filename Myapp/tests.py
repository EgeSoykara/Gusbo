from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone

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


class MarketplaceTests(TestCase):
    def setUp(self):
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
            is_available=True,
        )
        self.provider_hasan.service_types.add(self.service)

    def _future_datetime_local(self, days=1):
        return timezone.localtime(timezone.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")

    def test_home_page_loads(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mahallendeki En İyi Ustaları")

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
        self.assertContains(response, "Mahallendeki En İyi Ustaları")
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
        self.assertEqual(self.client.session.get("role"), "provider")

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

    def test_provider_can_confirm_appointment(self):
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
        self.assertEqual(appointment.status, "pending_customer")
        self.assertEqual(appointment.provider_note, "Saat uygundur.")

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
            body="Islem sonrasi mesajlar silinecek mi?",
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

        self.client.login(username="chatcustomer", password="GucluSifre123!")
        self.client.post(
            reverse("request_messages", args=[matched_request.id]),
            data={"body": "Merhaba, yarin musait misiniz?"},
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
        self.assertEqual(offer_2.status, "accepted")
        self.assertEqual(offer_1.status, "expired")

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
        self.assertContains(response, "Bu hesap usta hesabıdır")

    def test_provider_login_rejects_customer_account(self):
        User.objects.create_user(username="normalmusteri", password="GucluSifre123!")
        response = self.client.post(
            reverse("provider_login"),
            data={"username": "normalmusteri", "password": "GucluSifre123!"},
            follow=True,
        )
        self.assertContains(response, "Bu hesap usta olarak tanımlı değil")

    def test_provider_panel_snapshot_returns_pending_state(self):
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

        self.client.login(username="aliusta", password="GucluSifre123!")
        response = self.client.get(reverse("provider_panel_snapshot"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pending_offers_count"], 1)
        self.assertEqual(payload["latest_pending_offer_id"], pending_offer.id)

    def test_provider_panel_snapshot_forbidden_for_non_provider(self):
        User.objects.create_user(username="normaluser", password="GucluSifre123!")
        self.client.login(username="normaluser", password="GucluSifre123!")
        response = self.client.get(reverse("provider_panel_snapshot"))
        self.assertEqual(response.status_code, 403)
