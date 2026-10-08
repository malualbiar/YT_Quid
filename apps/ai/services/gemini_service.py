import os
import json
import hashlib
import logging
import re
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# ── Usage tracking model (lightweight, in-memory accumulation) ──────────────
# Stored in Django cache under key 'gemini_usage_today' as:
# {'date': 'YYYY-MM-DD', 'requests': int, 'input_tokens': int, 'output_tokens': int}

_USAGE_CACHE_KEY = 'gemini_usage_today'
_USAGE_CACHE_TTL = 86400  # 24 hours


def _get_api_key():
    return str(getattr(settings, 'GEMINI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '')).strip()


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

    @staticmethod
    def _split_pov_script_scenes(script):
        script = ' '.join((script or '').split())
        if not script:
            return []

        sentences = re.findall(r'[^.!?]+[.!?]+(?:["\')\]]+)?|[^.!?]+$', script)
        scenes = []
        current_words = []
        max_words = 18

        for sentence in sentences:
            clauses = re.split(r'(?<=[,;:—–])\s+', sentence.strip())
            for clause in clauses:
                words = clause.split()
                while words:
                    available = max_words - len(current_words)
                    if available == 0:
                        scenes.append(' '.join(current_words))
                        current_words = []
                        available = max_words
                    current_words.extend(words[:available])
                    words = words[available:]
                    if words:
                        scenes.append(' '.join(current_words))
                        current_words = []

        if current_words:
            scenes.append(' '.join(current_words))
        if len(scenes) <= 50:
            return scenes

        return [
            ' '.join(scenes[
                (index * len(scenes)) // 50:
                ((index + 1) * len(scenes)) // 50
            ])
            for index in range(50)
        ]

    @classmethod
    def _call_gemini(cls, prompt: str) -> dict:
        """
        Send prompt to Gemini and return parsed JSON dict.
        Supports model fallback cascade and retry logic for high-demand spikes (503/429).
        """
        import re
        import time

        api_key = _get_api_key()
        if not api_key:
            logger.warning('GEMINI_API_KEY not configured.')
            return {}

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.error(f'Gemini client initialization error: {e}')
            return {}

        configured_model = getattr(settings, 'GEMINI_MODEL', cls.MODEL) or cls.MODEL
        candidate_models = []
        for m in [configured_model, 'gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-flash-latest', 'gemini-3.7-flash', 'gemini-3.8-flash']:
            if m and m not in candidate_models:
                candidate_models.append(m)

        last_error = None
        for model_name in candidate_models:
            for attempt in range(2):
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(response_mime_type='application/json'),
                    )
                    text = (response.text or '').strip()

                    # Track usage
                    try:
                        in_tok = response.usage_metadata.prompt_token_count or 0
                        out_tok = response.usage_metadata.candidates_token_count or 0
                        _track_usage(in_tok, out_tok)
                    except Exception:
                        _track_usage(len(prompt) // 4, len(text) // 4)

                    # Extract JSON payload cleanly
                    if '```json' in text:
                        text = text.split('```json', 1)[1].split('```', 1)[0].strip()
                    elif '```' in text:
                        text = text.split('```', 1)[1].split('```', 1)[0].strip()

                    start_idx = text.find('{')
                    if start_idx != -1:
                        parsed, _ = json.JSONDecoder().raw_decode(text[start_idx:])
                    else:
                        parsed = json.loads(text)

                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f'Gemini JSON parse error on {model_name} (attempt {attempt + 1}): {e}')
                    last_error = e
                    break
                except Exception as e:
                    last_error = e
                    err_str = str(e).lower()
                    if '503' in err_str or '429' in err_str or 'unavailable' in err_str:
                        logger.warning(f'Gemini {model_name} transient error (attempt {attempt + 1}): {e}')
                        time.sleep(0.6 * (attempt + 1))
                        continue
                    else:
                        logger.warning(f'Gemini {model_name} failed: {e}')
                        break  # Move to next fallback model

        logger.error(f'All Gemini model attempts failed. Last error: {last_error}')
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
                           moment_description='', moment_reason='', moment_category='',
                           tone='viral', count=3):
        """
        Generate content for a single Short chop.
        Returns: title_variants (3), description, tags, hook_options (3).
        """
        yt_tags = yt_tags or []
        cache_key = cls._cache_key(
            'short', hook_text, yt_title, moment_description, moment_reason,
            moment_category, tone, chop_start, chop_end,
        )
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
- Moment category: {moment_category or 'unspecified'}
- Moment description: {(moment_description or '')[:600] or 'none'}
- Why it was selected: {(moment_reason or '')[:400] or 'none'}
- Original tags: {tags_str}
- Original description excerpt: {(yt_description or '')[:300]}

