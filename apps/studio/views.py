import os
import shutil
import time
import json
import zipfile
import threading
from io import BytesIO
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.core.files import File
from django.http import HttpResponse, JsonResponse, FileResponse
from django.urls import reverse
from django.db import connections

from .models import VideoProject, LongMixProject, ShortVideoProject, LyricVideoProject
from .services.renderer import VideoStudioRenderer
from .services.mix_engine import MixEngineService
from .services.shorts_engine import ShortsEngineService
from .services.lyrics_engine import LyricsEngineService
from .services.process_tracker import RenderProcessTracker
from apps.videos.models import Video

@login_required
def studio_home_view(request):
    """
    Main YouTube Video Studio dashboard: displays Lyrics Videos, Shorts & Chops, 1-Hour Loops, and Non-Stop Mixes.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    tab = request.GET.get('tab', 'lyrics')
    single_loops = VideoProject.objects.all().order_by('-created_at')
    mix_projects = LongMixProject.objects.all().order_by('-created_at')
    short_projects = ShortVideoProject.objects.all().order_by('-created_at')
    lyric_projects = LyricVideoProject.objects.all().order_by('-created_at')
    
    total_mix_duration = sum(m.duration_seconds for m in mix_projects)
    total_loop_duration = sum(p.duration_seconds for p in single_loops)
    total_shorts_duration = sum(s.duration_seconds for s in short_projects)
    total_lyrics_duration = sum(l.duration_seconds for l in lyric_projects)
    total_hours_produced = round((total_mix_duration + total_loop_duration + total_shorts_duration + total_lyrics_duration) / 3600, 1)

    total_shorts_count = sum(s.chop_count for s in short_projects)

    return render(request, 'studio/index.html', {
        'tab': tab,
        'projects': single_loops,
        'mix_projects': mix_projects,
        'short_projects': short_projects,
        'lyric_projects': lyric_projects,
        'total_shorts_count': total_shorts_count,
        'total_hours_produced': total_hours_produced,
        'format_choices': VideoProject.VideoFormat.choices,
    })


@login_required
@require_POST
def studio_render_view(request):
    """
    Handles single video loop rendering form submission.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    title = request.POST.get('title', '').strip() or 'My YouTube Chill Loop'
    video_format = request.POST.get('video_format', VideoProject.VideoFormat.ONE_HOUR_LOOP)
    audio_file = request.FILES.get('audio_file')
    cover_image = request.FILES.get('cover_image')

    if not audio_file:
        messages.error(request, "Please upload an audio file (.wav or .mp3).")
        return redirect('studio_home')

    if not cover_image:
        messages.error(request, "Please upload a cover image.")
        return redirect('studio_home')

    # Create project record
    project = VideoProject.objects.create(
        title=title,
        audio_file=audio_file,
        cover_image=cover_image,
        video_format=video_format,
        render_status=VideoProject.Status.RENDERING
    )

    studio_videos_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'videos')
    os.makedirs(studio_videos_dir, exist_ok=True)

    try:
        artwork_path = project.cover_image.path
        audio_path = project.audio_file.path

        if video_format == VideoProject.VideoFormat.ONE_HOUR_LOOP:
            out_filename = f"studio_{project.id}_1hour_loop.mp4"
            out_path = os.path.join(studio_videos_dir, out_filename)
            VideoStudioRenderer.render_1hour_loop(artwork_path, audio_path, out_path, duration_seconds=3600)
            project.duration_seconds = 3600
            success_msg = f"Generated 1-Hour Study Loop for '{project.title}'!"

        elif video_format == VideoProject.VideoFormat.SHORT:
            out_filename = f"studio_{project.id}_short.mp4"
            out_path = os.path.join(studio_videos_dir, out_filename)
            VideoStudioRenderer.render_short(artwork_path, audio_path, out_path, start_seconds=30, duration_seconds=15)
            project.duration_seconds = 15
            success_msg = f"Generated 15s YouTube Shorts vertical hook for '{project.title}'!"

        else:  # VISUALIZER
            out_filename = f"studio_{project.id}_visualizer.mp4"
            out_path = os.path.join(studio_videos_dir, out_filename)
            VideoStudioRenderer.render_visualizer(artwork_path, audio_path, out_path)
            project.duration_seconds = 180
            success_msg = f"Generated 1080p Official Visualizer for '{project.title}'!"

        # Save output video file
        with open(out_path, 'rb') as f:
            project.output_video.save(out_filename, File(f), save=False)

        project.render_status = VideoProject.Status.COMPLETED
        project.save()
        messages.success(request, success_msg)

    except Exception as e:
        project.render_status = VideoProject.Status.FAILED
        project.error_message = str(e)
        project.save()
        messages.error(request, f"Video generation notice: {str(e)}")

    return redirect('studio_home')


@login_required
@require_POST
def studio_delete_view(request, pk):
    """
    Deletes a generated single loop video project.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    project = get_object_or_404(VideoProject, pk=pk)
    title = project.title
    project.delete()
    messages.info(request, f"Deleted video project '{title}'.")
    return redirect('studio_home')


# =========================================================================
# NON-STOP CONTINUOUS LONG MIX MAKER VIEWS
# =========================================================================

@login_required
def mix_maker_view(request):
    """
    Interactive Continuous Long Mix Builder.
    Allows uploading multiple songs, importing from catalog / YouTube, configuring crossfade, and cover art.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    catalog_videos = Video.objects.filter(is_active=True).select_related('artist', 'channel').order_by('-current_views')[:30]

    return render(request, 'studio/mix_maker.html', {
        'catalog_videos': catalog_videos,
        'transition_choices': LongMixProject.TransitionCurve.choices,
    })


def _execute_mix_render(project_id, collected_tracks, crossfade_seconds, transition_curve, render_video, normalize_volume):
    from django.db import connections
    connections.close_all()
    try:
        project = LongMixProject.objects.get(id=project_id)
    except LongMixProject.DoesNotExist:
        return

    project_source_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'mix_source', str(project.id))
    os.makedirs(project_source_dir, exist_ok=True)
    mix_output_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'mixes')
    os.makedirs(mix_output_dir, exist_ok=True)
    RenderProcessTracker.clear_cancelled('mix', project.id)

    try:
        RenderProcessTracker.set_progress('mix', project.id, 5, 'Analyzing audio tracks and calculating cue points...')
        if RenderProcessTracker.is_cancelled('mix', project.id):
            project.render_status = LongMixProject.Status.CANCELLED
            project.error_message = "Rendering cancelled by user."
            project.save()
            return

        # 1. Calculate timeline and exact start timestamps
        timeline, total_duration = MixEngineService.calculate_track_timeline(
            collected_tracks,
            crossfade_seconds=crossfade_seconds
        )

        if RenderProcessTracker.is_cancelled('mix', project.id):
            project.render_status = LongMixProject.Status.CANCELLED
            project.error_message = "Rendering cancelled by user."
            project.save()
            return

        # 2. Render continuous master audio
        RenderProcessTracker.set_progress('mix', project.id, 25, f'Blending {len(timeline)} audio tracks with {crossfade_seconds}s crossfade...')
        audio_paths = [t['path'] for t in timeline]
        out_audio_filename = f"mix_{project.id}_master.mp3"
        out_audio_path = os.path.join(mix_output_dir, out_audio_filename)

        MixEngineService.render_continuous_mix(
            audio_paths=audio_paths,
            output_mp3_path=out_audio_path,
            crossfade_seconds=crossfade_seconds,
            transition_curve=transition_curve,
            normalize_volume=normalize_volume,
            project_id=project.id
        )

        if RenderProcessTracker.is_cancelled('mix', project.id):
            project.render_status = LongMixProject.Status.CANCELLED
            project.error_message = "Rendering cancelled by user."
            project.save()
            return

        with open(out_audio_path, 'rb') as f:
            project.output_audio.save(out_audio_filename, File(f), save=False)

        # 3. Optionally render 1080p MP4 video
        if render_video:
            RenderProcessTracker.set_progress('mix', project.id, 65, 'Encoding 1080p HD YouTube video canvas with artwork...')
            artwork_path = project.cover_image.path if project.cover_image else None
            out_video_filename = f"mix_{project.id}_1080p.mp4"
            out_video_path = os.path.join(mix_output_dir, out_video_filename)

            MixEngineService.render_mix_video(
                artwork_path=artwork_path,
                audio_path=out_audio_path,
                output_mp4_path=out_video_path,
                title=project.title,
                project_id=project.id
            )

            if RenderProcessTracker.is_cancelled('mix', project.id):
                project.render_status = LongMixProject.Status.CANCELLED
                project.error_message = "Rendering cancelled by user."
                project.save()
                return

            with open(out_video_path, 'rb') as f:
                project.output_video.save(out_video_filename, File(f), save=False)

        # 4. Save metadata
        project.track_count = len(timeline)
        project.duration_seconds = int(total_duration)
        project.tracklist_data = timeline
        project.render_status = LongMixProject.Status.COMPLETED
        project.save()
        RenderProcessTracker.set_progress('mix', project.id, 100, f"Non-Stop Mix with {len(timeline)} tracks ready!")

    except Exception as e:
        if RenderProcessTracker.is_cancelled('mix', project.id):
            project.render_status = LongMixProject.Status.CANCELLED
            project.error_message = "Rendering cancelled by user."
        else:
            project.render_status = LongMixProject.Status.FAILED
            project.error_message = str(e)
        project.save()
    finally:
        connections.close_all()


