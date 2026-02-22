from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import User

from .models import CustomerProfile, Provider, ProviderOffer, ProviderRating, ServiceRequest, ServiceType


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

    def test_home_page_loads(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mahallendeki En Iyi Ustalari")

    def test_service_request_creates_record(self):
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
        self.assertContains(response, "ustaya panelde onay icin iletildi")
        latest = ServiceRequest.objects.latest("created_at")
        self.assertEqual(latest.status, "pending_provider")
        self.assertEqual(ProviderOffer.objects.filter(service_request=latest, status="pending").count(), 2)

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

    def test_customer_cannot_update_existing_rating(self):
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
        self.client.post(
            reverse("rate_request", args=[service_request.id]),
            data={"score": 1, "comment": "Ikinci oy denemesi"},
            follow=True,
        )

        rating = ProviderRating.objects.get(service_request=service_request)
        self.assertEqual(rating.score, 5)
        self.assertEqual(rating.comment, "Ilk oy")

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
        self.client.login(username="aliusta", password="GucluSifre123!")
        self.client.post(
            reverse("provider_accept_offer", args=[offer.id]),
            follow=True,
        )

        service_request.refresh_from_db()
        offer.refresh_from_db()
        sibling_offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_hasan)
        sibling_offer.refresh_from_db()
        self.assertEqual(service_request.status, "matched")
        self.assertEqual(service_request.matched_provider, self.provider_ali)
        self.assertEqual(offer.status, "accepted")
        self.assertEqual(sibling_offer.status, "expired")

    def test_provider_reject_keeps_request_if_other_pending_offers_exist(self):
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
        self.client.login(username="aliusta", password="GucluSifre123!")
        self.client.post(reverse("provider_reject_offer", args=[first_offer.id]), follow=True)

        service_request.refresh_from_db()
        first_offer.refresh_from_db()
        second_offer = ProviderOffer.objects.get(service_request=service_request, provider=self.provider_hasan)
        self.assertEqual(first_offer.status, "rejected")
        self.assertEqual(second_offer.status, "pending")
        self.assertEqual(service_request.status, "pending_provider")

    def test_provider_reject_deletes_request_when_no_provider_left(self):
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
        self.client.login(username="mehmetusta", password="GucluSifre123!")
        self.client.post(reverse("provider_reject_offer", args=[only_offer.id]), follow=True)

        self.assertFalse(ServiceRequest.objects.filter(id=service_request.id).exists())

    def test_customer_login_rejects_provider_account(self):
        response = self.client.post(
            reverse("customer_login"),
            data={"username": "aliusta", "password": "GucluSifre123!"},
            follow=True,
        )
        self.assertContains(response, "Bu hesap usta hesabidir")

    def test_provider_login_rejects_customer_account(self):
        User.objects.create_user(username="normalmusteri", password="GucluSifre123!")
        response = self.client.post(
            reverse("provider_login"),
            data={"username": "normalmusteri", "password": "GucluSifre123!"},
            follow=True,
        )
        self.assertContains(response, "Bu hesap usta olarak tanimli degil")
