from django.urls import path
from . import views

urlpatterns = [
    path('ai/generate/', views.ai_generate_view, name='ai_generate'),
    path('ai/usage/', views.ai_usage_view, name='ai_usage'),
]