@login_required
@require_POST
def mix_render_view(request):
    """
    Processes multiple audio tracks in exact user-configured order, calculates timeline,
    blends via FFmpeg acrossfade, and optionally renders 1080p MP4 long mix video.
    Supports asynchronous execution with live progress polling and cancellation.
    """
    if not request.user.is_super_admin:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    title = request.POST.get('title', '').strip() or 'My Non-Stop Music Mix'
    description = request.POST.get('description', '').strip()
    crossfade_seconds = int(request.POST.get('crossfade_seconds', 6))
    transition_curve = request.POST.get('transition_curve', LongMixProject.TransitionCurve.QSIN)
    render_video = request.POST.get('render_video') in ['true', '1', 'on']
    normalize_volume = request.POST.get('normalize_volume', 'true') in ['true', '1', 'on']
    cover_image = request.FILES.get('cover_image')

    track_count_str = request.POST.get('track_count')

    # Create project entry
    project = LongMixProject.objects.create(
        title=title,
        description=description,
        cover_image=cover_image,
        transition_curve=transition_curve,
        crossfade_seconds=crossfade_seconds,
        render_video=render_video,
        render_status=LongMixProject.Status.RENDERING
    )

    project_source_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'mix_source', str(project.id))
    os.makedirs(project_source_dir, exist_ok=True)

    try:
        collected_tracks = []

        if track_count_str and track_count_str.isdigit():
            total_items = int(track_count_str)
            for idx in range(total_items):
                t_type = request.POST.get(f'track_type_{idx}', 'file')
                t_title = request.POST.get(f'track_title_{idx}', '').strip()
                t_artist = request.POST.get(f'track_artist_{idx}', '').strip()
                t_file = request.FILES.get(f'track_file_{idx}')
                t_url = request.POST.get(f'track_url_{idx}', '').strip()

                if t_type == 'file' and t_file:
                    safe_name = f"track_{idx + 1}_{t_file.name}"
                    dest_path = os.path.join(project_source_dir, safe_name)
                    with open(dest_path, 'wb+') as destination:
                        for chunk in t_file.chunks():
                            destination.write(chunk)

                    if not t_title:
                        t_title = os.path.splitext(t_file.name)[0]
                    dur = MixEngineService.inspect_audio_duration(dest_path)
                    collected_tracks.append({
                        'title': t_title,
                        'artist': t_artist,
                        'duration': dur,
                        'path': dest_path,
                    })

                elif (t_type == 'url' or t_url) and t_url:
                    try:
                        dest_path, dl_title, dur = MixEngineService.download_youtube_audio(t_url, project_source_dir)
                        if not t_title:
                            t_title = dl_title
                        collected_tracks.append({
                            'title': t_title,
                            'artist': t_artist,
                            'duration': dur,
                            'path': dest_path,
                        })
                    except Exception:
                        pass
        else:
            uploaded_files = request.FILES.getlist('track_files')
            track_titles = request.POST.getlist('track_titles[]')
            track_artists = request.POST.getlist('track_artists[]')
            track_urls = request.POST.getlist('track_urls[]')

            track_idx = 0
            for f in uploaded_files:
                dest_path = os.path.join(project_source_dir, f"track_{track_idx + 1}_{f.name}")
                with open(dest_path, 'wb+') as destination:
                    for chunk in f.chunks():
                        destination.write(chunk)
                t_title = track_titles[track_idx] if track_idx < len(track_titles) and track_titles[track_idx] else os.path.splitext(f.name)[0]
                t_artist = track_artists[track_idx] if track_idx < len(track_artists) else ''
                dur = MixEngineService.inspect_audio_duration(dest_path)
                collected_tracks.append({
                    'title': t_title,
                    'artist': t_artist,
                    'duration': dur,
                    'path': dest_path,
                })
                track_idx += 1

            for url in track_urls:
                if url and url.strip():
                    try:
                        dest_path, dl_title, dur = MixEngineService.download_youtube_audio(url.strip(), project_source_dir)
                        t_title = track_titles[track_idx] if track_idx < len(track_titles) and track_titles[track_idx] else dl_title
                        t_artist = track_artists[track_idx] if track_idx < len(track_artists) else ''
                        collected_tracks.append({
                            'title': t_title,
                            'artist': t_artist,
                            'duration': dur,
                            'path': dest_path,
                        })
                        track_idx += 1
                    except Exception:
                        pass

        if not collected_tracks:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'error': 'No valid audio tracks could be processed for mixing. Please add at least 1 audio track.'}, status=400)
            messages.error(request, "No valid audio tracks could be processed for mixing. Please add at least 1 audio track.")
            return redirect('mix_maker')

        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('async') == '1'

        if is_ajax:
            # Launch render in daemon worker thread
            thread = threading.Thread(
                target=_execute_mix_render,
                args=(project.id, collected_tracks, crossfade_seconds, transition_curve, render_video, normalize_volume),
                daemon=True
            )
            thread.start()

            return JsonResponse({
                'success': True,
                'project_id': project.id,
                'project_type': 'mix',
                'title': project.title,
                'redirect_url': reverse('mix_detail', kwargs={'pk': project.id})
            })

        # Synchronous execution fallback for direct POST
        _execute_mix_render(project.id, collected_tracks, crossfade_seconds, transition_curve, render_video, normalize_volume)
        project.refresh_from_db()
        if project.render_status == LongMixProject.Status.COMPLETED:
            messages.success(request, f"Successfully created nonstop mix '{project.title}' with {project.track_count} tracks ({project.duration_formatted})!")
        elif project.render_status == LongMixProject.Status.CANCELLED:
            messages.warning(request, f"Rendering was cancelled for '{project.title}'.")
        else:
            messages.error(request, f"Mix rendering notice: {project.error_message}")
        return redirect('mix_detail', pk=project.id)

    except Exception as e:
        project.render_status = LongMixProject.Status.FAILED
        project.error_message = str(e)
        project.save()
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'error': f"Mix rendering notice: {str(e)}"}, status=500)
        messages.error(request, f"Mix rendering notice: {str(e)}")
        return redirect('mix_detail', pk=project.id)


@login_required
def mix_detail_view(request, pk):
    """
    Displays the created continuous mix project:
    - Continuous audio player with chapter seeking
    - Full tracklist with exact start timestamps
    - 1-Click Copy YouTube Chapters / Description
    - Direct MP3 & MP4 Downloads
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    project = get_object_or_404(LongMixProject, pk=pk)
    
    return render(request, 'studio/mix_detail.html', {
        'project': project,
    })


@login_required
@require_POST
def mix_delete_view(request, pk):
    """
    Deletes a continuous mix project and its generated media.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    project = get_object_or_404(LongMixProject, pk=pk)
    title = project.title
    project.delete()
    messages.info(request, f"Deleted nonstop mix project '{title}'.")
    return redirect('studio_home')


@login_required
def mix_import_api(request):
    """
    AJAX helper to lookup catalog videos by query.
    """
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)

    query = request.GET.get('q', '').strip()
    videos = Video.objects.filter(is_active=True)
    if query:
        videos = videos.filter(title__icontains=query) | videos.filter(artist__stage_name__icontains=query)

    results = []
    for v in videos[:15]:
        results.append({
            'id': v.id,
            'title': v.title,
            'artist': v.artist.stage_name if v.artist else '',
            'duration': v.duration or '03:30',
            'thumbnail': v.thumbnail_url or '',
            'url': v.video_url or '',
        })

    return JsonResponse({'results': results})


# =========================================================================
# SHORT VIDEO GENERATOR & MULTI-CLIP VIDEO CHOPPER VIEWS
# =========================================================================

