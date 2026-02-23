from django import forms
from django.core.exceptions import ValidationError
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.utils import timezone

from .constants import NC_CITY_CHOICES, NC_DISTRICT_CHOICES
from .models import (
    CustomerProfile,
    Provider,
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


class ServiceSearchForm(forms.Form):
    service_type = forms.ModelChoiceField(
        queryset=ServiceType.objects.all(),
        empty_label="Hizmet türü seçin",
        required=False,
        label="Hizmet",
    )
    city = forms.ChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, required=False, label="Şehir")
    district = forms.ChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, required=False, label="İlçe")
    latitude = forms.FloatField(required=False, widget=forms.HiddenInput())
    longitude = forms.FloatField(required=False, widget=forms.HiddenInput())


class ServiceRequestForm(forms.ModelForm):
    city = forms.ChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, label="Şehir")
    district = forms.ChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, label="İlçe")

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
    city = forms.ChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, required=True, label="Şehir")
    district = forms.ChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, required=True, label="İlçe")

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
        if not Provider.objects.filter(user=user).exists():
            raise ValidationError(
                "Bu hesap usta olarak tanımlı değil.",
                code="invalid_login",
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
    city = forms.ChoiceField(choices=[("", "Şehir seçin")] + NC_CITY_CHOICES, required=True, label="Şehir")
    district = forms.ChoiceField(choices=[("", "İlçe seçin")] + NC_DISTRICT_CHOICES, required=True, label="İlçe")
    service_types = forms.ModelMultipleChoiceField(
        queryset=ServiceType.objects.all(),
        required=True,
        label="Verdiğin Hizmetler",
        widget=forms.SelectMultiple(attrs={"size": 8}),
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
                is_available=True,
            )
            provider.service_types.set(self.cleaned_data["service_types"])
        return user

    def clean_phone(self):
        return normalize_phone_value(self.cleaned_data.get("phone"))


class ProviderProfileForm(forms.ModelForm):
    city = forms.ChoiceField(choices=NC_CITY_CHOICES, required=True, label="Şehir")
    district = forms.ChoiceField(choices=NC_DISTRICT_CHOICES, required=True, label="İlçe")
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
    scheduled_for = forms.DateTimeField(
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Randevu Tarih/Saat",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    class Meta:
        model = ServiceAppointment
        fields = ["scheduled_for", "customer_note"]
        labels = {
            "customer_note": "Randevu Notu",
        }
        widgets = {
            "customer_note": forms.Textarea(attrs={"rows": 2, "placeholder": "İsteğe bağlı kısa not"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        min_dt = timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M")
        self.fields["scheduled_for"].widget.attrs["min"] = min_dt

    def clean_scheduled_for(self):
        scheduled_for = self.cleaned_data["scheduled_for"]
        if scheduled_for <= timezone.now():
            raise ValidationError("Randevu zamanı şimdiki zamandan ileri olmalıdır.")
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
