from django.db import models


class RadarChannel(models.Model):
    """
    YouTube channel manually added to the AI Radar for monitoring
    recent video uploads and discovering high-potential tracks.
    """
    channel_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Official YouTube Channel ID (e.g. UCxxxxxxxxxxxxxx)"
    )
    channel_name = models.CharField(
        max_length=255,
        help_text="YouTube Channel Display Title"
    )
    handle = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text="YouTube Channel Handle (e.g. @channel_name)"
    )
    channel_url = models.URLField(
        max_length=500,
        blank=True,
        default='',
        help_text="Full YouTube Channel URL"
    )
    thumbnail_url = models.URLField(
        max_length=1000,
        blank=True,
        default='',
        help_text="Channel avatar / thumbnail URL"
    )
    uploads_playlist_id = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text="Uploads playlist ID (UUxxxxxxxxxxxxxx) for low-quota fetching"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this channel is actively monitored in the radar feed"
    )
    subscriber_count = models.BigIntegerField(
        default=0,
        help_text="Approximate subscriber count"
    )
    video_count = models.IntegerField(
        default=0,
        help_text="Total video count from YouTube"
    )
    last_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when this channel was last synced"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['channel_name']
        verbose_name = 'Radar Tracked Channel'
        verbose_name_plural = 'Radar Tracked Channels'

    def __str__(self):
        return f"{self.channel_name} ({self.handle or self.channel_id})"

    def get_uploads_playlist_id(self):
        """Derives or returns the uploads playlist ID (UU...) from channel ID (UC...)"""
        if self.uploads_playlist_id:
            return self.uploads_playlist_id
        if self.channel_id.startswith('UC'):
            return 'UU' + self.channel_id[2:]
        return ''


class SeenVideo(models.Model):
    """
    Tracks YouTube video IDs that have been marked as 'Seen' by a user,
    so they are permanently hidden from the AI Radar feed for that user.
    """
    from django.conf import settings as _settings
    user = models.ForeignKey(
        _settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='seen_radar_videos',
    )
    video_id = models.CharField(max_length=20, db_index=True)
    video_title = models.CharField(max_length=500, blank=True, default='')
    marked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'video_id')
        ordering = ['-marked_at']
        verbose_name = 'Seen Radar Video'
        verbose_name_plural = 'Seen Radar Videos'

    def __str__(self):
        return f"{self.user} - {self.video_id}"