@login_required
def shorts_maker_view(request):
    """
    Interactive Short Video Generator & Multi-Clip Chopper UI:
    - Upload long video or audio+cover track
    - Interactive visual timeline with draggable / movable chops
    - 1-Click Auto-Split tools (15s, 30s, 60s)
    - Live Preview Player seeking only chop boundaries
    - Real-time 9:16 vertical phone mockup preview
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    catalog_videos = Video.objects.filter(is_active=True).select_related('artist', 'channel').order_by('-current_views')[:30]

    return render(request, 'studio/shorts_maker.html', {
        'catalog_videos': catalog_videos,
        'source_choices': ShortVideoProject.SourceType.choices,
        'aspect_choices': ShortVideoProject.AspectMode.choices,
        'theme_choices': ShortVideoProject.ThemeStyle.choices,
        'hook_position_choices': ShortVideoProject.HookPosition.choices,
    })


def _execute_shorts_render(project_id, chops, aspect_mode, theme_style, crop_focal_percent, source_type, hook_position='TOP'):
    from django.db import connections
    connections.close_all()
    try:
        project = ShortVideoProject.objects.get(id=project_id)
    except ShortVideoProject.DoesNotExist:
        return

    project_output_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'shorts_output', str(project.id))
    os.makedirs(project_output_dir, exist_ok=True)
    RenderProcessTracker.clear_cancelled('shorts', project.id)

    try:
        RenderProcessTracker.set_progress('shorts', project.id, 5, 'Inspecting media duration...')
        if source_type == ShortVideoProject.SourceType.VIDEO:
            src_path = project.source_video.path
            total_duration = ShortsEngineService.inspect_media_duration(src_path)
        else:
            src_path = project.audio_file.path
            total_duration = ShortsEngineService.inspect_media_duration(src_path)

        project.duration_seconds = total_duration
        project.save(update_fields=['duration_seconds'])

        if not chops:
            chops = ShortsEngineService.generate_chop_splits(total_duration, interval_seconds=15.0)

        import concurrent.futures

        total_chops = max(1, len(chops))
        RenderProcessTracker.set_progress('shorts', project.id, 10, f"Encoding {total_chops} vertical 9:16 chops with FFmpeg...")
        rendered_chops_map = {}
        completed_count = 0
        progress_lock = threading.Lock()

        def _encode_single_chop(idx_and_chop):
            idx, chop = idx_and_chop
            if RenderProcessTracker.is_cancelled('shorts', project.id):
                return None

            chop_id = idx + 1
            chop_title = chop.get('title', f"Part {chop_id}")
            hook_text = chop.get('hook_text', '')
            chop_hook_pos = chop.get('hook_position') or hook_position or getattr(project, 'hook_position', 'TOP') or 'TOP'
            start_s = float(chop.get('start_seconds', 0.0))
            end_s = float(chop.get('end_seconds', start_s + 15.0))
            dur_s = max(1.0, round(end_s - start_s, 2))

            safe_proj_title = "".join(c for c in project.title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_') or f"short_{project.id}"
            out_filename = f"{safe_proj_title}_part{chop_id}.mp4"
            out_path = os.path.join(project_output_dir, out_filename)
            overlay_png = os.path.join(project_output_dir, f"overlay_part{chop_id}.png")

            # Generate overlay if needed
            if theme_style != ShortVideoProject.ThemeStyle.CLEAN or hook_text:
                ShortsEngineService.prepare_overlay_banner(
                    hook_text=hook_text,
                    part_label=f"Part {chop_id}",
                    theme=theme_style,
                    hook_position=chop_hook_pos,
                    width=1080,
                    height=1920,
                    output_png_path=overlay_png
                )
            else:
                overlay_png = None

            # Render chop
            if source_type == ShortVideoProject.SourceType.VIDEO:
                ShortsEngineService.render_video_chop(
                    source_video_path=src_path,
                    output_mp4_path=out_path,
                    start_seconds=start_s,
                    duration_seconds=dur_s,
                    aspect_mode=aspect_mode,
                    overlay_png_path=overlay_png,
                    crop_focal_percent=crop_focal_percent,
                    project_id=project.id
                )
            else:
                ShortsEngineService.render_audio_cover_chop(
                    cover_path=project.cover_image.path,
                    audio_path=src_path,
                    output_mp4_path=out_path,
                    start_seconds=start_s,
                    duration_seconds=dur_s,
                    overlay_png_path=overlay_png,
                    project_id=project.id
                )

            # Cleanup overlay PNG
            if overlay_png and os.path.exists(overlay_png):
                try:
                    os.remove(overlay_png)
                except Exception:
                    pass

            nonlocal completed_count
            with progress_lock:
                completed_count += 1
                pct = int(10 + (completed_count / total_chops) * 80)
                RenderProcessTracker.set_progress(
                    'shorts', project.id, pct,
                    f"Encoded Chop {completed_count}/{total_chops}: '{chop_title}' ({dur_s}s)..."
                )

            rel_url = f"{settings.MEDIA_URL}studio/shorts_output/{project.id}/{out_filename}"
            return idx, {
                'id': chop_id,
                'title': chop_title,
                'hook_text': hook_text,
                'hook_position': chop_hook_pos,
                'start_seconds': start_s,
                'end_seconds': end_s,
                'duration': dur_s,
                'output_file': out_filename,
                'output_url': rel_url,
                'status': 'COMPLETED'
            }, out_path

        max_workers = min(3, max(1, len(chops)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_encode_single_chop, item) for item in enumerate(chops)]
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res:
                    idx, chop_info, out_file_path = res
                    rendered_chops_map[idx] = (chop_info, out_file_path)

        if RenderProcessTracker.is_cancelled('shorts', project.id):
            project.render_status = ShortVideoProject.Status.CANCELLED
            project.error_message = "Rendering cancelled by user."
            project.save()
            return

        rendered_chops = [rendered_chops_map[i][0] for i in range(len(chops)) if i in rendered_chops_map]
        first_output_path = rendered_chops_map[0][1] if 0 in rendered_chops_map else None

        if RenderProcessTracker.is_cancelled('shorts', project.id):
            project.render_status = ShortVideoProject.Status.CANCELLED
            project.error_message = "Rendering cancelled by user."
            project.save()
            return

        # Link first rendered chop to project.output_video
        if first_output_path and os.path.exists(first_output_path):
            with open(first_output_path, 'rb') as f:
                project.output_video.save(os.path.basename(first_output_path), f, save=False)

        project.chops_data = rendered_chops
        project.chop_count = len(rendered_chops)
        project.render_status = ShortVideoProject.Status.COMPLETED
        project.save()
        RenderProcessTracker.set_progress('shorts', project.id, 100, f"All {len(rendered_chops)} vertical shorts ready!")

    except Exception as e:
        if RenderProcessTracker.is_cancelled('shorts', project.id):
            project.render_status = ShortVideoProject.Status.CANCELLED
            project.error_message = "Rendering cancelled by user."
        else:
            project.render_status = ShortVideoProject.Status.FAILED
            project.error_message = str(e)
        project.save()
    finally:
        connections.close_all()


@login_required
@require_POST
def shorts_render_view(request):
    """
    Handles form submission to render 9:16 vertical shorts from video or audio.
    Supports asynchronous execution with live progress polling and cancellation.
    """
    if not request.user.is_super_admin:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    title = request.POST.get('title', '').strip() or 'My Viral Shorts'
    source_type = request.POST.get('source_type', ShortVideoProject.SourceType.VIDEO)
    aspect_mode = request.POST.get('aspect_mode', ShortVideoProject.AspectMode.BLURRED_FIT)
    crop_focal_percent = int(request.POST.get('crop_focal_percent', 50))
    theme_style = request.POST.get('theme_style', ShortVideoProject.ThemeStyle.VIRAL_HOOK)
    hook_position = request.POST.get('hook_position', ShortVideoProject.HookPosition.TOP)

    source_video = request.FILES.get('source_video')
    audio_file = request.FILES.get('audio_file')
    cover_image = request.FILES.get('cover_image')
    chops_json = request.POST.get('chops_json', '').strip()

    if source_type == ShortVideoProject.SourceType.VIDEO and not source_video:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Please upload a video file (.mp4, .mov, .mkv).'}, status=400)
        messages.error(request, "Please upload a video file (.mp4, .mov, .mkv).")
        return redirect('shorts_maker')

    if source_type == ShortVideoProject.SourceType.AUDIO_COVER and (not audio_file or not cover_image):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Please upload both an audio file and a cover image.'}, status=400)
        messages.error(request, "Please upload both an audio file and a cover image.")
        return redirect('shorts_maker')

    # Parse chops
    chops = []
    if chops_json:
        try:
            parsed = json.loads(chops_json)
            if isinstance(parsed, list) and len(parsed) > 0:
                chops = parsed
        except Exception:
            pass

    # Create project record
    project = ShortVideoProject.objects.create(
        title=title,
        source_type=source_type,
        source_video=source_video,
        audio_file=audio_file,
        cover_image=cover_image,
        aspect_mode=aspect_mode,
        crop_focal_percent=crop_focal_percent,
        theme_style=theme_style,
        hook_position=hook_position,
        chops_data=chops,
        render_status=ShortVideoProject.Status.RENDERING
    )

    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('async') == '1'

    if is_ajax:
        # Launch render in daemon worker thread
        thread = threading.Thread(
            target=_execute_shorts_render,
            args=(project.id, chops, aspect_mode, theme_style, crop_focal_percent, source_type, hook_position),
            daemon=True
        )
        thread.start()

        return JsonResponse({
            'success': True,
            'project_id': project.id,
            'project_type': 'shorts',
            'title': project.title,
            'redirect_url': reverse('shorts_detail', kwargs={'pk': project.id})
        })

    # Synchronous execution fallback for direct POST
    _execute_shorts_render(project.id, chops, aspect_mode, theme_style, crop_focal_percent, source_type, hook_position)
    project.refresh_from_db()
    if project.render_status == ShortVideoProject.Status.COMPLETED:
        messages.success(request, f"Successfully created {project.chop_count} vertical 9:16 shorts for '{project.title}'!")
    elif project.render_status == ShortVideoProject.Status.CANCELLED:
        messages.warning(request, f"Rendering was cancelled for '{project.title}'.")
    else:
        messages.error(request, f"Shorts generation error: {project.error_message}")
    return redirect('shorts_detail', pk=project.id)


@login_required
def shorts_detail_view(request, pk):
    """
    Detail page for a Shorts & Reels project:
    - Visual gallery of 9:16 vertical clips
    - Built-in vertical player with part selection
    - 1-Click Copy Title, SEO Description & Tags for each chop
    - Individual and All-in-One Download buttons
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    project = get_object_or_404(ShortVideoProject, pk=pk)
    
    # Prepare enriched chop items with copyable titles and descriptions
    enriched_chops = []
    for idx, c in enumerate(project.chops_data or []):
        c_copy = dict(c)
        c_copy['youtube_title'] = project.youtube_title_for_chop(idx)
        c_copy['youtube_description'] = project.youtube_description_for_chop(idx)
        enriched_chops.append(c_copy)

    return render(request, 'studio/short_detail.html', {
        'project': project,
        'chops': enriched_chops,
    })


