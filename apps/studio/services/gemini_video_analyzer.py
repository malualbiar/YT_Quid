import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time

from django.conf import settings

from .moment_ranker import rank_and_deduplicate, validate_moment
from .shorts_engine import ShortsEngineService


logger = logging.getLogger(__name__)


class VideoAnalysisError(Exception):
    pass


VIDEO_RESPONSE_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'video_summary': {'type': 'STRING'},
        'moments': {
            'type': 'ARRAY',
            'items': {
                'type': 'OBJECT',
                'properties': {
                    'start': {'type': 'NUMBER'},
                    'end': {'type': 'NUMBER'},
                    'title': {'type': 'STRING'},
                    'description': {'type': 'STRING'},
                    'category': {'type': 'STRING'},
                    'reason': {'type': 'STRING'},
                    'suggested_duration': {'type': 'NUMBER'},
                    'needs_context': {'type': 'BOOLEAN'},
                    'score_components': {
                        'type': 'OBJECT',
                        'properties': {
                            'hook_strength': {'type': 'INTEGER'},
                            'emotional_intensity': {'type': 'INTEGER'},
                            'surprise': {'type': 'INTEGER'},
                            'humor': {'type': 'INTEGER'},
                            'payoff': {'type': 'INTEGER'},
                            'standalone_context': {'type': 'INTEGER'},
                            'audio_clarity': {'type': 'INTEGER'},
                            'visual_interest': {'type': 'INTEGER'},
                            'duration_suitability': {'type': 'INTEGER'},
                        },
                        'required': [
                            'hook_strength', 'emotional_intensity', 'surprise', 'humor',
                            'payoff', 'standalone_context', 'audio_clarity',
                            'visual_interest', 'duration_suitability',
                        ],
                    },
                },
                'required': [
                    'start', 'end', 'title', 'description', 'category', 'reason',
                    'suggested_duration', 'needs_context', 'score_components',
                ],
            },
        },
    },
    'required': ['video_summary', 'moments'],
}


def analysis_limits():
    chunk_seconds = max(60, int(getattr(settings, 'ANALYSIS_CHUNK_MINUTES', 10)) * 60)
    return {
        'model': getattr(settings, 'GEMINI_MODEL', 'gemini-3.7-flash'),
        'chunk_seconds': chunk_seconds,
        'max_video_duration': max(60, int(getattr(settings, 'MAX_VIDEO_DURATION_SECONDS', 7200))),
        'max_video_size': max(1, int(getattr(settings, 'MAX_ANALYSIS_VIDEO_SIZE_BYTES', 2 * 1024 * 1024 * 1024))),
        'max_chunks': max(1, int(getattr(settings, 'MAX_ANALYSIS_CHUNKS', 12))),
        'max_candidates': max(1, int(getattr(settings, 'MAX_CANDIDATE_MOMENTS', 12))),
        'minimum_gap': max(0.0, float(getattr(settings, 'VIRAL_MOMENT_MIN_GAP_SECONDS', 15))),
        'boundary_overlap': max(0.0, float(getattr(settings, 'ANALYSIS_BOUNDARY_OVERLAP_SECONDS', 10))),
    }


def estimate_analysis_chunks(duration_seconds, chunk_seconds=None):
    chunk_seconds = chunk_seconds or analysis_limits()['chunk_seconds']
    return max(1, math.ceil(float(duration_seconds) / chunk_seconds))


