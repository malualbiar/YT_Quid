"""
Django settings for YoutubeAnalytics project.
"""

from pathlib import Path
import os
import sys
import dj_database_url
from dotenv import load_dotenv

if getattr(sys, 'frozen', False):
    BASE_DIR = Path(getattr(sys, '_MEIPASS', os.path.dirname(sys.executable)))
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

IS_RENDER = os.getenv('RENDER', '') == 'true'

# Load environment variables from .env file
load_dotenv(BASE_DIR / '.env')

# Quick-start development settings - unsuitable for production
SECRET_KEY = os.getenv(
    'SECRET_KEY',
    'django-insecure-youtube-artist-analytics-secret-key-change-in-production'
)

DEBUG = os.getenv('DEBUG', 'True') == 'True'

_allowed = [h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]
_allowed += ['testserver', '127.0.0.1', 'localhost']
# On Render, RENDER_EXTERNAL_HOSTNAME is set automatically.
_render_host = os.getenv('RENDER_EXTERNAL_HOSTNAME', '')
if _render_host:
    _allowed.append(_render_host)
ALLOWED_HOSTS = list(set(_allowed))

# Render requires HTTPS; only enforce when DEBUG is off.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    CSRF_TRUSTED_ORIGINS = [f'https://{h}' for h in ALLOWED_HOSTS if h not in ('testserver', '127.0.0.1', 'localhost')]

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',

    # Local Apps
    'apps.authentication.apps.AuthenticationConfig',
    'apps.artists.apps.ArtistsConfig',
    'apps.videos.apps.VideosConfig',
    'apps.analytics.apps.AnalyticsConfig',
    'apps.milestones.apps.MilestonesConfig',
    'apps.reports.apps.ReportsConfig',
    'apps.youtube.apps.YoutubeConfig',
    'apps.studio.apps.StudioConfig',
    'apps.radar.apps.RadarConfig',
    'apps.publishing.apps.PublishingConfig',
    'apps.downloader.apps.DownloaderConfig',
    'apps.ai.apps.AiConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.analytics.context_processors.global_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'

# Database
# On Render, DATABASE_URL is set automatically for PostgreSQL services.
# Falls back to SQLite for local and desktop use.
import shutil

if os.getenv('DATABASE_URL'):
    DATABASES = {
        'default': dj_database_url.config(
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
elif getattr(sys, 'frozen', False) or os.getenv('YT_QUID_DESKTOP') == '1':
    DATA_DIR = Path(os.getenv('APPDATA', os.path.expanduser('~'))) / 'YTQuid' if os.name == 'nt' else Path.home() / '.config' / 'ytquid'
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = DATA_DIR / 'db.sqlite3'
    if (DATA_DIR / '.env').exists():
        load_dotenv(DATA_DIR / '.env')
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': DB_PATH,
        }
    }
else:
    DB_PATH = BASE_DIR / 'db.sqlite3'
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': DB_PATH,
        }
    }

# Custom User Model
AUTH_USER_MODEL = 'authentication.User'

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = os.getenv('TIME_ZONE', 'Africa/Nairobi')
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
}

# Media files
MEDIA_URL = '/media/'
if getattr(sys, 'frozen', False) or os.getenv('YT_QUID_DESKTOP') == '1':
    MEDIA_ROOT = DATA_DIR / 'media'
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
else:
    MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Authentication URLs
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# YouTube API Settings
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY', '')
YOUTUBE_API_SERVICE_NAME = 'youtube'
YOUTUBE_API_VERSION = 'v3'
SYNC_INTERVAL_HOURS = int(os.getenv('SYNC_INTERVAL_HOURS', '6'))

# Google OAuth 2.0 Credentials (for video upload & publishing)
GOOGLE_OAUTH_CLIENT_ID = os.getenv('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv('GOOGLE_OAUTH_CLIENT_SECRET', '')
GOOGLE_OAUTH_REDIRECT_URI = os.getenv('GOOGLE_OAUTH_REDIRECT_URI', 'http://127.0.0.1:8000/publishing/oauth/callback/')

# Gemini AI Content Generation
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
