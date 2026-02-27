from datetime import timedelta
import unicodedata

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, UserCreationForm
from django.contrib.auth.models import User
from django.utils import timezone

from .constants import NC_CITY_CHOICES, NC_DISTRICT_CHOICES
from .models import (
    CustomerProfile,
    Provider,
    ProviderAvailabilitySlot,
    ProviderRating,
    ServiceAppointment,
    ServiceMessage,
    ServiceRequest,
    ServiceType,
)

ANY_DISTRICT_VALUE = "Herhangi"
DISTRICT_CHOICES_WITH_ANY = [("", "İlçe seçin"), (ANY_DISTRICT_VALUE, "Herhangi")] + NC_DISTRICT_CHOICES
PHONE_HELP_TEXT = "Örnek: 0555 123 45 67. +90 ile de girebilirsiniz."


def phone_widget_attrs():
    return {
        "placeholder": "0555 123 45 67",
        "inputmode": "numeric",
        "autocomplete": "tel-national",
        "maxlength": "14",
        "data-phone-field": "1",
        "pattern": "[0-9+()\\-\\s]*",
        "title": "Örnek: 0555 123 45 67",
    }


def normalize_phone_value(raw_value):
    phone_value = (raw_value or "").strip()
    digits = "".join(char for char in phone_value if char.isdigit())

    if digits.startswith("90") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif len(digits) == 10 and digits.startswith("5"):
        digits = "0" + digits

    if len(digits) != 11 or not digits.startswith("05"):
        raise ValidationError("Telefonu 05XX XXX XX XX formatında girin. Örnek: 0555 123 45 67.")
    return digits


def normalize_choice_value(raw_value):
    value = str(raw_value or "").strip().lower()
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in normalized if not unicodedata.combining(char))
    return "".join(char for char in without_marks if char.isalnum())


class FlexibleChoiceField(forms.ChoiceField):
    def valid_value(self, value):
        if super().valid_value(value):
            return True
        normalized_value = normalize_choice_value(value)
        if not normalized_value:
            return False
        for key, _label in self.choices:
            if normalize_choice_value(key) == normalized_value:
                return True
        return False


class ServiceSearchForm(forms.Form):
    service_type = forms.ModelChoiceField(
        queryset=ServiceType.objects.all(),
        empty_label="Hizmet türü seçin",
        required=False,
        label="Hizmet",
    )
    city = FlexibleChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, required=False, label="Şehir")
    district = FlexibleChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, required=False, label="İlçe")
    latitude = forms.FloatField(required=False, widget=forms.HiddenInput())
    longitude = forms.FloatField(required=False, widget=forms.HiddenInput())


class ServiceRequestForm(forms.ModelForm):
    city = FlexibleChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, label="Şehir")
    district = FlexibleChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, label="İlçe")

    class Meta:
        model = ServiceRequest
        fields = ["customer_name", "customer_phone", "service_type", "city", "district", "details"]
        labels = {
            "customer_name": "Ad Soyad",
            "customer_phone": "Telefon",
            "service_type": "İstenen Hizmet",
            "city": "Şehir",
            "district": "İlçe",
            "details": "Arıza/İş Detayı",
        }
        widgets = {
            "customer_phone": forms.TextInput(attrs=phone_widget_attrs()),
            "details": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer_phone"].help_text = PHONE_HELP_TEXT

    def clean_customer_phone(self):
        return normalize_phone_value(self.cleaned_data.get("customer_phone"))


class CustomerSignupForm(UserCreationForm):
    first_name = forms.CharField(max_length=150, required=True, label="Ad")
    last_name = forms.CharField(max_length=150, required=True, label="Soyad")
    email = forms.EmailField(required=True, label="E-posta")
    phone = forms.CharField(
        max_length=20,
        required=True,
        label="Telefon",
        help_text=PHONE_HELP_TEXT,
        widget=forms.TextInput(attrs=phone_widget_attrs()),
    )
    city = FlexibleChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, required=True, label="Şehir")
    district = FlexibleChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, required=True, label="İlçe")

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "phone", "city", "district", "password1", "password2"]
        labels = {
            "username": "Kullanıcı Adı",
        }

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
            CustomerProfile.objects.update_or_create(
                user=user,
                defaults={
                    "phone": self.cleaned_data["phone"],
                    "city": self.cleaned_data["city"],
                    "district": self.cleaned_data["district"],
                },
            )
        return user

    def clean_phone(self):
        return normalize_phone_value(self.cleaned_data.get("phone"))


