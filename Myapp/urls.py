from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('talep-olustur/', views.create_request, name='create_request'),
    path('talep/<int:request_id>/puanla/', views.rate_request, name='rate_request'),
    path('giris/', views.login_view, name='login'),
    path('musteri/giris/', views.login_view, name='customer_login'),
    path('usta/giris/', views.provider_login_view, name='provider_login'),
    path('kayit/', views.signup_view, name='signup'),
    path('musteri/kayit/', views.signup_view, name='customer_signup'),
    path('cikis/', views.logout_view, name='logout'),
    path('taleplerim/', views.my_requests, name='my_requests'),
    path('talep/<int:request_id>/tamamla/', views.complete_request, name='complete_request'),
    path('talep/<int:request_id>/iptal/', views.cancel_request, name='cancel_request'),
    path('talep/<int:request_id>/sil/', views.delete_cancelled_request, name='delete_cancelled_request'),
    path('taleplerim/iptalleri-sil/', views.delete_all_cancelled_requests, name='delete_all_cancelled_requests'),
    path('usta/talepler/', views.provider_requests, name='provider_requests'),
    path('usta/teklif/<int:offer_id>/kabul/', views.provider_accept_offer, name='provider_accept_offer'),
    path('usta/teklif/<int:offer_id>/reddet/', views.provider_reject_offer, name='provider_reject_offer'),
    path('contact/',views.contact,name='contact'),
]
