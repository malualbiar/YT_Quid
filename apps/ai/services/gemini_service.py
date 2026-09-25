import os
import json
import hashlib
import logging
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# ── Usage tracking model (lightweight, in-memory accumulation) ──────────────
# Stored in Django cache under key 'gemini_usage_today' as:
# {'date': 'YYYY-MM-DD', 'requests': int, 'input_tokens': int, 'output_tokens': int}

_USAGE_CACHE_KEY = 'gemini_usage_today'
_USAGE_CACHE_TTL = 86400  # 24 hours


def _get_api_key():
    return getattr(settings, 'GEMINI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '')


def _track_usage(input_tokens=0, output_tokens=0):
    """Accumulate today's Gemini API usage in Django cache."""
    from datetime import date
    today = date.today().isoformat()
    data = cache.get(_USAGE_CACHE_KEY) or {}
    if data.get('date') != today:
        data = {'date': today, 'requests': 0, 'input_tokens': 0, 'output_tokens': 0}
    data['requests'] += 1
    data['input_tokens'] += input_tokens
    data['output_tokens'] += output_tokens
    cache.set(_USAGE_CACHE_KEY, data, _USAGE_CACHE_TTL)


def get_gemini_usage_today():
    """Return today's Gemini usage dict for the API usage dashboard."""
    from datetime import date
    data = cache.get(_USAGE_CACHE_KEY) or {}
    if data.get('date') != date.today().isoformat():
        return {'date': date.today().isoformat(), 'requests': 0, 'input_tokens': 0, 'output_tokens': 0}
    return data


# ── Tone presets ─────────────────────────────────────────────────────────────

TONE_PRESETS = {
    'viral': 'High-energy, punchy, uses trending slang and emoji, optimised for clicks and shares.',
    'professional': 'Clean, authoritative, no emoji, keyword-dense, SEO-focused language.',
    'chill': 'Relaxed, warm, ambient feel — suitable for study/lofi/ambient content.',
    'educational': 'Informative, structured, clear, poses questions to engage curiosity.',
    'hype': 'Aggressive hype energy — capitalised power words, exclamation marks, very short sentences.',
}


class GeminiContentService:
    """
    Central AI content generation service using Google Gemini Flash.
    All public methods return a dict with keys:
        title_variants  — list of 3 dicts [{label, title}, ...]
        description     — str
        tags            — list[str]
        hook_options    — list of 3 str (shorts only)
    Results are cached by input hash to avoid duplicate API calls.
    """

    MODEL = 'gemini-3.5-flash'
    CACHE_TTL = 86400  # 24h

    # ── Internal helpers ──────────────────────────────────────────────────────

    @classmethod
    def _cache_key(cls, *parts):
        raw = '|'.join(str(p) for p in parts)
        return 'gemini_gen_' + hashlib.md5(raw.encode()).hexdigest()

    @classmethod
    def _call_gemini(cls, prompt: str) -> dict:
        """
        Send prompt to Gemini and return parsed JSON dict.
        Falls back to an empty result dict on any error.
        """
        api_key = _get_api_key()
        if not api_key:
            logger.warning('GEMINI_API_KEY not configured.')
            return {}

        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(
                cls.MODEL,
                generation_config={'response_mime_type': 'application/json'},
            )
            response = model.generate_content(prompt)
            text = response.text.strip()

            # Track usage
            try:
                in_tok = response.usage_metadata.prompt_token_count or 0
                out_tok = response.usage_metadata.candidates_token_count or 0
                _track_usage(in_tok, out_tok)
            except Exception:
                _track_usage(len(prompt) // 4, len(text) // 4)

            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f'Gemini JSON parse error: {e}')
            return {}
        except Exception as e:
            logger.error(f'Gemini API error: {e}')
            return {}

    @classmethod
    def _title_variants_prompt_section(cls):
        return (
            '"title_variants": [\n'
            '  {"label": "🔥 Hype", "title": "..."},\n'
            '  {"label": "🎯 SEO", "title": "..."},\n'
            '  {"label": "💬 Story", "title": "..."}\n'
            ']'
        )

    # ── Public generation methods ─────────────────────────────────────────────

    @classmethod
    def generate_for_short(cls, hook_text='', yt_title='', yt_description='',
                           yt_tags=None, chop_start=0, chop_end=30,
                           tone='viral', count=3):
        """
        Generate content for a single Short chop.
        Returns: title_variants (3), description, tags, hook_options (3).
        """
        yt_tags = yt_tags or []
        cache_key = cls._cache_key('short', hook_text, yt_title, tone, chop_start, chop_end)
        cached = cache.get(cache_key)
        if cached:
            return cached

        tone_desc = TONE_PRESETS.get(tone, TONE_PRESETS['viral'])
        tags_str = ', '.join(yt_tags[:20]) if yt_tags else 'none'

        prompt = f"""You are a YouTube Shorts content strategist. Generate optimised content for a Short clip.

SOURCE VIDEO CONTEXT:
- Original video title: {yt_title or 'Unknown'}
- Clip segment: {chop_start:.0f}s – {chop_end:.0f}s
- Existing hook text: {hook_text or 'none'}
- Original tags: {tags_str}
- Original description excerpt: {(yt_description or '')[:300]}

TONE: {tone_desc}

Return ONLY valid JSON matching this exact schema:
{{
  {cls._title_variants_prompt_section()},
  "description": "YouTube Short description (2-3 lines, CTA + relevant hashtags, max 200 chars)",
  "tags": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5", "#tag6", "#tag7", "#tag8"],
  "hook_options": [
    "Hook option 1 (max 8 words, punchy opener)",
    "Hook option 2 (curiosity-driven, different angle)",
    "Hook option 3 (story-based or relatable)"
  ]
}}"""

        result = cls._call_gemini(prompt)
        if result:
            cache.set(cache_key, result, cls.CACHE_TTL)
        return result

    @classmethod
    def generate_for_lyrics_video(cls, song_title='', artist='', genre='', mood='', tone='chill'):
        """Generate content for a Lyrics Video project."""
        cache_key = cls._cache_key('lyrics', song_title, artist, genre, mood, tone)
        cached = cache.get(cache_key)
        if cached:
            return cached

        tone_desc = TONE_PRESETS.get(tone, TONE_PRESETS['chill'])
        prompt = f"""You are a YouTube content strategist specialising in music lyric videos.

TRACK INFO:
- Song title: {song_title or 'Unknown'}
- Artist: {artist or 'Unknown'}
- Genre: {genre or 'Unspecified'}
- Mood: {mood or 'Unspecified'}

TONE: {tone_desc}

Return ONLY valid JSON:
{{
  {cls._title_variants_prompt_section()},
  "description": "Full YouTube description (3-4 lines, streaming links placeholder, hashtags, max 400 chars)",
  "tags": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5", "#tag6", "#tag7", "#tag8", "#tag9", "#tag10"]
}}"""

        result = cls._call_gemini(prompt)
        if result:
            cache.set(cache_key, result, cls.CACHE_TTL)
        return result

    @classmethod
    def generate_for_mix(cls, mix_title='', tracklist=None, genre='', tone='chill'):
        """Generate content for a Long Mix project, including AI chapter labels."""
        tracklist = tracklist or []
        cache_key = cls._cache_key('mix', mix_title, str(tracklist[:5]), genre, tone)
        cached = cache.get(cache_key)
        if cached:
            return cached

        tone_desc = TONE_PRESETS.get(tone, TONE_PRESETS['chill'])
        tracks_str = '\n'.join(
            f"  {i+1}. {t.get('title','?')} – {t.get('artist','?')}"
            for i, t in enumerate(tracklist[:20])
        ) or '  (no tracklist provided)'

        prompt = f"""You are a YouTube DJ mix content strategist.

MIX INFO:
- Mix title: {mix_title or 'Untitled Mix'}
- Genre: {genre or 'Mixed'}
- Track count: {len(tracklist)}
- Sample tracklist:
{tracks_str}

TONE: {tone_desc}

Return ONLY valid JSON:
{{
  {cls._title_variants_prompt_section()},
  "description": "YouTube description with tracklist reference, subscribe CTA, hashtags (max 500 chars)",
  "tags": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5", "#tag6", "#tag7", "#tag8", "#tag9", "#tag10"],
  "chapter_labels": ["Opening Groove", "Rising Energy", "Peak Hour", "Wind Down", "Outro Chill"]
}}"""

        result = cls._call_gemini(prompt)
        if result:
            cache.set(cache_key, result, cls.CACHE_TTL)
        return result

    @classmethod
    def generate_for_video_project(cls, title='', video_format='', genre='', tone='professional'):
        """Generate content for a generic VideoProject (1-hour loop, visualiser, short)."""
        cache_key = cls._cache_key('video_project', title, video_format, genre, tone)
        cached = cache.get(cache_key)
        if cached:
            return cached

        tone_desc = TONE_PRESETS.get(tone, TONE_PRESETS['professional'])
        prompt = f"""You are a YouTube music/ambient content strategist.

VIDEO INFO:
- Title: {title or 'Untitled'}
- Format: {video_format or 'Music Video'}
- Genre: {genre or 'Unspecified'}

TONE: {tone_desc}

Return ONLY valid JSON:
{{
  {cls._title_variants_prompt_section()},
  "description": "YouTube description optimised for the video format with chapters if appropriate, CTA, hashtags (max 400 chars)",
  "tags": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5", "#tag6", "#tag7", "#tag8"]
}}"""

        result = cls._call_gemini(prompt)
        if result:
            cache.set(cache_key, result, cls.CACHE_TTL)
        return result

    @classmethod
    def generate_hook_variants(cls, topic='', style='viral', count=3):
        """Generate N standalone hook text options for the hook banner overlay."""
        cache_key = cls._cache_key('hooks', topic, style, count)
        cached = cache.get(cache_key)
        if cached:
            return cached

        tone_desc = TONE_PRESETS.get(style, TONE_PRESETS['viral'])
        prompt = f"""You are a viral hook copywriter for YouTube Shorts.

TOPIC: {topic or 'General content'}
STYLE: {tone_desc}

Generate {count} short hook phrases for an on-screen banner overlay.
Rules: max 8 words each, punchy, no generic phrases, varied angles.

Return ONLY valid JSON:
{{
  "hook_options": ["hook 1", "hook 2", "hook 3"]
}}"""

        result = cls._call_gemini(prompt)
        if result:
            cache.set(cache_key, result, cls.CACHE_TTL)
        return result

    @classmethod
    def generate_hashtags(cls, topic='', platform='youtube', count=15):
        """Generate platform-specific hashtag sets."""
        cache_key = cls._cache_key('hashtags', topic, platform, count)
        cached = cache.get(cache_key)
        if cached:
            return cached

        prompt = f"""Generate {count} high-performing {platform} hashtags for content about: {topic}.
Mix: 3 broad (10M+ posts), 5 medium (1M-10M), 7 niche/specific.
Return ONLY valid JSON:
{{"tags": ["#tag1", "#tag2", ...]}}"""

        result = cls._call_gemini(prompt)
        if result:
            cache.set(cache_key, result, cls.CACHE_TTL)
        return result