@login_required
@require_POST
def shorts_delete_view(request, pk):
    """
    Deletes a short video project and cleans up its media files.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    project = get_object_or_404(ShortVideoProject, pk=pk)
    title = project.title
    
    # Cleanup output folder
    project_output_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'shorts_output', str(project.id))
    if os.path.exists(project_output_dir):
        try:
            shutil.rmtree(project_output_dir)
        except Exception:
            pass

    project.delete()
    messages.info(request, f"Deleted shorts project '{title}'.")
    return redirect('studio_home')


@login_required
def shorts_export_zip_view(request, pk):
    """
    Downloads all rendered vertical chops in a single ZIP archive.
    """
    if not request.user.is_super_admin:
        return HttpResponse("Access denied", status=403)

    project = get_object_or_404(ShortVideoProject, pk=pk)
    project_output_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'shorts_output', str(project.id))

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for chop in project.chops_data or []:
            fn = chop.get('output_file')
            if fn:
                full_path = os.path.join(project_output_dir, fn)
                if os.path.exists(full_path):
                    zip_file.write(full_path, arcname=fn)

    zip_buffer.seek(0)
    safe_title = "".join(c for c in project.title if c.isalnum() or c in (' ', '_', '-')).rstrip().replace(' ', '_')
    response = HttpResponse(zip_buffer.read(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{safe_title}_Shorts_Package.zip"'
    return response


# ==========================================
# LYRICS VIDEO STUDIO VIEWS
# ==========================================

@login_required
def lyrics_maker_view(request):
    """
    Interactive Studio for creating synchronized lyric videos:
    - Audio (.mp3, .wav) + Cover Art / Video Background OR Direct Video File (.mp4, .mov)
    - Interactive Tap-to-Sync (Spacebar rhythmic capture) with live video playback
    - Import / Export .LRC files or paste plain lyrics
    - Multi-style typography (Karaoke Wipe, Rolling 3-Line, Cyber Neon, Cinematic Minimal)
    - 16:9 Landscape & 9:16 Vertical formats
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    edit_id = request.GET.get('edit')
    edit_project = None
    if edit_id:
        try:
            edit_project = LyricVideoProject.objects.get(pk=edit_id)
        except LyricVideoProject.DoesNotExist:
            pass

    return render(request, 'studio/lyrics_maker.html', {
        'edit_project': edit_project,
        'source_types': LyricVideoProject.SourceType.choices,
        'animation_styles': LyricVideoProject.AnimationStyle.choices,
        'aspect_ratios': LyricVideoProject.AspectRatio.choices,
    })


