from django.test import TestCase
from django.utils import timezone
from apps.artists.models import Artist, YouTubeChannel
from apps.videos.models import Video, VideoStatisticSnapshot

class VideoModelTests(TestCase):
    def setUp(self):
        self.artist = Artist.objects.create(name='Singer', stage_name='Star')
        self.channel = YouTubeChannel.objects.create(
            artist=self.artist,
            channel_id='UCchannel12345678901234',
            channel_name='Star Official',
            channel_url='https://youtube.com/@star'
        )

    def test_video_creation_and_rates(self):
        video = Video.objects.create(
            youtube_video_id='dQw4w9WgXcQ',
            channel=self.channel,
            artist=self.artist,
            title='Never Gonna Give You Up',
            published_at=timezone.now(),
            duration='3:32',
            duration_seconds=212,
            video_url='https://youtube.com/watch?v=dQw4w9WgXcQ',
            current_views=100000,
            current_likes=5000,
            current_comments=500
        )
        self.assertEqual(video.like_rate, 5.0)  # (5,000 / 100,000) * 100 = 5.0%
        self.assertEqual(video.comment_rate, 0.5)  # (500 / 100,000) * 100 = 0.5%

    def test_snapshot_delta_tracking(self):
        video = Video.objects.create(
            youtube_video_id='test1234567',
            channel=self.channel,
            artist=self.artist,
            title='Test Track',
            published_at=timezone.now(),
            video_url='https://youtube.com/watch?v=test1234567',
            current_views=10000
        )

        s1 = VideoStatisticSnapshot.objects.create(
            video=video,
            recorded_at=timezone.now(),
            views=10000,
            views_change=0
        )

        s2 = VideoStatisticSnapshot.objects.create(
            video=video,
            recorded_at=timezone.now(),
            views=15000,
            views_change=5000
        )

        self.assertEqual(s2.views_change, 5000)
        self.assertEqual(video.snapshots.count(), 2)


class VideoDeletionViewTests(TestCase):
    def setUp(self):
        from apps.authentication.models import User
        self.user = User.objects.create_superuser(
            email='admin@example.com',
            username='admin',
            password='password123',
            role=User.Role.SUPER_ADMIN
        )
        self.client.force_login(self.user)
        self.artist = Artist.objects.create(name='Singer', stage_name='Star')
        self.channel = YouTubeChannel.objects.create(
            artist=self.artist,
            channel_id='UCchannel12345678901234',
            channel_name='Star Official',
            channel_url='https://youtube.com/@star'
        )
        self.video = Video.objects.create(
            youtube_video_id='dQw4w9WgXcQ',
            channel=self.channel,
            artist=self.artist,
            title='Song to be Deleted',
            published_at=timezone.now(),
            duration='3:32',
            duration_seconds=212,
            video_url='https://youtube.com/watch?v=dQw4w9WgXcQ',
            current_views=100000,
            is_active=True
        )

    def test_get_delete_confirm_page(self):
        res = self.client.get(f'/videos/{self.video.id}/delete/')
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, 'videos/delete_confirm.html')
        self.assertContains(res, 'Song to be Deleted')

    def test_post_delete_deactivates_video(self):
        res = self.client.post(f'/videos/{self.video.id}/delete/')
        self.assertEqual(res.status_code, 302)
        self.video.refresh_from_db()
        self.assertFalse(self.video.is_active)

    def test_deleted_song_not_in_catalog(self):
        self.video.is_active = False
        self.video.save()
        res = self.client.get('/videos/')
        self.assertEqual(res.status_code, 200)
        self.assertNotContains(res, 'Song to be Deleted')

    def test_deleted_song_detail_returns_404(self):
        self.video.is_active = False
        self.video.save()
        res = self.client.get(f'/videos/{self.video.id}/')
        self.assertEqual(res.status_code, 404)

    def test_deleted_song_not_in_global_search(self):
        self.video.is_active = False
        self.video.save()
        res = self.client.get('/api/search/?q=Deleted')
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(len(data.get('videos', [])), 0)