class CustomerLoginForm(AuthenticationForm):
    username = forms.CharField(label="Kullanıcı Adı")
    password = forms.CharField(label="Şifre", widget=forms.PasswordInput)

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if Provider.objects.filter(user=user).exists():
            raise ValidationError(
                "Bu hesap usta hesabıdır. Lütfen usta giriş ekranını kullanın.",
                code="invalid_login",
            )


class ProviderLoginForm(AuthenticationForm):
    username = forms.CharField(label="Usta Kullanıcı Adı")
    password = forms.CharField(label="Şifre", widget=forms.PasswordInput)

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        provider = Provider.objects.filter(user=user).first()
        if not provider:
            raise ValidationError(
                "Bu hesap usta olarak tanımlı değil.",
                code="invalid_login",
            )
        if not provider.is_verified:
            raise ValidationError(
                "Usta hesabınız admin onayı bekliyor. Onaydan sonra giriş yapabilirsiniz.",
                code="inactive",
            )
class ProviderSignupForm(UserCreationForm):
    full_name = forms.CharField(max_length=120, required=True, label="Ad Soyad")
    email = forms.EmailField(required=True, label="E-posta")
    phone = forms.CharField(
        max_length=20,
        required=True,
        label="Telefon",
        help_text=PHONE_HELP_TEXT,
        widget=forms.TextInput(attrs=phone_widget_attrs()),
    )
    city = FlexibleChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, required=True, label="Şehir")
    district = FlexibleChoiceField(choices=[("", "İlçe seçin")] + NC_DISTRICT_CHOICES, required=True, label="İlçe")
    service_types = forms.ModelMultipleChoiceField(
        queryset=ServiceType.objects.all(),
        required=True,
        label="Verdiğin Hizmetler",
        help_text="Birden fazla hizmeti tek tıkla seçebilirsiniz.",
        widget=forms.CheckboxSelectMultiple(attrs={"class": "service-types-checklist"}),
    )
    description = forms.CharField(
        required=False,
        label="Kısa Tanıtım",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Kendini ve tecrübeni kısaca anlat"}),
    )

    class Meta:
        model = User
        fields = [
            "username",
            "full_name",
            "email",
            "phone",
            "city",
            "district",
            "service_types",
            "description",
            "password1",
            "password2",
        ]
        labels = {
            "username": "Kullanıcı Adı",
        }

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
            provider = Provider.objects.create(
                user=user,
                full_name=self.cleaned_data["full_name"],
                city=self.cleaned_data["city"],
                district=self.cleaned_data["district"],
                phone=self.cleaned_data["phone"],
                description=self.cleaned_data.get("description", "").strip(),
                is_verified=False,
                is_available=True,
            )
            provider.service_types.set(self.cleaned_data["service_types"])
        return user

    def clean_phone(self):
        return normalize_phone_value(self.cleaned_data.get("phone"))


