import os
from datetime import datetime, timezone
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods, require_POST
from django.contrib import messages
from django.core.cache import cache

from apps.radar.models import RadarChannel, SeenVideo
from apps.radar.services.ai_radar_service import AIRadarService
from apps.radar.services.downloader_service import AIDownloaderService


@login_required
def radar_feed_view(request):
    """
    Dedicated AI Radar Feed:
    Monitors manually tracked YouTube accounts, computes Future Like Potential,
    applies in-memory filters without API quota cost, and supports on-page media playback.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    query = request.GET.get('q', '').strip()
    time_window = request.GET.get('time', '7d')
    try:
        min_likes = int(request.GET.get('min_likes', 0))
    except (ValueError, TypeError):
        min_likes = 0

    selected_channel = request.GET.get('channel', 'all')

    try:
        min_potential = int(request.GET.get('min_potential', 0))
    except (ValueError, TypeError):
        min_potential = 0

    sort_by = request.GET.get('sort', 'potential')
    force_refresh = bool(request.GET.get('refresh'))

    # Retrieve all tracked channels for management modal and channel filter
    channels = RadarChannel.objects.all().order_by('channel_name')
    active_channels_count = channels.filter(is_active=True).count()

    # Get IDs of videos this user has already marked as seen
    seen_video_ids = set(
        SeenVideo.objects.filter(user=request.user).values_list('video_id', flat=True)
    )

    # Get cached, filtered, and sorted tracks (0 API quota)
    tracks = AIRadarService.get_filtered_feed(
        time_window=time_window,
        min_likes=min_likes,
        channel_id=selected_channel,
        min_potential=min_potential,
        sort_by=sort_by,
        query=query,
        force_refresh=force_refresh,
    )

    # Exclude seen videos from the displayed feed
    tracks = [t for t in tracks if t['video_id'] not in seen_video_ids]

    last_synced_raw = cache.get('radar_last_synced_timestamp')
    last_synced_str = "Just now"
    if last_synced_raw:
        try:
            ls_dt = datetime.fromisoformat(last_synced_raw)
            diff_mins = max(0, int((datetime.now(timezone.utc) - ls_dt).total_seconds() / 60))
            last_synced_str = f"{diff_mins}m ago" if diff_mins > 0 else "Just now"
        except Exception:
            pass

    time_windows = [
        {'id': '6h',  'label': 'Past 6 hours'},
        {'id': '12h', 'label': 'Past 12 hours'},
        {'id': '24h', 'label': 'Past 24 hours'},
        {'id': '48h', 'label': 'Past 48 hours'},
        {'id': '72h', 'label': 'Past 3 days'},
        {'id': '7d',  'label': 'Past 7 days'},
        {'id': '14d', 'label': 'Past 14 days'},
        {'id': '21d', 'label': 'Past 21 days'},
    ]

    min_likes_options = [
        {'value': 0,    'label': 'Any Likes'},
        {'value': 25,   'label': '25+ Likes'},
        {'value': 50,   'label': '50+ Likes'},
        {'value': 100,  'label': '100+ Likes'},
        {'value': 500,  'label': '500+ Likes'},
        {'value': 1000, 'label': '1,000+ Likes'},
    ]

    potential_options = [
        {'value': 0,  'label': 'All Potential Ratings'},
        {'value': 40, 'label': 'Promising (40+)'},
        {'value': 60, 'label': 'High Potential (60+)'},
        {'value': 80, 'label': 'Viral Breakout (80+)'},
    ]

    sort_options = [
        {'id': 'potential', 'label': 'Future Like Potential'},
        {'id': 'velocity',  'label': 'Highest Like Velocity (Likes/Hr)'},
        {'id': 'likes',     'label': 'Most Likes'},
        {'id': 'views',     'label': 'Most Views'},
        {'id': 'date',      'label': 'Most Recent'},
    ]

    context = {
        'tracks': tracks,
        'total_tracks': len(tracks),
        'channels': channels,
        'active_channels_count': active_channels_count,
        'query': query,
        'current_time': time_window,
        'current_min_likes': min_likes,
        'current_channel': selected_channel,
        'current_potential': min_potential,
        'current_sort': sort_by,
        'last_synced_str': last_synced_str,
        'time_windows': time_windows,
        'min_likes_options': min_likes_options,
        'potential_options': potential_options,
        'sort_options': sort_options,
    }

    return render(request, 'radar/index.html', context)


@login_required
@require_POST
def add_channel_view(request):
    """
    Validates and adds a YouTube channel to the Radar by URL, handle, or ID.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied.")
        return redirect('dashboard')

    channel_input = request.POST.get('channel_input', '').strip()
    if not channel_input:
        messages.error(request, "Please provide a YouTube Channel URL, handle (@name), or Channel ID.")
        return redirect('radar_feed')

    try:
        channel, created = AIRadarService.add_tracked_channel(channel_input)
        if created:
            messages.success(request, f"Added channel '{channel.channel_name}' to AI Radar.")
        else:
            messages.info(request, f"Updated channel '{channel.channel_name}'.")
    except Exception as e:
        messages.error(request, f"Failed to add channel: {str(e)}")

    return redirect('radar_feed')


