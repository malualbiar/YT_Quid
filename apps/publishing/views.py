import os
import json
import logging
from datetime import datetime, timedelta
from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.utils import timezone
from django.urls import reverse

from .models import YouTubeOAuthAccount, PublishingJob
from .services.oauth_service import YouTubeOAuthService
from .services.uploader_service import YouTubeUploaderService

from apps.studio.models import VideoProject, LongMixProject, ShortVideoProject, LyricVideoProject
from apps.artists.models import YouTubeChannel

logger = logging.getLogger(__name__)

@login_required
def publisher_dashboard_view(request):
    """
    Main YouTube Publishing & Drafts Dispatcher Dashboard.
    """
    accounts = YouTubeOAuthAccount.objects.all()
    default_account = accounts.filter(is_default=True).first() or accounts.first()
    is_oauth_configured = YouTubeOAuthService.is_configured()

    # Publishing stats
    total_jobs = PublishingJob.objects.count()
    completed_jobs = PublishingJob.objects.filter(status=PublishingJob.Status.SUCCESS).count()
    active_jobs = PublishingJob.objects.filter(status__in=[PublishingJob.Status.QUEUED, PublishingJob.Status.UPLOADING, PublishingJob.Status.PROCESSING]).order_by('-created_at')
    failed_jobs = PublishingJob.objects.filter(status=PublishingJob.Status.FAILED).count()
    draft_count = PublishingJob.objects.filter(privacy_status=PublishingJob.PrivacyStatus.PRIVATE_DRAFT, status=PublishingJob.Status.SUCCESS).count()

    # Recent completed/queued jobs
    recent_jobs = PublishingJob.objects.select_related('account').all().order_by('-created_at')[:15]

    # Dismissed project IDs stored in session (per source_type)
    dismissed = request.session.get('dismissed_projects', {})

    def _dismissed_ids(source_type_key):
        return set(dismissed.get(source_type_key, []))

    # Available studio projects ready for publishing (exclude dismissed)
    rendered_loops = VideoProject.objects.filter(render_status=VideoProject.Status.COMPLETED).exclude(
        pk__in=_dismissed_ids('VIDEO_LOOP')).order_by('-created_at')[:12]
    rendered_mixes = LongMixProject.objects.filter(render_status=LongMixProject.Status.COMPLETED).exclude(
        pk__in=_dismissed_ids('LONG_MIX')).order_by('-created_at')[:12]
    rendered_shorts = ShortVideoProject.objects.filter(render_status=ShortVideoProject.Status.COMPLETED).exclude(
        pk__in=_dismissed_ids('SHORT_VIDEO')).order_by('-created_at')[:12]
    rendered_lyrics = LyricVideoProject.objects.filter(render_status=LyricVideoProject.Status.COMPLETED).exclude(
        pk__in=_dismissed_ids('LYRIC_VIDEO')).order_by('-created_at')[:12]

    # Build sets of source IDs that have a SUCCESS publishing job (uploaded to YouTube)
    success_jobs = PublishingJob.objects.filter(
        status=PublishingJob.Status.SUCCESS,
        source_id__isnull=False
    ).values('source_type', 'source_id')

    uploaded_ids = {}
    for job in success_jobs:
        uploaded_ids.setdefault(job['source_type'], set()).add(job['source_id'])

    return render(request, 'publishing/dashboard.html', {
        'accounts': accounts,
        'default_account': default_account,
        'is_oauth_configured': is_oauth_configured,
        'total_jobs': total_jobs,
        'completed_jobs': completed_jobs,
        'active_jobs': active_jobs,
        'failed_jobs': failed_jobs,
        'draft_count': draft_count,
        'recent_jobs': recent_jobs,
        'rendered_loops': rendered_loops,
        'rendered_mixes': rendered_mixes,
        'rendered_shorts': rendered_shorts,
        'rendered_lyrics': rendered_lyrics,
        'uploaded_loop_ids': uploaded_ids.get('VIDEO_LOOP', set()),
        'uploaded_mix_ids': uploaded_ids.get('LONG_MIX', set()),
        'uploaded_short_ids': uploaded_ids.get('SHORT_VIDEO', set()),
        'uploaded_lyric_ids': uploaded_ids.get('LYRIC_VIDEO', set()),
        'privacy_choices': PublishingJob.PrivacyStatus.choices,
    })


