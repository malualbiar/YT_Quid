import io
import wave
import struct
from unittest.mock import patch
from PIL import Image
from django.test import TestCase, Client
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from apps.authentication.models import User
from apps.studio.models import VideoProject, NarrationVideoProject
from apps.studio.services.renderer import VideoStudioRenderer

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
        self.assertContains(response, 'Drop AI-generated images here')
        self.assertContains(response, 'Kokoro voice')

        response = self.client.post(reverse('narration_render'), {
            'title': 'POV Test',
            'script': 'The train was empty.',
            'scenes_json': '[{"text": "The train was empty.", "image_index": 0}]',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('Add at least one AI-generated image', response.json()['error'])
        self.assertFalse(NarrationVideoProject.objects.exists())

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
