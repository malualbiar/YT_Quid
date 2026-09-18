import os
import re
import logging
from datetime import datetime, timezone, timedelta
from django.conf import settings
from django.core.cache import cache
from apps.radar.models import RadarChannel
from apps.youtube.services.youtube_client import YouTubeClient, YouTubeAPIError

logger = logging.getLogger(__name__)


class AIRadarService:
    """
    Dedicated AI Radar Service.
    Monitors manually tracked YouTube channels for recent uploads,
    identifies songs with high future like potential, saves API quota through
    cached feeds, and powers on-page media playback.
    """

    CACHE_KEY = 'radar_monitored_feed_data'
    CACHE_TIMEOUT = 14400  # 4 hours cache TTL
    MAX_RECENCY_DAYS = 21  # Strict cutoff: ignore any video older than 21 days

    TIME_WINDOWS = {
        '6h':  timedelta(hours=6),
        '12h': timedelta(hours=12),
        '24h': timedelta(hours=24),
        '48h': timedelta(hours=48),
        '72h': timedelta(hours=72),
        '7d':  timedelta(days=7),
        '14d': timedelta(days=14),
        '21d': timedelta(days=21),
    }

    # ─────────────────────────────────────────────────────────────────────────
    # 1. CHANNEL MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def resolve_channel_details(cls, identifier):
        """
        Resolves a user-provided input (channel URL, handle @name, or Channel ID UC...)
        into complete channel metadata and uploads playlist ID.
        """
        raw = identifier.strip()
        if not raw:
            raise ValueError("Channel URL, handle, or ID is required.")

        # Extract handle or channel ID from various URL formats
        channel_id = None
        handle = None

        if 'youtube.com' in raw or 'youtu.be' in raw:
            # Handle URLs: https://www.youtube.com/@handle
            handle_match = re.search(r'youtube\.com/@([\w\.-]+)', raw)
            if handle_match:
                handle = f"@{handle_match.group(1)}"

            # Channel ID URLs: https://www.youtube.com/channel/(UC[\w-]+)
            ch_match = re.search(r'youtube\.com/channel/(UC[\w-]+)', raw)
            if ch_match:
                channel_id = ch_match.group(1)
        elif raw.startswith('@'):
            handle = raw
        elif raw.startswith('UC') and len(raw) >= 20:
            channel_id = raw
        else:
            # Assume handle if no @ provided
            handle = f"@{raw}" if not raw.startswith('UC') else raw

        api_key = getattr(settings, 'YOUTUBE_API_KEY', '') or os.getenv('YOUTUBE_API_KEY', '')
        client = YouTubeClient(api_key=api_key) if api_key else None

        channel_info = None

        # 1. Attempt official YouTube Data API lookup
        if client:
            try:
                if channel_id:
                    channel_info = client.get_channel_by_id(channel_id)
                elif handle:
                    channel_info = client.get_channel_by_handle_or_for_username(handle)
            except Exception as e:
                logger.warning(f"YouTube Data API channel lookup warning: {e}")

        # 2. Extract details from API response
        if channel_info:
            cid = channel_info.get('id', '')
            snippet = channel_info.get('snippet', {})
            stats = channel_info.get('statistics', {})
            content_details = channel_info.get('contentDetails', {})

            title = snippet.get('title', handle or cid)
            custom_url = snippet.get('customUrl', '')
            if custom_url and not custom_url.startswith('@'):
                custom_url = f"@{custom_url}"

            thumbs = snippet.get('thumbnails', {})
            thumb_url = (
                thumbs.get('high', {}).get('url') or
                thumbs.get('medium', {}).get('url') or
                thumbs.get('default', {}).get('url') or ''
            )

            related_playlists = content_details.get('relatedPlaylists', {})
            uploads_pl = related_playlists.get('uploads') or (f"UU{cid[2:]}" if cid.startswith('UC') else '')

            subs = int(stats.get('subscriberCount', 0))
            vids = int(stats.get('videoCount', 0))

            return {
                'channel_id': cid,
                'channel_name': title,
                'handle': custom_url or handle or '',
                'channel_url': f"https://www.youtube.com/{custom_url or 'channel/' + cid}",
                'thumbnail_url': thumb_url,
                'uploads_playlist_id': uploads_pl,
                'subscriber_count': subs,
                'video_count': vids,
            }

        # 3. yt-dlp fallback if API key is not configured or lookup missed
        try:
            import yt_dlp
            target_url = raw if raw.startswith('http') else (f"https://www.youtube.com/{handle}" if handle else f"https://www.youtube.com/channel/{channel_id}")
            ydl_opts = {
                'extract_flat': True,
                'skip_download': True,
                'quiet': True,
                'no_warnings': True,
                'playlistend': 1,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target_url, download=False)
                if info:
                    cid = info.get('channel_id') or info.get('id') or channel_id or ''
                    cname = info.get('channel') or info.get('uploader') or info.get('title') or 'YouTube Channel'
                    uploads_pl = f"UU{cid[2:]}" if cid.startswith('UC') else ''
                    thumbs = info.get('thumbnails', [])
                    t_url = thumbs[-1].get('url', '') if thumbs else ''

                    return {
                        'channel_id': cid,
                        'channel_name': cname,
                        'handle': handle or (f"@{info.get('uploader_id')}" if info.get('uploader_id') else ''),
                        'channel_url': info.get('channel_url') or target_url,
                        'thumbnail_url': t_url,
                        'uploads_playlist_id': uploads_pl,
                        'subscriber_count': int(info.get('channel_follower_count') or 0),
                        'video_count': 0,
                    }
        except Exception as e:
            logger.warning(f"yt-dlp channel lookup warning: {e}")

        # 4. If all else fails but we have a valid channel ID
        if channel_id and channel_id.startswith('UC'):
            return {
                'channel_id': channel_id,
                'channel_name': handle or channel_id,
                'handle': handle or '',
                'channel_url': f"https://www.youtube.com/channel/{channel_id}",
                'thumbnail_url': '',
                'uploads_playlist_id': f"UU{channel_id[2:]}",
                'subscriber_count': 0,
                'video_count': 0,
            }

        raise ValueError(f"Could not find YouTube channel for '{identifier}'. Please check the URL or handle.")

    @classmethod
    def add_tracked_channel(cls, identifier):
        """Adds or updates a monitored YouTube channel in the database."""
        details = cls.resolve_channel_details(identifier)
        channel, created = RadarChannel.objects.update_or_create(
            channel_id=details['channel_id'],
            defaults={
                'channel_name': details['channel_name'],
                'handle': details['handle'],
                'channel_url': details['channel_url'],
                'thumbnail_url': details['thumbnail_url'],
                'uploads_playlist_id': details['uploads_playlist_id'],
                'subscriber_count': details['subscriber_count'],
                'video_count': details['video_count'],
                'is_active': True,
            }
        )
        # Invalidate cache so new channel's uploads appear on next fetch
        cls.clear_cache()
        return channel, created

    @classmethod
    def toggle_channel(cls, channel_id):
        """Toggles active state of a monitored channel."""
        try:
            ch = RadarChannel.objects.get(channel_id=channel_id)
            ch.is_active = not ch.is_active
            ch.save(update_fields=['is_active'])
            cls.clear_cache()
            return ch
        except RadarChannel.DoesNotExist:
            return None

    @classmethod
    def delete_channel(cls, channel_id):
        """Removes a channel from the monitored list."""
        deleted_count, _ = RadarChannel.objects.filter(channel_id=channel_id).delete()
        if deleted_count > 0:
            cls.clear_cache()
        return deleted_count > 0

    @classmethod
    def clear_cache(cls):
        """Clears the cached monitored video feed."""
        try:
            cache.delete(cls.CACHE_KEY)
            cache.delete('radar_last_synced_timestamp')
        except Exception as e:
            logger.warning(f"Error clearing radar cache: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # 2. INGESTION & CACHED FEED SYNC (QUOTA OPTIMIZED)
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def sync_channel_feed(cls, force_refresh=False):
        """
        Ingests recent uploads from all active RadarChannels.
        Uses uploads playlist (1 quota unit) + batch video hydration (1 unit per 50 vids).
        Strictly excludes older videos (>14 days).
        Computes Like Velocity, Engagement Ratio, and Future Potential Score.
        Caches aggregated results.
        """
        if not force_refresh:
            cached_data = cache.get(cls.CACHE_KEY)
            if cached_data is not None:
                return cached_data

        active_channels = list(RadarChannel.objects.filter(is_active=True))
        if not active_channels:
            # Return empty list if no channels are currently monitored
            cache.set(cls.CACHE_KEY, [], cls.CACHE_TIMEOUT)
            cache.set('radar_last_synced_timestamp', datetime.now(timezone.utc).isoformat(), cls.CACHE_TIMEOUT)
            return []

        api_key = getattr(settings, 'YOUTUBE_API_KEY', '') or os.getenv('YOUTUBE_API_KEY', '')
        client = YouTubeClient(api_key=api_key) if api_key else None

        now = datetime.now(timezone.utc)
        cutoff_date = now - timedelta(days=cls.MAX_RECENCY_DAYS)

        aggregated_videos = []
        seen_video_ids = set()

        for ch in active_channels:
            uploads_pl_id = ch.get_uploads_playlist_id()
            channel_vids = []

            # 1. Fetch recent uploads from playlist (1 quota point)
            if client and uploads_pl_id:
                try:
                    pl_data = client.get_playlist_items(uploads_pl_id, max_results=30)
                    items = pl_data.get('items', [])
                    for it in items:
                        snip = it.get('snippet', {})
                        vid_id = it.get('contentDetails', {}).get('videoId') or snip.get('resourceId', {}).get('videoId')
                        pub_str = snip.get('publishedAt', '')

                        if not vid_id or vid_id in seen_video_ids:
                            continue

                        # Check recency before adding
                        try:
                            pub_dt = datetime.fromisoformat(pub_str.replace('Z', '+00:00'))
                            if pub_dt < cutoff_date:
                                # Older video: exclude
                                continue
                        except Exception:
                            pub_dt = now - timedelta(hours=12)

                        seen_video_ids.add(vid_id)
                        channel_vids.append({
                            'video_id': vid_id,
                            'title': snip.get('title', 'Untitled'),
                            'channel_title': ch.channel_name,
                            'channel_id': ch.channel_id,
                            'channel_handle': ch.handle,
                            'channel_avatar': ch.thumbnail_url,
                            'published_at': pub_dt,
                            'thumbnail_url': (
                                snip.get('thumbnails', {}).get('maxres', {}).get('url') or
                                snip.get('thumbnails', {}).get('high', {}).get('url') or
                                snip.get('thumbnails', {}).get('medium', {}).get('url') or
                                f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
                            ),
                            'description': snip.get('description', ''),
                        })
                except Exception as e:
                    logger.warning(f"Error fetching playlist items for {ch.channel_name}: {e}")

            # 2. yt-dlp fallback if API failed or no API key
            if not channel_vids:
                try:
                    import yt_dlp
                    ydl_opts = {
                        'extract_flat': True,
                        'skip_download': True,
                        'quiet': True,
                        'no_warnings': True,
                        'playlistend': 25,
                    }
                    target_url = f"{ch.channel_url}/videos" if ch.channel_url else f"https://www.youtube.com/channel/{ch.channel_id}/videos"
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        data = ydl.extract_info(target_url, download=False)
                        entries = data.get('entries', []) if data else []
                        for entry in entries:
                            if not entry:
                                continue
                            vid_id = entry.get('id')
                            if not vid_id or vid_id in seen_video_ids or len(vid_id) != 11:
                                continue

                            # Parse approx upload date
                            raw_date = entry.get('upload_date')
                            if raw_date and len(raw_date) == 8:
                                try:
                                    pub_dt = datetime.strptime(raw_date, '%Y%m%d').replace(tzinfo=timezone.utc)
                                    if pub_dt < cutoff_date:
                                        continue
                                except Exception:
                                    pub_dt = now - timedelta(hours=24)
                            else:
                                pub_dt = now - timedelta(hours=24)

                            seen_video_ids.add(vid_id)
                            thumbs = entry.get('thumbnails', [])
                            t_url = thumbs[-1].get('url') if thumbs else f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"

                            channel_vids.append({
                                'video_id': vid_id,
                                'title': entry.get('title', 'Untitled'),
                                'channel_title': ch.channel_name,
                                'channel_id': ch.channel_id,
                                'channel_handle': ch.handle,
                                'channel_avatar': ch.thumbnail_url,
                                'published_at': pub_dt,
                                'thumbnail_url': t_url,
                                'description': entry.get('description', ''),
                                'views': int(entry.get('view_count') or 0),
                                'duration_seconds': int(entry.get('duration') or 180),
                            })
                except Exception as e:
                    logger.warning(f"yt-dlp fallback error for {ch.channel_name}: {e}")

            aggregated_videos.extend(channel_vids)

        if not aggregated_videos:
            cache.set(cls.CACHE_KEY, [], cls.CACHE_TIMEOUT)
            cache.set('radar_last_synced_timestamp', now.isoformat(), cls.CACHE_TIMEOUT)
            return []

        # 3. Batch hydrate statistics & durations via YouTube API (1 unit per 50 vids)
        if client:
            all_vids = [v['video_id'] for v in aggregated_videos]
            try:
                video_items = client.get_videos_batch(all_vids)
                video_lookup = {v['id']: v for v in video_items}

                for item in aggregated_videos:
                    v_detail = video_lookup.get(item['video_id'])
                    if v_detail:
                        snip = v_detail.get('snippet', {})
                        stats = v_detail.get('statistics', {})
                        content_details = v_detail.get('contentDetails', {})

                        # Exact publishedAt
                        pub_str = snip.get('publishedAt', '')
                        if pub_str:
                            try:
                                item['published_at'] = datetime.fromisoformat(pub_str.replace('Z', '+00:00'))
                            except Exception:
                                pass

                        item['views'] = int(stats.get('viewCount', 0))
                        item['likes'] = int(stats.get('likeCount', 0))
                        item['comments'] = int(stats.get('commentCount', 0))

                        # Duration parsing
                        dur_str, dur_secs = cls._parse_iso_duration(content_details.get('duration', 'PT3M30S'))
                        item['duration'] = dur_str
                        item['duration_seconds'] = dur_secs
            except Exception as e:
                logger.warning(f"Batch hydration error: {e}")

        # 4. Calculate Like Velocity, Engagement Ratio & Future Potential Score
        final_tracks = []
        for v in aggregated_videos:
            pub_date = v.get('published_at', now - timedelta(hours=12))

            # Exclude older than cutoff
            if pub_date < cutoff_date:
                continue

            hours_ago = max(0.1, (now - pub_date).total_seconds() / 3600.0)
            views = int(v.get('views', 0))
            likes = int(v.get('likes', 0))
            comments = int(v.get('comments', 0))

            # Duration fallback
            dur_secs = v.get('duration_seconds', 200)
            dur_str = v.get('duration') or f"{dur_secs // 60}:{dur_secs % 60:02d}"

            # Like velocity (likes per hour)
            like_velocity = round(likes / hours_ago, 2)
            # View velocity (views per hour)
            view_velocity = round(views / hours_ago, 1)
            # Engagement ratio (like % of views)
            engagement_ratio = round((likes / max(views, 50)) * 100.0, 2)

            # High Potential Score Calculation (0 - 100)
            potential_score, potential_tier, potential_label = cls._compute_potential_score(
                likes=likes,
                views=views,
                hours_ago=hours_ago,
                like_velocity=like_velocity,
                engagement_ratio=engagement_ratio
            )

            final_tracks.append({
                'video_id': v['video_id'],
                'title': v['title'],
                'channel_title': v['channel_title'],
                'channel_id': v['channel_id'],
                'channel_handle': v['channel_handle'],
                'channel_avatar': v['channel_avatar'],
                'thumbnail_url': v['thumbnail_url'],
                'youtube_url': f"https://www.youtube.com/watch?v={v['video_id']}",
                'published_at': pub_date,
                'hours_ago': round(hours_ago, 1),
                'relative_time': cls._format_relative_time(hours_ago),
                'views': views,
                'likes': likes,
                'comments': comments,
                'duration': dur_str,
                'duration_seconds': dur_secs,
                'like_velocity': like_velocity,
                'view_velocity': view_velocity,
                'engagement_ratio': engagement_ratio,
                'potential_score': potential_score,
                'potential_tier': potential_tier,
                'potential_label': potential_label,
            })

        # Save to cache
        cache.set(cls.CACHE_KEY, final_tracks, cls.CACHE_TIMEOUT)
        cache.set('radar_last_synced_timestamp', now.isoformat(), cls.CACHE_TIMEOUT)

        return final_tracks

    # ─────────────────────────────────────────────────────────────────────────
    # 3. HIGH FUTURE LIKE POTENTIAL SCORING ENGINE
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _compute_potential_score(cls, likes, views, hours_ago, like_velocity, engagement_ratio):
        """
        Computes a 0 - 100 Future Like Potential Score based on:
        - Like Velocity (speed of like accumulation)
        - Engagement Ratio (like-to-view percentage; normal is 2-4%, >8% is high potential)
        - Recency Freshness Bonus (early momentum yields higher future trajectory)
        - Absolute momentum
        """
        # 1. Like velocity component (max 40 pts)
        # 10+ likes/hr is significant for emerging music releases
        velocity_comp = min(40.0, (like_velocity / 15.0) * 40.0)

        # 2. Engagement ratio component (max 35 pts)
        # 10% engagement ratio reaches maximum weight
        engagement_comp = min(35.0, (engagement_ratio / 10.0) * 35.0)

        # 3. Freshness & early acceleration bonus (max 15 pts)
        if hours_ago <= 12:
            freshness_comp = 15.0
        elif hours_ago <= 24:
            freshness_comp = 10.0
        elif hours_ago <= 48:
            freshness_comp = 6.0
        else:
            freshness_comp = 2.0

        # 4. Absolute volume bonus (max 10 pts)
        volume_comp = min(10.0, (likes / 200.0) * 10.0)

        raw_score = velocity_comp + engagement_comp + freshness_comp + volume_comp
        score = max(5, min(99, round(raw_score)))

        if score >= 80:
            tier = 'breakout'
            label = '🚀 Viral Breakout'
        elif score >= 60:
            tier = 'high'
            label = '🔥 High Potential'
        elif score >= 40:
            tier = 'promising'
            label = '📈 Promising'
        else:
            tier = 'standard'
            label = '⚡ Standard'

        return score, tier, label

    # ─────────────────────────────────────────────────────────────────────────
    # 4. IN-MEMORY FILTERING & SORTING (0 API QUOTA USED)
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def get_filtered_feed(
        cls,
        time_window='7d',
        min_likes=0,
        channel_id='all',
        min_potential=0,
        sort_by='potential',
        query='',
        force_refresh=False
    ):
        """
        Retrieves cached feed and applies in-memory filters and sorting.
        Consumes 0 API quota!
        """
        raw_tracks = cls.sync_channel_feed(force_refresh=force_refresh)
        if not raw_tracks:
            return []

        filtered = []
        now = datetime.now(timezone.utc)
        max_delta = cls.TIME_WINDOWS.get(time_window, timedelta(days=cls.MAX_RECENCY_DAYS))
        search_query = query.strip().lower()

        for t in raw_tracks:
            # 1. Time / Hours ago filter
            pub_date = t.get('published_at')
            if pub_date:
                age = now - pub_date
                if age > max_delta or age < timedelta(seconds=0):
                    continue

            # 2. Min Likes filter
            if min_likes > 0 and t.get('likes', 0) < min_likes:
                continue

            # 3. Channel filter
            if channel_id and channel_id != 'all' and t.get('channel_id') != channel_id:
                continue

            # 4. Potential Score filter
            if min_potential > 0 and t.get('potential_score', 0) < min_potential:
                continue

            # 5. Search query
            if search_query:
                title = t.get('title', '').lower()
                ch_title = t.get('channel_title', '').lower()
                if search_query not in title and search_query not in ch_title:
                    continue

            filtered.append(t)

        # Sorting
        if sort_by == 'potential':
            filtered.sort(key=lambda x: (x.get('potential_score', 0), x.get('like_velocity', 0)), reverse=True)
        elif sort_by == 'velocity':
            filtered.sort(key=lambda x: x.get('like_velocity', 0), reverse=True)
        elif sort_by == 'likes':
            filtered.sort(key=lambda x: x.get('likes', 0), reverse=True)
        elif sort_by == 'views':
            filtered.sort(key=lambda x: x.get('views', 0), reverse=True)
        elif sort_by == 'date':
            filtered.sort(key=lambda x: x.get('published_at', datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

        return filtered

    # ─────────────────────────────────────────────────────────────────────────
    # 5. UTILITY HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _parse_iso_duration(cls, iso_str):
        """Parses ISO 8601 duration e.g. PT3M45S into '3:45' and total seconds"""
        if not iso_str:
            return '3:30', 210
        pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
        match = re.match(pattern, iso_str)
        if not match:
            return '3:30', 210
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        total_seconds = (hours * 3600) + (minutes * 60) + seconds
        if hours > 0:
            formatted = f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            formatted = f"{minutes}:{seconds:02d}"
        return formatted, total_seconds

    @classmethod
    def _format_relative_time(cls, hours_ago):
        """Formats hours_ago float into human readable time badge"""
        if hours_ago < 1:
            mins = max(1, int(hours_ago * 60))
            return f"{mins}m ago"
        elif hours_ago < 24:
            hrs = int(hours_ago)
            return f"{hrs}h ago"
        else:
            days = int(hours_ago / 24)
            return f"{days}d ago"