class ProviderProfileForm(forms.ModelForm):
    city = FlexibleChoiceField(choices=NC_CITY_CHOICES, required=True, label="Şehir")
    district = FlexibleChoiceField(choices=NC_DISTRICT_CHOICES, required=True, label="İlçe")
    is_available = forms.TypedChoiceField(
        choices=[("True", "Müsait"), ("False", "Müsait Değil")],
        coerce=lambda value: value == "True",
        label="Çalışma Durumu",
    )

    class Meta:
        model = Provider
        fields = ["full_name", "phone", "city", "district", "service_types", "description", "is_available"]
        labels = {
            "full_name": "Ad Soyad",
            "phone": "Telefon",
            "service_types": "Hizmet Türleri",
            "description": "Kısa Tanıtım",
        }
        help_texts = {
            "phone": PHONE_HELP_TEXT,
            "service_types": "Birden fazla hizmeti tek tıkla seçebilirsiniz.",
        }
        widgets = {
            "phone": forms.TextInput(attrs=phone_widget_attrs()),
            "service_types": forms.CheckboxSelectMultiple(attrs={"class": "service-types-checklist"}),
            "description": forms.Textarea(attrs={"rows": 3, "placeholder": "Profil açıklaması"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["is_available"].initial = "True" if self.instance.is_available else "False"

    def save(self, commit=True):
        provider = super().save(commit=False)
        provider.is_available = self.cleaned_data["is_available"]
        if commit:
            provider.save()
            self.save_m2m()
        return provider

    def clean_phone(self):
        return normalize_phone_value(self.cleaned_data.get("phone"))


class ProviderAvailabilitySlotForm(forms.ModelForm):
    class Meta:
        model = ProviderAvailabilitySlot
        fields = ["weekday", "start_time", "end_time", "is_active"]
        labels = {
            "weekday": "Gun",
            "start_time": "Baslangic",
            "end_time": "Bitis",
            "is_active": "Aktif",
        }
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, provider=None, **kwargs):
        self.provider = provider
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        weekday = cleaned_data.get("weekday")
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")
        if weekday is None or not start_time or not end_time:
            return cleaned_data

        if end_time <= start_time:
            raise ValidationError("Bitis saati baslangic saatinden sonra olmalidir.")

        if self.provider:
            overlap_qs = ProviderAvailabilitySlot.objects.filter(
                provider=self.provider,
                weekday=weekday,
                start_time__lt=end_time,
                end_time__gt=start_time,
            )
            if self.instance and self.instance.pk:
                overlap_qs = overlap_qs.exclude(pk=self.instance.pk)
            if overlap_qs.exists():
                raise ValidationError("Ayni gunde cakisan bir musaitlik araligi zaten var.")
        return cleaned_data


class AccountIdentityForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email"]
        labels = {
            "username": "Kullanıcı Adı",
            "first_name": "Ad",
            "last_name": "Soyad",
            "email": "E-posta",
        }


class CustomerContactSettingsForm(forms.ModelForm):
    city = FlexibleChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, required=True, label="Şehir")
    district = FlexibleChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, required=True, label="İlçe")

    class Meta:
        model = CustomerProfile
        fields = ["phone", "city", "district"]
        labels = {
            "phone": "Telefon",
        }
        widgets = {
            "phone": forms.TextInput(attrs=phone_widget_attrs()),
        }
        help_texts = {
            "phone": PHONE_HELP_TEXT,
        }

    def clean_phone(self):
        return normalize_phone_value(self.cleaned_data.get("phone"))


class ProviderContactSettingsForm(forms.ModelForm):
    city = FlexibleChoiceField(choices=NC_CITY_CHOICES, required=True, label="Şehir")
    district = FlexibleChoiceField(choices=NC_DISTRICT_CHOICES, required=True, label="İlçe")

    class Meta:
        model = Provider
        fields = ["full_name", "phone", "city", "district"]
        labels = {
            "full_name": "Ad Soyad",
            "phone": "Telefon",
        }
        widgets = {
            "phone": forms.TextInput(attrs=phone_widget_attrs()),
        }
        help_texts = {
            "phone": PHONE_HELP_TEXT,
        }

    def clean_phone(self):
        return normalize_phone_value(self.cleaned_data.get("phone"))


