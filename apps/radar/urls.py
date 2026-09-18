from django.urls import path
from . import views

urlpatterns = [
    path('radar/', views.radar_feed_view, name='radar_feed'),
    path('radar/refresh/', views.refresh_radar_view, name='radar_refresh'),
    path('radar/channels/add/', views.add_channel_view, name='radar_add_channel'),
    path('radar/channels/<str:channel_id>/toggle/', views.toggle_channel_view, name='radar_toggle_channel'),
    path('radar/channels/<str:channel_id>/delete/', views.delete_channel_view, name='radar_delete_channel'),
    path('radar/download/', views.download_audio_view, name='radar_download_audio'),
    path('radar/seen/mark/', views.mark_seen_view, name='radar_mark_seen'),
    path('radar/seen/unmark/', views.unmark_seen_view, name='radar_unmark_seen'),
]