def inspect_video_duration(file_path):
    ffmpeg = ShortsEngineService.get_ffmpeg_binary()
    try:
        result = subprocess.run(
            [ffmpeg, '-i', os.path.abspath(str(file_path))],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors='ignore',
            timeout=30,
            **ShortsEngineService.get_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoAnalysisError('Could not inspect this video. Check that it is a supported video file.') from exc
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    if not match:
        raise VideoAnalysisError('Could not read the video duration. Try an MP4, MOV, MKV, or WebM file.')
    hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
    duration = hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(duration) or duration <= 0:
        raise VideoAnalysisError('The video has an invalid duration.')
    return duration


def _analysis_prompt(segment_duration, repair=False):
    prompt = f'''You are analyzing a {segment_duration:.1f}-second video segment to find viral short-form clips.

STEP 1 – TRANSCRIBE: Write out everything said in the video verbatim in the video_summary. Note the speaker (Speaker 1, Speaker 2…) for each line. Mark filler words like "um", "uh", "like", "you know", "so", "and" at sentence starts with [FILLER].

STEP 2 – UNDERSTAND: For each moment you find, analyze:
  • What is being said? (quote the key line)
  • Is there a clear hook in the first 3 seconds?
  • Where are the natural pauses and filler gaps (identify them for Step 8 silence cutting)?
  • Who is speaking — how many speakers? Any speaker change mid-clip?
  • What is visually happening (motion, facial expression, gesture, text on screen)?

STEP 3 – SCORE & SELECT: Identify the strongest moments for viral shorts. Look for:
  emotional reactions • humor • surprising statements • impressive skill • wins/fails
  tension • strong opinions • punchlines • dramatic revelations • relatable moments
  satisfying payoffs • cliffhangers • "wait for it" setups

For each moment return:
  start, end  — seconds RELATIVE to the start of THIS segment
  title       — punchy viral title (max 60 chars)
  description — what happens, why it works as a short
  category    — one of: humor | reaction | educational | skill | opinion | story | fail | win | highlight
  reason      — specific evidence from the video that makes it viral-worthy
  suggested_duration — ideal clip length in seconds (15–59)
  needs_context — true if viewer needs prior video context to understand
  score_components — rate 1–10 with explicit evidence:
    hook_strength        (strong opening grab)
    emotional_intensity  (feeling conveyed)
    surprise             (unexpected moment)
    humor                (laugh factor)
    payoff               (satisfying conclusion)
    standalone_context   (10=fully self-contained, 1=needs full video)
    audio_clarity        (speech/audio quality)
    visual_interest      (movement, composition, expression)
    duration_suitability (10=perfect for 15-60s short, 1=too long/short)

RULES:
  • Timestamps must be seconds relative to the START of this segment only.
  • Only report moments that ACTUALLY OCCUR — never invent events.
  • Prefer moments with a clear hook in the first 2–3 seconds.
  • A 10 on standalone_context means a complete stranger instantly understands it.'''
    if repair:
        prompt += '\nReturn only a schema-conforming JSON object. No markdown fences, no prose outside the JSON.'
    return prompt



def _call_gemini(client, uploaded_file, segment_duration):
    from google.genai import types

    prompt = _analysis_prompt(segment_duration)
    config = types.GenerateContentConfig(
        response_mime_type='application/json',
        response_schema=VIDEO_RESPONSE_SCHEMA,
    )

    candidate_models = []
    primary_model = analysis_limits()['model']
    for m in [primary_model, 'gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-flash-latest', 'gemini-3.7-flash']:
        if m and m not in candidate_models:
            candidate_models.append(m)

    last_error = None
    for model_name in candidate_models:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[uploaded_file, prompt],
                    config=config,
                )
                raw_text = (response.text or '').strip()
                if '```json' in raw_text:
                    raw_text = raw_text.split('```json', 1)[1].split('```', 1)[0].strip()
                elif '```' in raw_text:
                    raw_text = raw_text.split('```', 1)[1].split('```', 1)[0].strip()

                payload = json.loads(raw_text)
                if isinstance(payload, dict) and isinstance(payload.get('moments'), list):
                    return payload
                elif attempt == 0:
                    prompt = _analysis_prompt(segment_duration, repair=True)
                    continue
            except (json.JSONDecodeError, TypeError) as exc:
                last_error = exc
                if attempt == 0:
                    prompt = _analysis_prompt(segment_duration, repair=True)
                    continue
            except Exception as exc:
                last_error = exc
                err_str = str(exc).lower()
                if '503' in err_str or '429' in err_str or 'unavailable' in err_str:
                    logger.warning('Gemini video analysis %s transient error (attempt %s): %s', model_name, attempt + 1, exc)
                    time.sleep(0.7 * (attempt + 1))
                    continue
                else:
                    logger.warning('Gemini video analysis %s failed: %s', model_name, exc)
                    break

    if last_error:
        raise VideoAnalysisError(f'Gemini analysis failed: {_friendly_gemini_error(last_error)}')
    raise VideoAnalysisError('Gemini could not return valid moment data. Please retry the analysis.')


