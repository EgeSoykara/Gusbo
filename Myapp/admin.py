from django import forms
from django.contrib import admin
from django.contrib import messages

from .constants import NC_CITY_CHOICES, NC_DISTRICT_CHOICES
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


def with_existing_choice(choices, current_value):
    if current_value and current_value not in [value for value, _ in choices]:
        return choices + [(current_value, f"{current_value} (Mevcut)")]
    return choices


class ProviderAdminForm(forms.ModelForm):
    city = forms.ChoiceField(choices=NC_CITY_CHOICES, label="Şehir")
    district = forms.ChoiceField(choices=NC_DISTRICT_CHOICES, label="İlçe")

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
    city = forms.ChoiceField(choices=NC_CITY_CHOICES, label="Şehir")
    district = forms.ChoiceField(choices=NC_DISTRICT_CHOICES, label="İlçe")

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
    city = forms.ChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, required=False, label="Şehir")
    district = forms.ChoiceField(choices=[("", "İlçe seçin")] + NC_DISTRICT_CHOICES, required=False, label="İlçe")

    class Meta:
        model = CustomerProfile
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        city_value = self.instance.city if self.instance and self.instance.pk else None
        district_value = self.instance.district if self.instance and self.instance.pk else None
        self.fields["city"].choices = with_existing_choice([("", "Şehir seçin")] + list(NC_CITY_CHOICES), city_value)
        self.fields["district"].choices = with_existing_choice([("", "İlçe seçin")] + list(NC_DISTRICT_CHOICES), district_value)


@admin.register(ServiceType)
class ServiceTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    form = ProviderAdminForm
    list_display = (
        "full_name",
        "user",
        "service_types_list",
        "city",
        "district",
        "phone",
        "latitude",
        "longitude",
        "is_available",
        "rating",
    )
    list_filter = ("service_types", "city", "is_available")
    search_fields = ("full_name", "user__username", "city", "district", "phone", "service_types__name")
    filter_horizontal = ("service_types",)

    @admin.display(description="Hizmet Türleri")
    def service_types_list(self, obj):
        return obj.service_types_display()


@admin.register(ServiceRequest)
class ServiceRequestAdmin(admin.ModelAdmin):
    form = ServiceRequestAdminForm
    list_display = (
        "customer_name",
        "customer",
        "service_type",
        "city",
        "district",
        "status",
        "matched_provider",
        "matched_offer",
        "matched_at",
        "created_at",
    )
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
    list_filter = ("score", "provider__city")
    search_fields = ("service_request__id", "provider__full_name", "customer__username", "comment")


@admin.register(ProviderOffer)
class ProviderOfferAdmin(admin.ModelAdmin):
    list_display = (
        "service_request",
        "provider",
        "sequence",
        "status",
        "quote_amount",
        "expires_at",
        "reminder_sent_at",
        "token",
        "sent_at",
        "responded_at",
    )
    list_filter = ("status", "provider__city")
    search_fields = ("service_request__id", "provider__full_name", "token", "last_delivery_detail", "quote_note")


@admin.register(ServiceAppointment)
class ServiceAppointmentAdmin(admin.ModelAdmin):
    list_display = ("service_request", "provider", "customer", "scheduled_for", "status", "updated_at")
    list_filter = ("status", "provider__city")
    search_fields = ("service_request__id", "provider__full_name", "customer__username", "customer_note", "provider_note")


@admin.register(ServiceMessage)
class ServiceMessageAdmin(admin.ModelAdmin):
    list_display = ("service_request", "sender_user", "sender_role", "created_at", "read_at")
    list_filter = ("sender_role",)
    search_fields = ("service_request__id", "sender_user__username", "body")


@admin.register(ProviderWallet)
class ProviderWalletAdmin(admin.ModelAdmin):
    list_display = ("provider", "balance", "updated_at")
    search_fields = ("provider__full_name", "provider__user__username")
    actions = ("add_10_credits", "add_50_credits")

    @admin.action(description="+10 kredi yükle")
    def add_10_credits(self, request, queryset):
        self._bulk_credit_load(request, queryset, 10)

    @admin.action(description="+50 kredi yükle")
    def add_50_credits(self, request, queryset):
        self._bulk_credit_load(request, queryset, 50)

    def _bulk_credit_load(self, request, queryset, credit_amount):
        updated_count = 0
        for wallet in queryset.select_related("provider"):
            wallet.balance += credit_amount
            wallet.save(update_fields=["balance", "updated_at"])
            CreditTransaction.objects.create(
                provider=wallet.provider,
                wallet=wallet,
                transaction_type="admin_load",
                amount=credit_amount,
                balance_after=wallet.balance,
                note=f"Admin panelinden +{credit_amount} kredi yüklendi.",
            )
            updated_count += 1
        self.message_user(request, f"{updated_count} cüzdana kredi yüklendi.", level=messages.SUCCESS)


@admin.register(CreditTransaction)
class CreditTransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "provider", "transaction_type", "amount", "balance_after", "reference_offer")
    list_filter = ("transaction_type", "provider__city")
    search_fields = ("provider__full_name", "provider__user__username", "note", "reference_offer__service_request__id")
