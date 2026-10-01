import logging
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import connections
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from .models import ShortVideoAnalysis, ShortVideoMoment, ShortVideoProject
from .services.gemini_video_analyzer import (
    VideoAnalysisError,
    analysis_limits,
    analyze_video,
    estimate_analysis_chunks,
    inspect_video_duration,
)
from .services.shorts_engine import ShortsEngineService


logger = logging.getLogger(__name__)


def _is_super_admin(request):
    return request.user.is_authenticated and request.user.is_super_admin


def _analysis_payload(analysis):
    moments = []
    for moment in analysis.moments.all():
        try:
            thumbnail_url = moment.thumbnail.url if moment.thumbnail else ''
        except ValueError:
            thumbnail_url = ''
        moments.append({
            'id': moment.pk,
            'start_seconds': moment.start_seconds,
            'end_seconds': moment.end_seconds,
            'title': moment.title,
            'description': moment.description,
            'category': moment.category,
            'reason': moment.reason,
            'suggested_duration': moment.suggested_duration,
            'score': moment.score,
            'score_components': moment.score_components,
            'needs_context': moment.needs_context,
            'selected': moment.selected,
            'thumbnail_url': thumbnail_url,
        })
    return {
        'success': True,
        'analysis_id': analysis.pk,
        'project_id': analysis.project_id,
        'project_title': analysis.project.title,
        'status': analysis.status,
        'video_duration': analysis.video_duration,
        'video_summary': analysis.video_summary,
        'model': analysis.model,
        'completed_chunks': analysis.completed_chunks,
        'chunk_count': analysis.chunk_count,
        'error': analysis.error_message,
        'video_url': analysis.project.source_video.url if analysis.project.source_video else '',
        'moments': moments,
    }


def _safe_youtube_source(path):
    if not path:
        return None
    download_root = os.path.realpath(os.path.join(settings.MEDIA_ROOT, 'studio', 'yt_downloads'))
    real_path = os.path.realpath(path)
    try:
        inside_downloads = os.path.commonpath([download_root, real_path]) == download_root
    except ValueError:
        inside_downloads = False
    if not inside_downloads or not os.path.isfile(real_path):
        return None
    return real_path


def _discard_new_project(project, delete_source=True):
    if delete_source and project.source_video:
        project.source_video.delete(save=False)
    project.delete()