def _friendly_gemini_error(exc):
    message = str(exc).lower()
    if '429' in message or 'rate' in message or 'quota' in message:
        return 'Gemini API rate limit reached. Please wait and try again.'
    if '503' in message or 'unavailable' in message or 'temporarily' in message:
        return 'Gemini is temporarily unavailable. Please wait and try again.'
    if 'api key' in message or 'unauthorized' in message or 'permission' in message:
        return 'Gemini rejected the server API key. Check GEMINI_API_KEY.'
    return 'Gemini could not analyze this video segment. Please retry later.'


def _has_audio_track(file_path, ffmpeg):
    try:
        proc = subprocess.run(
            [ffmpeg, '-i', os.path.abspath(str(file_path))],
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            errors='ignore',
            timeout=20,
            **ShortsEngineService.get_subprocess_kwargs(),
        )
        return 'Audio:' in (proc.stderr or '')
    except Exception:
        return True


def analyze_video(file_path, duration_seconds, progress_callback=None):
    limits = analysis_limits()
    if duration_seconds > limits['max_video_duration']:
        raise VideoAnalysisError(
            f"Video exceeds the configured {limits['max_video_duration'] // 60}-minute analysis limit."
        )
    chunk_count = estimate_analysis_chunks(duration_seconds, limits['chunk_seconds'])
    if chunk_count > limits['max_chunks']:
        raise VideoAnalysisError(
            f"Analysis needs {chunk_count} segments, above the configured limit of {limits['max_chunks']}."
        )
    api_key = str(getattr(settings, 'GEMINI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '')).strip()
    if not api_key:
        raise VideoAnalysisError('Gemini is not configured. Set GEMINI_API_KEY on the server.')

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
    except Exception as exc:
        logger.exception('Unable to initialize Gemini video client')
        raise VideoAnalysisError('Gemini is unavailable. Check the server configuration.') from exc

    all_moments = []
    summaries = []
    ffmpeg = ShortsEngineService.get_ffmpeg_binary()
    overlap = limits['boundary_overlap']
    has_audio = _has_audio_track(file_path, ffmpeg)

    with tempfile.TemporaryDirectory(prefix='viral_moment_chunks_') as temp_dir:
        for index in range(chunk_count):
            base_start = index * limits['chunk_seconds']
            segment_start = max(0.0, base_start - (overlap / 2 if index else 0.0))
            segment_end = min(
                duration_seconds,
                (index + 1) * limits['chunk_seconds'] + (overlap / 2 if index < chunk_count - 1 else 0.0),
            )
            segment_duration = segment_end - segment_start

            chunk_ok = False
            last_ffmpeg_err = ''

            # Prefer lightweight audio extraction (4.5 MB vs 500 MB video) — Gemini processes audio instantly without queue timeouts
            if has_audio:
                chunk_path = os.path.join(temp_dir, f'segment_{index + 1:03d}.mp3')
                cmd = [
                    ffmpeg, '-y',
                    '-ss', f'{segment_start:.3f}',
                    '-i', os.path.abspath(str(file_path)),
                    '-t', f'{segment_duration:.3f}',
                    '-vn', '-ac', '1', '-ar', '16000', '-b:a', '64k',
                    chunk_path,
                ]
                try:
                    proc = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        errors='ignore',
                        timeout=180,
                        **ShortsEngineService.get_subprocess_kwargs(),
                    )
                    if proc.returncode == 0 and os.path.isfile(chunk_path) and os.path.getsize(chunk_path) > 0:
                        chunk_ok = True
                    else:
                        last_ffmpeg_err = (proc.stderr or '')[-500:]
                except Exception as exc:
                    last_ffmpeg_err = str(exc)

            # Fallback to video chunking only if audio extraction failed or file has no audio track
            if not chunk_ok:
                chunk_path = os.path.join(temp_dir, f'segment_{index + 1:03d}.mp4')
                _ffmpeg_base = [
                    ffmpeg, '-y',
                    '-ss', f'{segment_start:.3f}',
                    '-i', os.path.abspath(str(file_path)),
                    '-t', f'{segment_duration:.3f}',
                    '-avoid_negative_ts', 'make_zero',
                ]
                _copy_cmd = _ffmpeg_base + [
                    '-map', '0:v:0', '-map', '0:a?',
                    '-c:v', 'copy', '-c:a', 'copy',
                    '-movflags', '+faststart',
                    chunk_path,
                ]
                _transcode_cmd = _ffmpeg_base + [
                    '-map', '0:v:0', '-map', '0:a?',
                    '-vf', 'scale=-2:480',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '32', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-b:a', '96k',
                    chunk_path,
                ]
                for cmd_label, cmd in [('stream-copy', _copy_cmd), ('transcode', _transcode_cmd)]:
                    try:
                        proc = subprocess.run(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            errors='ignore',
                            timeout=300,
                            **ShortsEngineService.get_subprocess_kwargs(),
                        )
                        if proc.returncode == 0 and os.path.isfile(chunk_path) and os.path.getsize(chunk_path) > 0:
                            chunk_ok = True
                            break
                        last_ffmpeg_err = (proc.stderr or '')[-800:]
                        if os.path.isfile(chunk_path):
                            try:
                                os.remove(chunk_path)
                            except OSError:
                                pass
                    except subprocess.TimeoutExpired:
                        last_ffmpeg_err = f'{cmd_label} timed out after 300 s'
                    except OSError as exc:
                        last_ffmpeg_err = str(exc)

            if not chunk_ok:
                raise VideoAnalysisError(
                    f'Could not extract media segment {index + 1}/{chunk_count} for analysis. '
                    f'FFmpeg error: {last_ffmpeg_err[-300:]}'
                )

            uploaded_file = None
            try:
                uploaded_file = client.files.upload(file=chunk_path)
                deadline = time.monotonic() + 300
                while str(getattr(uploaded_file, 'state', '')).upper().endswith('PROCESSING'):
                    if time.monotonic() >= deadline:
                        raise VideoAnalysisError('Gemini media processing timed out. Please retry later.')
                    time.sleep(2)
                    uploaded_file = client.files.get(name=uploaded_file.name)
                if str(getattr(uploaded_file, 'state', '')).upper().endswith('FAILED'):
                    raise VideoAnalysisError('Gemini could not process this media segment. Try a supported video file.')
                result = _call_gemini(client, uploaded_file, segment_duration)
            except VideoAnalysisError:
                raise
            except Exception as exc:
                user_message = _friendly_gemini_error(exc)
                if user_message == 'Gemini could not analyze this video segment. Please retry later.':
                    logger.exception('Gemini video analysis request failed')
                else:
                    logger.warning('Gemini video analysis provider error: %s', user_message)
                raise VideoAnalysisError(user_message) from exc
            finally:
                if uploaded_file is not None:
                    try:
                        client.files.delete(name=uploaded_file.name)
                    except Exception:
                        logger.warning('Could not delete temporary Gemini upload')

            summary = str(result.get('video_summary', '')).strip()
            if summary:
                summaries.append(summary[:1500])
            for item in result['moments']:
                validated = validate_moment(item, segment_start, segment_duration, duration_seconds)
                if validated:
                    all_moments.append(validated)
            if progress_callback:
                progress_callback(index + 1, chunk_count)

    moments = rank_and_deduplicate(
        all_moments,
        minimum_gap_seconds=limits['minimum_gap'],
        maximum=limits['max_candidates'],
    )
    return {
        'video_summary': ' '.join(summaries)[:4000],
        'moments': moments,
        'chunk_count': chunk_count,
    }