import time
import logging
from django.utils import timezone
from django.db import transaction
from apps.artists.models import YouTubeChannel
from apps.videos.models import Video, VideoStatisticSnapshot, ChannelStatisticSnapshot
from apps.milestones.models import VideoMilestone, NotificationLog
from apps.youtube.models import SyncLog, SystemSettings
from .channel_service import ChannelService
from .video_service import VideoService
from .youtube_client import YouTubeAPIError

logger = logging.getLogger(__name__)

# Milestone view thresholds
MILESTONE_THRESHOLDS = [
    (10_000, VideoMilestone.MilestoneType.VIEWS_10K),
    (50_000, VideoMilestone.MilestoneType.VIEWS_50K),
    (100_000, VideoMilestone.MilestoneType.VIEWS_100K),
    (250_000, VideoMilestone.MilestoneType.VIEWS_250K),
    (500_000, VideoMilestone.MilestoneType.VIEWS_500K),
    (1_000_000, VideoMilestone.MilestoneType.VIEWS_1M),
    (5_000_000, VideoMilestone.MilestoneType.VIEWS_5M),
    (10_000_000, VideoMilestone.MilestoneType.VIEWS_10M),
]

class SyncService:
    def __init__(self, channel_service=None, video_service=None):
        self.channel_service = channel_service or ChannelService()
        self.video_service = video_service or VideoService()

    def sync_channel(self, channel: YouTubeChannel, max_videos=100) -> SyncLog:
        """
        Idempotent synchronization for a single channel:
        1. Fetch updated channel info & save ChannelStatisticSnapshot.
        2. Discover uploads playlist and fetch all video data in batches.
        3. Create/update Video records.
        4. Calculate delta views/likes/comments and store VideoStatisticSnapshot.
        5. Check and trigger milestones & spike notifications.
        6. Record SyncLog.
        """
        start_time = time.time()
        sync_log = SyncLog.objects.create(
            channel=channel,
            channel_name_cached=channel.channel_name,
            started_at=timezone.now(),
            status=SyncLog.Status.RUNNING
        )

        try:
            channel.sync_status = YouTubeChannel.SyncStatus.SYNCING
            channel.save(update_fields=['sync_status'])

            # 1. Update Channel Info
            ch_data = self.channel_service.resolve_channel(channel.channel_id)
            
            # Channel Deltas
            prev_views = channel.total_views
            prev_subs = channel.subscriber_count
            prev_vids = channel.video_count

            channel.channel_name = ch_data['channel_name']
            channel.thumbnail_url = ch_data['thumbnail_url'] or channel.thumbnail_url
            channel.description = ch_data['description']
            channel.subscriber_count = ch_data['subscriber_count']
            channel.total_views = ch_data['total_views']
            channel.video_count = ch_data['video_count']
            if ch_data['uploads_playlist_id']:
                channel.uploads_playlist_id = ch_data['uploads_playlist_id']
            channel.last_synced_at = timezone.now()
            channel.sync_status = YouTubeChannel.SyncStatus.SUCCESS
            channel.last_sync_error = ''
            channel.save()

            # Record Channel Snapshot
            ChannelStatisticSnapshot.objects.create(
                channel=channel,
                recorded_at=timezone.now(),
                subscriber_count=channel.subscriber_count,
                total_views=channel.total_views,
                video_count=channel.video_count,
                subscriber_change=max(0, channel.subscriber_count - prev_subs) if prev_subs else 0,
                view_change=max(0, channel.total_views - prev_views) if prev_views else 0,
                video_change=max(0, channel.video_count - prev_vids) if prev_vids else 0,
            )

            # 2. Sync Videos from uploads playlist (with fallback to channel search)
            videos_data = self.video_service.fetch_channel_videos(
                uploads_playlist_id=channel.uploads_playlist_id,
                channel_id=channel.channel_id,
                max_videos=max_videos
            )

            new_count = 0
            updated_count = 0
            snapshots_count = 0

            with transaction.atomic():
                for v_item in videos_data:
                    v_id = v_item['youtube_video_id']
                    video = Video.objects.filter(youtube_video_id=v_id).first()

                    if not video:
                        # New video discovered
                        video = Video.objects.create(
                            youtube_video_id=v_id,
                            channel=channel,
                            artist=channel.artist,
                            title=v_item['title'],
                            description=v_item['description'],
                            thumbnail_url=v_item['thumbnail_url'],
                            published_at=v_item['published_at'],
                            duration=v_item['duration'],
                            duration_seconds=v_item['duration_seconds'],
                            video_url=v_item['video_url'],
                            current_views=v_item['current_views'],
                            current_likes=v_item['current_likes'],
                            current_comments=v_item['current_comments'],
                            last_synced_at=timezone.now()
                        )
                        new_count += 1
                        views_change = 0
                        likes_change = 0
                        comments_change = 0
                    else:
                        # If video was deleted or set inactive, skip processing to avoid resurrecting it with snapshots/alerts
                        if not video.is_active:
                            continue

                        # Existing video update
                        prev_v_views = video.current_views
                        prev_v_likes = video.current_likes
                        prev_v_comments = video.current_comments

                        views_change = max(0, v_item['current_views'] - prev_v_views)
                        likes_change = max(0, v_item['current_likes'] - prev_v_likes)
                        comments_change = max(0, v_item['current_comments'] - prev_v_comments)

                        video.title = v_item['title']
                        video.thumbnail_url = v_item['thumbnail_url'] or video.thumbnail_url
                        video.duration = v_item['duration'] or video.duration
                        video.duration_seconds = v_item['duration_seconds'] or video.duration_seconds
                        video.current_views = v_item['current_views']
                        video.current_likes = v_item['current_likes']
                        video.current_comments = v_item['current_comments']
                        video.last_synced_at = timezone.now()
                        video.save()
                        updated_count += 1

                    # Create Video Snapshot
                    VideoStatisticSnapshot.objects.create(
                        video=video,
                        recorded_at=timezone.now(),
                        views=video.current_views,
                        likes=video.current_likes,
                        comments=video.current_comments,
                        views_change=views_change,
                        likes_change=likes_change,
                        comments_change=comments_change
                    )
                    snapshots_count += 1

                    # Check Milestones
                    self._check_video_milestones(video)

                    # Check Spike Alert (> threshold in 24h)
                    settings_obj = SystemSettings.get_settings()
                    if views_change >= settings_obj.spike_alert_threshold:
                        NotificationLog.objects.create(
                            notification_type=NotificationLog.NotificationType.SPIKE_ALERT,
                            title=f"Spike Alert: {video.title}",
                            message=f"'{video.title}' gained +{views_change:,} views in the latest sync cycle!",
                            link=f"/videos/{video.id}/"
                        )

            # Detect videos deleted on YouTube: if complete playlist was retrieved, any active video not found is deactivated
            if videos_data and len(videos_data) < max_videos:
                fetched_ids = {v['youtube_video_id'] for v in videos_data}
                channel.videos.filter(is_active=True).exclude(youtube_video_id__in=fetched_ids).update(is_active=False)

            # Trigger real-time growth metrics update for this artist/channel
            try:
                from apps.analytics.services import AnalyticsService
                if channel.artist_id:
                    AnalyticsService.update_video_growth_metrics(artist_id=channel.artist_id)
            except Exception:
                pass

            # Finish Log
            duration = round(time.time() - start_time, 2)
            sync_log.completed_at = timezone.now()
            sync_log.status = SyncLog.Status.SUCCESS
            sync_log.videos_found = len(videos_data)
            sync_log.new_videos = new_count
            sync_log.videos_updated = updated_count
            sync_log.snapshots_created = snapshots_count
            sync_log.duration_seconds = duration
            sync_log.save()

            return sync_log

        except Exception as e:
            logger.exception(f"Error syncing channel {channel.channel_id}: {e}")
            duration = round(time.time() - start_time, 2)
            error_msg = str(e)
            
            channel.sync_status = YouTubeChannel.SyncStatus.FAILED
            channel.last_sync_error = error_msg
            channel.save(update_fields=['sync_status', 'last_sync_error'])

            sync_log.completed_at = timezone.now()
            sync_log.status = SyncLog.Status.FAILED
            sync_log.duration_seconds = duration
            sync_log.error_message = error_msg
            sync_log.save()

            NotificationLog.objects.create(
                notification_type=NotificationLog.NotificationType.SYNC_ERROR,
                title=f"Sync Failed: {channel.channel_name}",
                message=f"Failed to sync channel '{channel.channel_name}': {error_msg}",
                link=f"/channels/"
            )
            return sync_log

    def _check_video_milestones(self, video: Video):
        """Check if video reached any new milestones and notify"""
        for threshold, m_type in MILESTONE_THRESHOLDS:
            if video.current_views >= threshold:
                milestone, created = VideoMilestone.objects.get_or_create(
                    video=video,
                    threshold=threshold,
                    defaults={
                        'milestone_type': m_type,
                        'views_at_milestone': video.current_views,
                        'notified': True
                    }
                )
                if created:
                    NotificationLog.objects.create(
                        notification_type=NotificationLog.NotificationType.MILESTONE,
                        title=f"🎉 Milestone: {video.title}",
                        message=f"'{video.title}' by {video.artist.stage_name} just crossed {threshold:,} views!",
                        link=f"/videos/{video.id}/"
                    )

    def sync_all_channels(self) -> list[SyncLog]:
        """Sync all registered channels sequentially"""
        channels = YouTubeChannel.objects.all()
        logs = []
        for channel in channels:
            log = self.sync_channel(channel)
            logs.append(log)
        return logs