def _create_thumbnail(moment, source_path):
    ffmpeg = ShortsEngineService.get_ffmpeg_binary()
    with tempfile.TemporaryDirectory(prefix='viral_moment_thumbnail_') as temp_dir:
        image_path = os.path.join(temp_dir, 'moment.jpg')
        try:
            result = subprocess.run(
                [
                    ffmpeg, '-y', '-ss', f'{moment.start_seconds:.3f}', '-i', source_path,
                    '-frames:v', '1', '-vf', 'scale=360:640:force_original_aspect_ratio=decrease',
                    '-q:v', '3', image_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                **ShortsEngineService.get_subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return
        if result.returncode != 0 or not os.path.isfile(image_path):
            return
        with open(image_path, 'rb') as image_file:
            moment.thumbnail.save(f'analysis_{moment.analysis_id}_moment_{moment.pk}.jpg', File(image_file), save=True)


def _run_analysis(analysis_id):
    connections.close_all()
    try:
        analysis = ShortVideoAnalysis.objects.select_related('project').get(pk=analysis_id)
        analysis.status = ShortVideoAnalysis.Status.ANALYZING
        analysis.save(update_fields=['status', 'updated_at'])
        source_path = analysis.project.source_video.path

        def report_progress(completed, total):
            ShortVideoAnalysis.objects.filter(pk=analysis_id).update(completed_chunks=completed, chunk_count=total)
            connections.close_all()

        result = analyze_video(source_path, analysis.video_duration, progress_callback=report_progress)
        analysis.video_summary = result['video_summary']
        analysis.chunk_count = result['chunk_count']
        analysis.completed_chunks = result['chunk_count']
        analysis.moments.all().delete()
        moments = [ShortVideoMoment(analysis=analysis, **item) for item in result['moments']]
        created = ShortVideoMoment.objects.bulk_create(moments)
        for moment in created:
            try:
                _create_thumbnail(moment, source_path)
            except Exception:
                logger.warning('Could not create a thumbnail for suggested moment %s', moment.pk)
        analysis.status = ShortVideoAnalysis.Status.COMPLETED
        analysis.error_message = ''
        analysis.save(update_fields=['video_summary', 'chunk_count', 'completed_chunks', 'status', 'error_message', 'updated_at'])
    except VideoAnalysisError as exc:
        ShortVideoAnalysis.objects.filter(pk=analysis_id).update(
            status=ShortVideoAnalysis.Status.FAILED,
            error_message=str(exc),
        )
    except Exception:
        logger.exception('Unexpected error while analyzing Shorts video')
        ShortVideoAnalysis.objects.filter(pk=analysis_id).update(
            status=ShortVideoAnalysis.Status.FAILED,
            error_message='Video analysis failed unexpectedly. Please retry.',
        )
    finally:
        connections.close_all()


@require_POST
def viral_moments_analyze_view(request):
    if not _is_super_admin(request):
        return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)

    limits = analysis_limits()
    analysis_id = request.POST.get('viral_analysis_id')
    project_id = request.POST.get('project_id')
    existing_project = None
    if analysis_id:
        analysis = get_object_or_404(ShortVideoAnalysis, pk=analysis_id)
        existing_project = analysis.project
    elif project_id:
        existing_project = get_object_or_404(ShortVideoProject, pk=project_id)

    if existing_project and (not existing_project.source_video or existing_project.source_type != ShortVideoProject.SourceType.VIDEO):
        return JsonResponse({'error': 'A source video is required for moment analysis.'}, status=400)
    if existing_project and existing_project.viral_analyses.filter(status__in=[
        ShortVideoAnalysis.Status.PENDING,
        ShortVideoAnalysis.Status.ANALYZING,
    ]).exists():
        return JsonResponse({'error': 'This video is already being analyzed.'}, status=409)

    project = existing_project
    owns_source_file = False
    if project is None:
        youtube_path = _safe_youtube_source(request.POST.get('yt_downloaded_path', '').strip())
        active_source_mode = request.POST.get('active_source_mode') or ('YT_URL' if youtube_path else 'VIDEO')
        if active_source_mode not in ('VIDEO', 'YT_URL'):
            return JsonResponse({'error': 'AI moment analysis requires a video source.'}, status=400)
        source_video = request.FILES.get('source_video') if active_source_mode == 'VIDEO' else None
        if source_video is None and youtube_path is None:
            return JsonResponse({'error': 'Upload a video or load a valid YouTube video before analysis.'}, status=400)
        if active_source_mode == 'VIDEO' and source_video is None:
            return JsonResponse({'error': 'Upload a video before analysis.'}, status=400)
        if active_source_mode == 'YT_URL' and youtube_path is None:
            return JsonResponse({'error': 'The downloaded YouTube video is no longer available. Download it again.'}, status=400)
        title = request.POST.get('title', '').strip() or 'Potential Shorts Analysis'
        project = ShortVideoProject.objects.create(
            title=title[:255],
            source_type=ShortVideoProject.SourceType.VIDEO,
            source_video=source_video,
            source_yt_url=request.POST.get('source_yt_url', '').strip(),
            yt_video_title=request.POST.get('yt_video_title', '').strip()[:500],
            yt_video_description=request.POST.get('yt_video_description', '').strip(),
            yt_channel_name=request.POST.get('yt_channel_name', '').strip()[:255],
            render_status=ShortVideoProject.Status.PENDING,
        )
        if youtube_path and active_source_mode == 'YT_URL':
            try:
                media_root = Path(settings.MEDIA_ROOT).resolve()
                relative_name = Path(youtube_path).relative_to(media_root).as_posix()
                project.source_video.name = relative_name
                project.save(update_fields=['source_video', 'updated_at'])
            except (OSError, ValueError):
                _discard_new_project(project)
                return JsonResponse({'error': 'The downloaded YouTube video is no longer available. Download it again.'}, status=400)
        else:
            owns_source_file = True

    try:
        is_local_youtube_import = project.source_video.name.startswith('studio/yt_downloads/') or (
            bool(project.source_yt_url) and project.source_video.name.startswith('studio/shorts_source/')
        )
        if project.source_video.size > limits['max_video_size'] and not is_local_youtube_import:
            if not existing_project:
                _discard_new_project(project, delete_source=owns_source_file)
            return JsonResponse({'error': 'Uploaded video exceeds the configured analysis upload size limit.'}, status=413)
        duration = inspect_video_duration(project.source_video.path)
    except VideoAnalysisError as exc:
        if not existing_project:
            _discard_new_project(project, delete_source=owns_source_file)
        return JsonResponse({'error': str(exc)}, status=400)
    if duration > limits['max_video_duration']:
        if not existing_project:
            _discard_new_project(project, delete_source=owns_source_file)
        return JsonResponse({
            'error': f"Video duration exceeds the configured limit of {limits['max_video_duration'] // 60} minutes.",
        }, status=413)

    chunk_count = estimate_analysis_chunks(duration, limits['chunk_seconds'])
    if chunk_count > limits['max_chunks']:
        if not existing_project:
            _discard_new_project(project, delete_source=owns_source_file)
        return JsonResponse({
            'error': f'Analysis needs {chunk_count} segments, above the configured limit of {limits["max_chunks"]}.',
        }, status=413)

    project.duration_seconds = duration
    project.save(update_fields=['duration_seconds', 'updated_at'])
    analysis = ShortVideoAnalysis.objects.create(
        project=project,
        model=limits['model'],
        video_duration=duration,
        chunk_count=chunk_count,
    )
    worker = threading.Thread(target=_run_analysis, args=(analysis.pk,), daemon=True)
    worker.start()
    return JsonResponse({
        'success': True,
        'analysis_id': analysis.pk,
        'project_id': project.pk,
        'duration': duration,
        'chunk_count': chunk_count,
        'estimated_requests': chunk_count,
        'maximum_requests': chunk_count * 2,
        'model': limits['model'],
    }, status=202)


@require_GET
def viral_moments_status_view(request, analysis_id):
    if not _is_super_admin(request):
        return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)
    analysis = get_object_or_404(ShortVideoAnalysis.objects.select_related('project'), pk=analysis_id)
    return JsonResponse(_analysis_payload(analysis))


