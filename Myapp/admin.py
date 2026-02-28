from django import forms
from django.contrib import admin
from django.contrib import messages
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from .constants import NC_CITY_CHOICES, NC_DISTRICT_CHOICES
from .models import (
    CustomerProfile,
    IdempotencyRecord,
    NotificationCursor,
    Provider,
    ProviderAvailabilitySlot,
    ProviderOffer,
    ProviderRating,
    SchedulerHeartbeat,
    ServiceAppointment,
    ServiceMessage,
    ServiceRequest,
    ServiceType,
    WorkflowEvent,
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
        "is_verified",
        "verified_at",
        "rating",
    )
    list_filter = ("service_types", "city", "is_available", "is_verified")
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


@admin.register(ProviderAvailabilitySlot)
class ProviderAvailabilitySlotAdmin(admin.ModelAdmin):
    list_display = ("provider", "weekday", "start_time", "end_time", "is_active", "updated_at")
    list_filter = ("weekday", "is_active", "provider__city")
    search_fields = ("provider__full_name", "provider__user__username")


@admin.register(ServiceMessage)
class ServiceMessageAdmin(admin.ModelAdmin):
    list_display = ("service_request", "sender_user", "sender_role", "created_at", "read_at")
    list_filter = ("sender_role",)
    search_fields = ("service_request__id", "sender_user__username", "body")


@admin.register(WorkflowEvent)
class WorkflowEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "target_type",
        "service_request",
        "appointment",
        "from_status",
        "to_status",
        "actor_role",
        "actor_user",
        "source",
        "note",
    )
    list_filter = ("target_type", "actor_role", "source", "from_status", "to_status", "created_at")
    search_fields = (
        "service_request__id",
        "appointment__id",
        "actor_user__username",
        "note",
    )
    list_select_related = ("service_request", "appointment", "actor_user")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    readonly_fields = (
        "target_type",
        "service_request",
        "appointment",
        "from_status",
        "to_status",
        "actor_user",
        "actor_role",
        "source",
        "note",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IdempotencyRecord)
class IdempotencyRecordAdmin(admin.ModelAdmin):
    list_display = ("created_at", "scope", "endpoint", "user", "key_short")
    list_filter = ("scope", "created_at")
    search_fields = ("scope", "endpoint", "user__username", "key")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    readonly_fields = ("key", "scope", "endpoint", "user", "created_at")
    actions = ("purge_records_older_than_2_days",)

    @admin.display(description="Anahtar")
    def key_short(self, obj):
        return f"{obj.key[:12]}..."

    @admin.action(description="2 günden eski kayıtları temizle")
    def purge_records_older_than_2_days(self, request, queryset):
        cutoff = timezone.now() - timedelta(days=2)
        deleted_count, _ = IdempotencyRecord.objects.filter(created_at__lt=cutoff).delete()
        self.message_user(request, f"{deleted_count} idempotency kaydı temizlendi.", level=messages.SUCCESS)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SchedulerHeartbeat)
class SchedulerHeartbeatAdmin(admin.ModelAdmin):
    list_display = (
        "worker_name",
        "run_count",
        "last_started_at",
        "last_success_at",
        "last_error_at",
        "healthy",
        "updated_at",
    )
    list_filter = ("updated_at", "last_error_at")
    search_fields = ("worker_name", "last_error")
    ordering = ("worker_name",)
    readonly_fields = (
        "worker_name",
        "run_count",
        "last_started_at",
        "last_success_at",
        "last_error_at",
        "last_error",
        "updated_at",
    )

    @admin.display(boolean=True, description="Sağlıklı")
    def healthy(self, obj):
        stale_after = max(10, int(getattr(settings, "LIFECYCLE_HEARTBEAT_STALE_SECONDS", 180)))
        reference_at = obj.last_success_at or obj.last_started_at or obj.updated_at
        if not reference_at:
            return False
        age_seconds = (timezone.now() - reference_at).total_seconds()
        return age_seconds <= stale_after

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(NotificationCursor)
class NotificationCursorAdmin(admin.ModelAdmin):
    list_display = ("user", "workflow_seen_at", "updated_at")
    search_fields = ("user__username",)
    ordering = ("-updated_at",)
    readonly_fields = ("user", "workflow_seen_at", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False
