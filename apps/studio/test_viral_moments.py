import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
try:
    from google.genai import types
except ImportError:
    types = None
from apps.ai.services.gemini_service import GeminiContentService

from apps.authentication.models import User
from apps.studio.models import ShortVideoAnalysis, ShortVideoMoment, ShortVideoProject
from apps.studio.services.gemini_video_analyzer import (
    VIDEO_RESPONSE_SCHEMA,
    VideoAnalysisError,
    analysis_limits,
    analyze_video,
    estimate_analysis_chunks,
    _friendly_gemini_error,
)
from apps.studio.services.moment_ranker import SCORE_FACTORS, rank_and_deduplicate, validate_moment


class MomentRankerTests(SimpleTestCase):
    def make_moment(self, start, end, score=8, title='Moment'):
        return {
            'start': start,
            'end': end,
            'title': title,
            'description': 'A specific moment.',
            'category': 'reaction',
            'reason': 'A clear reaction and payoff.',
            'suggested_duration': end - start,
            'needs_context': False,
            'score_components': {factor: score for factor in SCORE_FACTORS},
        }

    def test_moment_scores_are_transparent_equal_weight_average(self):
        candidate = self.make_moment(12, 24)
        candidate['score_components']['surprise'] = 10
        result = validate_moment(candidate, 100, 60, 500)
        self.assertEqual(result['start_seconds'], 112)
        self.assertEqual(result['end_seconds'], 124)
        self.assertEqual(result['score'], 82)
        self.assertEqual(result['score_components']['surprise'], 10)

    def test_invalid_timestamps_or_score_factors_are_rejected(self):
        self.assertIsNone(validate_moment(self.make_moment(40, 20), 0, 60, 60))
        invalid = self.make_moment(1, 5)
        invalid['score_components']['humor'] = 11
        self.assertIsNone(validate_moment(invalid, 0, 60, 60))

    def test_ranker_suppresses_overlapping_or_nearby_candidates(self):
        lower_score = validate_moment(self.make_moment(20, 40, 7, 'Lower'), 0, 60, 60)
        higher_score = validate_moment(self.make_moment(25, 45, 9, 'Higher'), 0, 60, 60)
        distant = validate_moment(self.make_moment(55, 59, 8, 'Distant'), 0, 60, 60)
        result = rank_and_deduplicate([lower_score, higher_score, distant], minimum_gap_seconds=5)
        self.assertEqual([item['title'] for item in result], ['Higher', 'Distant'])

    @override_settings(ANALYSIS_CHUNK_MINUTES=10, MAX_ANALYSIS_CHUNKS=4)
    def test_chunk_estimate_and_limits_are_configurable(self):
        self.assertEqual(estimate_analysis_chunks(1201), 3)
        self.assertEqual(analysis_limits()['max_chunks'], 4)

    @override_settings(GEMINI_API_KEY='')
    @patch('apps.studio.services.gemini_video_analyzer.os.getenv', return_value='')
    def test_blank_gemini_key_has_clear_configuration_error(self, mock_getenv):
        with self.assertRaisesMessage(VideoAnalysisError, 'Set GEMINI_API_KEY on the server.'):
            analyze_video('unused.mp4', 60)

    def test_gemini_503_is_reported_as_temporary_provider_outage(self):
        self.assertEqual(
            _friendly_gemini_error(Exception('503 UNAVAILABLE: temporary overload')),
            'Gemini is temporarily unavailable. Please wait and try again.',
        )

    def test_gemini_sdk_accepts_structured_response_schema(self):
        config = types.GenerateContentConfig(
            response_mime_type='application/json',
            response_schema=VIDEO_RESPONSE_SCHEMA,
        )
        self.assertEqual(config.response_mime_type, 'application/json')

    @patch.object(GeminiContentService, '_call_gemini', return_value={'hook_options': ['Wait for it']})
    def test_short_copy_generation_receives_moment_context(self, mock_call):
        GeminiContentService.generate_for_short(
            yt_title='Gameplay session',
            chop_start=42,
            chop_end=58,
            moment_description='A sudden comeback changes the match.',
            moment_reason='A clear reversal with a decisive payoff.',
            moment_category='gameplay win',
        )
        prompt = mock_call.call_args.args[0]
        self.assertIn('A sudden comeback changes the match.', prompt)
        self.assertIn('A clear reversal with a decisive payoff.', prompt)
        self.assertIn('gameplay win', prompt)

    @override_settings(GEMINI_API_KEY='test-key', GEMINI_MODEL='configured-video-model')
    @patch('google.genai.Client')
    def test_shared_gemini_service_uses_configured_model(self, mock_client_class):
        response = Mock(text='{}', usage_metadata=Mock(prompt_token_count=1, candidates_token_count=1))
        mock_client = mock_client_class.return_value
        mock_client.models.generate_content.return_value = response

        result = GeminiContentService._call_gemini('test prompt')

        self.assertEqual(result, {})
        mock_client_class.assert_called_once_with(api_key='test-key')
        mock_client.models.generate_content.assert_called_once()
        call = mock_client.models.generate_content.call_args.kwargs
        self.assertEqual(call['model'], 'configured-video-model')
        self.assertEqual(call['contents'], 'test prompt')
        self.assertEqual(call['config'].response_mime_type, 'application/json')


class ViralMomentEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            email='viral-admin@example.com',
            username='viraladmin',
            password='test-password-123',
            role=User.Role.SUPER_ADMIN,
        )
        self.client.login(email='viral-admin@example.com', password='test-password-123')

    def test_shorts_maker_renders_analysis_controls_and_context_padding(self):
        response = self.client.get(reverse('shorts_maker'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Find Viral Moments')
        self.assertContains(response, 'Shorts Potential ranking')
        self.assertContains(response, 'Seconds of context before each moment')
        self.assertContains(response, 'Create Selected Clips')

    def test_generated_metadata_overrides_are_used_for_export_copy(self):
        project = ShortVideoProject.objects.create(
            title='Source video',
            chops_data=[{
                'title': 'Chosen short title',
                'ai_title': 'Chosen short title',
                'ai_description': 'A concise description.',
                'ai_tags': ['#shorts', '#gameplay'],
            }],
        )
        self.assertEqual(project.youtube_title_for_chop(0), 'Chosen short title')
        self.assertEqual(project.youtube_description_for_chop(0), 'A concise description.\n\n#shorts #gameplay')

    def test_analysis_start_persists_video_and_returns_cost_estimate(self):
        with tempfile.TemporaryDirectory() as media_root, \
                override_settings(MEDIA_ROOT=media_root), \
                patch('apps.studio.viral_moments_views.inspect_video_duration', return_value=65), \
                patch('apps.studio.viral_moments_views.threading.Thread') as thread:
            response = self.client.post(reverse('shorts_viral_analyze'), {
                'title': 'Test source video',
                'source_type': 'VIDEO',
                'source_video': SimpleUploadedFile('source.mp4', b'video bytes', content_type='video/mp4'),
            })

            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload['estimated_requests'], 1)
            self.assertEqual(payload['model'], analysis_limits()['model'])
            analysis = ShortVideoAnalysis.objects.get(pk=payload['analysis_id'])
            self.assertEqual(analysis.status, ShortVideoAnalysis.Status.PENDING)
            self.assertTrue(analysis.project.source_video)
            thread.return_value.start.assert_called_once()

    def test_youtube_analysis_reuses_local_download_without_upload_cap(self):
        with tempfile.TemporaryDirectory() as media_root:
            download_dir = Path(media_root) / 'studio' / 'yt_downloads'
            download_dir.mkdir(parents=True)
            downloaded_video = download_dir / 'youtube-source.mp4'
            downloaded_video.write_bytes(b'local youtube video larger than browser cap')
            with override_settings(MEDIA_ROOT=media_root, MAX_ANALYSIS_VIDEO_SIZE_BYTES=8), \
                    patch('apps.studio.viral_moments_views.inspect_video_duration', return_value=65), \
                    patch('apps.studio.viral_moments_views.threading.Thread') as thread:
                response = self.client.post(reverse('shorts_viral_analyze'), {
                    'title': 'Imported YouTube source',
                    'source_type': 'VIDEO',
                    'active_source_mode': 'YT_URL',
                    'yt_downloaded_path': str(downloaded_video),
                    'source_yt_url': 'https://www.youtube.com/watch?v=testvideo',
                    'source_video': SimpleUploadedFile('stale-large-upload.mp4', b'x' * 64, content_type='video/mp4'),
                })

            self.assertEqual(response.status_code, 202)
            project = ShortVideoProject.objects.get(pk=response.json()['project_id'])
            self.assertEqual(project.source_video.name, 'studio/yt_downloads/youtube-source.mp4')
            self.assertTrue(downloaded_video.exists())
            self.assertFalse((Path(media_root) / 'studio' / 'shorts_source').exists())
            thread.return_value.start.assert_called_once()

    def test_analysis_status_returns_persisted_structured_moments(self):
        project = ShortVideoProject.objects.create(title='Stored source')
        analysis = ShortVideoAnalysis.objects.create(
            project=project,
            model='test-model',
            status=ShortVideoAnalysis.Status.COMPLETED,
            video_duration=100,
        )
        ShortVideoMoment.objects.create(
            analysis=analysis,
            start_seconds=12,
            end_seconds=28,
            title='Unexpected reaction',
            category='reaction',
            reason='Clear reaction and payoff.',
            suggested_duration=16,
            score=84,
            score_components={factor: 8 for factor in SCORE_FACTORS},
        )

        response = self.client.get(reverse('shorts_viral_analysis_status', args=[analysis.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['moments'][0]['score'], 84)
        self.assertEqual(response.json()['moments'][0]['title'], 'Unexpected reaction')

    def test_review_api_persists_selection_and_edited_timestamps(self):
        project = ShortVideoProject.objects.create(title='Review source')
        analysis = ShortVideoAnalysis.objects.create(
            project=project,
            model='test-model',
            status=ShortVideoAnalysis.Status.COMPLETED,
            video_duration=100,
        )
        moment = ShortVideoMoment.objects.create(
            analysis=analysis,
            start_seconds=12,
            end_seconds=25,
            title='Suggested title',
        )
        review_payload = {'moments': [{
                'id': moment.pk,
                'start_seconds': 10.5,
                'end_seconds': 28,
                'title': 'Edited title',
                'selected': True,
            }]}
        response = self.client.post(
            reverse('shorts_viral_analysis_review', args=[analysis.pk]),
            data=json.dumps(review_payload),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        moment.refresh_from_db()
        self.assertTrue(moment.selected)
        self.assertEqual(moment.start_seconds, 10.5)
        self.assertEqual(moment.end_seconds, 28)
        self.assertEqual(moment.title, 'Edited title')

        review_payload['moments'][0]['end_seconds'] = 101
        invalid_response = self.client.post(
            reverse('shorts_viral_analysis_review', args=[analysis.pk]),
            data=json.dumps(review_payload),
            content_type='application/json',
        )
        self.assertEqual(invalid_response.status_code, 400)

    def test_render_reuses_analysis_project_and_persists_selection_and_edits(self):
        project = ShortVideoProject.objects.create(title='Analyzed video', duration_seconds=60)
        analysis = ShortVideoAnalysis.objects.create(
            project=project,
            model='test-model',
            status=ShortVideoAnalysis.Status.COMPLETED,
            video_duration=60,
        )
        moment = ShortVideoMoment.objects.create(
            analysis=analysis,
            start_seconds=10,
            end_seconds=20,
            title='Original moment',
            score=80,
        )
        chops = [{
            'id': 1,
            'moment_id': moment.pk,
            'title': 'Edited moment',
            'start_seconds': 8.5,
            'end_seconds': 22,
            'render_enabled': True,
        }]
        with patch('apps.studio.views.threading.Thread') as thread:
            response = self.client.post(reverse('shorts_render'), {
                'title': 'Analyzed video',
                'source_type': 'VIDEO',
                'viral_analysis_id': analysis.pk,
                'chops_json': json.dumps(chops),
            }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['project_id'], project.pk)
        self.assertEqual(ShortVideoProject.objects.count(), 1)
        moment.refresh_from_db()
        self.assertTrue(moment.selected)
        self.assertEqual(moment.start_seconds, 8.5)
        self.assertEqual(moment.end_seconds, 22)
        thread.return_value.start.assert_called_once()