@login_required
@require_POST
def toggle_channel_view(request, channel_id):
    """
    Toggles active/paused state of a tracked channel.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied.")
        return redirect('dashboard')

    ch = AIRadarService.toggle_channel(channel_id)
    if ch:
        status_str = "active" if ch.is_active else "paused"
        messages.success(request, f"Channel '{ch.channel_name}' is now {status_str}.")
    else:
        messages.error(request, "Channel not found.")

    return redirect('radar_feed')


@login_required
@require_POST
def delete_channel_view(request, channel_id):
    """
    Removes a tracked channel from the Radar.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied.")
        return redirect('dashboard')

    if AIRadarService.delete_channel(channel_id):
        messages.success(request, "Channel removed from AI Radar.")
    else:
        messages.error(request, "Channel not found.")

    return redirect('radar_feed')


@login_required
def refresh_radar_view(request):
    """
    Clears the cache and forces a fresh live sync of tracked channels.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied.")
        return redirect('dashboard')

    AIRadarService.clear_cache()
    AIRadarService.sync_channel_feed(force_refresh=True)
    messages.success(request, "AI Radar feed refreshed with latest uploads from all tracked channels.")
    return redirect('radar_feed')


@login_required
def download_audio_view(request):
    """
    Directly downloads audio from YouTube as lossless WAV (or MP3) using yt-dlp.
    """
    if not request.user.is_super_admin:
        messages.error(request, "Access denied. Super Admin privileges required.")
        return redirect('dashboard')

    video_id = request.GET.get('video_id') or request.POST.get('video_id')
    audio_format = (request.GET.get('format') or request.POST.get('format') or 'wav').lower()
    custom_title = request.GET.get('title') or request.POST.get('title') or 'AI Track'

    if not video_id:
        messages.error(request, "Video ID or URL is required for download.")
        return redirect('radar_feed')

    try:
        file_path, extracted_title = AIDownloaderService.download_audio_file(
            video_url_or_id=video_id,
            audio_format=audio_format
        )

        final_title = custom_title if custom_title and custom_title != 'AI Track' else extracted_title
        clean_filename = AIDownloaderService.sanitize_filename(final_title)
        extension = 'wav' if audio_format == 'wav' else 'mp3'
        download_name = f"{clean_filename}.{extension}"

        content_type = 'audio/wav' if audio_format == 'wav' else 'audio/mpeg'

        response = FileResponse(open(file_path, 'rb'), content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{download_name}"'
        return response

    except Exception as e:
        messages.error(request, f"Audio download error: {str(e)}")
        return redirect('radar_feed')


@login_required
@require_POST
def mark_seen_view(request):
    """
    Marks a video as 'seen' for the current user so it no longer appears in the Radar feed.
    Accepts JSON body or POST params: video_id, video_title.
    Returns JSON { success, seen_count } for AJAX calls.
    """
    if not request.user.is_super_admin:
        return JsonResponse({'success': False, 'error': 'Access denied.'}, status=403)

    import json
    try:
        body = json.loads(request.body)
    except Exception:
        body = {}

    video_id = body.get('video_id') or request.POST.get('video_id', '').strip()
    video_title = body.get('video_title') or request.POST.get('video_title', '').strip()

    if not video_id:
        return JsonResponse({'success': False, 'error': 'video_id is required.'}, status=400)

    SeenVideo.objects.get_or_create(
        user=request.user,
        video_id=video_id,
        defaults={'video_title': video_title[:500]},
    )

    seen_count = SeenVideo.objects.filter(user=request.user).count()
    return JsonResponse({'success': True, 'video_id': video_id, 'seen_count': seen_count})


@login_required
@require_POST
def unmark_seen_view(request):
    """
    Removes a video from the seen list so it can reappear in the Radar feed.
    Accepts JSON body or POST params: video_id.
    """
    if not request.user.is_super_admin:
        return JsonResponse({'success': False, 'error': 'Access denied.'}, status=403)

    import json
    try:
        body = json.loads(request.body)
    except Exception:
        body = {}

    video_id = body.get('video_id') or request.POST.get('video_id', '').strip()
    if not video_id:
        return JsonResponse({'success': False, 'error': 'video_id is required.'}, status=400)

    deleted_count, _ = SeenVideo.objects.filter(user=request.user, video_id=video_id).delete()
    return JsonResponse({'success': True, 'deleted': deleted_count > 0, 'video_id': video_id})
