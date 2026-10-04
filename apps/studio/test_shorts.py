import io
import json
import os
import sys
import tempfile
import wave
import struct
from unittest.mock import Mock, patch
from PIL import Image
from django.test import TestCase, Client, override_settings
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

    def test_rendered_chop_title_beats_generated_part_label(self):
        project = ShortVideoProject.objects.create(
            title="Aesthetic Vibes",
            yt_video_title="Original Song Breakdown",
            chops_data=[
                {
                    "id": 1,
                    "title": "The Hook Nobody Expected",
                    "hook_text": "Wait for the twist...",
                    "start_seconds": 0.0,
                    "end_seconds": 15.0,
                    "duration": 15.0,
                    "status": "COMPLETED",
                }
            ]
        )

        self.assertEqual(project.youtube_title_for_chop(0), "The Hook Nobody Expected")
        self.assertNotIn("Original Song Breakdown", project.youtube_title_for_chop(0))
        self.assertNotIn("Part 1", project.youtube_title_for_chop(0))
        self.assertIn("The_Hook_Nobody_Expected", project.export_filename_for_chop(0))

    def test_generate_chop_splits(self):
        chops = ShortsEngineService.generate_chop_splits(total_duration=45.0, interval_seconds=15.0)
        self.assertEqual(len(chops), 3)
        self.assertEqual(chops[0]['start_seconds'], 0.0)
        self.assertEqual(chops[0]['end_seconds'], 15.0)
        self.assertEqual(chops[1]['start_seconds'], 15.0)
        self.assertEqual(chops[1]['end_seconds'], 30.0)
        self.assertEqual(chops[2]['start_seconds'], 30.0)
        self.assertEqual(chops[2]['end_seconds'], 45.0)

    def test_write_caption_srt_groups_words_with_clip_local_timestamps(self):
        words = [
            {'word': 'Hello', 'start': 0.2, 'end': 0.5},
            {'word': 'there,', 'start': 0.5, 'end': 0.8},
            {'word': 'welcome', 'start': 0.8, 'end': 1.2},
            {'word': 'back!', 'start': 1.2, 'end': 1.6},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle_path = os.path.join(temp_dir, 'captions.srt')
            result = ShortsEngineService.write_caption_srt(words, 15.0, subtitle_path)
            with open(result, encoding='utf-8') as subtitle_file:
                srt = subtitle_file.read()

        self.assertIn('00:00:00,200 --> 00:00:01,600', srt)
        self.assertIn('Hello there, welcome back!', srt)

    def test_auto_caption_font_size_fits_vertical_video(self):
        caption_filter = ShortsEngineService._caption_filter('captions.srt', 30)
        self.assertIn('FontSize=30', caption_filter)
        self.assertIn('MarginV=320', caption_filter)
        self.assertIn('FontSize=72', ShortsEngineService._caption_filter('captions.srt', 99))

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
        self.assertContains(response, "form_state.js")
        self.assertContains(response, "Interactive Timeline")
        self.assertContains(response, "Hook Banner Vertical Position")
        self.assertContains(response, 'id="customSplitSeconds"')
        self.assertContains(response, "autoSplitCustomChops()")
        self.assertContains(response, "AI Fill All")
        self.assertNotContains(response, "aiFillAllChops(true)")

    @patch.object(ShortsEngineService, 'render_video_chop')
    @patch.object(ShortsEngineService, 'inspect_media_duration', return_value=45.0)
    def test_shorts_render_view_success(self, mock_duration, mock_render_chop):
        # Create dummy video file
        dummy_video = SimpleUploadedFile("sample.mp4", b"fake mp4 video bytes", content_type="video/mp4")

        chops_payload = [
            {"id": 1, "title": "Part 1", "hook_text": "Intro Hook", "hook_position": "TOP", "start_seconds": 0.0, "end_seconds": 15.0, "duration": 15.0, "render_enabled": False},
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
        self.assertEqual(project.chop_count, 1)
        self.assertEqual(project.chops_data[0]['id'], 2)
        self.assertEqual(project.hook_position, ShortVideoProject.HookPosition.BOTTOM)
        self.assertEqual(mock_render_chop.call_count, 1)

    def test_shorts_render_burns_auto_captions(self):
        dummy_video = SimpleUploadedFile("sample.mp4", b"fake mp4 video bytes", content_type="video/mp4")
        chops_payload = [
            {"id": 1, "title": "Part 1", "start_seconds": 0.0, "end_seconds": 15.0, "duration": 15.0},
        ]

        mock_whisper = Mock()
        whisper_module = Mock(WhisperModel=mock_whisper)
        with patch.object(ShortsEngineService, 'render_video_chop') as mock_render_chop, \
                patch.object(ShortsEngineService, 'inspect_media_duration', return_value=15.0), \
                patch.object(ShortsEngineService, 'transcribe_clip_captions', return_value='captions.srt') as mock_transcribe, \
            patch.dict(sys.modules, {'faster_whisper': whisper_module}):
            response = self.client.post(reverse('shorts_render'), {
                'title': 'Captioned Short',
                'source_type': 'VIDEO',
                'source_video': dummy_video,
                'chops_json': json.dumps(chops_payload),
                'auto_captions': '1',
                'caption_font_size': '24',
            }, follow=True)

        self.assertEqual(response.status_code, 200)
        mock_whisper.assert_called_once_with('tiny', device='cpu', compute_type='int8')
        mock_transcribe.assert_called_once()
        self.assertEqual(mock_render_chop.call_args.kwargs['subtitle_path'], 'captions.srt')
        self.assertEqual(mock_render_chop.call_args.kwargs['caption_font_size'], 24)

    def test_shorts_render_rejects_when_all_chops_are_unselected(self):
        response = self.client.post(
            reverse('shorts_render'),
            {
                'title': 'No Selected Clips',
                'source_type': 'VIDEO',
                'source_video': SimpleUploadedFile('sample.mp4', b'video', content_type='video/mp4'),
                'chops_json': json.dumps([
                    {'id': 1, 'start_seconds': 0, 'end_seconds': 15, 'render_enabled': False},
                ]),
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ShortVideoProject.objects.filter(title='No Selected Clips').exists())

    def test_youtube_preview_serves_http_byte_ranges(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            download_dir = os.path.join(media_root, 'studio', 'yt_downloads')
            os.makedirs(download_dir)
            with open(os.path.join(download_dir, 'preview.mp4'), 'wb') as media_file:
                media_file.write(b'0123456789')

            response = self.client.get(
                reverse('shorts_yt_preview', kwargs={'filename': 'preview.mp4'}),
                HTTP_RANGE='bytes=3-6',
            )
            response_body = b''.join(response.streaming_content)

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response_body, b'3456')
        self.assertEqual(response['Content-Range'], 'bytes 3-6/10')
        self.assertEqual(response['Accept-Ranges'], 'bytes')

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
        self.assertEqual(project.caption_style, ShortVideoProject.CaptionStyle.BEAST_YELLOW)
        self.assertTrue(project.visual_progress_bar)
        self.assertTrue(project.audio_normalize)

    def test_format_ass_timestamp(self):
        self.assertEqual(ShortsEngineService.format_ass_timestamp(0.0), "0:00:00.00")
        self.assertEqual(ShortsEngineService.format_ass_timestamp(65.456), "0:01:05.46")
        self.assertEqual(ShortsEngineService.format_ass_timestamp(3661.129), "1:01:01.13")

    def test_write_caption_ass_generates_kinetic_highlight_dialogue(self):
        words = [
            {'word': 'Stop', 'start': 0.0, 'end': 0.4},
            {'word': 'scrolling', 'start': 0.4, 'end': 0.8},
            {'word': 'right', 'start': 0.8, 'end': 1.1},
            {'word': 'now!', 'start': 1.1, 'end': 1.5},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            ass_path = os.path.join(temp_dir, 'kinetic.ass')
            result = ShortsEngineService.write_caption_ass(
                words=words,
                duration_seconds=5.0,
                output_ass_path=ass_path,
                font_size=32,
                style_name='BEAST_YELLOW'
            )
            self.assertEqual(result, ass_path)
            with open(ass_path, 'r', encoding='utf-8') as f:
                content = f.read()

        self.assertIn('[Script Info]', content)
        self.assertIn('[V4+ Styles]', content)
        self.assertIn('[Events]', content)
        self.assertIn('Style: Default', content)
        self.assertIn('Dialogue:', content)
        self.assertIn('STOP', content)
        self.assertIn('SCROLLING', content)
        # Check active kinetic highlight tag was injected
        self.assertIn('{\\c&H0000E5FF&\\b1}', content)

    def test_caption_filter_ass_handling(self):
        ass_filter = ShortsEngineService._caption_filter('C:/path/to/caps.ass', 30)
        self.assertTrue(ass_filter.startswith("ass=filename='"))
        self.assertIn("caps.ass", ass_filter)

    @patch('apps.ai.services.gemini_service.GeminiContentService.generate_for_short')
    def test_shorts_generate_hooks_api(self, mock_gemini):
        mock_gemini.return_value = {
            'hook_options': [
                'Wait for this insane python trick! 🔥',
                'You will NOT believe how easy this is 🤯',
                'Stop writing python code like this ⚠️'
            ]
        }
        # GET request
        res_get = self.client.get(reverse('shorts_generate_hooks_api'))
        self.assertEqual(res_get.status_code, 200)
        data = res_get.json()
        self.assertTrue(data['success'])
        self.assertIn('formulas', data)
        self.assertTrue(len(data['formulas']) >= 5)

        # POST request with topic
        res_post = self.client.post(
            reverse('shorts_generate_hooks_api'),
            data=json.dumps({'title': 'Python Coding Tips', 'source_type': 'VIDEO'}),
            content_type='application/json'
        )
        self.assertEqual(res_post.status_code, 200)
        post_data = res_post.json()
        self.assertTrue(post_data['success'])
        self.assertIn('ai_hooks', post_data)
        self.assertEqual(len(post_data['ai_hooks']), 3)
        self.assertEqual(post_data['ai_hooks'][0], 'Wait for this insane python trick! 🔥')

    def test_silence_to_keep_calculation(self):
        """Pipeline Step 8: Silence removal converts silence ranges to speech keep ranges."""
        from apps.studio.services.silence_cutter import silence_to_keep

        # No silence -> keep full duration
        kept_full = silence_to_keep([], 30.0)
        self.assertEqual(kept_full, [(0.0, 30.0)])

        # Single silence segment in middle
        kept_mid = silence_to_keep([(5.0, 8.0)], 20.0)
        self.assertEqual(len(kept_mid), 2)
        # First speech chunk ends around 5.08s (with pad)
        self.assertAlmostEqual(kept_mid[0][0], 0.0, places=1)
        self.assertTrue(kept_mid[0][1] <= 5.15)
        # Second speech chunk starts around 7.95s (with pad)
        self.assertTrue(kept_mid[1][0] >= 7.9)
        self.assertAlmostEqual(kept_mid[1][1], 20.0, places=1)

    def test_shortvideoproject_remove_pauses_field(self):
        """Pipeline Step 8: ShortVideoProject includes remove_pauses flag."""
        project = ShortVideoProject.objects.create(
            title="Silence Removal Test",
            remove_pauses=True,
            visual_progress_bar=True,
            audio_normalize=True,
        )
        self.assertTrue(project.remove_pauses)
        project.remove_pauses = False
        project.save(update_fields=['remove_pauses'])
        project.refresh_from_db()
        self.assertFalse(project.remove_pauses)