TONE: {tone_desc}

Keep every suggestion faithful to the supplied video context. Do not invent events, imply guaranteed virality, or use misleading claims.

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
    def generate_narration_youtube_metadata(cls, script='', working_title=''):
        """Generate YouTube packaging metadata grounded in the complete narration script."""
        script = (script or '').strip()
        if not script:
            return None

        cache_key = cls._cache_key('narration_youtube_metadata', script, working_title)
        cached = cache.get(cache_key)
        if cached:
            return cached

        prompt = f"""You are a YouTube packaging strategist for compelling narrated story videos.

VIDEO SCRIPT:
{script}

Working title for context (improve it; do not simply repeat it unless it is already best):
{working_title or 'Untitled'}

Create metadata based on the actual story in the script. Do not invent plot details, outcomes, people, or claims that are not supported by it.

Return ONLY valid JSON with exactly these fields:
{{
  "title": "One catchy, curiosity-driven YouTube title, maximum 100 characters, accurate to the script",
  "description": "A polished YouTube description with a strong opening hook, accurate story summary, natural call to action, and a few relevant hashtags",
  "thumbnail_prompt": "A detailed image-generation prompt for an attractive 16:9 YouTube thumbnail based on the script's most compelling moment; specify a clear focal subject, expressive emotion, cinematic composition, strong contrast, vivid but cohesive colors, uncluttered background, and no watermark or logos. Request no generated text so the creator can add readable text separately."
}}"""
        result = cls._call_gemini(prompt)
        if not isinstance(result, dict):
            return None

        metadata = {
            'title': str(result.get('title', '')).strip(),
            'description': str(result.get('description', '')).strip(),
            'thumbnail_prompt': str(result.get('thumbnail_prompt', '')).strip(),
        }
        if not all(metadata.values()):
            logger.error('Gemini returned incomplete YouTube metadata for a narration script.')
            return None

        metadata['title'] = metadata['title'][:100]
        cache.set(cache_key, metadata, cls.CACHE_TTL)
        return metadata

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

    @classmethod
    def generate_pov_image_prompts(cls, script='', scenes=None, style='cinematic', aspect_ratio='16:9', generator='gemini'):
        """
        Generate AI image prompts for POV narration script scenes.
        Returns:
            visual_theme: str
            style: str
            generator: str
            scene_prompts: list of dicts [
                {
                    "scene_number": int,
                    "scene_text": str,
                    "image_prompt": str,
                    "negative_prompt": str,
                    "camera_angle": str,
                    "lighting": str
                }
            ]
        """
        scenes = scenes or cls._split_pov_script_scenes(script)
        cache_key = cls._cache_key('pov_image_prompts', script, str(scenes), style, aspect_ratio, generator)
        cached = cache.get(cache_key)
        if cached:
            return cached

        style_descriptions = {
            'cinematic': 'Dramatic cinematic lighting, 35mm film camera lens, shallow depth of field, anamorphic lens flares, 8k resolution movie screen visual quality.',
            'photorealistic': 'Hyper-realistic DSLR photography, 8K resolution, highly detailed realistic textures, natural lighting, 50mm f/1.4 lens.',
            'cyberpunk': 'Neon blue and magenta glow, dark rain-slicked futuristic streets, dark dystopian atmosphere, glowing holograms, chrome details.',
            'dark_fantasy': 'Moody atmospheric lighting, gothic architecture, misty ethereal glow, intricate dark fantasy oil painting style.',
            'anime': 'High quality anime screenshot aesthetic, Studio Ghibli or Makoto Shinkai style, vibrant colors, atmospheric background art.',
            'surreal': 'Dreamlike ethereal atmosphere, impossible physics and geometry, soft volumetric fog, mysterious vibe.',
            'horror': 'Eerie desaturated palette, deep dark shadows, high film grain, vintage 80s analog horror aesthetic, unsettling atmospheric details.',
        }
        style_desc = style_descriptions.get(style, style_descriptions['cinematic'])

        generator_rules = {
            'gemini': f'Craft clear, highly detailed visual prompts optimized for Google Gemini / Imagen web image generation with aspect ratio {aspect_ratio}.',
            'midjourney': f'End each image prompt string with Midjourney parameters like --ar {aspect_ratio} --v 6.0 --style raw.',
            'dalle3': 'Craft detailed natural language prompts formatted for DALL-E 3 / ChatGPT.',
            'stable_diffusion': 'Include quality boosters like "masterpiece, highly detailed, 8k" and a separate negative_prompt field.',
            'flux': 'Craft highly descriptive photorealistic prompts optimized for Flux AI.',
        }
        gen_rule = generator_rules.get(generator, generator_rules['gemini'])

        if scenes:
            scene_items = []
            for i, sc in enumerate(scenes[:50]):
                text = sc if isinstance(sc, str) else (sc.get('text') if isinstance(sc, dict) else str(sc))
                scene_items.append(f"Scene {i+1}: {text}")
            context_body = "EXPLICIT SCENES:\n" + "\n".join(scene_items)
            scene_count_instruction = (
                f"Generate exactly {len(scene_items)} scene_prompts, one for each numbered scene above. "
                "Keep each scene_text faithful to its matching script excerpt; do not merge, skip, reorder, "
                "or add scenes."
            )
        else:
            context_body = f"FULL SCRIPT:\n{script}"
            scene_count_instruction = "Create a separate scene prompt for every distinct visual beat in the script, up to 50 scenes."

        prompt = f"""You are an expert AI prompt engineer specializing in POV (Point Of View / First-Person) narrative image prompts for AI image generators (Midjourney, DALL-E 3, Flux, Stable Diffusion).

CONTENT TO PROCESS:
{context_body}

TARGET VISUAL STYLE: {style} ({style_desc})
ASPECT RATIO: {aspect_ratio}
TARGET GENERATOR: {generator} ({gen_rule})

INSTRUCTIONS:
0. SCENE-BY-SCENE COVERAGE: {scene_count_instruction}
1. CHARACTER CONSISTENCY ANCHOR:
   - Identify or define a precise, detailed "Character Visual Anchor" for the main protagonist/narrator appearing across the script.
   - Specify exact character details: age group, ethnicity, hair style & color, eye color, facial features, clothing/outfit, and signature accessories (e.g. "a 28-year-old male with short dark brown hair, subtle stubble, wearing a worn dark olive jacket and silver wrist watch").
   - EVERY generated image_prompt MUST explicitly incorporate this exact character description so that AI image generators render the SAME consistent character in every scene.

2. SINGLE STANDALONE IMAGE MANDATE:
   Each generated image_prompt MUST start with this exact header block:
"IMPORTANT INSTRUCTION: Generate a single, standalone image for EACH scene listed below. Do NOT combine the scenes into a collage, grid, contact sheet, or split-screen image. Render each image separately one by one.

Aspect Ratio: {aspect_ratio}"

3. PERSPECTIVE & VISUAL DETAILS:
   - EVERY prompt MUST explicitly represent a First-Person / POV perspective (e.g. "POV shot looking at...", "First-person perspective showing hands wearing...").
   - Include rich visual details: environment, key objects, camera lens, depth of field, atmospheric lighting, color palette, and mood.
   - Strictly prohibit collages, split-screens, contact sheets, or grid panels in the visual output.
   - Provide a matching negative prompt for unwanted artifacts (e.g. "inconsistent character, different face, collage, split-screen, grid, contact sheet, multi-panel, third person, 3d render, low quality, distorted...").
   - Provide short tags for camera_angle and lighting.

Return ONLY valid JSON matching this exact schema:
{{
  "visual_theme": "1-2 sentence overview of the visual style, color palette, and mood across all scenes",
  "character_anchor": "Exact description of the persistent character (age, hair, clothing, signature features) used across all prompts for 100% visual consistency",
  "style": "{style}",
  "generator": "{generator}",
  "scene_prompts": [
    {{
      "scene_number": 1,
      "scene_text": "Exact text or brief summary of scene 1",
      "image_prompt": "IMPORTANT INSTRUCTION: Generate a single, standalone image for EACH scene listed below. Do NOT combine the scenes into a collage, grid, contact sheet, or split-screen image. Render each image separately one by one.\\n\\nAspect Ratio: {aspect_ratio}\\n\\nCharacter: The same persistent protagonist (28-year-old male with short dark hair, dark olive jacket). POV shot looking at...",
      "negative_prompt": "inconsistent character, different face, collage, split-screen, grid, contact sheet, multi-panel, blurry, low res, third-person perspective...",
      "camera_angle": "First-person eye level",
      "lighting": "Dramatic cinematic rim lighting"
    }}
  ]
}}"""

        result = cls._call_gemini(prompt)
        if result and isinstance(result, dict) and 'scene_prompts' in result:
            scene_prompts = result.get('scene_prompts')
            invalid_prompts = (
                not isinstance(scene_prompts, list)
                or len(scene_prompts) != len(scene_items)
                or any(
                    not isinstance(item, dict) or not str(item.get('image_prompt', '')).strip()
                    for item in scene_prompts
                )
            ) if scenes else False
            if invalid_prompts:
                logger.error(
                    'Gemini returned an incomplete POV prompt set (%s prompts for %s requested scenes).',
                    len(scene_prompts) if isinstance(scene_prompts, list) else 'an invalid number of',
                    len(scene_items),
                )
                return None

            required_header = (
                "IMPORTANT INSTRUCTION: Generate a single, standalone image for EACH scene listed below. "
                "Do NOT combine the scenes into a collage, grid, contact sheet, or split-screen image. "
                f"Render each image separately one by one.\n\nAspect Ratio: {aspect_ratio}"
            )
            char_anchor = result.get('character_anchor', '').strip()
            for index, item in enumerate(scene_prompts):
                if isinstance(item, dict) and 'image_prompt' in item:
                    item['scene_number'] = index + 1
                    if scenes:
                        source_scene = scenes[index]
                        item['scene_text'] = (
                            source_scene if isinstance(source_scene, str)
                            else source_scene.get('text', '') if isinstance(source_scene, dict)
                            else str(source_scene)
                        )
                    prompt_str = item['image_prompt'].strip()
                    if "IMPORTANT INSTRUCTION: Generate a single" not in prompt_str:
                        char_prefix = f"Character Anchor: {char_anchor}\n\n" if char_anchor and char_anchor.lower() not in prompt_str.lower() else ""
                        item['image_prompt'] = f"{required_header}\n\n{char_prefix}{prompt_str}"
            cache.set(cache_key, result, cls.CACHE_TTL)
        return result

    @classmethod
    def structure_script_into_scenes(cls, script=''):
        """
        Analyze raw script and intelligently break it down into coherent visual POV scenes with Gemini.
        Returns dict with key 'scenes': list of dicts [{ "scene_number": int, "text": str, "visual_summary": str }]
        """
        script = (script or '').strip()
        if not script:
            return {'scenes': []}

        cache_key = cls._cache_key('structure_script_scenes', script[:600])
        cached = cache.get(cache_key)
        if cached:
            return cached

        prompt = f"""You are an expert story director and video editor specializing in POV (Point Of View) video narratives.

RAW SCRIPT:
{script}

INSTRUCTIONS:
1. Intelligently structure and split the raw script into distinct, well-paced visual scenes.
2. Each scene should represent a clear narrative beat, action change, or location transition suitable for a standalone image and narration clip.
3. Keep the narration text for each scene natural, engaging, and well-balanced (1 to 3 sentences per scene beat).
4. Do NOT drop or omit any narrative content from the original story.

Return ONLY valid JSON matching this schema:
{{
  "scenes": [
    {{
      "scene_number": 1,
      "text": "Exact script text for scene 1",
      "visual_summary": "Brief visual context summary of scene 1"
    }}
  ]
}}"""

        result = cls._call_gemini(prompt)
        if result and isinstance(result, dict) and 'scenes' in result:
            cache.set(cache_key, result, cls.CACHE_TTL)
            return result
        return {'scenes': []}

    @classmethod
    def reorder_scenes_by_narrative_logic(cls, scene_texts: list[str]) -> list[int]:
        """
        Given a list of scene narration texts (already numbered/split), ask Gemini to
        return the optimal narrative order as a 0-based index list.
        e.g. scene_texts = ["Scene 3 text", "Scene 1 text", "Scene 2 text"]
             returns [1, 2, 0] meaning: put original index 1 first, then 2, then 0.
        """
        if not scene_texts:
            return list(range(len(scene_texts)))

        numbered = '\n'.join(f'[{i}] {t}' for i, t in enumerate(scene_texts))
        n = len(scene_texts)

        prompt = f"""You are a professional video editor and storytelling expert.
Below are {n} scene narration clips, labelled [0] through [{n - 1}].
They may be out of narrative order.

SCENES:
{numbered}

TASK:
Determine the best chronological / narrative order for these scenes.
Return ONLY a JSON object with key "order" whose value is an array of the original [0-based] indices in the optimal order.
Do NOT include any explanation — only JSON.

Example for 4 scenes: {{ "order": [2, 0, 3, 1] }}"""

        result = cls._call_gemini(prompt)
        if result and isinstance(result, dict) and 'order' in result:
            order = result['order']
            # Validate: must be a permutation of range(n)
            if isinstance(order, list) and sorted(order) == list(range(n)):
                return order
        # Fallback: return original order
        return list(range(n))
