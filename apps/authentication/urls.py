from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    path('system/users/', views.users_manage_view, name='users_manage'),
    path('legal/terms/', views.terms_of_service_view, name='terms_of_service'),
    path('legal/privacy/', views.privacy_policy_view, name='privacy_policy'),
]