class AccountPasswordChangeForm(PasswordChangeForm):
    old_password = forms.CharField(label="Mevcut Şifre", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    new_password1 = forms.CharField(label="Yeni Şifre", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    new_password2 = forms.CharField(label="Yeni Şifre (Tekrar)", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))


class ProviderRatingForm(forms.ModelForm):
    class Meta:
        model = ProviderRating
        fields = ["score", "comment"]
        labels = {
            "score": "Puan",
            "comment": "Yorum",
        }
        widgets = {
            "comment": forms.Textarea(attrs={"rows": 2, "placeholder": "İsteğe bağlı kısa yorum"}),
        }


class AppointmentCreateForm(forms.ModelForm):
    QUICK_TIME_CHOICES = (
        ("", "Detayli tarih sec"),
        ("now", "Simdi"),
        ("30m", "30 dakika sonra"),
        ("1h", "1 saat sonra"),
        ("2h", "2 saat sonra"),
    )
    QUICK_TIME_MINUTES = {
        "now": 1,
        "30m": 30,
        "1h": 60,
        "2h": 120,
    }
    appointment_preset = forms.ChoiceField(
        choices=QUICK_TIME_CHOICES,
        required=False,
        label="Hizli Secim",
    )
    scheduled_for = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Randevu Tarih/Saat",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    class Meta:
        model = ServiceAppointment
        fields = ["scheduled_for", "customer_note"]
        labels = {
            "customer_note": "Randevu Notu",
        }
        widgets = {
            "customer_note": forms.Textarea(attrs={"rows": 2, "placeholder": "Istege bagli kisa not"}),
        }

    def __init__(self, *args, **kwargs):
        self.provider = kwargs.pop("provider", None)
        self.current_appointment_id = kwargs.pop("current_appointment_id", None)
        super().__init__(*args, **kwargs)
        min_dt = timezone.localtime(timezone.now() + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
        self.fields["scheduled_for"].widget.attrs["min"] = min_dt

    def _validate_provider_availability(self, scheduled_for):
        if not self.provider:
            return

        if not self.provider.is_available:
            raise ValidationError("Secilen usta su an musait degil.")

        local_dt = timezone.localtime(scheduled_for)
        weekday = local_dt.weekday()
        time_value = local_dt.time()
        active_slots = self.provider.availability_slots.filter(is_active=True)
        if active_slots.exists():
            day_slots = active_slots.filter(weekday=weekday)
            has_slot_match = any(slot.start_time <= time_value < slot.end_time for slot in day_slots)
            if not has_slot_match:
                raise ValidationError("Secilen saat ustanin tanimli musaitlik araliginda degil.")

        buffer_minutes = max(5, int(getattr(settings, "APPOINTMENT_SLOT_BUFFER_MINUTES", 45)))
        range_start = scheduled_for - timedelta(minutes=buffer_minutes)
        range_end = scheduled_for + timedelta(minutes=buffer_minutes)
        conflict_qs = ServiceAppointment.objects.filter(
            provider=self.provider,
            status__in=["pending", "pending_customer", "confirmed"],
            scheduled_for__gte=range_start,
            scheduled_for__lte=range_end,
        )
        if self.current_appointment_id:
            conflict_qs = conflict_qs.exclude(id=self.current_appointment_id)
        if conflict_qs.exists():
            raise ValidationError("Bu saat dolu. Lutfen baska bir zaman secin.")

    def clean_scheduled_for(self):
        scheduled_for = self.cleaned_data.get("scheduled_for")
        preset = (self.data.get("appointment_preset") or "").strip()
        if preset:
            minutes = self.QUICK_TIME_MINUTES.get(preset)
            if minutes is None:
                raise ValidationError("Gecersiz hizli tarih secimi.")
            scheduled_for = timezone.now() + timedelta(minutes=minutes)
            self.cleaned_data["scheduled_for"] = scheduled_for

        if not scheduled_for:
            raise ValidationError("Randevu zamani secmelisiniz.")
        if scheduled_for <= timezone.now():
            raise ValidationError("Randevu zamani simdiki zamandan ileri olmali.")
        self._validate_provider_availability(scheduled_for)
        return scheduled_for


class ServiceMessageForm(forms.ModelForm):
    class Meta:
        model = ServiceMessage
        fields = ["body"]
        labels = {
            "body": "Mesaj",
        }
        widgets = {
            "body": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Mesajınızı yazın (maks 1000 karakter)"},
            ),
        }

    def clean_body(self):
        body = (self.cleaned_data.get("body") or "").strip()
        if len(body) < 2:
            raise ValidationError("Mesaj en az 2 karakter olmalı.")
        return body


