import os
import json
import tempfile
from datetime import timedelta
from unittest.mock import patch, MagicMock
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.publishing.models import YouTubeOAuthAccount, PublishingJob
from apps.studio.models import LongMixProject, ShortVideoProject, LyricVideoProject, VideoProject
from apps.artists.models import Artist, YouTubeChannel

User = get_user_model()

class PublishingAppTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='testcreator',
            email='creator@test.com',
            password='testpassword123',
            role=User.Role.SUPER_ADMIN
        )
        self.client = Client()
        self.client.force_login(self.user)

        # Create artist and channel
        self.artist = Artist.objects.create(name='Test Artist', stage_name='DJ Test')
        self.channel = YouTubeChannel.objects.create(
            artist=self.artist,
            channel_id='UC1234567890TestChannel',
            channel_name='DJ Test Official',
            channel_url='https://youtube.com/@djtest',
            subscriber_count=50000,
            video_count=120
        )

        # Create OAuth Account
        self.oauth_account = YouTubeOAuthAccount.objects.create(
            channel_id='UC1234567890TestChannel',
            channel_title='DJ Test Official',
            channel_custom_url='@djtest',
            subscriber_count=50000,
            video_count=120,
            is_active=True,
            is_default=True,
            token_json={
                'access_token': 'mock_access_token',
                'refresh_token': 'mock_refresh_token',
                'client_id': 'mock_client_id.apps.googleusercontent.com',
                'client_secret': 'mock_client_secret',
                'token_uri': 'https://oauth2.googleapis.com/token'
            }
        )

    def test_oauth_account_model(self):
        self.assertTrue(self.oauth_account.is_default)
        self.assertIn('DJ Test Official', str(self.oauth_account))

        # Adding a second default should reset the first
        acc2 = YouTubeOAuthAccount.objects.create(
            channel_id='UC9999999999SecondChannel',
            channel_title='Second Channel',
            is_default=True,
            token_json={'access_token': 'abc'}
        )
        self.oauth_account.refresh_from_db()
        self.assertFalse(self.oauth_account.is_default)
        self.assertTrue(acc2.is_default)

    def test_publishing_job_model_and_properties(self):
        job = PublishingJob.objects.create(
            account=self.oauth_account,
            title='1-Hour Chill Beats [Study & Focus]',
            description='Enjoy this non-stop loop.\n00:00 - Start',
            tags=['chill', 'study', 'focus'],
            privacy_status=PublishingJob.PrivacyStatus.PRIVATE_DRAFT,
            video_file_path='C:/fake/path/video.mp4',
            status=PublishingJob.Status.QUEUED
        )
        self.assertTrue(job.is_active)
        self.assertIsNone(job.youtube_studio_url)

        # Set video ID
        job.youtube_video_id = 'dQw4w9WgXcQ'
        job.status = PublishingJob.Status.SUCCESS
        job.save()

        self.assertFalse(job.is_active)
        self.assertEqual(job.youtube_studio_url, 'https://studio.youtube.com/video/dQw4w9WgXcQ/edit')
        self.assertEqual(job.youtube_watch_url, 'https://www.youtube.com/watch?v=dQw4w9WgXcQ')

    def test_publisher_dashboard_view(self):
        response = self.client.get(reverse('publishing_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'publishing/dashboard.html')
        self.assertIn('accounts', response.context)
        self.assertIn('recent_jobs', response.context)

    def test_publisher_queue_view(self):
        response = self.client.get(reverse('publishing_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'publishing/queue.html')

    def test_job_status_api(self):
        job = PublishingJob.objects.create(
            account=self.oauth_account,
            title='Testing Status API',
            status=PublishingJob.Status.UPLOADING,
            progress_percent=45,
            video_file_path='C:/fake/path/video.mp4'
        )
        response = self.client.get(reverse('publishing_job_status_api'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('jobs', data)
        self.assertTrue(any(j['id'] == job.id and j['progress_percent'] == 45 for j in data['jobs']))

    @patch('apps.publishing.services.uploader_service.YouTubeUploaderService.start_upload_async')
    def test_create_job_view_from_form(self, mock_start_upload):
        # Create a temporary dummy video file
        dummy_video = SimpleUploadedFile("test_video.mp4", b"dummy video bytes", content_type="video/mp4")
        
        post_data = {
            'title': 'My Automated Test Draft',
            'description': 'Test description',
            'tags': 'music, test, draft',
            'privacy_status': 'private',
            'video_file': dummy_video,
            'category_id': '10',
            'account_id': self.oauth_account.id
        }

        response = self.client.post(reverse('publishing_create_job'), post_data)
        self.assertEqual(response.status_code, 302) # Redirect to dashboard

        job = PublishingJob.objects.filter(title='My Automated Test Draft').first()
        self.assertIsNotNone(job)
        self.assertEqual(job.privacy_status, 'private')
        self.assertEqual(job.account, self.oauth_account)
        mock_start_upload.assert_called_once_with(job.id)

    @patch('apps.publishing.services.uploader_service.YouTubeUploaderService.start_upload_async')
    def test_create_job_resolves_shorts_clip_path(self, mock_start_upload):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            project = ShortVideoProject.objects.create(
                title='YouTube source shorts',
                chops_data=[
                    {'output_file': 'part1.mp4', 'output_url': '/media/studio/shorts_output/1/part1.mp4'},
                    {'output_file': 'part2.mp4', 'output_url': '/media/studio/shorts_output/1/part2.mp4'},
                ],
            )
            output_dir = os.path.join(media_root, 'studio', 'shorts_output', str(project.pk))
            os.makedirs(output_dir)
            clip_path = os.path.join(output_dir, 'part2.mp4')
            with open(clip_path, 'wb') as clip_file:
                clip_file.write(b'short clip')

            response = self.client.post(
                reverse('publishing_create_job'),
                {
                    'title': 'Part 2',
                    'account_id': self.oauth_account.id,
                    'source_type': PublishingJob.SourceType.SHORT_VIDEO,
                    'source_id': project.id,
                    'source_chop_index': 1,
                    'video_file_path': '/media/studio/shorts_output/1/part2.mp4',
                },
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )

            self.assertEqual(response.status_code, 200)
            job = PublishingJob.objects.get(title='Part 2')
            self.assertEqual(job.video_file_path, clip_path)
            mock_start_upload.assert_called_once_with(job.id)

    @patch('apps.publishing.services.uploader_service.YouTubeUploaderService.start_upload_async')
    def test_batch_queue_shorts_view(self, mock_start_upload):
        short_project = ShortVideoProject.objects.create(
            title='Viral Rap Hook Shorts',
            chop_count=2,
            chops_data=[
                {
                    'id': 1,
                    'title': 'Viral Rap Hook Part 1',
                    'start_seconds': 0.0,
                    'end_seconds': 15.0,
                    'duration': 15.0,
                    'output_url': '/media/studio/shorts_output/fake_chop_1.mp4'
                },
                {
                    'id': 2,
                    'title': 'Viral Rap Hook Part 2',
                    'start_seconds': 15.0,
                    'end_seconds': 30.0,
                    'duration': 15.0,
                    'output_url': '/media/studio/shorts_output/fake_chop_2.mp4'
                }
            ],
            render_status=ShortVideoProject.Status.COMPLETED
        )

        response = self.client.post(
            reverse('publishing_batch_shorts', args=[short_project.id]),
            {'privacy_status': 'private', 'drip_interval_hours': '24'}
        )
        self.assertEqual(response.status_code, 302)

    def test_cancel_and_retry_job_view(self):
        job = PublishingJob.objects.create(
            account=self.oauth_account,
            title='Cancellable Job',
            status=PublishingJob.Status.QUEUED,
            video_file_path='C:/fake/path/video.mp4'
        )

        # Cancel
        self.client.post(reverse('publishing_cancel_job', args=[job.id]))
        job.refresh_from_db()
        self.assertEqual(job.status, PublishingJob.Status.CANCELLED)

        # Retry
        with patch('apps.publishing.services.uploader_service.YouTubeUploaderService.start_upload_async'):
            self.client.post(reverse('publishing_retry_job', args=[job.id]))
            job.refresh_from_db()
            self.assertEqual(job.status, PublishingJob.Status.QUEUED)
            self.assertEqual(job.retry_count, 1)

    @patch('apps.publishing.services.uploader_service.YouTubeUploaderService.start_upload_async')
    def test_upload_engine_switching(self, mock_start_upload):
        dummy_video = SimpleUploadedFile("engine_test.mp4", b"dummy video content", content_type="video/mp4")
        
        # 1. Test Browser Automation selection
        post_data_browser = {
            'title': 'Browser Bot Zero Quota Video',
            'upload_engine': PublishingJob.UploadEngine.BROWSER_AUTOMATION,
            'privacy_status': 'private',
            'video_file': dummy_video
        }
        resp1 = self.client.post(reverse('publishing_create_job'), post_data_browser)
        self.assertEqual(resp1.status_code, 302)

        job1 = PublishingJob.objects.filter(title='Browser Bot Zero Quota Video').first()
        self.assertIsNotNone(job1)
        self.assertEqual(job1.upload_engine, PublishingJob.UploadEngine.BROWSER_AUTOMATION)

        # 2. Test Studio Assistant selection
        dummy_video2 = SimpleUploadedFile("assistant_test.mp4", b"dummy video content 2", content_type="video/mp4")
        post_data_assistant = {
            'title': 'Studio Assistant Manual Video',
            'upload_engine': PublishingJob.UploadEngine.STUDIO_DISPATCHER,
            'privacy_status': 'unlisted',
            'video_file': dummy_video2
        }
        resp2 = self.client.post(reverse('publishing_create_job'), post_data_assistant)
        self.assertEqual(resp2.status_code, 302)

        job2 = PublishingJob.objects.filter(title='Studio Assistant Manual Video').first()
        self.assertIsNotNone(job2)
        self.assertEqual(job2.upload_engine, PublishingJob.UploadEngine.STUDIO_DISPATCHER)

