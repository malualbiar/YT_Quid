import os
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from apps.radar.models import RadarChannel
from apps.radar.services.ai_radar_service import AIRadarService
from apps.radar.services.downloader_service import AIDownloaderService

User = get_user_model()


class AIRadarTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testradaruser',
            email='radar@test.com',
            password='testpassword123',
            role=User.Role.SUPER_ADMIN
        )
        self.client = Client()

        # Create sample tracked channel
        self.channel = RadarChannel.objects.create(
            channel_id='UC1234567890abcdefghij',
            channel_name='AI Music Hits',
            handle='@aimusichits',
            channel_url='https://www.youtube.com/@aimusichits',
            thumbnail_url='https://example.com/avatar.jpg',
            uploads_playlist_id='UU1234567890abcdefghij',
            is_active=True,
            subscriber_count=15000,
            video_count=42,
        )

    def test_radar_channel_model_and_playlist_derivation(self):
        """Test RadarChannel properties and uploads playlist generation"""
        self.assertEqual(str(self.channel), "AI Music Hits (@aimusichits)")
        self.assertEqual(self.channel.get_uploads_playlist_id(), "UU1234567890abcdefghij")

        # Test fallback playlist calculation
        ch2 = RadarChannel(channel_id='UCabcdef1234567890')
        self.assertEqual(ch2.get_uploads_playlist_id(), "UUabcdef1234567890")

    def test_compute_potential_score(self):
        """Test potential score algorithm returns higher scores for high engagement and velocity"""
        # Low engagement, low velocity
        score_low, tier_low, _ = AIRadarService._compute_potential_score(
            likes=5, views=1000, hours_ago=48, like_velocity=0.1, engagement_ratio=0.5
        )
        self.assertEqual(tier_low, 'standard')
        self.assertLess(score_low, 40)

        # High engagement, breakout velocity
        score_high, tier_high, _ = AIRadarService._compute_potential_score(
            likes=450, views=3000, hours_ago=6, like_velocity=75.0, engagement_ratio=15.0
        )
        self.assertIn(tier_high, ['high', 'breakout'])
        self.assertGreaterEqual(score_high, 70)

    def test_get_filtered_feed_in_memory(self):
        """Test in-memory filtering by hours_ago, min_likes, channel, potential score and sorting"""
        now = datetime.now(timezone.utc)
        mock_raw_tracks = [
            {
                'video_id': 'vid1',
                'title': 'Viral Suno Hit',
                'channel_title': 'AI Music Hits',
                'channel_id': 'UC1234567890abcdefghij',
                'published_at': now - timedelta(hours=4),
                'hours_ago': 4.0,
                'views': 5000,
                'likes': 500,
                'like_velocity': 125.0,
                'engagement_ratio': 10.0,
                'potential_score': 90,
                'potential_tier': 'breakout',
            },
            {
                'video_id': 'vid2',
                'title': 'Chill Lofi Beat',
                'channel_title': 'Other Channel',
                'channel_id': 'UCOtherChannelId123',
                'published_at': now - timedelta(hours=36),
                'hours_ago': 36.0,
                'views': 800,
                'likes': 30,
                'like_velocity': 0.83,
                'engagement_ratio': 3.75,
                'potential_score': 35,
                'potential_tier': 'standard',
            },
        ]

        with patch.object(AIRadarService, 'sync_channel_feed', return_value=mock_raw_tracks):
            # Test filter min_likes = 100
            filtered = AIRadarService.get_filtered_feed(min_likes=100)
            self.assertEqual(len(filtered), 1)
            self.assertEqual(filtered[0]['video_id'], 'vid1')

            # Test filter time_window = 24h
            filtered_time = AIRadarService.get_filtered_feed(time_window='24h')
            self.assertEqual(len(filtered_time), 1)
            self.assertEqual(filtered_time[0]['video_id'], 'vid1')

            # Test filter channel_id
            filtered_channel = AIRadarService.get_filtered_feed(channel_id='UC1234567890abcdefghij')
            self.assertEqual(len(filtered_channel), 1)
            self.assertEqual(filtered_channel[0]['video_id'], 'vid1')

            # Test filter min_potential = 60
            filtered_pot = AIRadarService.get_filtered_feed(min_potential=60)
            self.assertEqual(len(filtered_pot), 1)
            self.assertEqual(filtered_pot[0]['video_id'], 'vid1')

    def test_sanitize_filename(self):
        """Test filename sanitization removes illegal characters"""
        dirty = 'My/Awesome: Track? *Special* <AI> "Hit" | 2024'
        clean = AIDownloaderService.sanitize_filename(dirty)
        for char in ['/', ':', '?', '*', '<', '>', '"', '|']:
            self.assertNotIn(char, clean)

    def test_radar_feed_view_authenticated(self):
        """Test radar view loads successfully with filter query params"""
        self.client.force_login(self.user)
        response = self.client.get(reverse('radar_feed'), {
            'time': '24h',
            'min_likes': '25',
            'sort': 'potential'
        })
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'radar/index.html')
        self.assertIn('tracks', response.context)
        self.assertIn('channels', response.context)
        self.assertIn('active_channels_count', response.context)

    def test_add_channel_view(self):
        """Test adding a channel via POST endpoint"""
        self.client.force_login(self.user)
        with patch.object(AIRadarService, 'resolve_channel_details', return_value={
            'channel_id': 'UC9876543210zyxwvutsrq',
            'channel_name': 'Future Beats',
            'handle': '@futurebeats',
            'channel_url': 'https://youtube.com/@futurebeats',
            'thumbnail_url': '',
            'uploads_playlist_id': 'UU9876543210zyxwvutsrq',
            'subscriber_count': 5000,
            'video_count': 10,
        }):
            response = self.client.post(reverse('radar_add_channel'), {
                'channel_input': '@futurebeats'
            })
            self.assertEqual(response.status_code, 302)
            self.assertTrue(RadarChannel.objects.filter(channel_id='UC9876543210zyxwvutsrq').exists())

    def test_toggle_and_delete_channel_views(self):
        """Test toggling active state and deleting channel"""
        self.client.force_login(self.user)

        # Toggle to paused
        self.client.post(reverse('radar_toggle_channel', kwargs={'channel_id': self.channel.channel_id}))
        self.channel.refresh_from_db()
        self.assertFalse(self.channel.is_active)

        # Delete channel
        self.client.post(reverse('radar_delete_channel', kwargs={'channel_id': self.channel.channel_id}))
        self.assertFalse(RadarChannel.objects.filter(channel_id=self.channel.channel_id).exists())

    def test_refresh_radar_view(self):
        """Test refresh view forces sync and redirects"""
        self.client.force_login(self.user)
        with patch.object(AIRadarService, 'sync_channel_feed') as mock_sync:
            mock_sync.return_value = []
            response = self.client.get(reverse('radar_refresh'))
            self.assertEqual(response.status_code, 302)
            self.assertRedirects(response, reverse('radar_feed'))
            # Verify force_refresh=True was passed at least once
            force_refresh_calls = [
                c for c in mock_sync.call_args_list
                if c.kwargs.get('force_refresh') is True or (c.args and c.args[0] is True)
            ]
            self.assertTrue(len(force_refresh_calls) >= 1, "sync_channel_feed was never called with force_refresh=True")

    @patch('apps.radar.services.downloader_service.AIDownloaderService.download_audio_file')
    def test_download_audio_view_wav(self, mock_download):
        """Test WAV audio download view returns FileResponse with correct headers"""
        dummy_wav = os.path.join(os.path.dirname(__file__), 'dummy_test.wav')
        with open(dummy_wav, 'wb') as f:
            f.write(b'RIFF....WAVEfmt ')

        response = None
        try:
            mock_download.return_value = (dummy_wav, "Test Track")
            self.client.force_login(self.user)

            response = self.client.get(reverse('radar_download_audio'), {
                'video_id': 'kJQP7kiw5Fk',
                'format': 'wav',
                'title': 'Test Track'
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'audio/wav')
            self.assertIn('attachment;', response['Content-Disposition'])
            self.assertIn('Test Track.wav', response['Content-Disposition'])
        finally:
            if response is not None:
                response.close()
            if os.path.exists(dummy_wav):
                try:
                    os.remove(dummy_wav)
                except Exception:
                    pass

    def test_radar_access_denied_for_non_superadmin(self):
        """Test non-superadmin user gets redirected to dashboard"""
        viewer = User.objects.create_user(
            username='viewerradar',
            email='viewerradar@test.com',
            password='password123',
            role=User.Role.VIEWER
        )
        self.client.force_login(viewer)

        response = self.client.get(reverse('radar_feed'))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('dashboard'))
