from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('talep-olustur/', views.create_request, name='create_request'),
    path('webhooks/whatsapp/', views.whatsapp_webhook, name='whatsapp_webhook'),
    path('talep/<int:request_id>/puanla/', views.rate_request, name='rate_request'),
    path('giris/', views.login_view, name='login'),
    path('kayit/', views.signup_view, name='signup'),
    path('cikis/', views.logout_view, name='logout'),
    path('taleplerim/', views.my_requests, name='my_requests'),
    path('talep/<int:request_id>/tamamla/', views.complete_request, name='complete_request'),
    path('contact/',views.contact,name='contact'),
]