@login_required
@require_POST
def lyrics_render_view(request):
    """
    Handles form submission to render a synchronized lyric video with FFmpeg.
    Supports both Audio Track + Cover Art and direct Video File inputs.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    title = request.POST.get('title', '').strip() or 'My Song Lyrics'
    artist_name = request.POST.get('artist_name', '').strip()
    source_type = request.POST.get('source_type', LyricVideoProject.SourceType.AUDIO_IMAGE)
    animation_style = request.POST.get('animation_style', LyricVideoProject.AnimationStyle.KARAOKE_WIPE)
    aspect_ratio = request.POST.get('aspect_ratio', LyricVideoProject.AspectRatio.LANDSCAPE_16_9)
    font_family = request.POST.get('font_family', 'Arial').strip()
    font_size = int(request.POST.get('font_size', 48))
    highlight_color = request.POST.get('highlight_color', '#00E5FF').strip()
    text_color = request.POST.get('text_color', '#FFFFFF').strip()
    position_mode = request.POST.get('position_mode', 'CENTER').strip()
    cover_layout = request.POST.get('cover_layout', LyricVideoProject.CoverLayout.AMBIENT).strip()
    # Sliders send integer percentages (e.g. 100, 150) — divide by 100 for the
    # PIL/renderer which expects decimal multipliers (1.0, 1.5).
    cover_size       = float(request.POST.get('cover_size')       or 100) / 100.0
    cover_blur       = float(request.POST.get('cover_blur')       or 8.0)   # already px, no conversion
    cover_opacity    = float(request.POST.get('cover_opacity')    or 100) / 100.0
    cover_offset     = float(request.POST.get('cover_offset')     or 0.0)   # px offset, no conversion
    cover_brightness = float(request.POST.get('cover_brightness') or 100) / 100.0
    cover_contrast   = float(request.POST.get('cover_contrast')   or 100) / 100.0
    cover_saturation = float(request.POST.get('cover_saturation') or 100) / 100.0
    cover_vignette   = float(request.POST.get('cover_vignette')   or 0.0)  / 100.0

    # Advanced Typography Parameters
    font_weight = request.POST.get('font_weight', 'bold').strip()
    font_italic = request.POST.get('font_italic') == 'true'
    letter_spacing = float(request.POST.get('letter_spacing') or 0.0)
    line_height = float(request.POST.get('line_height') or 1.4)
    text_transform = request.POST.get('text_transform', 'none').strip()
    text_stroke_width = float(request.POST.get('text_stroke_width') or 2.5)
    text_shadow_depth = float(request.POST.get('text_shadow_depth') or 2.0)
    font_scale_x = int(request.POST.get('font_scale_x') or 100)
    font_scale_y = int(request.POST.get('font_scale_y') or 100)
    bg_opacity = int(request.POST.get('bg_opacity') or 0)
    time_offset = float(request.POST.get('time_offset') or 0.0)

    lyrics_raw_text = request.POST.get('lyrics_raw_text', '').strip()

    audio_file = request.FILES.get('audio_file')
    replacement_audio = request.FILES.get('replacement_audio') or request.FILES.get('replacement_audio_file')
    video_file = request.FILES.get('video_file') or request.FILES.get('background_video')
    background_image = request.FILES.get('background_image')
    lrc_file = request.FILES.get('lrc_file')
    lyrics_data_raw = request.POST.get('lyrics_data', '').strip()

    # If video file uploaded or mode is video, adjust source_type
    if video_file:
        source_type = LyricVideoProject.SourceType.VIDEO
        final_audio = replacement_audio
    else:
        source_type = LyricVideoProject.SourceType.AUDIO_IMAGE
        final_audio = audio_file

    if not final_audio and not video_file:
        messages.error(request, "Please upload an audio file (.mp3, .wav) or a video file (.mp4, .mov).")
        return redirect('lyrics_maker')

    # Parse or build lyrics data list
    lyrics_data = []
    if lyrics_data_raw:
        try:
            lyrics_data = json.loads(lyrics_data_raw)
        except Exception:
            lyrics_data = []

    if not lyrics_data and lrc_file:
        try:
            lrc_text = lrc_file.read().decode('utf-8', errors='ignore')
            lyrics_data = LyricsEngineService.parse_lrc_file(lrc_text)
        except Exception:
            pass

def _execute_lyrics_render(project_id, title, artist_name, loop_video):
    from django.db import connections
    connections.close_all()
    try:
        project = LyricVideoProject.objects.get(id=project_id)
    except LyricVideoProject.DoesNotExist:
        return

    lyrics_output_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'lyrics_output')
    os.makedirs(lyrics_output_dir, exist_ok=True)
    safe_title = "".join(c for c in (title or project.title) if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
    safe_artist = "".join(c for c in (artist_name or project.artist_name) if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
    prefix = f"{safe_artist}_{safe_title}" if (safe_artist and safe_title) else (safe_title or f"lyrics_{project.id}")
    out_filename = f"{prefix}_lyrics_{project.id}.mp4"
    out_path = os.path.join(lyrics_output_dir, out_filename)
    RenderProcessTracker.clear_cancelled('lyrics', project.id)

    try:
        audio_path = project.audio_file.path if project.audio_file else None
        bg_vid_path = project.background_video.path if project.background_video else None
        bg_img_path = project.background_image.path if project.background_image else None

        primary_media = audio_path or bg_vid_path
        duration = LyricsEngineService.inspect_media_duration(primary_media) if primary_media else 180.0

        lyrics_data = project.lyrics_data
        if not lyrics_data and project.lyrics_raw_text:
            lyrics_data = LyricsEngineService.auto_distribute_raw_lyrics(project.lyrics_raw_text, total_duration=duration)
            project.lyrics_data = lyrics_data
            project.save(update_fields=['lyrics_data'])

        render_res = LyricsEngineService.render_lyrics_video(
            audio_path=audio_path,
            background_image_path=bg_img_path,
            background_video_path=bg_vid_path,
            lyrics_data=lyrics_data,
            output_video_path=out_path,
            aspect_ratio=project.aspect_ratio,
            animation_style=project.animation_style,
            font_family=project.font_family,
            font_size=project.font_size,
            highlight_color=project.highlight_color,
            text_color=project.text_color,
            position_mode=project.position_mode,
            cover_layout=project.cover_layout,
            cover_size=project.cover_size,
            cover_blur=project.cover_blur,
            cover_opacity=project.cover_opacity,
            cover_offset=project.cover_offset,
            cover_brightness=project.cover_brightness,
            cover_contrast=project.cover_contrast,
            cover_saturation=project.cover_saturation,
            cover_vignette=project.cover_vignette,
            font_weight=project.font_weight,
            font_italic=project.font_italic,
            letter_spacing=project.letter_spacing,
            line_height=project.line_height,
            text_transform=project.text_transform,
            text_stroke_width=project.text_stroke_width,
            text_shadow_depth=project.text_shadow_depth,
            font_scale_x=project.font_scale_x,
            font_scale_y=project.font_scale_y,
            bg_opacity=project.bg_opacity,
            title=title,
            artist=artist_name,
            loop_video=loop_video,
            project_id=project.id,
            time_offset=project.time_offset,
        )

        if RenderProcessTracker.is_cancelled('lyrics', project.id):
            project.render_status = LyricVideoProject.Status.CANCELLED
            project.error_message = "Rendering cancelled by user."
            project.save()
            return

        project.output_video.name = f"studio/lyrics_output/{out_filename}"
        project.duration_seconds = render_res.get('duration', duration)
        project.render_status = LyricVideoProject.Status.COMPLETED
        project.save()
        RenderProcessTracker.set_progress('lyrics', project.id, 100, "1080p Lyric Video ready!")

    except Exception as e:
        if RenderProcessTracker.is_cancelled('lyrics', project.id):
            project.render_status = LyricVideoProject.Status.CANCELLED
            project.error_message = "Rendering cancelled by user."
        else:
            project.render_status = LyricVideoProject.Status.FAILED
            project.error_message = str(e)
        project.save()
    finally:
        connections.close_all()


@login_required
@require_POST
def lyrics_render_view(request):
    """
    Handles form submission to render a 1080p Synced Lyric Video.
    Supports asynchronous execution with live progress polling and cancellation.
    """
    if not request.user.is_super_admin:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    title = request.POST.get('title', '').strip() or 'My Synced Lyrics Video'
    artist_name = request.POST.get('artist_name', '').strip()
    animation_style = request.POST.get('animation_style', LyricVideoProject.AnimationStyle.KARAOKE_WIPE)
    aspect_ratio = request.POST.get('aspect_ratio', LyricVideoProject.AspectRatio.LANDSCAPE_16_9)
    font_family = request.POST.get('font_family', 'Arial').strip()
    font_size = int(request.POST.get('font_size', 48))
    highlight_color = request.POST.get('highlight_color', '#00E5FF').strip()
    text_color = request.POST.get('text_color', '#FFFFFF').strip()
    position_mode = request.POST.get('position_mode', LyricVideoProject.PositionMode.CENTER)
    cover_layout     = request.POST.get('cover_layout', LyricVideoProject.CoverLayout.AMBIENT)
    cover_size       = float(request.POST.get('cover_size')       or 100) / 100.0
    cover_blur       = float(request.POST.get('cover_blur')       or 8.0)
    cover_opacity    = float(request.POST.get('cover_opacity')    or 100) / 100.0
    cover_offset     = float(request.POST.get('cover_offset')     or 0.0)
    cover_brightness = float(request.POST.get('cover_brightness') or 100) / 100.0
    cover_contrast   = float(request.POST.get('cover_contrast')   or 100) / 100.0
    cover_saturation = float(request.POST.get('cover_saturation') or 100) / 100.0
    cover_vignette   = float(request.POST.get('cover_vignette')   or 0.0)  / 100.0

    # Advanced typography settings
    font_weight = request.POST.get('font_weight', 'bold').strip()
    font_italic = request.POST.get('font_italic') in ('on', 'true', '1', True)
    letter_spacing = float(request.POST.get('letter_spacing') or 0.0)
    line_height = float(request.POST.get('line_height') or 1.4)
    text_transform = request.POST.get('text_transform', 'none').strip()
    text_stroke_width = float(request.POST.get('text_stroke_width') or 2.5)
    text_shadow_depth = float(request.POST.get('text_shadow_depth') or 2.0)
    font_scale_x = int(request.POST.get('font_scale_x') or 100)
    font_scale_y = int(request.POST.get('font_scale_y') or 100)
    bg_opacity = int(request.POST.get('bg_opacity') or 0)
    time_offset = float(request.POST.get('time_offset') or 0.0)

    lyrics_raw_text = request.POST.get('lyrics_raw_text', '').strip()

    audio_file = request.FILES.get('audio_file')
    replacement_audio = request.FILES.get('replacement_audio') or request.FILES.get('replacement_audio_file')
    video_file = request.FILES.get('video_file') or request.FILES.get('background_video')
    background_image = request.FILES.get('background_image')
    lrc_file = request.FILES.get('lrc_file')
    lyrics_data_raw = request.POST.get('lyrics_data', '').strip()

    # If video file uploaded or mode is video, adjust source_type
    if video_file:
        source_type = LyricVideoProject.SourceType.VIDEO
        final_audio = replacement_audio
    else:
        source_type = LyricVideoProject.SourceType.AUDIO_IMAGE
        final_audio = audio_file

    if not final_audio and not video_file:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'error': 'Please upload an audio file (.mp3, .wav) or a video file (.mp4, .mov).'}, status=400)
        messages.error(request, "Please upload an audio file (.mp3, .wav) or a video file (.mp4, .mov).")
        return redirect('lyrics_maker')

    # Parse or build lyrics data list
    lyrics_data = []
    if lyrics_data_raw:
        try:
            lyrics_data = json.loads(lyrics_data_raw)
        except Exception:
            lyrics_data = []

    if not lyrics_data and lrc_file:
        try:
            lrc_text = lrc_file.read().decode('utf-8', errors='ignore')
            lyrics_data = LyricsEngineService.parse_lrc_file(lrc_text)
        except Exception:
            pass

    if not lyrics_data and lyrics_raw_text:
        lyrics_data = LyricsEngineService.auto_distribute_raw_lyrics(lyrics_raw_text, total_duration=180.0)

    loop_video = request.POST.get('loop_video') in ('on', 'true', '1', True) or ('loop_video' not in request.POST)

    # Create project record
    project = LyricVideoProject.objects.create(
        title=title,
        artist_name=artist_name,
        source_type=source_type,
        audio_file=final_audio,
        background_image=background_image,
        background_video=video_file,
        lyrics_raw_text=lyrics_raw_text,
        lyrics_data=lyrics_data,
        animation_style=animation_style,
        aspect_ratio=aspect_ratio,
        font_family=font_family,
        font_size=font_size,
        highlight_color=highlight_color,
        text_color=text_color,
        position_mode=position_mode,
        cover_layout=cover_layout,
        cover_size=cover_size,
        cover_blur=cover_blur,
        cover_opacity=cover_opacity,
        cover_offset=cover_offset,
        cover_brightness=cover_brightness,
        cover_contrast=cover_contrast,
        cover_saturation=cover_saturation,
        cover_vignette=cover_vignette,
        font_weight=font_weight,
        font_italic=font_italic,
        letter_spacing=letter_spacing,
        line_height=line_height,
        text_transform=text_transform,
        text_stroke_width=text_stroke_width,
        text_shadow_depth=text_shadow_depth,
        font_scale_x=font_scale_x,
        font_scale_y=font_scale_y,
        bg_opacity=bg_opacity,
        time_offset=time_offset,
        render_status=LyricVideoProject.Status.RENDERING
    )

    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('async') == '1'

    if is_ajax:
        # Launch render in daemon worker thread
        thread = threading.Thread(
            target=_execute_lyrics_render,
            args=(project.id, title, artist_name, loop_video),
            daemon=True
        )
        thread.start()

        return JsonResponse({
            'success': True,
            'project_id': project.id,
            'project_type': 'lyrics',
            'title': project.title,
            'redirect_url': reverse('lyrics_detail', kwargs={'pk': project.id})
        })

    # Synchronous execution fallback for direct POST
    _execute_lyrics_render(project.id, title, artist_name, loop_video)
    project.refresh_from_db()
    if project.render_status == LyricVideoProject.Status.COMPLETED:
        messages.success(request, f"Successfully rendered 1080p Lyric Video for '{project.title}'!")
    elif project.render_status == LyricVideoProject.Status.CANCELLED:
        messages.warning(request, f"Rendering was cancelled for '{project.title}'.")
    else:
        messages.error(request, f"Lyric video generation error: {project.error_message}")
    return redirect('lyrics_detail', pk=project.id)


@login_required
def lyrics_detail_view(request, pk):
    """
    Detail page for a Lyric Video project:
    - HD 1080p Video Player with aspect ratio adaptation
    - 1-Click Download MP4 & Export Synced .LRC
    - Ready-to-copy YouTube Title & Description with complete lyrics
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    project = get_object_or_404(LyricVideoProject, pk=pk)

    # (value, label, dot_color_class, sub_label) — mirrors the 11 preset cards in lyrics_maker.html
    animation_style_cards = [
        # Tier 1 · Core (4 cards)
        ('KARAOKE_WIPE',    'Karaoke Wipe',    'bg-emerald-400', 'Classic'),
        ('ROLLING_3LINE',   'Rolling 3-Line',  'bg-zinc-600',    'Smooth'),
        ('PLAYFUL_POP',     'Playful Pop',     'bg-pink-400',    'Bouncy'),
        ('BUBBLE_BOUNCE',   'Bubble Bounce',   'bg-cyan-400',    'Bubbly'),
        # Tier 2 · Creative (4 cards)
        ('DREAMY_DRIFT',    'Dreamy Drift',    'bg-violet-400',  'Soft'),
        ('NEON_GLOW',       'Neon Glow',       'bg-cyan-400',    'Electric'),
        ('HANDWRITTEN_INK', 'Handwritten Ink', 'bg-amber-400',   'Personal'),
        ('RETRO_VHS',       'Retro VHS',       'bg-rose-400',    'Nostalgic'),
        # Tier 3 · Signature (3 cards, last grid row padded with empty divs in maker)
        ('CINEMATIC',       'Cinematic Serif', 'bg-amber-400',   'Epic'),
        ('VINTAGE_COUNTRY', 'Vintage Country', 'bg-orange-400',  'Warm'),
    ]

    return render(request, 'studio/lyrics_detail.html', {
        'project': project,
        'lrc_content': project.lrc_content,
        'youtube_title': project.youtube_title,
        'youtube_description': project.youtube_description,
        'animation_style_cards': animation_style_cards,
    })