@login_required
def queue_list_view(request):
    """
    Dedicated view for searching and managing the full publishing queue & history.
    """
    status_filter = request.GET.get('status', 'ALL')
    privacy_filter = request.GET.get('privacy', 'ALL')
    search_query = request.GET.get('q', '').strip()

    jobs = PublishingJob.objects.select_related('account').all().order_by('-created_at')

    if status_filter == 'ACTIVE':
        jobs = jobs.filter(status__in=[PublishingJob.Status.QUEUED, PublishingJob.Status.UPLOADING, PublishingJob.Status.PROCESSING])
    elif status_filter in [PublishingJob.Status.SUCCESS, PublishingJob.Status.FAILED, PublishingJob.Status.CANCELLED]:
        jobs = jobs.filter(status=status_filter)

    if privacy_filter in [PublishingJob.PrivacyStatus.PRIVATE_DRAFT, PublishingJob.PrivacyStatus.UNLISTED, PublishingJob.PrivacyStatus.PUBLIC, PublishingJob.PrivacyStatus.SCHEDULED]:
        jobs = jobs.filter(privacy_status=privacy_filter)

    if search_query:
        jobs = jobs.filter(title__icontains=search_query) | jobs.filter(youtube_video_id__icontains=search_query)

    active_count = PublishingJob.objects.filter(status__in=[PublishingJob.Status.QUEUED, PublishingJob.Status.UPLOADING, PublishingJob.Status.PROCESSING]).count()

    return render(request, 'publishing/queue.html', {
        'jobs': jobs,
        'status_filter': status_filter,
        'privacy_filter': privacy_filter,
        'search_query': search_query,
        'active_count': active_count,
        'privacy_choices': PublishingJob.PrivacyStatus.choices,
    })


@login_required
def oauth_connect_view(request):
    """
    Starts Google OAuth 2.0 flow for YouTube Channel authorization.
    """
    if not YouTubeOAuthService.is_configured():
        messages.error(request, "Google OAuth 2.0 is not configured. Please set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in settings or .env.")
        return redirect('publishing_dashboard')

    redirect_uri = request.build_absolute_uri(reverse('publishing_oauth_callback'))
    try:
        auth_url = YouTubeOAuthService.get_authorization_url(redirect_uri)
        return redirect(auth_url)
    except Exception as e:
        logger.error(f"Error starting OAuth flow: {e}")
        messages.error(request, f"Failed to start YouTube authorization: {e}")
        return redirect('publishing_dashboard')


@login_required
def oauth_callback_view(request):
    """
    Receives authorization code from Google OAuth and registers the connected channel.
    """
    error = request.GET.get('error')
    if error:
        messages.error(request, f"Google authorization was declined or cancelled: {error}")
        return redirect('publishing_dashboard')

    code = request.GET.get('code')
    if not code:
        messages.error(request, "No authorization code received from Google.")
        return redirect('publishing_dashboard')

    redirect_uri = request.build_absolute_uri(reverse('publishing_oauth_callback'))

    try:
        token_data = YouTubeOAuthService.exchange_code_for_tokens(code, redirect_uri)
        account = YouTubeOAuthService.register_or_update_account(token_data)
        messages.success(request, f"Successfully connected YouTube channel: '{account.channel_title}'!")
    except Exception as e:
        logger.error(f"OAuth callback error: {e}", exc_info=True)
        messages.error(request, f"Failed to connect YouTube channel: {e}")

    return redirect('publishing_dashboard')


@login_required
@require_POST
def oauth_disconnect_view(request, account_id):
    """
    Disconnects and removes a linked YouTube channel account.
    """
    try:
        name = YouTubeOAuthService.disconnect_account(account_id)
        messages.success(request, f"Disconnected YouTube channel: '{name}'.")
    except Exception as e:
        messages.error(request, f"Failed to disconnect channel: {e}")

    return redirect('publishing_dashboard')


