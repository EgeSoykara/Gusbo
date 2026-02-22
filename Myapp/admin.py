from django import forms
from django.contrib import admin

from .constants import NC_CITY_CHOICES, NC_DISTRICT_CHOICES
from .models import CustomerProfile, Provider, ProviderOffer, ProviderRating, ServiceRequest, ServiceType


def with_existing_choice(choices, current_value):
    if current_value and current_value not in [value for value, _ in choices]:
        return choices + [(current_value, f"{current_value} (Mevcut)")]
    return choices


class ProviderAdminForm(forms.ModelForm):
    city = forms.ChoiceField(choices=NC_CITY_CHOICES, label="Sehir")
    district = forms.ChoiceField(choices=NC_DISTRICT_CHOICES, label="Ilce")

    class Meta:
        model = Provider
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        city_value = self.instance.city if self.instance and self.instance.pk else None
        district_value = self.instance.district if self.instance and self.instance.pk else None
        self.fields["city"].choices = with_existing_choice(list(NC_CITY_CHOICES), city_value)
        self.fields["district"].choices = with_existing_choice(list(NC_DISTRICT_CHOICES), district_value)


class ServiceRequestAdminForm(forms.ModelForm):
    city = forms.ChoiceField(choices=NC_CITY_CHOICES, label="Sehir")
    district = forms.ChoiceField(choices=NC_DISTRICT_CHOICES, label="Ilce")

    class Meta:
        model = ServiceRequest
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        city_value = self.instance.city if self.instance and self.instance.pk else None
        district_value = self.instance.district if self.instance and self.instance.pk else None
        self.fields["city"].choices = with_existing_choice(list(NC_CITY_CHOICES), city_value)
        self.fields["district"].choices = with_existing_choice(list(NC_DISTRICT_CHOICES), district_value)


class CustomerProfileAdminForm(forms.ModelForm):
    city = forms.ChoiceField(choices=[("", "Sehir secin")] + NC_CITY_CHOICES, required=False, label="Sehir")
    district = forms.ChoiceField(choices=[("", "Ilce secin")] + NC_DISTRICT_CHOICES, required=False, label="Ilce")

    class Meta:
        model = CustomerProfile
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        city_value = self.instance.city if self.instance and self.instance.pk else None
        district_value = self.instance.district if self.instance and self.instance.pk else None
        self.fields["city"].choices = with_existing_choice([("", "Sehir secin")] + list(NC_CITY_CHOICES), city_value)
        self.fields["district"].choices = with_existing_choice([("", "Ilce secin")] + list(NC_DISTRICT_CHOICES), district_value)


@admin.register(ServiceType)
class ServiceTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    form = ProviderAdminForm
    list_display = (
        "full_name",
        "service_type",
        "city",
        "district",
        "phone",
        "latitude",
        "longitude",
        "is_available",
        "rating",
    )
    list_filter = ("service_type", "city", "is_available")
    search_fields = ("full_name", "city", "district", "phone")


@admin.register(ServiceRequest)
class ServiceRequestAdmin(admin.ModelAdmin):
    form = ServiceRequestAdminForm
    list_display = ("customer_name", "customer", "service_type", "city", "district", "status", "matched_provider", "created_at")
    list_filter = ("status", "service_type", "city")
    search_fields = ("customer_name", "customer_phone", "city", "district")


@admin.register(CustomerProfile)
class CustomerProfileAdmin(admin.ModelAdmin):
    form = CustomerProfileAdminForm
    list_display = ("user", "phone", "city", "district", "created_at")
    search_fields = ("user__username", "phone", "city", "district")


@admin.register(ProviderRating)
class ProviderRatingAdmin(admin.ModelAdmin):
    list_display = ("service_request", "provider", "customer", "score", "updated_at")
    list_filter = ("score", "provider__city", "provider__service_type")
    search_fields = ("service_request__id", "provider__full_name", "customer__username", "comment")


@admin.register(ProviderOffer)
class ProviderOfferAdmin(admin.ModelAdmin):
    list_display = ("service_request", "provider", "sequence", "status", "token", "sent_at", "responded_at")
    list_filter = ("status", "provider__city", "provider__service_type")
    search_fields = ("service_request__id", "provider__full_name", "token", "last_delivery_detail")
