import io
import json
import wave
import struct
from unittest.mock import patch
from PIL import Image
from django.test import TestCase, Client
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.authentication.models import User
from apps.studio.models import ShortVideoProject
from apps.studio.services.shorts_engine import ShortsEngineService

class ShortVideoProjectTestCase(TestCase):

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            email='admin@shorts.com',
            username='shortsadmin',
            password='adminpassword123',
            role=User.Role.SUPER_ADMIN
        )
        self.client.login(email='admin@shorts.com', password='adminpassword123')

        self.viewer = User.objects.create_user(
            email='viewer@shorts.com',
            username='shortsviewer',
            password='viewerpassword123',
            role=User.Role.VIEWER
        )

    def test_model_properties_and_seo(self):
        project = ShortVideoProject.objects.create(
            title="Aesthetic Vibes",
            duration_seconds=95.0,
            chop_count=3,
            chops_data=[
                {"id": 1, "title": "Part 1", "hook_text": "Wait for 0:15... 🎧", "start_seconds": 0.0, "end_seconds": 30.0, "duration": 30.0, "status": "COMPLETED"},
                {"id": 2, "title": "Part 2", "hook_text": "Insane drop! 🔥", "start_seconds": 30.0, "end_seconds": 60.0, "duration": 30.0, "status": "COMPLETED"},
                {"id": 3, "title": "Part 3", "hook_text": "Outro vibe", "start_seconds": 60.0, "end_seconds": 95.0, "duration": 35.0, "status": "COMPLETED"},
            ]
        )

        self.assertEqual(project.duration_formatted, "01m 35s")
        self.assertEqual(project.completed_chops_count, 3)
        self.assertIn("Part 1", project.youtube_title_for_chop(0))
        self.assertIn("Wait for 0:15", project.youtube_title_for_chop(0))
        self.assertIn("#shorts", project.youtube_description_for_chop(0))

    def test_generate_chop_splits(self):
        chops = ShortsEngineService.generate_chop_splits(total_duration=45.0, interval_seconds=15.0)
        self.assertEqual(len(chops), 3)
        self.assertEqual(chops[0]['start_seconds'], 0.0)
        self.assertEqual(chops[0]['end_seconds'], 15.0)
        self.assertEqual(chops[1]['start_seconds'], 15.0)
        self.assertEqual(chops[1]['end_seconds'], 30.0)
        self.assertEqual(chops[2]['start_seconds'], 30.0)
        self.assertEqual(chops[2]['end_seconds'], 45.0)

    def test_prepare_overlay_banner(self):
        for pos in ['TOP', 'CENTER', 'BOTTOM']:
            img = ShortsEngineService.prepare_overlay_banner(
                hook_text="Wait for the drop! 🔥",
                part_label="Part 1",
                theme="VIRAL_HOOK",
                hook_position=pos,
                width=1080,
                height=1920
            )
            self.assertIsNotNone(img)
            self.assertEqual(img.size, (1080, 1920))
            self.assertEqual(img.mode, 'RGBA')

    def test_shorts_maker_view(self):
        response = self.client.get(reverse('shorts_maker'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Shorts Maker")
        self.assertContains(response, "Interactive Timeline")
        self.assertContains(response, "Hook Banner Vertical Position")

    @patch.object(ShortsEngineService, 'render_video_chop')
    @patch.object(ShortsEngineService, 'inspect_media_duration', return_value=45.0)
    def test_shorts_render_view_success(self, mock_duration, mock_render_chop):
        # Create dummy video file
        dummy_video = SimpleUploadedFile("sample.mp4", b"fake mp4 video bytes", content_type="video/mp4")

        chops_payload = [
            {"id": 1, "title": "Part 1", "hook_text": "Intro Hook", "hook_position": "TOP", "start_seconds": 0.0, "end_seconds": 15.0, "duration": 15.0},
            {"id": 2, "title": "Part 2", "hook_text": "Climax", "hook_position": "CENTER", "start_seconds": 15.0, "end_seconds": 30.0, "duration": 15.0},
        ]

        response = self.client.post(reverse('shorts_render'), {
            'title': 'Viral Drum Solo',
            'source_type': 'VIDEO',
            'source_video': dummy_video,
            'aspect_mode': 'BLURRED_FIT',
            'theme_style': 'VIRAL_HOOK',
            'hook_position': 'BOTTOM',
            'chops_json': json.dumps(chops_payload),
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        project = ShortVideoProject.objects.filter(title='Viral Drum Solo').first()
        self.assertIsNotNone(project)
        self.assertEqual(project.render_status, ShortVideoProject.Status.COMPLETED)
        self.assertEqual(project.chop_count, 2)
        self.assertEqual(project.hook_position, ShortVideoProject.HookPosition.BOTTOM)
        self.assertEqual(mock_render_chop.call_count, 2)

    def test_shorts_detail_and_delete_view(self):
        project = ShortVideoProject.objects.create(
            title="Guitar Solo Clips",
            duration_seconds=30.0,
            chop_count=2,
            chops_data=[
                {"id": 1, "title": "Part 1", "start_seconds": 0.0, "end_seconds": 15.0, "duration": 15.0, "status": "COMPLETED", "output_file": "part1.mp4", "output_url": "/media/part1.mp4"},
                {"id": 2, "title": "Part 2", "start_seconds": 15.0, "end_seconds": 30.0, "duration": 15.0, "status": "COMPLETED", "output_file": "part2.mp4", "output_url": "/media/part2.mp4"},
            ]
        )

        # Detail view
        res = self.client.get(reverse('shorts_detail', kwargs={'pk': project.id}))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Guitar Solo Clips")
        self.assertContains(res, "Part 1")
        self.assertContains(res, "Part 2")

        # Export zip view
        res_zip = self.client.get(reverse('shorts_export_zip', kwargs={'pk': project.id}))
        self.assertEqual(res_zip.status_code, 200)
        self.assertEqual(res_zip['Content-Type'], 'application/zip')

        # Delete view
        del_res = self.client.post(reverse('shorts_delete', kwargs={'pk': project.id}), follow=True)
        self.assertEqual(del_res.status_code, 200)
        self.assertFalse(ShortVideoProject.objects.filter(pk=project.id).exists())

    def test_permissions_denied_for_non_admin(self):
        self.client.force_login(self.viewer)

        res_maker = self.client.get(reverse('shorts_maker'))
        self.assertEqual(res_maker.status_code, 302)
        self.assertRedirects(res_maker, reverse('dashboard'))

        res_render = self.client.post(reverse('shorts_render'), {})
        self.assertEqual(res_render.status_code, 302)
        self.assertRedirects(res_render, reverse('dashboard'))

    def test_yt_metadata_and_seo_integration(self):
        """Fix 6: Title, description, and hashtags come from the original long video / YouTube metadata."""
        project = ShortVideoProject.objects.create(
            title="Shorts Project",
            source_yt_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            yt_video_title="Original Epic Song Performance (Official Live)",
            yt_video_description="Official live performance recorded at Wembley Stadium.",
            yt_video_tags=["livemusic", "wembley", "guitar", "epic"],
            yt_channel_name="Rock Legends",
            duration_seconds=60.0,
            chop_count=2,
            chops_data=[
                {"id": 1, "title": "Part 1", "hook_text": "Custom Drop 🔥", "show_hook_banner": True, "start_seconds": 0.0, "end_seconds": 30.0},
                {"id": 2, "title": "Part 2", "hook_text": "", "show_hook_banner": False, "start_seconds": 30.0, "end_seconds": 60.0},
            ]
        )

        # Title should use original YouTube video title
        title_0 = project.youtube_title_for_chop(0)
        self.assertIn("Original Epic Song Performance (Official Live)", title_0)
        self.assertIn("Part 1", title_0)
        self.assertIn("Custom Drop 🔥", title_0)

        # Description should contain original snippet, channel credit, source url, and #tags
        desc_0 = project.youtube_description_for_chop(0)
        self.assertIn("Original Epic Song Performance (Official Live)", desc_0)
        self.assertIn("Official live performance recorded at Wembley Stadium", desc_0)
        self.assertIn("Original by Rock Legends", desc_0)
        self.assertIn("https://www.youtube.com/watch?v=dQw4w9WgXcQ", desc_0)
        self.assertIn("#livemusic", desc_0)
        self.assertIn("#wembley", desc_0)

    @patch('apps.studio.services.shorts_engine.ShortsEngineService.prepare_overlay_banner')
    @patch('apps.studio.services.shorts_engine.ShortsEngineService.render_video_chop')
    @patch('apps.studio.services.shorts_engine.ShortsEngineService.inspect_media_duration', return_value=30.0)
    def test_hook_banner_disabled_and_custom_text(self, mock_dur, mock_render_chop, mock_overlay):
        """Fix 1 & Fix 3: banner removed when turned off; custom text rendered when customized."""
        from apps.studio.views import _execute_shorts_render
        dummy_video = SimpleUploadedFile("sample.mp4", b"fake mp4 video bytes", content_type="video/mp4")
        project = ShortVideoProject.objects.create(
            title="Hook Test",
            source_type=ShortVideoProject.SourceType.VIDEO,
            source_video=dummy_video,
            render_status=ShortVideoProject.Status.RENDERING
        )

        chops = [
            # Chop 1: Custom hook text + banner ON -> prepare_overlay_banner MUST be called with custom text
            {"id": 1, "title": "Part 1", "hook_text": "Custom Brand New Text 🚀", "show_hook_banner": True, "show_cta_badge": True, "start_seconds": 0.0, "end_seconds": 15.0},
            # Chop 2: Both banner and CTA turned OFF -> prepare_overlay_banner MUST NOT be called (overlay_png=None)
            {"id": 2, "title": "Part 2", "hook_text": "Ignored text", "show_hook_banner": False, "show_cta_badge": False, "start_seconds": 15.0, "end_seconds": 30.0},
        ]

        _execute_shorts_render(
            project.id,
            chops=chops,
            aspect_mode='BLURRED_FIT',
            theme_style='VIRAL_HOOK',
            crop_focal_percent=50,
            source_type=ShortVideoProject.SourceType.VIDEO,
            show_cta_badge=False
        )

        # Overlay banner should only have been created for Chop 1, NOT for Chop 2
        self.assertEqual(mock_overlay.call_count, 1)
        # Verify custom hook text was passed to overlay generator
        call_kwargs = mock_overlay.call_args[1]
        self.assertEqual(call_kwargs['hook_text'], "Custom Brand New Text 🚀")

    @patch('apps.studio.services.shorts_engine.ShortsEngineService.download_youtube_video')
    def test_shorts_yt_download_view_ajax(self, mock_yt_dlp):
        """Fix 5: AJAX endpoint downloads YouTube video and returns metadata."""
        mock_yt_dlp.return_value = {
            'path': 'C:\\fake\\path\\video.mp4',
            'title': 'Downloaded Test Video',
            'description': 'Test Description',
            'tags': ['tag1', 'tag2'],
            'channel_name': 'Test Channel',
            'duration': 120.0,
            'video_id': 'abc123xyz',
        }

        response = self.client.post(reverse('shorts_yt_download'), {
            'yt_url': 'https://www.youtube.com/watch?v=abc123xyz'
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['title'], 'Downloaded Test Video')
        self.assertEqual(data['duration'], 120.0)
        self.assertEqual(data['tags'], ['tag1', 'tag2'])

    def test_show_cta_badge_toggle(self):
        """Test that show_cta_badge=False suppresses the bottom CTA pill completely."""
        # 1. When hook_text is provided but show_cta_badge=False, banner is generated WITHOUT CTA pill
        img = ShortsEngineService.prepare_overlay_banner(
            hook_text="Hook Only",
            part_label="Part 1",
            theme="VIRAL_HOOK",
            show_cta_badge=False
        )
        self.assertIsNotNone(img)

        # 2. When neither hook banner nor CTA pill is enabled, no overlay is generated
        no_overlay = ShortsEngineService.prepare_overlay_banner(
            hook_text="",
            part_label="",
            theme="CLEAN",
            show_cta_badge=False
        )
        self.assertIsNone(no_overlay)

        # 3. Model field default is True
        project = ShortVideoProject.objects.create(title="CTA Test Project")
        self.assertTrue(project.show_cta_badge)