@login_required
@require_POST
def lyrics_delete_view(request, pk):
    """
    Deletes a lyric video project and associated media files.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    project = get_object_or_404(LyricVideoProject, pk=pk)
    title = project.title

    # Delete output video if exists
    if project.output_video and os.path.exists(project.output_video.path):
        try:
            os.remove(project.output_video.path)
        except Exception:
            pass

    project.delete()
    messages.info(request, f"Deleted lyric video project '{title}'.")
    return redirect('studio_home')


@login_required
def lyrics_export_lrc_view(request, pk):
    """
    Direct download of the synchronized .LRC file.
    """
    if not request.user.is_super_admin:
        return HttpResponse("Access denied", status=403)

    project = get_object_or_404(LyricVideoProject, pk=pk)
    lrc_content = project.lrc_content
    safe_title = "".join(c for c in project.title if c.isalnum() or c in (' ', '_', '-')).rstrip().replace(' ', '_')
    
    response = HttpResponse(lrc_content, content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{safe_title}.lrc"'
    return response


@login_required
def lyrics_download_video_view(request, pk):
    """
    Downloads the rendered 1080p lyric video with the inputted song title filename.
    """
    if not request.user.is_super_admin:
        return HttpResponse("Access denied", status=403)

    project = get_object_or_404(LyricVideoProject, pk=pk)
    if not project.output_video or not os.path.exists(project.output_video.path):
        messages.error(request, "Output video file not found on disk.")
        return redirect('lyrics_detail', pk=pk)

    filename = project.export_filename
    response = FileResponse(open(project.output_video.path, 'rb'), content_type='video/mp4')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_POST
def lyrics_update_data_api(request, pk):
    """
    Updates the synchronized lyrics JSON data and spellings for an existing project.
    """
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)

    project = get_object_or_404(LyricVideoProject, pk=pk)
    lyrics_json = request.POST.get('lyrics_data', '').strip()
    title = request.POST.get('title', '').strip()
    artist_name = request.POST.get('artist_name', '').strip()

    if title:
        project.title = title
    if artist_name is not None:
        project.artist_name = artist_name

    if lyrics_json:
        try:
            parsed = json.loads(lyrics_json)
            if isinstance(parsed, list):
                project.lyrics_data = parsed
        except Exception as e:
            return JsonResponse({'error': f'Invalid lyrics JSON: {str(e)}'}, status=400)

    project.save()
    return JsonResponse({
        'success': True,
        'message': f"Lyrics updated successfully for '{project.title}'!",
        'title': project.title,
        'artist_name': project.artist_name,
        'export_filename': project.export_filename,
        'youtube_title': project.youtube_title,
        'youtube_description': project.youtube_description
    })


@login_required
def shorts_download_chop_view(request, pk, chop_idx):
    """
    Downloads a specific chop clip from a shorts project with the project/song title.
    """
    if not request.user.is_super_admin:
        return HttpResponse("Access denied", status=403)

    project = get_object_or_404(ShortVideoProject, pk=pk)
    chops = project.chops_data or []
    if chop_idx < 0 or chop_idx >= len(chops):
        return HttpResponse("Chop not found", status=404)

    chop = chops[chop_idx]
    out_file = chop.get('output_file')
    if not out_file:
        return HttpResponse("Output video not ready", status=404)

    full_path = os.path.join(settings.MEDIA_ROOT, 'studio', 'shorts_output', str(project.id), out_file)
    if not os.path.exists(full_path):
        return HttpResponse("File not found on server", status=404)

    filename = project.export_filename_for_chop(chop_idx)
    response = FileResponse(open(full_path, 'rb'), content_type='video/mp4')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_POST
def lyrics_parse_api(request):
    """
    Helper API for client-side Tap-to-Sync: parses raw text or uploaded .LRC file and returns structured JSON.
    """
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    raw_text = request.POST.get('raw_text', '').strip()
    lrc_file = request.FILES.get('lrc_file')
    duration = float(request.POST.get('duration', 180.0))

    if lrc_file:
        try:
            content = lrc_file.read().decode('utf-8', errors='ignore')
            parsed = LyricsEngineService.parse_lrc_file(content)
            return JsonResponse({'success': True, 'lyrics_data': parsed})
        except Exception as e:
            return JsonResponse({'error': f"Failed to parse LRC file: {str(e)}"}, status=400)

    if raw_text:
        # Check if raw text looks like LRC format
        if '[' in raw_text and ']' in raw_text:
            parsed = LyricsEngineService.parse_lrc_file(raw_text)
            if parsed:
                return JsonResponse({'success': True, 'lyrics_data': parsed})

        # Plain text: auto-distribute lines
        distributed = LyricsEngineService.auto_distribute_raw_lyrics(raw_text, total_duration=duration)
        return JsonResponse({'success': True, 'lyrics_data': distributed})

    return JsonResponse({'error': 'No lyrics provided'}, status=400)


@login_required
@require_POST
def lyrics_vocal_sync_api(request):
    """
    Automatic Vocal Frequency Alignment API:
    Isolates vocal frequency bandpass (300Hz-3400Hz), detects singing vs instrumental breaks,
    and automatically snaps text lines to the detected vocal phrase timestamps.
    Accepts audio files (.mp3, .wav) or video files (.mp4, .mov, .webm, .mkv).
    """
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    raw_text = request.POST.get('raw_text', '').strip()
    media_file = request.FILES.get('replacement_audio') or request.FILES.get('audio_file') or request.FILES.get('video_file') or request.FILES.get('background_video')
    duration = float(request.POST.get('duration', 180.0))

    if not raw_text:
        return JsonResponse({'error': 'Please provide song lyrics text to sync.'}, status=400)

    if not media_file:
        # Fallback to even distribution if no audio/video file uploaded yet
        distributed = LyricsEngineService.auto_distribute_raw_lyrics(raw_text, total_duration=duration)
        return JsonResponse({
            'success': True,
            'lyrics_data': distributed,
            'is_vocal_detected': False,
            'message': 'No audio or video file uploaded yet. Lyrics spaced evenly across estimated duration.'
        })

    import tempfile
    ext = os.path.splitext(media_file.name)[1] or '.wav'
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
        for chunk in media_file.chunks():
            tf.write(chunk)
        temp_media_path = tf.name

    try:
        total_dur = LyricsEngineService.inspect_media_duration(temp_media_path)
        vocal_segments = LyricsEngineService.detect_vocal_segments(temp_media_path)
        aligned = LyricsEngineService.align_lyrics_with_vocal_segments(raw_text, vocal_segments, total_dur)

        return JsonResponse({
            'success': True,
            'lyrics_data': aligned,
            'vocal_segments': vocal_segments,
            'is_vocal_detected': True,
            'duration': total_dur,
            'message': f'Successfully detected {len(vocal_segments)} vocal phrases and auto-aligned {len(aligned)} lyric lines!'
        })
    except Exception as e:
        # Fallback gracefully to auto-distribute
        distributed = LyricsEngineService.auto_distribute_raw_lyrics(raw_text, total_duration=duration)
        return JsonResponse({
            'success': True,
            'lyrics_data': distributed,
            'is_vocal_detected': False,
            'message': f'Frequency detector fallback: {str(e)}'
        })
    finally:
        if os.path.exists(temp_media_path):
            try:
                os.remove(temp_media_path)
            except Exception:
                pass


@login_required
def lyrics_online_search_api(request):
    """
    1-Click Free Synced Lyrics Database Search (LRCLIB API):
    Queries millions of public synchronized tracks by Title and Artist.
    """
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    title = request.GET.get('title', '') or request.POST.get('title', '')
    artist = request.GET.get('artist', '') or request.POST.get('artist', '')

    result = LyricsEngineService.fetch_online_synced_lyrics(title, artist)
    status_code = 200 if result.get('success') else 404
    return JsonResponse(result, status=status_code)


@login_required
@require_POST
def lyrics_ai_transcribe_api(request):
    """
    AI Lyrics Transcription & Millisecond Timestamp Sync API (Local faster-whisper):
    Takes audio file (.mp3, .wav) or video file (.mp4, .mov, .webm, .mkv),
    transcribes singing/speech, and extracts exact word/line timestamps.
    """
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    media_file = request.FILES.get('replacement_audio') or request.FILES.get('audio_file') or request.FILES.get('video_file') or request.FILES.get('background_video')
    if not media_file:
        return JsonResponse({'error': 'Please select or upload an audio or video file first.'}, status=400)

    model_size = request.POST.get('model_size', 'base').strip() or 'base'
    initial_prompt = request.POST.get('initial_prompt', '').strip() or None
    use_demucs = request.POST.get('use_demucs', '').lower() in ('1', 'true', 'yes')

    import tempfile
    ext = os.path.splitext(media_file.name)[1] or '.wav'
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
        for chunk in media_file.chunks():
            tf.write(chunk)
        temp_media_path = tf.name

    try:
        res = LyricsEngineService.transcribe_and_sync_with_whisper(
            temp_media_path,
            model_size=model_size,
            initial_prompt=initial_prompt,
            use_demucs=use_demucs
        )
        line_count = len(res.get('lyrics_data', []))
        model_used = res.get('whisper_model', model_size)

        if use_demucs:
            if res.get('demucs_applied'):
                res['message'] = (
                    f"Transcribed {line_count} lines using Demucs vocal separation "
                    f"+ Whisper {model_used}."
                )
            else:
                # Demucs ran but failed — user needs to know
                err = res.get('demucs_error') or 'unknown error'
                res['message'] = (
                    f"Transcribed {line_count} lines with Whisper {model_used} "
                    f"(Demucs separation FAILED — fell back to full mix). "
                    f"Reason: {err}"
                )
                res['success'] = True  # still return lyrics, just with a warning
        else:
            res['message'] = (
                f"Transcribed {line_count} lines with Whisper {model_used}."
            )
        return JsonResponse(res)
    except Exception as e:
        return JsonResponse({'error': f"AI Transcription error: {str(e)}"}, status=500)
    finally:
        if os.path.exists(temp_media_path):
            try:
                os.remove(temp_media_path)
            except Exception:
                pass


@login_required
@require_POST
def studio_cancel_render_view(request, project_type, pk):
    """
    Cancels an in-progress video rendering job for any project type
    (shorts, lyrics, mix, loop).
    """
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)

    model_map = {
        'shorts': ShortVideoProject,
        'lyrics': LyricVideoProject,
        'mix': LongMixProject,
        'loop': VideoProject,
    }

    model_cls = model_map.get(project_type.lower())
    if not model_cls:
        return JsonResponse({'error': 'Invalid project type.'}, status=400)

    try:
        project = model_cls.objects.get(pk=pk)
        RenderProcessTracker.cancel(project_type, pk)
        project.render_status = getattr(model_cls.Status, 'CANCELLED', 'CANCELLED')
        project.error_message = "Rendering cancelled by user."
        project.save()

        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.content_type == 'application/json':
            return JsonResponse({'success': True, 'message': f'Rendering for project #{pk} cancelled.'})

        messages.info(request, f"Cancelled rendering for '{project.title}'.")
        return redirect('studio_home')
    except model_cls.DoesNotExist:
        return JsonResponse({'error': 'Project not found.'}, status=404)


@login_required
def studio_render_progress_view(request, project_type, pk):
    """
    Live API polling endpoint:
    Returns the real-time render percentage, current step description, elapsed seconds,
    and completion redirect URL for any active studio project.
    """
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Super Admin privileges required.'}, status=403)

    model_map = {
        'shorts': (ShortVideoProject, 'shorts_detail'),
        'lyrics': (LyricVideoProject, 'lyrics_detail'),
        'mix': (LongMixProject, 'mix_detail'),
        'loop': (VideoProject, 'studio_home'),
    }

    config = model_map.get(str(project_type).lower())
    if not config:
        return JsonResponse({'error': 'Invalid project type.'}, status=400)

    model_cls, detail_url_name = config
    try:
        project = model_cls.objects.get(pk=pk)
    except model_cls.DoesNotExist:
        return JsonResponse({'error': 'Project not found.'}, status=404)

    progress_info = RenderProcessTracker.get_progress(project_type, pk)
    elapsed = round(time.time() - progress_info.get('started_at', time.time()), 1)

    status = project.render_status
    if RenderProcessTracker.is_cancelled(project_type, pk):
        status = 'CANCELLED'

    redirect_url = reverse(detail_url_name, kwargs={'pk': pk}) if detail_url_name != 'studio_home' else reverse('studio_home')

    return JsonResponse({
        'success': True,
        'project_id': project.id,
        'project_type': project_type,
        'title': project.title,
        'status': status,
        'progress': 100 if status == 'COMPLETED' else progress_info.get('pct', 0),
        'step': progress_info.get('step', 'Processing video frames...'),
        'elapsed_seconds': elapsed,
        'error_message': getattr(project, 'error_message', ''),
        'redirect_url': redirect_url
    })








# =========================================================================
# AUTOMATION PIPELINE VIEWS
# =========================================================================

@login_required
def automation_dashboard_view(request):
    """Lists all AutomationPipeline objects."""
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    from .models import AutomationPipeline
    from apps.publishing.models import YouTubeOAuthAccount

    pipelines = AutomationPipeline.objects.all()
    oauth_accounts = YouTubeOAuthAccount.objects.filter(is_active=True)
    default_account = oauth_accounts.filter(is_default=True).first() or oauth_accounts.first()

    # Check if faster-whisper is installed
    try:
        import faster_whisper  # noqa: F401
        whisper_available = True
    except ImportError:
        whisper_available = False

    return render(request, 'studio/automation.html', {
        'pipelines': pipelines,
        'oauth_accounts': oauth_accounts,
        'default_account': default_account,
        'whisper_available': whisper_available,
    })


@login_required
def automation_create_view(request):
    """GET: show builder form. POST: create pipeline and immediately run it."""
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    if request.method == 'POST':
        import uuid as _uuid
        from .models import AutomationPipeline
        from .services.automation_runner import AutomationRunnerService

        name = request.POST.get('name', '').strip() or 'My Automation'

        # Global pipeline step flags (from Step 1 panel)
        do_render  = request.POST.get('do_render',  '1') in ('1', 'true', 'on')
        do_publish = request.POST.get('do_publish', '1') in ('1', 'true', 'on')

        # ── Collect per-song configs ──────────────────────────────────────
        # Form sends: songs[0][url], songs[0][audio_format], songs[0][background_image], …
        # Detect how many song slots were submitted by scanning the POST keys.
        import re as _re
        song_indices = sorted({
            int(m.group(1))
            for k in request.POST.keys()
            for m in [_re.match(r'^songs\[(\d+)\]', k)]
            if m
        })

        if not song_indices:
            messages.error(request, "Please add at least one song URL.")
            return redirect('automation_dashboard')

        cover_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'automation_covers')
        vid_dir   = os.path.join(settings.MEDIA_ROOT, 'studio', 'automation_bgvid')
        os.makedirs(cover_dir, exist_ok=True)
        os.makedirs(vid_dir,   exist_ok=True)

        def _get(idx, field, default=''):
            return request.POST.get(f'songs[{idx}][{field}]', default)

        def _save_upload(file_obj, dest_dir, default_ext):
            """Saves an uploaded file to dest_dir, returns abs path."""
            ext  = os.path.splitext(file_obj.name)[1] or default_ext
            path = os.path.join(dest_dir, f"{_uuid.uuid4().hex}{ext}")
            with open(path, 'wb+') as f:
                for chunk in file_obj.chunks():
                    f.write(chunk)
            return path

        youtube_urls  = []
        songs_configs = []   # one config dict per song, in URL order

        for idx in song_indices:
            url = _get(idx, 'url', '').strip()
            if not url:
                continue  # skip blank slots

            cfg = {
                'audio_format':      _get(idx, 'audio_format',    'wav'),
                'animation_style':   _get(idx, 'animation_style', 'KARAOKE_WIPE'),
                'font_family':       _get(idx, 'font_family',      'Arial'),
                'font_size':         int(_get(idx, 'font_size', '48') or 48),
                'highlight_color':   _get(idx, 'highlight_color', '#00E5FF'),
                'text_color':        _get(idx, 'text_color',      '#FFFFFF'),
                'font_weight':       _get(idx, 'font_weight',     'bold'),
                'text_stroke_width': float(_get(idx, 'text_stroke_width', '2.5') or 2.5),
                'text_shadow_depth': float(_get(idx, 'text_shadow_depth', '2.0') or 2.0),
                'aspect_ratio':      _get(idx, 'aspect_ratio',    '16:9'),
                'position_mode':     _get(idx, 'position_mode',   'CENTER'),
                'lyrics_text':       _get(idx, 'lyrics_text',     ''),
                'privacy_status':    _get(idx, 'privacy_status',  'private'),
                'category_id':       _get(idx, 'category_id',     '10'),
                'tags':              _get(idx, 'tags',             'lyrics, lyricvideo, music'),
                'description':       _get(idx, 'description',     ''),
                'account_id':        _get(idx, 'account_id',      ''),
                'use_whisper':       _get(idx, 'use_whisper',      '0') in ('1', 'true', 'on'),
                'use_demucs':        _get(idx, 'use_demucs',       '0') in ('1', 'true', 'on'),
                'generate_thumbnail': _get(idx, 'generate_thumbnail', '1') in ('1', 'true', 'on'),
                'thumbnail_style':   _get(idx, 'thumbnail_style', 'FROSTED_SCRIM'),
                'thumbnail_badge':   _get(idx, 'thumbnail_badge', 'OFFICIAL LYRIC VIDEO'),
                'thumbnail_theme':   _get(idx, 'thumbnail_theme', 'DARK_STUDIO'),
                'do_render':         do_render,
                'do_publish':        do_publish,
            }

            # Cover image upload for this song slot
            cover_key = f'songs[{idx}][background_image]'
            cover_file = request.FILES.get(cover_key)
            if cover_file:
                cfg['background_image_path'] = _save_upload(cover_file, cover_dir, '.jpg')

            # Background video upload for this song slot
            vid_key  = f'songs[{idx}][background_video]'
            vid_file = request.FILES.get(vid_key)
            if vid_file:
                cfg['background_video_path'] = _save_upload(vid_file, vid_dir, '.mp4')

            youtube_urls.append(url)
            songs_configs.append(cfg)

        if not youtube_urls:
            messages.error(request, "Please add at least one YouTube URL.")
            return redirect('automation_dashboard')

        # shared_config holds the Song 0 settings as the "same for all" fallback
        shared_config = songs_configs[0] if songs_configs else {}

        pipeline = AutomationPipeline.objects.create(
            name=name,
            youtube_urls=youtube_urls,
            shared_config=shared_config,
            # Store per-song overrides under a dedicated key
            results=[{'song_config': cfg} for cfg in songs_configs],
            status=AutomationPipeline.Status.IDLE,
        )
        AutomationRunnerService.run_pipeline_async(pipeline.id)
        messages.success(request, f"🚀 Pipeline '{name}' started for {len(youtube_urls)} song(s)!")
        return redirect('automation_dashboard')

    # GET — redirect to dashboard (form lives there)
    return redirect('automation_dashboard')


@login_required
@require_POST
def automation_run_view(request, pk):
    """Reset a pipeline to IDLE/step 0 and re-run it."""
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    from .models import AutomationPipeline
    from .services.automation_runner import AutomationRunnerService

    pipeline = get_object_or_404(AutomationPipeline, pk=pk)
    pipeline.status = AutomationPipeline.Status.IDLE
    pipeline.current_song_index = 0
    pipeline.log = ''
    pipeline.results = []
    pipeline.save()
    AutomationRunnerService.run_pipeline_async(pipeline.id)
    messages.success(request, f"Pipeline '{pipeline.name}' re-started!")
    return redirect('automation_dashboard')


@login_required
def automation_status_api(request, pk):
    """Returns JSON status for live polling."""
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    from .models import AutomationPipeline
    pipeline = get_object_or_404(AutomationPipeline, pk=pk)
    return JsonResponse({
        'status': pipeline.status,
        'current_song_index': pipeline.current_song_index,
        'total_songs': len(pipeline.youtube_urls),
        'log': pipeline.log,
        'results': pipeline.results,
        'result_ids': {
            'download_job_id': None,
            'lyric_project_id': None,
            'publishing_job_id': None,
        },
    })


@login_required
@require_POST
def automation_delete_view(request, pk):
    """Delete a pipeline record."""
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    from .models import AutomationPipeline
    pipeline = get_object_or_404(AutomationPipeline, pk=pk)
    name = pipeline.name
    pipeline.delete()
    messages.success(request, f"Pipeline '{name}' deleted.")
    return redirect('automation_dashboard')
