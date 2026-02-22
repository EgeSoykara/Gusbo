from django.db import models
from django.contrib.auth.models import User
from django.db.models import Avg
from django.utils import timezone


class ServiceType(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Provider(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="provider_profile",
    )
    full_name = models.CharField(max_length=120)
    service_types = models.ManyToManyField(ServiceType, related_name="providers", blank=True)
    city = models.CharField(max_length=80)
    district = models.CharField(max_length=80)
    phone = models.CharField(max_length=20)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    rating = models.DecimalField(max_digits=2, decimal_places=1, default=5.0)
    is_available = models.BooleanField(default=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_available", "-rating", "full_name"]

    def __str__(self):
        return self.full_name

    def service_types_display(self):
        return ", ".join(self.service_types.values_list("name", flat=True))


class ServiceRequest(models.Model):
    STATUS_CHOICES = (
        ("new", "Yeni"),
        ("pending_provider", "Usta Onayi Bekleniyor"),
        ("matched", "Eslestirildi"),
        ("completed", "Tamamlandi"),
        ("cancelled", "Iptal Edildi"),
    )

    customer_name = models.CharField(max_length=120)
    customer_phone = models.CharField(max_length=20)
    city = models.CharField(max_length=80)
    district = models.CharField(max_length=80)
    service_type = models.ForeignKey(ServiceType, on_delete=models.PROTECT, related_name="requests")
    details = models.TextField()
    matched_provider = models.ForeignKey(
        Provider,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_requests",
    )
    customer = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="service_requests",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.customer_name} - {self.service_type.name}"


class ProviderOffer(models.Model):
    STATUS_CHOICES = (
        ("pending", "Beklemede"),
        ("accepted", "Kabul"),
        ("rejected", "Red"),
        ("expired", "Sure Doldu"),
        ("failed", "Gonderim Basarisiz"),
    )

    service_request = models.ForeignKey(ServiceRequest, on_delete=models.CASCADE, related_name="provider_offers")
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="offers")
    token = models.CharField(max_length=24, unique=True)
    sequence = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    last_delivery_detail = models.CharField(max_length=120, blank=True)
    sent_at = models.DateTimeField(default=timezone.now)
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["service_request_id", "sequence"]
        unique_together = ("service_request", "provider")

    def __str__(self):
        return f"Talep {self.service_request_id} -> {self.provider.full_name} ({self.status})"


class CustomerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="customer_profile")
    phone = models.CharField(max_length=20, blank=True)
    city = models.CharField(max_length=80, blank=True)
    district = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.username


class ProviderRating(models.Model):
    SCORE_CHOICES = (
        (1, "1"),
        (2, "2"),
        (3, "3"),
        (4, "4"),
        (5, "5"),
    )

    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="ratings")
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="provider_ratings")
    service_request = models.OneToOneField(
        ServiceRequest,
        on_delete=models.CASCADE,
        related_name="provider_rating",
        null=True,
        blank=True,
    )
    score = models.PositiveSmallIntegerField(choices=SCORE_CHOICES)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        request_label = self.service_request_id if self.service_request_id else "N/A"
        return f"{self.customer.username} -> {self.provider.full_name} / Talep {request_label}: {self.score}"

    @staticmethod
    def refresh_provider_average(provider_id):
        avg_score = (
            ProviderRating.objects.filter(provider_id=provider_id)
            .aggregate(avg_value=Avg("score"))
            .get("avg_value")
        )
        Provider.objects.filter(id=provider_id).update(rating=round(avg_score, 1) if avg_score is not None else 0.0)

    def save(self, *args, **kwargs):
        previous_provider_id = None
        if self.pk:
            previous_provider_id = (
                ProviderRating.objects.filter(pk=self.pk).values_list("provider_id", flat=True).first()
            )
        super().save(*args, **kwargs)
        ProviderRating.refresh_provider_average(self.provider_id)
        if previous_provider_id and previous_provider_id != self.provider_id:
            ProviderRating.refresh_provider_average(previous_provider_id)

    def delete(self, *args, **kwargs):
        provider_id = self.provider_id
        super().delete(*args, **kwargs)
        ProviderRating.refresh_provider_average(provider_id)
