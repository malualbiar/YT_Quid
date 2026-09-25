from django.urls import path
from . import views

urlpatterns = [
    # Single 1-Hour Loop / Visualizer routes
    path('studio/', views.studio_home_view, name='studio_home'),
    path('studio/render/', views.studio_render_view, name='studio_render'),
    path('studio/<int:pk>/delete/', views.studio_delete_view, name='studio_delete'),

    # Non-Stop Continuous Long Mix Maker routes
    path('studio/mix/', views.mix_maker_view, name='mix_maker'),
    path('studio/mix/render/', views.mix_render_view, name='mix_render'),
    path('studio/mix/<int:pk>/', views.mix_detail_view, name='mix_detail'),
    path('studio/mix/<int:pk>/delete/', views.mix_delete_view, name='mix_delete'),
    path('studio/mix/api/search/', views.mix_import_api, name='mix_import_api'),

    # Short Video Generator & Multi-Clip Chopper routes
    path('studio/shorts/', views.shorts_maker_view, name='shorts_maker'),
    path('studio/shorts/render/', views.shorts_render_view, name='shorts_render'),
    path('studio/shorts/<int:pk>/', views.shorts_detail_view, name='shorts_detail'),
    path('studio/shorts/<int:pk>/delete/', views.shorts_delete_view, name='shorts_delete'),
    path('studio/shorts/<int:pk>/export-zip/', views.shorts_export_zip_view, name='shorts_export_zip'),
    path('studio/shorts/<int:pk>/download/<int:chop_idx>/', views.shorts_download_chop_view, name='shorts_download_chop'),
    path('studio/shorts/yt-download/', views.shorts_yt_download_view, name='shorts_yt_download'),

    # AI & Tap-to-Sync Lyrics Video Generator routes
    path('studio/lyrics/', views.lyrics_maker_view, name='lyrics_maker'),
    path('studio/lyrics/render/', views.lyrics_render_view, name='lyrics_render'),
    path('studio/lyrics/<int:pk>/', views.lyrics_detail_view, name='lyrics_detail'),
    path('studio/lyrics/<int:pk>/delete/', views.lyrics_delete_view, name='lyrics_delete'),
    path('studio/lyrics/<int:pk>/download/', views.lyrics_download_video_view, name='lyrics_download_video'),
    path('studio/lyrics/<int:pk>/export-lrc/', views.lyrics_export_lrc_view, name='lyrics_export_lrc'),
    path('studio/lyrics/<int:pk>/update-lyrics/', views.lyrics_update_data_api, name='lyrics_update_data_api'),
    path('studio/lyrics/api/parse/', views.lyrics_parse_api, name='lyrics_parse_api'),
    path('studio/lyrics/api/vocal-sync/', views.lyrics_vocal_sync_api, name='lyrics_vocal_sync_api'),
    path('studio/lyrics/api/search-online/', views.lyrics_online_search_api, name='lyrics_online_search_api'),
    path('studio/lyrics/api/ai-transcribe/', views.lyrics_ai_transcribe_api, name='lyrics_ai_transcribe_api'),
    # Global Cancel Render and Progress routes
    path('studio/cancel/<str:project_type>/<int:pk>/', views.studio_cancel_render_view, name='studio_cancel_render'),
    path('studio/api/progress/<str:project_type>/<int:pk>/', views.studio_render_progress_view, name='studio_render_progress'),

    # Automation Pipeline routes
    path('studio/automation/', views.automation_dashboard_view, name='automation_dashboard'),
    path('studio/automation/create/', views.automation_create_view, name='automation_create'),
    path('studio/automation/<int:pk>/run/', views.automation_run_view, name='automation_run'),
    path('studio/automation/<int:pk>/status/', views.automation_status_api, name='automation_status_api'),
    path('studio/automation/<int:pk>/delete/', views.automation_delete_view, name='automation_delete'),
]