@login_required
@require_POST
def oauth_set_default_view(request, account_id):
    """
    Sets an account as the default target for publishing.
    """
    account = get_object_or_404(YouTubeOAuthAccount, pk=account_id)
    account.is_default = True
    account.save()
    messages.success(request, f"Set '{account.channel_title}' as default publishing channel.")
    return redirect('publishing_dashboard')


@login_required
@require_POST
def create_job_view(request):
    """
    Creates and initiates a YouTube Publishing / Drafts job.
    Accepts both standard form POST and JSON AJAX.
    """
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')

    title = request.POST.get('title', '').strip() or 'My Video'
    description = request.POST.get('description', '').strip()
    tags_raw = request.POST.get('tags', '')
    category_id = request.POST.get('category_id', '10')
    privacy_status = request.POST.get('privacy_status', PublishingJob.PrivacyStatus.PRIVATE_DRAFT)
    publish_at_str = request.POST.get('publish_at', '').strip()
    made_for_kids = request.POST.get('made_for_kids') in ['1', 'true', 'True', 'on']
    embeddable = request.POST.get('embeddable', '1') in ['1', 'true', 'True', 'on']

    upload_engine = request.POST.get('upload_engine', PublishingJob.UploadEngine.API_V3)

    account_id = request.POST.get('account_id')
    account = None
    if account_id:
        account = YouTubeOAuthAccount.objects.filter(pk=account_id, is_active=True).first()
    if not account:
        account = (
            YouTubeOAuthAccount.objects.filter(is_default=True, is_active=True).first()
            or YouTubeOAuthAccount.objects.filter(is_active=True).first()
        )

    if upload_engine == PublishingJob.UploadEngine.API_V3 and not account:
        err_msg = "No YouTube channel is connected for API v3. Please connect a channel or switch to Browser Automation / Studio Assistant mode."
        if is_ajax:
            return JsonResponse({'success': False, 'error': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('publishing_dashboard')

    # Resolve video file path
    video_file_path = request.POST.get('video_file_path', '').strip()
    uploaded_video = request.FILES.get('video_file')
    
    if uploaded_video:
        upload_dir = os.path.join(settings.MEDIA_ROOT, 'publishing', 'custom_videos')
        os.makedirs(upload_dir, exist_ok=True)
        dest_path = os.path.join(upload_dir, f"custom_{int(timezone.now().timestamp())}_{uploaded_video.name}")
        with open(dest_path, 'wb+') as destination:
            for chunk in uploaded_video.chunks():
                destination.write(chunk)
        video_file_path = dest_path

    if not video_file_path or not os.path.exists(video_file_path):
        err_msg = f"Video file not found or path is invalid: {video_file_path}"
        if is_ajax:
            return JsonResponse({'success': False, 'error': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('publishing_dashboard')

    # Resolve thumbnail path
    thumbnail_path = request.POST.get('thumbnail_path', '').strip()
    uploaded_thumb = request.FILES.get('thumbnail_file')
    if uploaded_thumb:
        thumb_dir = os.path.join(settings.MEDIA_ROOT, 'publishing', 'custom_thumbs')
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_dest = os.path.join(thumb_dir, f"thumb_{int(timezone.now().timestamp())}_{uploaded_thumb.name}")
        with open(thumb_dest, 'wb+') as destination:
            for chunk in uploaded_thumb.chunks():
                destination.write(chunk)
        thumbnail_path = thumb_dest

    # Parse tags
    if isinstance(tags_raw, list):
        tags = tags_raw
    else:
        tags = [t.strip().lstrip('#') for t in tags_raw.replace('\n', ',').split(',') if t.strip()]

    # Parse publish_at date if scheduled
    publish_at = None
    if privacy_status == PublishingJob.PrivacyStatus.SCHEDULED and publish_at_str:
        try:
            publish_at = datetime.fromisoformat(publish_at_str)
            if timezone.is_naive(publish_at):
                publish_at = timezone.make_aware(publish_at, timezone.utc)
        except Exception as pe:
            logger.warning(f"Could not parse publish_at datetime '{publish_at_str}': {pe}")

    source_type = request.POST.get('source_type', PublishingJob.SourceType.CUSTOM_FILE)
    source_id = request.POST.get('source_id') or None
    source_chop_index = request.POST.get('source_chop_index') or None

    job = PublishingJob.objects.create(
        account=account,
        upload_engine=upload_engine,
        title=title,
        description=description,
        tags=tags,
        category_id=category_id,
        privacy_status=privacy_status,
        publish_at=publish_at,
        made_for_kids=made_for_kids,
        embeddable=embeddable,
        video_file_path=video_file_path,
        thumbnail_path=thumbnail_path,
        source_type=source_type,
        source_id=int(source_id) if source_id else None,
        source_chop_index=int(source_chop_index) if source_chop_index is not None else None,
        status=PublishingJob.Status.QUEUED,
    )

    # Launch asynchronous background upload
    YouTubeUploaderService.start_upload_async(job.id)

    status_label = "Draft" if privacy_status == PublishingJob.PrivacyStatus.PRIVATE_DRAFT else privacy_status.capitalize()
    success_msg = f"Started upload for '{job.title}' ({status_label}) to '{account.channel_title}'!"

    if is_ajax:
        return JsonResponse({
            'success': True,
            'job_id': job.id,
            'title': job.title,
            'channel': account.channel_title,
            'status': job.status,
            'message': success_msg,
            'queue_url': reverse('publishing_queue')
        })

    messages.success(request, success_msg)
    return redirect('publishing_dashboard')


@login_required
@require_POST
def batch_queue_shorts_view(request, project_id):
    """
    Batch queues all completed chops from a ShortVideoProject to YouTube.
    Supports instant batch draft or staggered/drip scheduling (e.g. 1 Short per day).
    """
    project = get_object_or_404(ShortVideoProject, pk=project_id)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')

    account = (
        YouTubeOAuthAccount.objects.filter(is_default=True, is_active=True).first()
        or YouTubeOAuthAccount.objects.filter(is_active=True).first()
    )
    if not account:
        err_msg = "No active YouTube channel connected. Please connect a channel first in YouTube Publisher."
        if is_ajax:
            return JsonResponse({'success': False, 'error': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('shorts_detail', pk=project_id)

    privacy_status = request.POST.get('privacy_status', PublishingJob.PrivacyStatus.PRIVATE_DRAFT)
    drip_hours = int(request.POST.get('drip_interval_hours', '0'))

    chops = project.chops_data or []
    queued_jobs = []
    base_time = timezone.now()

    for idx, chop in enumerate(chops):
        chop_url = chop.get('output_url', '')
        if not chop_url and project.output_video:
            chop_url = project.output_video.url

        if not chop_url:
            continue

        rel_path = chop_url.lstrip('/').replace('media/', '')
        abs_video_path = os.path.join(settings.MEDIA_ROOT, rel_path)
        if not os.path.exists(abs_video_path):
            continue

        chop_title = project.youtube_title_for_chop(idx)
        chop_desc = project.youtube_description_for_chop(idx)
        
        publish_at = None
        current_privacy = privacy_status
        if drip_hours > 0 and idx > 0:
            current_privacy = PublishingJob.PrivacyStatus.SCHEDULED
            publish_at = base_time + timedelta(hours=drip_hours * idx)

        thumbnail_path = project.cover_image.path if project.cover_image else ''

        chop_tags = list(project.yt_video_tags[:15]) if project.yt_video_tags else ['shorts', 'viral', 'reels', 'music', 'fyp']
        if 'shorts' not in [t.lower() for t in chop_tags]:
            chop_tags.insert(0, 'shorts')

        job = PublishingJob.objects.create(
            account=account,
            title=chop_title,
            description=chop_desc,
            tags=chop_tags,
            category_id='10',
            privacy_status=current_privacy,
            publish_at=publish_at,
            made_for_kids=False,
            video_file_path=abs_video_path,
            thumbnail_path=thumbnail_path,
            source_type=PublishingJob.SourceType.SHORT_VIDEO,
            source_id=project.id,
            source_chop_index=idx,
            status=PublishingJob.Status.QUEUED,
        )
        queued_jobs.append(job)

    # Launch ALL queued jobs async (each uploads sequentially in its own thread)
    for job in queued_jobs:
        YouTubeUploaderService.start_upload_async(job.id)

    msg = f"Successfully queued {len(queued_jobs)} Shorts/Chops to '{account.channel_title}'!"
    if is_ajax:
        return JsonResponse({'success': True, 'queued_count': len(queued_jobs), 'message': msg})

    messages.success(request, msg)
    return redirect('publishing_queue')


@login_required
@require_GET
def job_status_api(request):
    """
    JSON polling endpoint returning current progress & states for active upload jobs.
    """
    job_ids_str = request.GET.get('ids', '')
    if job_ids_str:
        job_ids = [int(i) for i in job_ids_str.split(',') if i.isdigit()]
        jobs = PublishingJob.objects.filter(id__in=job_ids)
    else:
        # Return all active jobs
        jobs = PublishingJob.objects.filter(status__in=[
            PublishingJob.Status.QUEUED,
            PublishingJob.Status.UPLOADING,
            PublishingJob.Status.PROCESSING
        ])

    data = []
    for j in jobs:
        data.append({
            'id': j.id,
            'title': j.title,
            'status': j.status,
            'status_display': j.get_status_display(),
            'privacy_status': j.privacy_status,
            'privacy_display': j.get_privacy_status_display(),
            'progress_percent': j.progress_percent,
            'bytes_uploaded': j.bytes_uploaded,
            'total_bytes': j.total_bytes,
            'youtube_video_id': j.youtube_video_id,
            'youtube_url': j.youtube_url,
            'youtube_studio_url': j.youtube_studio_url,
            'error_message': j.error_message,
        })

    return JsonResponse({'jobs': data})


@login_required
@require_POST
def cancel_job_view(request, job_id):
    """
    Cancels an active upload job.
    """
    YouTubeUploaderService.cancel_upload(job_id)
    messages.info(request, f"Upload job #{job_id} was cancelled.")
    return redirect(request.META.get('HTTP_REFERER') or 'publishing_queue')


@login_required
@require_POST
def retry_job_view(request, job_id):
    """
    Retries a failed or cancelled upload job.
    """
    job = get_object_or_404(PublishingJob, pk=job_id)
    job.status = PublishingJob.Status.QUEUED
    job.retry_count += 1
    job.error_message = ''
    job.progress_percent = 0
    job.save(update_fields=['status', 'retry_count', 'error_message', 'progress_percent'])

    YouTubeUploaderService.start_upload_async(job.id)
    messages.success(request, f"Retrying upload for '{job.title}'...")
    return redirect(request.META.get('HTTP_REFERER') or 'publishing_queue')


@login_required
@require_POST
def delete_job_view(request, job_id):
    """
    Deletes a job record from the database.
    """
    job = get_object_or_404(PublishingJob, pk=job_id)
    job.delete()
    messages.success(request, "Publishing job record deleted.")
    return redirect(request.META.get('HTTP_REFERER') or 'publishing_queue')


@login_required
@require_POST
def dismiss_project_view(request, source_type, source_id):
    """
    Removes a studio project card from the Ready for YouTube section by storing
    its ID in the session. Does not delete the project from the database.
    """
    valid_types = {'VIDEO_LOOP', 'LONG_MIX', 'SHORT_VIDEO', 'LYRIC_VIDEO'}
    if source_type not in valid_types:
        return redirect('publishing_dashboard')

    dismissed = request.session.get('dismissed_projects', {})
    ids = dismissed.get(source_type, [])
    if source_id not in ids:
        ids.append(source_id)
    dismissed[source_type] = ids
    request.session['dismissed_projects'] = dismissed
    request.session.modified = True

    return redirect('publishing_dashboard')
