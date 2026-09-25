import os
import sys
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('apps.authentication.urls')),
    path('', include('apps.analytics.urls')),
    path('', include('apps.artists.urls')),
    path('', include('apps.videos.urls')),
    path('', include('apps.milestones.urls')),
    path('', include('apps.reports.urls')),
    path('', include('apps.youtube.urls')),
    path('', include('apps.studio.urls')),
    path('', include('apps.radar.urls')),
    path('', include('apps.publishing.urls')),
    path('', include('apps.downloader.urls')),
    path('', include('apps.ai.urls')),
]

if settings.DEBUG or getattr(sys, 'frozen', False) or getattr(settings, 'DEBUG', False) or os.getenv('YT_QUID_DESKTOP') == '1':
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)


