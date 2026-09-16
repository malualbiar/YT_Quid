import os
import shutil
import tempfile
from PIL import Image
from django.test import TestCase
from django.conf import settings
from apps.studio.services.thumbnail_generator import ThumbnailGeneratorService, CANVAS_WIDTH, CANVAS_HEIGHT


class ThumbnailGeneratorTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        # Create a sample input image to simulate a downloaded YouTube thumbnail
        self.sample_img_path = os.path.join(self.temp_dir, 'sample_yt_thumb.jpg')
        img = Image.new('RGB', (800, 600), color=(70, 130, 180))
        img.save(self.sample_img_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_frosted_scrim_generation(self):
        out_path = os.path.join(self.temp_dir, 'test_scrim.jpg')
        res = ThumbnailGeneratorService.generate_thumbnail(
            title="Blinding Lights",
            artist="The Weeknd",
            background_image_path=self.sample_img_path,
            style=ThumbnailGeneratorService.Style.FROSTED_SCRIM,
            badge_text="OFFICIAL LYRIC VIDEO",
            output_path=out_path
        )
        self.assertTrue(os.path.exists(res))
        with Image.open(res) as img:
            self.assertEqual(img.size, (CANVAS_WIDTH, CANVAS_HEIGHT))
            self.assertEqual(img.format, 'JPEG')

    def test_ambient_card_generation(self):
        out_path = os.path.join(self.temp_dir, 'test_ambient.jpg')
        res = ThumbnailGeneratorService.generate_thumbnail(
            title="Starboy",
            artist="The Weeknd",
            background_image_path=self.sample_img_path,
            style=ThumbnailGeneratorService.Style.AMBIENT_CARD,
            badge_text="4K AUDIO LYRICS",
            output_path=out_path
        )
        self.assertTrue(os.path.exists(res))
        with Image.open(res) as img:
            self.assertEqual(img.size, (CANVAS_WIDTH, CANVAS_HEIGHT))

    def test_themed_backdrop_generation_without_source_image(self):
        out_path = os.path.join(self.temp_dir, 'test_themed.jpg')
        res = ThumbnailGeneratorService.generate_thumbnail(
            title="Cybernetic Dream",
            artist="Synth Runner",
            background_image_path=None,
            style=ThumbnailGeneratorService.Style.THEMED_BACKDROP,
            badge_text="OFFICIAL LYRIC VIDEO",
            theme_palette='CYBERPUNK_NEON',
            output_path=out_path
        )
        self.assertTrue(os.path.exists(res))
        with Image.open(res) as img:
            self.assertEqual(img.size, (CANVAS_WIDTH, CANVAS_HEIGHT))

    def test_fallback_on_corrupt_file(self):
        corrupt_path = os.path.join(self.temp_dir, 'corrupt.jpg')
        with open(corrupt_path, 'w') as f:
            f.write("not an image")

        out_path = os.path.join(self.temp_dir, 'test_fallback.jpg')
        res = ThumbnailGeneratorService.generate_thumbnail(
            title="Emergency Fallback Track",
            artist="Artist X",
            background_image_path=corrupt_path,
            output_path=out_path
        )
        self.assertTrue(os.path.exists(res))
        with Image.open(res) as img:
            self.assertEqual(img.size, (CANVAS_WIDTH, CANVAS_HEIGHT))