@require_POST
def viral_moments_review_view(request, analysis_id):
    if not _is_super_admin(request):
        return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)
    analysis = get_object_or_404(ShortVideoAnalysis, pk=analysis_id)
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'Invalid review data.'}, status=400)
    moments = payload.get('moments') if isinstance(payload, dict) else None
    if analysis.status != ShortVideoAnalysis.Status.COMPLETED or not isinstance(moments, list):
        return JsonResponse({'error': 'This analysis is not ready for review updates.'}, status=409)

    updates = []
    seen_ids = set()
    for item in moments:
        if not isinstance(item, dict):
            return JsonResponse({'error': 'Invalid moment review data.'}, status=400)
        try:
            moment_id = int(item['id'])
            start = float(item['start_seconds'])
            end = float(item['end_seconds'])
        except (KeyError, TypeError, ValueError):
            return JsonResponse({'error': 'Moment timestamps must be valid numbers.'}, status=400)
        if moment_id in seen_ids or not (0 <= start < end <= analysis.video_duration):
            return JsonResponse({'error': 'Moment timestamps must be inside the source video.'}, status=400)
        if not isinstance(item.get('selected'), bool):
            return JsonResponse({'error': 'Moment selection must be true or false.'}, status=400)
        title = str(item.get('title', '')).strip()
        if not title or len(title) > 180:
            return JsonResponse({'error': 'Moment titles must contain 1 to 180 characters.'}, status=400)
        seen_ids.add(moment_id)
        updates.append((moment_id, start, end, title, item['selected']))

    existing_ids = set(analysis.moments.filter(pk__in=seen_ids).values_list('pk', flat=True))
    if existing_ids != seen_ids:
        return JsonResponse({'error': 'A reviewed moment does not belong to this analysis.'}, status=400)
    for moment_id, start, end, title, selected in updates:
        analysis.moments.filter(pk=moment_id).update(
            start_seconds=start,
            end_seconds=end,
            title=title,
            selected=selected,
        )
    return JsonResponse({'success': True})