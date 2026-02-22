from django import forms
from django.core.exceptions import ValidationError
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User

from .constants import NC_CITY_CHOICES, NC_DISTRICT_CHOICES
from .models import CustomerProfile, Provider, ProviderRating, ServiceRequest, ServiceType

ANY_DISTRICT_VALUE = "Herhangi"
DISTRICT_CHOICES_WITH_ANY = [("", "Ilce secin"), (ANY_DISTRICT_VALUE, "Herhangi")] + NC_DISTRICT_CHOICES


class ServiceSearchForm(forms.Form):
    service_type = forms.ModelChoiceField(
        queryset=ServiceType.objects.all(),
        empty_label="Hizmet turu secin",
        required=False,
        label="Hizmet",
    )
    city = forms.ChoiceField(choices=[("", "Sehir secin")] + NC_CITY_CHOICES, required=False, label="Sehir")
    district = forms.ChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, required=False, label="Ilce")
    latitude = forms.FloatField(required=False, widget=forms.HiddenInput())
    longitude = forms.FloatField(required=False, widget=forms.HiddenInput())


class ServiceRequestForm(forms.ModelForm):
    city = forms.ChoiceField(choices=[("", "Sehir secin")] + NC_CITY_CHOICES, label="Sehir")
    district = forms.ChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, label="Ilce")

    class Meta:
        model = ServiceRequest
        fields = ["customer_name", "customer_phone", "service_type", "city", "district", "details"]
        labels = {
            "customer_name": "Ad Soyad",
            "customer_phone": "Telefon",
            "service_type": "Istenen Hizmet",
            "city": "Sehir",
            "district": "Ilce",
            "details": "Ariza/Is Detayi",
        }
        widgets = {
            "details": forms.Textarea(attrs={"rows": 4}),
        }


class CustomerSignupForm(UserCreationForm):
    first_name = forms.CharField(max_length=150, required=True, label="Ad")
    last_name = forms.CharField(max_length=150, required=True, label="Soyad")
    email = forms.EmailField(required=True, label="E-posta")
    phone = forms.CharField(max_length=20, required=True, label="Telefon")
    city = forms.ChoiceField(choices=[("", "Sehir secin")] + NC_CITY_CHOICES, required=True, label="Sehir")
    district = forms.ChoiceField(choices=DISTRICT_CHOICES_WITH_ANY, required=True, label="Ilce")

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "phone", "city", "district", "password1", "password2"]
        labels = {
            "username": "Kullanici Adi",
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


class CustomerLoginForm(AuthenticationForm):
    username = forms.CharField(label="Kullanici Adi")
    password = forms.CharField(label="Sifre", widget=forms.PasswordInput)

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if Provider.objects.filter(user=user).exists():
            raise ValidationError(
                "Bu hesap usta hesabidir. Lutfen usta giris ekranini kullanin.",
                code="invalid_login",
            )


class ProviderLoginForm(AuthenticationForm):
    username = forms.CharField(label="Usta Kullanici Adi")
    password = forms.CharField(label="Sifre", widget=forms.PasswordInput)

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not Provider.objects.filter(user=user).exists():
            raise ValidationError(
                "Bu hesap usta olarak tanimli degil.",
                code="invalid_login",
            )


class ProviderRatingForm(forms.ModelForm):
    class Meta:
        model = ProviderRating
        fields = ["score", "comment"]
        labels = {
            "score": "Puan",
            "comment": "Yorum",
        }
        widgets = {
            "comment": forms.Textarea(attrs={"rows": 2, "placeholder": "Istege bagli kisa yorum"}),
        }
