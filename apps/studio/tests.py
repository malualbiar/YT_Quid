import io
import wave
import struct
import json
import os
import tempfile
from unittest.mock import patch
from PIL import Image
from django.test import TestCase, Client
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from apps.authentication.models import User
from apps.studio.models import VideoProject, NarrationVideoProject
from apps.studio.services.narration_engine import NarrationEngineService
from apps.studio.services.renderer import VideoStudioRenderer
from apps.ai.services.gemini_service import GeminiContentService

class StudioViewsTestCase(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            email='admin@analytics.com',
            username='admin',
            password='adminpassword123',
            role=User.Role.SUPER_ADMIN
        )
        self.client.login(email='admin@analytics.com', password='adminpassword123')

    def test_studio_home_view(self):
        response = self.client.get(reverse('studio_home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Creator Studio')
        self.assertContains(response, '1-Hour Study / Chill Loop')
        self.assertContains(response, 'POV Narration Studio')

    def test_narration_maker_requires_images_when_starting_a_render(self):
        response = self.client.get(reverse('narration_maker'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Drop images here')
        self.assertContains(response, 'Neural voice')

        response = self.client.post(reverse('narration_render'), {
            'title': 'POV Test',
            'script': 'The train was empty.',
            'scenes_json': '[{"text": "The train was empty.", "image_index": 0}]',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('Add at least one AI-generated image', response.json()['error'])
        self.assertFalse(NarrationVideoProject.objects.exists())

    def test_narration_render_rejects_more_than_50_scenes(self):
        scenes = [{'text': f'Scene {index}', 'image_index': 0} for index in range(51)]
        response = self.client.post(reverse('narration_render'), {
            'title': 'Too many POV scenes',
            'script': 'A long story.',
            'scenes_json': json.dumps(scenes),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('no more than 50 image scenes', response.json()['error'])
        self.assertFalse(NarrationVideoProject.objects.exists())

    def test_narration_video_download_uses_project_title_as_filename(self):
        project = NarrationVideoProject.objects.create(
            title='A Night in the City',
            render_status=NarrationVideoProject.Status.COMPLETED,
        )
        project.output_video.save(
            'narrated_story_1.mp4',
            SimpleUploadedFile('narrated_story_1.mp4', b'mp4-content', content_type='video/mp4'),
        )

        response = self.client.get(reverse('narration_download_video', args=[project.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Disposition'],
            'attachment; filename="A Night in the City.mp4"',
        )
        self.assertEqual(b''.join(response.streaming_content), b'mp4-content')

    @patch.object(GeminiContentService, 'generate_narration_youtube_metadata')
    def test_narration_metadata_endpoint_generates_and_saves_script_based_metadata(self, mock_generate):
        project = NarrationVideoProject.objects.create(
            title='Working title',
            script='A traveler discovers a hidden city beneath the desert.',
            render_status=NarrationVideoProject.Status.COMPLETED,
        )
        project.output_video.save(
            'render.mp4',
            SimpleUploadedFile('render.mp4', b'mp4-content', content_type='video/mp4'),
        )
        metadata = {
            'title': 'The City Hidden Beneath the Desert',
            'description': 'A traveler finds a lost city beneath the sands.',
            'thumbnail_prompt': 'Cinematic 16:9 view of a traveler before a buried city.',
        }
        mock_generate.return_value = metadata

        response = self.client.post(reverse('narration_generate_youtube_metadata', args=[project.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data'], metadata)
        project.refresh_from_db()
        self.assertEqual(project.youtube_metadata, metadata)
        self.assertEqual(project.youtube_metadata_error, '')
        mock_generate.assert_called_once_with(
            script='A traveler discovers a hidden city beneath the desert.',
            working_title='Working title',
        )

    def test_completed_narration_page_replaces_scene_breakdown_with_youtube_kit(self):
        project = NarrationVideoProject.objects.create(
            title='A Story',
            script='The script.',
            scenes_data=[{'id': 1, 'text': 'Narration scene text.'}],
            youtube_metadata={
                'title': 'A Catchy YouTube Title',
                'description': 'A script-based description.',
                'thumbnail_prompt': 'A cinematic thumbnail prompt.',
            },
            render_status=NarrationVideoProject.Status.COMPLETED,
        )
        project.output_video.save(
            'render.mp4',
            SimpleUploadedFile('render.mp4', b'mp4-content', content_type='video/mp4'),
        )

        response = self.client.get(reverse('narration_detail', args=[project.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'YouTube publishing kit')
        self.assertContains(response, 'A Catchy YouTube Title')
        self.assertContains(response, 'A script-based description.')
        self.assertContains(response, 'A cinematic thumbnail prompt.')
        self.assertNotContains(response, 'Scene breakdown')

    @patch.object(GeminiContentService, '_call_gemini')
    def test_gemini_narration_metadata_uses_script_and_returns_complete_fields(self, mock_call_gemini):
        script = 'A lighthouse keeper discovers a signal from a ship lost decades ago.'
        mock_call_gemini.return_value = {
            'title': 'The Lighthouse Received a Signal From a Lost Ship',
            'description': 'A lighthouse keeper uncovers a mysterious signal.',
            'thumbnail_prompt': 'A dramatic 16:9 lighthouse scene during a storm.',
        }

        metadata = GeminiContentService.generate_narration_youtube_metadata(
            script=script,
            working_title='Lighthouse story',
        )

        self.assertEqual(
            set(metadata),
            {'title', 'description', 'thumbnail_prompt'},
        )
        self.assertIn(script, mock_call_gemini.call_args.args[0])
        self.assertIn('no generated text', mock_call_gemini.call_args.args[0].lower())
        self.assertLessEqual(len(metadata['title']), 100)

    @patch.object(GeminiContentService, '_call_gemini')
    def test_gemini_narration_metadata_rejects_incomplete_response(self, mock_call_gemini):
        mock_call_gemini.return_value = {
            'title': 'A title',
            'description': '',
            'thumbnail_prompt': 'A prompt',
        }

        metadata = GeminiContentService.generate_narration_youtube_metadata(
            script='A story.',
        )

        self.assertIsNone(metadata)

    def test_narration_render_rejects_more_than_50_images(self):
        image_files = [
            SimpleUploadedFile(f'scene-{index}.jpg', b'image-data', content_type='image/jpeg')
            for index in range(51)
        ]
        response = self.client.post(reverse('narration_render'), {
            'title': 'Too many POV images',
            'script': 'A story.',
            'scenes_json': json.dumps([{'text': 'A story.', 'image_index': 0}]),
            'scene_images': image_files,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('Upload no more than 50 scene images', response.json()['error'])
        self.assertFalse(NarrationVideoProject.objects.exists())

    def test_impact_shake_targets_action_word_in_narration(self):
        text = 'Then, after a long silence, thunder struck the valley.'

        self.assertEqual(
            NarrationEngineService.impact_shake_start_fraction(text),
            5 / 9,
        )
        self.assertIsNone(
            NarrationEngineService.impact_shake_start_fraction(
                'The clouds drifted quietly over the valley.'
            )
        )

    def test_atmosphere_effect_auto_matches_script_cues(self):
        self.assertEqual(
            NarrationEngineService.resolve_atmosphere_effect(
                'auto',
                'A storm rolled over the desert while embers filled the air.',
            ),
            'rain',
        )
        self.assertEqual(
            NarrationEngineService.resolve_atmosphere_effect(
                'auto',
                'Dust blew across the dry desert.',
            ),
            'dust',
        )
        self.assertEqual(
            NarrationEngineService.resolve_atmosphere_effect(
                'rain',
                'Sunlight filled the room.',
            ),
            'rain',
        )
        self.assertEqual(
            NarrationEngineService.resolve_atmosphere_effect(
                'auto',
                'A quiet conversation in the room.',
            ),
            'none',
        )

    def test_atmosphere_effects_build_expected_video_filters(self):
        self.assertIn('drawbox=', NarrationEngineService.atmosphere_filter_chain('dust', 1920, 1080))
        self.assertIn('between(t,', NarrationEngineService.atmosphere_filter_chain('lightning', 1920, 1080))
        self.assertIn('eval=frame', NarrationEngineService.atmosphere_filter_chain('candle_flicker', 1920, 1080))
        self.assertEqual('', NarrationEngineService.atmosphere_filter_chain('none', 1920, 1080))

    @patch.object(NarrationEngineService, '_build_drawtext_filter', return_value='')
    @patch.object(NarrationEngineService, '_duration', return_value=1.0)
    @patch.object(VideoStudioRenderer, 'get_optimal_encoder_args', return_value=[])
    @patch.object(VideoStudioRenderer, 'get_ffmpeg_binary', return_value='ffmpeg')
    @patch.object(NarrationEngineService, '_run')
    def test_render_applies_and_can_disable_impact_shake(
        self,
        mock_run,
        mock_ffmpeg,
        mock_encoder_args,
        mock_duration,
        mock_caption_filter,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, 'scene.jpg')
            audio_path = os.path.join(temp_dir, 'scene.wav')
            for path in (image_path, audio_path):
                with open(path, 'wb') as output:
                    output.write(b'test')

            scene = {
                'image_path': image_path,
                'audio_path': audio_path,
                'text': 'Then thunder struck the valley.',
                'movement': 'zoom_in',
                'camera_effect': 'auto',
            }
            NarrationEngineService.render(
                [scene],
                'en-US-AvaNeural',
                '16:9',
                os.path.join(temp_dir, 'auto.mp4'),
                os.path.join(temp_dir, 'auto.wav'),
            )
            auto_filter = mock_run.call_args_list[0].args[0][
                mock_run.call_args_list[0].args[0].index('-filter_complex') + 1
            ]
            self.assertIn('between(on,', auto_filter)
            self.assertIn('exp(', auto_filter)

            scene['camera_effect'] = 'none'
            NarrationEngineService.render(
                [scene],
                'en-US-AvaNeural',
                '16:9',
                os.path.join(temp_dir, 'none.mp4'),
                os.path.join(temp_dir, 'none.wav'),
            )
            no_effect_filter = mock_run.call_args_list[3].args[0][
                mock_run.call_args_list[3].args[0].index('-filter_complex') + 1
            ]
            self.assertNotIn('between(on,', no_effect_filter)

    @patch.object(GeminiContentService, '_call_gemini')
    def test_pov_prompts_return_one_script_matched_prompt_per_scene(self, mock_call_gemini):
        mock_call_gemini.return_value = {
            'visual_theme': 'A tense night journey.',
            'character_anchor': 'A traveler in a dark coat.',
            'scene_prompts': [
                {'image_prompt': 'POV: a train platform in the rain.'},
                {'image_prompt': 'POV: a train entering a tunnel.'},
            ],
        }
        scenes = [
            'You wait alone on the rain-soaked platform.',
            'The train plunges into a dark tunnel.',
        ]

        result = GeminiContentService.generate_pov_image_prompts(
            script=' '.join(scenes),
            scenes=scenes,
        )

        self.assertEqual(len(result['scene_prompts']), len(scenes))
        self.assertEqual(
            [item['scene_text'] for item in result['scene_prompts']],
            scenes,
        )
        self.assertEqual(
            [item['scene_number'] for item in result['scene_prompts']],
            [1, 2],
        )

    @patch.object(GeminiContentService, '_call_gemini')
    def test_pov_prompt_generation_splits_long_script_when_scenes_are_omitted(self, mock_call_gemini):
        script = ' '.join(f'word{index}' for index in range(40))
        expected_scenes = GeminiContentService._split_pov_script_scenes(script)
        mock_call_gemini.return_value = {
            'scene_prompts': [
                {'image_prompt': f'POV image prompt {index + 1}'}
                for index in range(len(expected_scenes))
            ],
        }

        result = GeminiContentService.generate_pov_image_prompts(script=script)

        self.assertEqual(len(expected_scenes), 3)
        self.assertEqual(len(result['scene_prompts']), len(expected_scenes))
        self.assertEqual(
            [item['scene_text'] for item in result['scene_prompts']],
            expected_scenes,
        )

    @patch.object(VideoStudioRenderer, 'render_visualizer')
    def test_studio_render_view(self, mock_render_vis):
        def fake_render(artwork, audio, out_path):
            with open(out_path, 'wb') as f:
                f.write(b'dummy video content')
            return out_path
        mock_render_vis.side_effect = fake_render

        # Generate dummy 1-second WAV
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(struct.pack("<44100h", *([1000] * 44100)))
        wav_buf.seek(0)
        audio_file = SimpleUploadedFile('test.wav', wav_buf.read(), content_type='audio/wav')

        # Generate dummy image
        img = Image.new('RGB', (600, 600), color=(30, 60, 90))
        img_buf = io.BytesIO()
        img.save(img_buf, format='JPEG')
        img_buf.seek(0)
        image_file = SimpleUploadedFile('cover.jpg', img_buf.read(), content_type='image/jpeg')

        response = self.client.post(reverse('studio_render'), {
            'title': 'Midnight Chill Vibes',
            'video_format': VideoProject.VideoFormat.VISUALIZER,
            'audio_file': audio_file,
            'cover_image': image_file,
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        project = VideoProject.objects.filter(title='Midnight Chill Vibes').first()
        self.assertIsNotNone(project)
        self.assertEqual(project.render_status, VideoProject.Status.COMPLETED)
        self.assertTrue(bool(project.output_video))

    def test_studio_access_denied_for_non_superadmin(self):
        viewer = User.objects.create_user(
            email='viewer@test.com',
            username='viewer',
            password='password123',
            role=User.Role.VIEWER
        )
        self.client.force_login(viewer)

        # GET studio_home
        response = self.client.get(reverse('studio_home'))
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('dashboard'))

        # POST studio_render
        response = self.client.post(reverse('studio_render'), {})
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('dashboard'))

    @patch('apps.ai.services.gemini_service.GeminiContentService._call_gemini')
    def test_narration_generate_prompts_view(self, mock_call_gemini):
        mock_call_gemini.return_value = {
            'visual_theme': 'Cyberpunk dystopian aesthetic',
            'style': 'cyberpunk',
            'generator': 'midjourney',
            'scene_prompts': [
                {
                    'scene_number': 1,
                    'scene_text': 'POV: You step out into the rain.',
                    'image_prompt': 'POV shot looking at rain-soaked futuristic street with neon signs --ar 9:16',
                    'negative_prompt': 'blurry, 3rd person',
                    'camera_angle': 'Eye level',
                    'lighting': 'Neon ambient glow'
                }
            ]
        }

        response = self.client.post(
            reverse('narration_generate_prompts'),
            data=json.dumps({
                'script': 'POV: You step out into the rain.',
                'style': 'cyberpunk',
                'aspect_ratio': '9:16',
                'generator': 'midjourney'
            }),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertTrue(json_data['success'])
        self.assertEqual(json_data['data']['visual_theme'], 'Cyberpunk dystopian aesthetic')
        self.assertEqual(len(json_data['data']['scene_prompts']), 1)
        self.assertIn('POV shot looking at rain-soaked', json_data['data']['scene_prompts'][0]['image_prompt'])

    @patch('apps.ai.services.gemini_service.GeminiContentService._call_gemini')
    def test_ai_generate_view_pov_prompts(self, mock_call_gemini):
        mock_call_gemini.return_value = {
            'visual_theme': 'Cinematic mystery',
            'style': 'cinematic',
            'generator': 'midjourney',
            'scene_prompts': [
                {
                    'scene_number': 1,
                    'scene_text': 'You find an old key on the floor.',
                    'image_prompt': 'POV shot looking down at ornate golden key on dark wooden floorboards --ar 9:16',
                    'negative_prompt': 'blurry, low quality',
                    'camera_angle': 'Looking down',
                    'lighting': 'Single spotlight beam'
                }
            ]
        }

        response = self.client.post(
            reverse('ai_generate'),
            data=json.dumps({
                'content_type': 'pov_prompts',
                'tone': 'viral',
                'context': {
                    'script': 'You find an old key on the floor.',
                    'style': 'cinematic',
                    'aspect_ratio': '9:16',
                    'generator': 'midjourney'
                }
            }),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertTrue(json_data['success'])
        self.assertEqual(json_data['data']['visual_theme'], 'Cinematic mystery')
