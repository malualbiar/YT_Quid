import json
import logging
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from apps.ai.services.gemini_service import (
    GeminiContentService,
    TONE_PRESETS,
    get_gemini_usage_today,
)

logger = logging.getLogger(__name__)


@login_required
@require_POST
def ai_generate_view(request):
    """
    Universal AJAX endpoint for AI content generation.

    POST body (JSON):
        content_type  — "short" | "lyrics" | "mix" | "video_project" | "hooks" | "hashtags"
        tone          — "viral" | "professional" | "chill" | "educational" | "hype"
        context       — dict of content-type-specific fields (see docs per type)

    Response:
        { success: true, data: { title_variants, description, tags, hook_options? } }
    """
    if not request.user.is_super_admin:
        return JsonResponse({'error': 'Access denied.'}, status=403)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON body.'}, status=400)

    content_type = body.get('content_type', '')
    tone = body.get('tone', 'viral')
    context = body.get('context', {})

    if tone not in TONE_PRESETS:
        tone = 'viral'

    result = {}

    if content_type == 'short':
        result = GeminiContentService.generate_for_short(
            hook_text=context.get('hook_text', ''),
            yt_title=context.get('yt_title', ''),
            yt_description=context.get('yt_description', ''),
            yt_tags=context.get('yt_tags', []),
            chop_start=float(context.get('chop_start', 0)),
            chop_end=float(context.get('chop_end', 30)),
            tone=tone,
        )

    elif content_type == 'lyrics':
        result = GeminiContentService.generate_for_lyrics_video(
            song_title=context.get('song_title', ''),
            artist=context.get('artist', ''),
            genre=context.get('genre', ''),
            mood=context.get('mood', ''),
            tone=tone,
        )

    elif content_type == 'mix':
        result = GeminiContentService.generate_for_mix(
            mix_title=context.get('mix_title', ''),
            tracklist=context.get('tracklist', []),
            genre=context.get('genre', ''),
            tone=tone,
        )

    elif content_type == 'video_project':
        result = GeminiContentService.generate_for_video_project(
            title=context.get('title', ''),
            video_format=context.get('video_format', ''),
            genre=context.get('genre', ''),
            tone=tone,
        )

    elif content_type == 'hooks':
        result = GeminiContentService.generate_hook_variants(
            topic=context.get('topic', ''),
            style=tone,
            count=int(context.get('count', 3)),
        )

    elif content_type == 'hashtags':
        result = GeminiContentService.generate_hashtags(
            topic=context.get('topic', ''),
            platform=context.get('platform', 'youtube'),
            count=int(context.get('count', 15)),
        )

    else:
        return JsonResponse({'error': f'Unknown content_type: {content_type}'}, status=400)

    if not result:
        return JsonResponse({
            'error': 'AI generation failed. Check GEMINI_API_KEY in your .env file.'
        }, status=500)

    return JsonResponse({'success': True, 'data': result})


@login_required
def ai_usage_view(request):
    """Returns today's Gemini usage stats as JSON — used by the API usage dashboard."""
    usage = get_gemini_usage_today()
    return JsonResponse({'success': True, 'gemini': usage})
