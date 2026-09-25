from apps.milestones.models import NotificationLog
from apps.youtube.models import YouTubeApiUsage, SyncLog

def global_context(request):
    """Context processor for notifications, theme, and system status in all views"""
    unread_notifications_count = NotificationLog.objects.filter(is_read=False).count()
    recent_notifications = NotificationLog.objects.all()[:5]
    last_sync = SyncLog.objects.first()

    try:
        from apps.publishing.models import YouTubeOAuthAccount
        oauth_accounts = list(YouTubeOAuthAccount.objects.filter(is_active=True).order_by('-is_default', 'channel_title'))
    except Exception:
        oauth_accounts = []

    return {
        'unread_notifications_count': unread_notifications_count,
        'recent_notifications': recent_notifications,
        'last_sync_log': last_sync,
        'oauth_accounts': oauth_accounts,
    }
