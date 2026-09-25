from django.urls import path
from . import views

urlpatterns = [
    path('videos/', views.video_list_view, name='video_list'),
    path('videos/<int:pk>/', views.video_detail_view, name='video_detail'),
    path('videos/<int:pk>/delete/', views.video_delete_view, name='video_delete'),
]
