import os
import sys
import shutil
import glob
import subprocess
import math
import re
import time
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw, ImageFont

from django.conf import settings
from .renderer import VideoStudioRenderer
from .process_tracker import RenderProcessTracker

class LyricsEngineService:

    @classmethod
    def get_ffmpeg_binary(cls):
        return VideoStudioRenderer.get_ffmpeg_binary()

    @classmethod
    def get_subprocess_kwargs(cls):
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        return kwargs

    @classmethod
    def inspect_media_duration(cls, file_path):
        """
        Inspects video or audio file and returns duration in seconds as float.
        """
        ffmpeg = cls.get_ffmpeg_binary()
        file_path = os.path.abspath(str(file_path))
        
        try:
            cmd = [ffmpeg, '-i', file_path]
            proc = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, errors='ignore')
            match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', proc.stderr)
            if match:
                hours = int(match.group(1))
                mins = int(match.group(2))
                secs = float(match.group(3))
                total_seconds = hours * 3600 + mins * 60 + secs
                return max(1.0, round(total_seconds, 2))
        except Exception:
            pass

        return 60.0

    @classmethod
    def parse_lrc_file(cls, lrc_text):
        """
        Parses standard .LRC synchronized lyrics text format:
        [00:12.34]Lyric line text
        Returns a sorted list of dicts:
        [{'line': '...', 'start': 12.34, 'end': 16.50}, ...]
        """
        if not lrc_text:
            return []

        lines = lrc_text.strip().splitlines()
        parsed_entries = []

        # Matches [mm:ss.xx] or [mm:ss:xx] or [mm:ss]
        pattern = re.compile(r'\[(\d{1,2}):(\d{2})(?:[\.:](\d{1,3}))?\]')

        for raw_line in lines:
            raw_line = raw_line.strip()
            if not raw_line or raw_line.startswith('[ti:') or raw_line.startswith('[ar:') or raw_line.startswith('[al:') or raw_line.startswith('[by:'):
                continue

            matches = list(pattern.finditer(raw_line))
            if not matches:
                continue

            # Strip all timestamps from the line to get the lyric text
            lyric_text = pattern.sub('', raw_line).strip()

            for match in matches:
                mins = int(match.group(1))
                secs = int(match.group(2))
                ms_raw = match.group(3)
                
                if ms_raw:
                    if len(ms_raw) == 2:
                        frac = int(ms_raw) / 100.0
                    else:
                        frac = int(ms_raw) / 1000.0
                else:
                    frac = 0.0

                timestamp = round(mins * 60 + secs + frac, 2)
                parsed_entries.append({
                    'start': timestamp,
                    'line': lyric_text
                })

        # Sort chronologically by start timestamp
        parsed_entries.sort(key=lambda x: x['start'])

        # Compute end timestamp for each line
        result = []
        for i, entry in enumerate(parsed_entries):
            start = entry['start']
            line = entry['line']
            if not line:
                continue

            if i + 1 < len(parsed_entries):
                next_start = parsed_entries[i + 1]['start']
                end = min(next_start, round(start + 8.0, 2))
                if end <= start:
                    end = round(start + 3.0, 2)
            else:
                end = round(start + 5.0, 2)

            result.append({
                'line': line,
                'start': start,
                'end': end
            })

        return result

    @classmethod
    def export_lrc_string(cls, lyrics_data, title='', artist=''):
        """
        Exports lyrics_data list to standard .LRC format string.
        """
        lines = []
        if title:
            lines.append(f"[ti:{title}]")
        if artist:
            lines.append(f"[ar:{artist}]")
        lines.append("[by:YT Quid Creator Studio]")

        for item in lyrics_data:
            start_sec = float(item.get('start', 0.0))
            mins = int(start_sec // 60)
            secs = int(start_sec % 60)
            centis = int(round((start_sec - int(start_sec)) * 100))
            lyric = item.get('line', '').strip()
            if lyric:
                lines.append(f"[{mins:02d}:{secs:02d}.{centis:02d}]{lyric}")

        return "\n".join(lines)

    @classmethod
    def auto_distribute_raw_lyrics(cls, raw_text, total_duration):
        """
        Evenly distributes raw un-synced text lines across the total audio duration.
        """
        raw_lines = [l.strip() for l in raw_text.strip().splitlines() if l.strip()]
        if not raw_lines:
            return []

        count = len(raw_lines)
        total = max(5.0, float(total_duration))
        # Add 2s lead-in padding and 2s outro padding
        lead_in = 2.0
        active_duration = max(2.0, total - lead_in - 2.0)
        slot_duration = active_duration / count

        result = []
        for idx, line in enumerate(raw_lines):
            start = round(lead_in + (idx * slot_duration), 2)
            end = round(min(total, start + slot_duration * 0.92), 2)
            result.append({
                'line': line,
                'start': start,
                'end': end
            })

        return result

    @classmethod
    def normalize_vocal_segments(cls, vocal_segments, min_gap=0.75, min_duration=1.0):
        """
        Merge nearby vocal phrases split by brief breaths or tiny instrumental gaps,
        while rejecting fragments too short to represent a sung phrase.
        """
        if not vocal_segments:
            return []

        normalized = []
        for segment in sorted(vocal_segments, key=lambda s: s.get('start', 0.0)):
            start = float(segment.get('start', 0.0))
            end = float(segment.get('end', start))
            duration = max(0.0, end - start)

            if normalized and (start - normalized[-1]['end']) <= min_gap:
                normalized[-1]['end'] = max(normalized[-1]['end'], end)
                normalized[-1]['duration'] = round(normalized[-1]['end'] - normalized[-1]['start'], 2)
            else:
                normalized.append({
                    'start': round(start, 2),
                    'end': round(end, 2),
                    'duration': round(duration, 2)
                })

        filtered = []
        for segment in normalized:
            duration = max(0.0, segment['end'] - segment['start'])
            if duration >= min_duration:
                filtered.append(segment)

        return filtered

    @classmethod
    def detect_vocal_segments(cls, audio_path, min_silence_duration=0.35, noise_threshold_db=-35):
        """
        Uses vocal frequency bandpass filtering (300Hz-3400Hz) and silence detection
        to identify active vocal singing segments vs instrumental intros, solos & breaks.
        Returns list of dicts:
        [{'start': 12.4, 'end': 16.8, 'duration': 4.4}, ...]
        """
        ffmpeg = cls.get_ffmpeg_binary()
        audio_path = os.path.abspath(str(audio_path))
        total_duration = cls.inspect_media_duration(audio_path)

        # 1. Focus on the vocal formant band while trimming obvious low-end percussion energy.
        # 2. Use a stricter gain curve so kick/snare transients do not dominate the result.
        # 3. Silence detector is then applied to sustained vocal-like activity instead of broad mix energy.
        af_filter = (
            f"highpass=f=120,"
            f"lowpass=f=3000,"
            f"compand=attacks=0.02:decays=0.12:points=-70/-70|-30/-12|-10/-3|0/0,"
            f"silencedetect=noise={noise_threshold_db}dB:d={min_silence_duration}"
        )

        cmd = [
            ffmpeg, '-i', audio_path,
            '-af', af_filter,
            '-f', 'null', '-'
        ]

        try:
            proc = subprocess.run(
                cmd,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                errors='ignore'
            )

            silence_ranges = []
            cur_silence_start = None

            for line in proc.stderr.splitlines():
                if 'silence_start:' in line:
                    m = re.search(r'silence_start:\s*([0-9.]+)', line)
                    if m:
                        cur_silence_start = float(m.group(1))
                elif 'silence_end:' in line:
                    m = re.search(r'silence_end:\s*([0-9.]+)', line)
                    if m:
                        end_t = float(m.group(1))
                        start_t = cur_silence_start if cur_silence_start is not None else 0.0
                        silence_ranges.append((start_t, end_t))
                        cur_silence_start = None

            vocal_segments = []
            last_end = 0.0

            for s_start, s_end in silence_ranges:
                if s_start > last_end + 0.5:
                    vocal_segments.append({
                        'start': round(last_end, 2),
                        'end': round(s_start, 2),
                        'duration': round(s_start - last_end, 2)
                    })
                last_end = s_end

            if total_duration > last_end + 0.5:
                vocal_segments.append({
                    'start': round(last_end, 2),
                    'end': round(total_duration, 2),
                    'duration': round(total_duration - last_end, 2)
                })

            valid_segments = [s for s in vocal_segments if s['duration'] >= 0.9]
            valid_segments = cls.normalize_vocal_segments(valid_segments)
            return valid_segments if valid_segments else [{'start': 2.0, 'end': total_duration - 1.0, 'duration': total_duration - 3.0}]

        except Exception:
            return [{'start': 2.0, 'end': total_duration - 1.0, 'duration': total_duration - 3.0}]

    @classmethod
    def align_lyrics_with_vocal_segments(cls, raw_text, vocal_segments, total_duration):
        """
        Maps raw lyrics lines to detected vocal frequency segments, ensuring
        no lyrics are displayed during instrumental solos, intros, and drum breaks.
        """
        lines = [l.strip() for l in raw_text.strip().splitlines() if l.strip()]
        cleaned_lines = [l for l in lines if not (l.startswith('[') and l.endswith(']'))]
        if not cleaned_lines:
            return []

        if not vocal_segments:
            return cls.auto_distribute_raw_lyrics(raw_text, total_duration)

        line_count = len(cleaned_lines)
        seg_count = len(vocal_segments)

        result = []

        if line_count <= seg_count:
            for idx, line in enumerate(cleaned_lines):
                seg_idx = int(idx * (seg_count / line_count))
                seg = vocal_segments[min(seg_idx, seg_count - 1)]
                seg_dur = max(1.0, seg['end'] - seg['start'])
                lead_in = seg_dur * 0.16
                start = round(seg['start'] + lead_in, 2)
                end = round(min(seg['end'], seg['start'] + seg_dur * 0.9), 2)
                if end <= start:
                    end = round(min(seg['end'], start + 0.9), 2)
                result.append({
                    'line': line,
                    'start': start,
                    'end': end
                })
        else:
            lines_left = line_count
            for seg_idx, seg in enumerate(vocal_segments):
                if lines_left <= 0:
                    break

                remaining_slots = max(1, seg_count - seg_idx)
                target_lines = max(1, math.ceil(lines_left / remaining_slots))
                seg_lines = cleaned_lines[len(result):len(result) + target_lines]
                if not seg_lines:
                    break

                seg_dur = max(1.0, seg['end'] - seg['start'])
                phrase_start = seg['start'] + (seg_dur * 0.18)
                usable_dur = max(0.8, seg['end'] - phrase_start)
                sub_slot = usable_dur / len(seg_lines)

                for s_idx, s_line in enumerate(seg_lines):
                    start = round(phrase_start + (s_idx * sub_slot), 2)
                    end = round(min(seg['end'], start + max(0.9, sub_slot * 0.9)), 2)
                    if end <= start:
                        end = round(min(seg['end'], start + 0.9), 2)
                    result.append({
                        'line': s_line,
                        'start': start,
                        'end': end
                    })

                lines_left -= len(seg_lines)

        return result

    @classmethod
    def fetch_online_synced_lyrics(cls, title, artist=''):
        """
        Queries the free public LRCLIB database for 1-click synchronized lyrics.
        Returns dict with success status, lyrics_data list, and raw LRC content.
        """
        import urllib.request
        import urllib.parse
        import json

        title = str(title).strip()
        artist = str(artist).strip()

        if not title:
            return {'success': False, 'message': 'Please provide a song title.'}

        params = {'track_name': title}
        if artist:
            params['artist_name'] = artist

        url = f"https://lrclib.net/api/get?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'YTQuid/1.2.0 (contact@ytquid.app)'})

        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                synced = data.get('syncedLyrics', '')
                if synced:
                    parsed = cls.parse_lrc_file(synced)
                    return {
                        'success': True,
                        'is_synced': True,
                        'lyrics_data': parsed,
                        'lrc_text': synced,
                        'plain_lyrics': data.get('plainLyrics', ''),
                        'track_name': data.get('trackName', title),
                        'artist_name': data.get('artistName', artist)
                    }
        except Exception:
            pass

        query_str = f"{title} {artist}".strip()
        search_url = f"https://lrclib.net/api/search?q={urllib.parse.quote(query_str)}"
        req_search = urllib.request.Request(search_url, headers={'User-Agent': 'YTQuid/1.2.0 (contact@ytquid.app)'})

        try:
            with urllib.request.urlopen(req_search, timeout=4) as resp:
                results = json.loads(resp.read().decode('utf-8'))
                for item in results:
                    synced = item.get('syncedLyrics')
                    if synced:
                        parsed = cls.parse_lrc_file(synced)
                        return {
                            'success': True,
                            'is_synced': True,
                            'lyrics_data': parsed,
                            'lrc_text': synced,
                            'plain_lyrics': item.get('plainLyrics', ''),
                            'track_name': item.get('trackName', title),
                            'artist_name': item.get('artistName', artist)
                        }

                if results and results[0].get('plainLyrics'):
                    return {
                        'success': True,
                        'is_synced': False,
                        'lyrics_data': [],
                        'plain_lyrics': results[0].get('plainLyrics'),
                        'track_name': results[0].get('trackName', title),
                        'artist_name': results[0].get('artistName', artist)
                    }
        except Exception as e:
            return {'success': False, 'message': f'Search query failed: {str(e)}'}

        return {'success': False, 'message': f'No synced lyrics found for "{title}". You can use Vocal Frequency Auto-Sync instead.'}

    # ---------------------------------------------------------------------------
    # Filler / annotation normalisation applied before bar-building.
    #
    # Two categories:
    #   STRIP  — pure speech disfluencies or Whisper annotation artifacts that
    #            have no place in song lyrics (uh, um, [music], etc.)
    #   NORMALISE — real sung sounds that Whisper stretches with extra letters;
    #            collapsed to their canonical lyric spelling so the canvas
    #            renders them cleanly (ohhhh → Oh, hmmm → Hmm, etc.)
    # ---------------------------------------------------------------------------
    _FILLER_STRIP = {
        # Speech disfluencies
        'uh', 'um', 'er', 'err', 'uhh', 'umm', 'uhm',
        # Whisper annotation tokens
        '[music]', '[singing]', '[applause]', '[laughter]',
        '[noise]', '[silence]', '[inaudible]', '(music)',
        '(singing)', '(applause)', '(laughter)', '(inaudible)',
        # Bare music-note Unicode (shouldn't reach here but belt-and-braces)
        '\u266a', '\u266b',
    }

    # Maps a normalised lowercase stem → canonical display form.
    # Applied after collapsing repeated characters (ohhhh → oh → Oh).
    _FILLER_NORMALISE = {
        'oh':   'Oh',
        'ohh':  'Oh',
        'ooh':  'Ooh',
        'ah':   'Ah',
        'ahh':  'Ah',
        'aah':  'Ah',
        'hmm':  'Hmm',
        'hm':   'Hmm',
        'mm':   'Mm',
        'mmm':  'Mmm',
        'na':   'Na',
        'la':   'La',
        'hey':  'Hey',
        'woah': 'Woah',
        'whoa': 'Woah',
        'yeah': 'Yeah',
        'yah':  'Yeah',
        'ya':   'Ya',
        'yo':   'Yo',
        'oo':   'Ooh',
        'ooo':  'Ooh',
    }

    @classmethod
    def _normalise_word(cls, word):
        """
        Normalise a single Whisper output word:
        - Returns None  → caller should drop the word entirely (filler/artifact)
        - Returns str   → cleaned display form to use instead

        Steps:
        1. Strip surrounding punctuation/brackets to get a clean token.
        2. Check the strip list (exact match, case-insensitive).
        3. Collapse runs of 3+ identical letters to 2 (ohhhh → ohh, hmmm → hmm).
        4. Check the normalise map; if found, return the canonical form.
        5. Otherwise return the original word unchanged.
        """
        import re
        stripped = word.strip().strip('[](){}.,!?;:\'"').lower()
        if not stripped:
            return None
        if stripped in cls._FILLER_STRIP:
            return None
        # Collapse triple-or-more repeated characters: "ohhhh" → "ohh", "hmmm" → "hmm"
        collapsed = re.sub(r'(.)\1{2,}', r'\1\1', stripped)
        if collapsed in cls._FILLER_NORMALISE:
            return cls._FILLER_NORMALISE[collapsed]
        # Also try single-char collapse for cases like "ooh" vs "oooh"
        single = re.sub(r'(.)\1+', r'\1', stripped)
        if single in cls._FILLER_NORMALISE:
            return cls._FILLER_NORMALISE[single]
        return word  # unchanged — not a known filler

    @classmethod
    def format_words_into_lyric_bars(cls, words, max_words=7, max_chars=36, max_duration=4.2, min_pause=0.35):
        """
        Splits a continuous stream of timestamped words into clean, rhythmic song lyric bars (short lines).
        Uses vocal breath pauses, punctuation, word counts, and max duration to create optimal song bars.
        """
        if not words:
            return []

        # Pre-pass: normalise / strip filler words before building bars
        cleaned = []
        for w in words:
            result = cls._normalise_word(w['word'])
            if result is None:
                continue  # strip this word
            cleaned.append({**w, 'word': result})
        words = cleaned
        if not words:
            return []

        bars = []
        current_bar_words = []

        def flush_bar(bar_words):
            if not bar_words:
                return
            bar_start = round(bar_words[0]['start'], 2)
            bar_end = round(max(bar_words[-1]['end'], bar_start + 0.8), 2)
            line = " ".join(x['word'].strip() for x in bar_words).strip()
            if line:
                bars.append({
                    'line': line,
                    'start': bar_start,
                    'end': bar_end,
                    'words': list(bar_words)
                })

        for i, w in enumerate(words):
            if not current_bar_words:
                current_bar_words = [w]
                continue

            prev_w = current_bar_words[-1]
            pause_after = max(0.0, w['start'] - prev_w['end'])
            word_count = len(current_bar_words) + 1
            char_len = sum(len(x['word']) for x in current_bar_words) + len(w['word']) + (word_count - 1)
            bar_dur = w['end'] - current_bar_words[0]['start']
            prev_text = prev_w['word'].strip()

            is_pause_split = (pause_after >= min_pause and len(current_bar_words) >= 2) or (pause_after >= 0.6)
            is_punct_split = prev_text.endswith((',', '.', '!', '?', ';', ':', '—', '-')) and len(current_bar_words) >= 2
            is_length_split = (word_count > max_words) or (char_len >= max_chars)
            is_duration_split = (bar_dur >= max_duration and len(current_bar_words) >= 2)

            if is_pause_split or is_punct_split or is_length_split or is_duration_split:
                flush_bar(current_bar_words)
                current_bar_words = []

            current_bar_words.append(w)

        flush_bar(current_bar_words)

        # Trim bar end times and inject ♪ instrumental placeholders for gaps
        # longer than this threshold — these are intros, solos, and bridges
        # where no vocals are present. Without this, the screen is blank for
        # potentially 20–30 seconds with no feedback to the viewer.
        INSTRUMENTAL_GAP_THRESHOLD = 12.0  # seconds — raised from 4s; short missed sections shouldn't become instrumentals

        filled = []
        for idx in range(len(bars)):
            bar = bars[idx]

            if idx + 1 < len(bars):
                next_start = bars[idx + 1]['start']
                # Clamp overlapping or nearly-adjacent bar ends
                if bar['end'] > next_start:
                    bar['end'] = round(next_start, 2)
                elif next_start - bar['end'] < 0.6:
                    bar['end'] = round(next_start, 2)

                filled.append(bar)

                # Insert instrumental placeholder if the gap to the next bar
                # is long enough to be a meaningful instrumental section
                gap = bars[idx + 1]['start'] - bar['end']
                if gap >= INSTRUMENTAL_GAP_THRESHOLD:
                    instr_start = round(bar['end'] + 0.3, 2)
                    instr_end = round(bars[idx + 1]['start'] - 0.3, 2)
                    if instr_end > instr_start:
                        filled.append({
                            'line': '♪',
                            'start': instr_start,
                            'end': instr_end,
                            'words': []
                        })
            else:
                filled.append(bar)

        return filled

    @classmethod
    def separate_vocals_with_demucs(cls, audio_path, output_dir=None):
        """
        Runs Facebook Demucs (htdemucs model) to separate the vocal stem from
        a mixed audio/video track.  Returns (vocals_path, diagnostics_dict).
        vocals_path is None if separation failed; diagnostics carries the reason.

        Demucs writes its output under:
            <output_dir>/htdemucs/<track_name>/vocals.wav
        """
        import tempfile

        audio_path = os.path.abspath(str(audio_path))
        diag = {'stdout': '', 'stderr': '', 'returncode': None, 'error': None}

        if not os.path.exists(audio_path):
            diag['error'] = f'Input file not found: {audio_path}'
            return None, diag

        if output_dir is None:
            output_dir = tempfile.mkdtemp(prefix='demucs_')

        try:
            cmd = [
                sys.executable, '-m', 'demucs',
                '-n', 'htdemucs',
                '--two-stems', 'vocals',
                '--out', output_dir,
                audio_path
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors='ignore',
                **cls.get_subprocess_kwargs()
            )
            diag['returncode'] = proc.returncode
            diag['stdout'] = proc.stdout[-2000:] if proc.stdout else ''
            diag['stderr'] = proc.stderr[-2000:] if proc.stderr else ''

            # Demucs places output at: <out>/htdemucs/<stem_name>/vocals.wav
            stem_name = os.path.splitext(os.path.basename(audio_path))[0]
            vocals_path = os.path.join(output_dir, 'htdemucs', stem_name, 'vocals.wav')

            if os.path.exists(vocals_path):
                return vocals_path, diag

            # Fallback: search recursively in case the model subfolder name differs
            matches = glob.glob(os.path.join(output_dir, '**', 'vocals.wav'), recursive=True)
            if matches:
                return matches[0], diag

            diag['error'] = (
                f'vocals.wav not found after Demucs ran '
                f'(exit {proc.returncode}). '
                f'stderr tail: {diag["stderr"][-500:]}'
            )

        except Exception as exc:
            diag['error'] = str(exc)

        return None, diag

    @classmethod
    def transcribe_and_sync_with_whisper(cls, audio_path, model_size='base', initial_prompt=None, use_demucs=False):
        """
        Uses local Whisper AI (faster-whisper) with word-level timestamping
        to automatically transcribe speech/singing and generate short, rhythmic song lyric bars.
        Works 100% offline for any song (AI generated, Suno, Udio, unreleased tracks, or commercial).
        """
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise ImportError("faster-whisper is not installed. Please install it with: pip install faster-whisper")

        audio_path = os.path.abspath(str(audio_path))
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        import tempfile

        # If input is a video file, pre-extract audio to a clean 16kHz mono WAV.
        # This avoids codec issues and ensures Whisper processes the complete audio track.
        _video_exts = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v', '.flv'}
        extracted_wav = None
        if os.path.splitext(audio_path)[1].lower() in _video_exts:
            ffmpeg = cls.get_ffmpeg_binary()
            tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            tmp.close()
            extracted_wav = tmp.name
            cmd = [
                ffmpeg, '-y', '-i', audio_path,
                '-vn',                # drop video stream
                '-ac', '1',          # mono
                '-ar', '16000',      # 16kHz — Whisper's native rate
                '-c:a', 'pcm_s16le', # uncompressed WAV
                extracted_wav
            ]
            result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            if result.returncode != 0 or not os.path.exists(extracted_wav):
                extracted_wav = None
            else:
                audio_path = extracted_wav

        # --- Demucs vocal separation ----------------------------------------
        # When enabled, run the full mix through Demucs before Whisper.
        # Demucs isolates the vocal stem so Whisper only hears singing — no
        # drums, bass, guitars, or reverb tails.  This dramatically reduces
        # hallucinations and missed words, especially on dense productions.
        demucs_dir = None
        vocals_wav = None
        demucs_diag = {}
        if use_demucs:
            demucs_dir = tempfile.mkdtemp(prefix='demucs_')
            vocals_wav, demucs_diag = cls.separate_vocals_with_demucs(
                audio_path, output_dir=demucs_dir
            )
            if vocals_wav and os.path.exists(vocals_wav):
                audio_path = vocals_wav
            # Separation failed — fall through to the raw mix so the user still
            # gets *something*, but we record the reason so it surfaces in the
            # API response and the frontend can warn the user.

        try:
            from difflib import SequenceMatcher

            # ----------------------------------------------------------------
            # Model & decode settings — tuned separately for two operating
            # modes:
            #
            #   MIXED (no Demucs): Whisper hears full song mix — drums, bass,
            #   reverb, and instruments all compete with the vocal.  We must
            #   keep conservative settings to avoid hallucination explosions.
            #
            #   DEMUCS (clean vocal stem): Whisper only hears the isolated
            #   singer.  We can push quality settings much harder because the
            #   ambiguity that causes hallucinations is gone.
            # ----------------------------------------------------------------

            if use_demucs and vocals_wav:
                # Clean vocal stem — keep the user-requested model size.
                # Do NOT silently upgrade to medium/large: those models are
                # ~1-1.5 GB and trigger a download on first use, adding
                # 5-10 minutes to the first transcription.  base (145 MB,
                # already cached from normal use) is excellent on a clean
                # isolated vocal stem — the accuracy boost from medium is
                # marginal and the time cost is enormous on CPU-only machines.
                effective_model  = model_size   # respect what the user chose
                effective_beam   = 5    # beam 10 on CPU doubles transcription time for tiny gain
                # VAD MUST stay off even on a clean stem.  Whisper's internal
                # speech classifier was trained on speech, not singing.  Sung
                # syllables, falsetto, breathy verses, and held notes all score
                # below VAD's threshold and get silently dropped — exactly the
                # "missing lyrics" symptom being reported.
                effective_vad    = False
                # 0.45 is the sweet spot for singing:
                #   - rejects true silence on the clean stem (better than 0.3)
                #   - keeps breathy/quiet sung passages (better than 0.6)
                effective_thresh = 0.45
                # Small fallback temperatures let Whisper attempt a lower-
                # confidence decode rather than silently skipping a hard segment.
                effective_temp   = [0.0, 0.2, 0.4]
                effective_cratio = 1.8
                prompt_text = "Lyrics:"
            else:
                # Conservative settings for a noisy full mix.
                effective_model  = model_size
                effective_beam   = 5
                effective_vad    = False  # VAD kills quiet vocal passages on a mix
                effective_thresh = 0.2    # lowered from 0.3 — keeps quiet/breathy vocal passages
                effective_temp   = None   # Whisper's default fallback schedule
                effective_cratio = 2.4
                prompt_text      = "Lyrics:"

            model = WhisperModel(effective_model, device="cpu", compute_type="int8")

            transcribe_kwargs = dict(
                beam_size=effective_beam,
                word_timestamps=True,
                vad_filter=effective_vad,
                condition_on_previous_text=False,
                no_speech_threshold=effective_thresh,
                compression_ratio_threshold=effective_cratio,
                initial_prompt=prompt_text,
                # 25-second chunks instead of the default 30.
                # Shorter windows mean vocal phrases are less likely to land
                # exactly on a chunk boundary and get split mid-word.
                chunk_length=25,
                prepend_punctuations="\"'\u00bf([{-",
                append_punctuations="\"'.,\uff0c!\uff01?\uff1f:\uff1a\u201d)}\u3001",
            )
            if effective_temp is not None:
                transcribe_kwargs['temperature'] = tuple(effective_temp) if isinstance(effective_temp, list) else effective_temp

            # ---- helper: extract a time slice from the audio file -----------
            def _extract_slice(src, t_start, t_end, suffix='.wav'):
                """Cut [t_start, t_end] from src into a temp file. Returns path."""
                tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                tmp.close()
                ffmpeg = cls.get_ffmpeg_binary()
                duration = max(0.5, t_end - t_start)
                cmd = [
                    ffmpeg, '-y',
                    '-ss', str(round(t_start, 3)),
                    '-t',  str(round(duration, 3)),
                    '-i',  src,
                    '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
                    tmp.name
                ]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               **cls.get_subprocess_kwargs())
                return tmp.name if os.path.exists(tmp.name) else None

            # ---- helper: run Whisper on a single slice ----------------------
            def _transcribe_slice(slice_path, t_offset, relaxed=True):
                """
                Transcribe a short audio slice and return words with times
                offset by t_offset so they align with the full track.
                Uses more permissive thresholds to catch quiet/breathy segments
                that the first pass rejected.
                """
                slice_kwargs = dict(
                    beam_size=effective_beam,
                    word_timestamps=True,
                    vad_filter=False,
                    condition_on_previous_text=False,
                    # Much more permissive — accept almost anything
                    no_speech_threshold=0.15 if relaxed else effective_thresh,
                    compression_ratio_threshold=2.4,
                    initial_prompt=prompt_text,
                    chunk_length=25,
                    prepend_punctuations="\"'\u00bf([{-",
                    append_punctuations="\"'.,\uff0c!\uff01?\uff1f:\uff1a\u201d)}\u3001",
                    temperature=(0.0, 0.2, 0.4, 0.6),
                )
                recovered = []
                try:
                    s_segs, _ = model.transcribe(slice_path, **slice_kwargs)
                    for seg in s_segs:
                        if not seg.text or not seg.text.strip():
                            continue
                        if seg.words:
                            for w in seg.words:
                                rw = w.word.strip()
                                if rw and rw not in ('♪', '♫', '🎵', '🎶'):
                                    recovered.append({
                                        'word':  rw,
                                        'start': round(w.start + t_offset, 2),
                                        'end':   round(w.end   + t_offset, 2),
                                    })
                        else:
                            words_list = seg.text.strip().split()
                            dur  = max(0.5, seg.end - seg.start)
                            slot = dur / max(1, len(words_list))
                            for s_i, wt in enumerate(words_list):
                                if wt.strip() and wt not in ('♪', '♫'):
                                    recovered.append({
                                        'word':  wt,
                                        'start': round(seg.start + s_i * slot + t_offset, 2),
                                        'end':   round(seg.start + (s_i + 1) * slot + t_offset, 2),
                                    })
                except Exception:
                    pass
                return recovered

            segments, info = model.transcribe(audio_path, **transcribe_kwargs)

            all_words = []
            _prompt_lower = prompt_text.lower().strip().rstrip(':').strip()

            # Known Whisper hallucination phrases — exact full-segment matches only
            _HALLUCINATION_PHRASES = {
                "song lyrics. transcribe every sung word exactly as heard, including repeated phrases, chorus lines, and ad-libs. do not summarise or paraphrase",
                "song lyrics. transcribe every sung word exactly as heard, including repeated phrases, chorus lines, and ad-libs.",
                "song lyrics formatted in short musical bars and rhyming verse lines",
                "lyrics formatted in short musical bars and rhyming verse lines",
                "thank you for watching",
                "thanks for watching",
                "please subscribe",
                "like and subscribe",
                "subtitles by",
                "transcribed by",
                "lyrics",
            }

            def _is_hallucination(text):
                t = text.lower().strip().rstrip('.')
                if t in _HALLUCINATION_PHRASES:
                    return True
                if _prompt_lower and t == _prompt_lower:
                    return True
                return False

            # Fuzzy repeat suppression — hallucination loops repeat 10-20+ times
            # with near-identical text. Real choruses repeat 2-4 times with minor
            # variation. Suppress only when similarity > 0.92 AND 5+ consecutive runs.
            # With Demucs we tighten to 0.95 / 6 repeats — a clean stem rarely
            # produces false positives so we can be slightly more aggressive.
            _repeat_sim   = 0.95 if (use_demucs and vocals_wav) else 0.92
            _repeat_limit = 6    if (use_demucs and vocals_wav) else 5
            _repeat_text  = None
            _repeat_count = 0

            def _is_hallucination_loop(text):
                nonlocal _repeat_text, _repeat_count
                if _repeat_text is None:
                    _repeat_text = text
                    _repeat_count = 1
                    return False
                ratio = SequenceMatcher(None, text.lower(), _repeat_text.lower()).ratio()
                if ratio > _repeat_sim:
                    _repeat_count += 1
                else:
                    _repeat_text = text
                    _repeat_count = 1
                return _repeat_count >= _repeat_limit

            # On a clean Demucs vocal stem, Whisper sometimes outputs bare ♪/♫
            # tokens for silent gaps (no instruments to fill the space).  We strip
            # those here — format_words_into_lyric_bars inserts its own instrumental
            # placeholders based on timing gaps, which are more accurate.
            # On a mixed track we keep them (they mark real instrumental sections).
            _strip_music_tokens = use_demucs and bool(vocals_wav)

            for seg in segments:
                seg_text = seg.text.strip() if seg.text else ''
                if not seg_text:
                    continue
                if _is_hallucination(seg_text):
                    continue
                if _is_hallucination_loop(seg_text):
                    continue

                if seg.words:
                    for w in seg.words:
                        raw_w = w.word.strip()
                        if not raw_w:
                            continue
                        # Strip bare music-note tokens on the Demucs path
                        if _strip_music_tokens and raw_w in ('♪', '♫', '🎵', '🎶'):
                            continue
                        all_words.append({
                            'word': raw_w,
                            'start': max(0.0, round(w.start - 0.15, 2)),
                            'end': max(0.0, round(w.end - 0.15, 2))
                        })
                else:
                    words_list = seg_text.split()
                    dur  = max(0.5, seg.end - seg.start)
                    slot = dur / len(words_list)
                    for s_idx, wt in enumerate(words_list):
                        if _strip_music_tokens and wt in ('♪', '♫', '🎵', '🎶'):
                            continue
                        all_words.append({
                            'word': wt,
                            'start': max(0.0, round(seg.start + s_idx * slot - 0.15, 2)),
                            'end': max(0.0, round(seg.start + (s_idx + 1) * slot - 0.15, 2))
                        })

            # -----------------------------------------------------------------
            # WORD-DENSITY HALLUCINATION FILTER
            # Whisper hallucinates a single short word ("Hey", "Yeah", "Oh")
            # repeated dozens of times over an instrumental section.  The
            # segment-level repeat suppressor above stops new segments once
            # the limit fires, but the first 4-5 "allowed" repeats still land
            # in all_words.  This pass catches the remaining loop debris:
            #
            # Rule: if the same normalised word appears 5+ times within any
            # 3-second window, every occurrence of that word inside that window
            # is removed.  Real lyrics don't repeat a single word 5 times in
            # 3 seconds; hallucination loops always do.
            # -----------------------------------------------------------------
            _DENSITY_WINDOW  = 3.0   # seconds
            _DENSITY_MAX     = 4     # max occurrences of same word in window

            if all_words:
                # Walk through all_words; build a set of indices to drop.
                drop_indices = set()
                n = len(all_words)
                i = 0
                while i < n:
                    w_norm = all_words[i]['word'].strip().lower().strip('.,!?')
                    if not w_norm:
                        i += 1
                        continue
                    # Collect all words with same normalised form within window
                    window = [i]
                    j = i + 1
                    while j < n and all_words[j]['start'] - all_words[i]['start'] <= _DENSITY_WINDOW:
                        if all_words[j]['word'].strip().lower().strip('.,!?') == w_norm:
                            window.append(j)
                        j += 1
                    if len(window) > _DENSITY_MAX:
                        drop_indices.update(window)
                    i += 1

                if drop_indices:
                    all_words = [w for k, w in enumerate(all_words) if k not in drop_indices]

            # -----------------------------------------------------------------
            # GAP-RETRY PASS
            # After the first full transcription, scan all_words for any gap
            # > 8 seconds between consecutive words.  These are sections
            # Whisper silently skipped (quiet verse, falsetto, breathy passage,
            # chunk-boundary misalignment).  Re-run Whisper on each gap slice
            # with very permissive thresholds and merge recovered words in.
            # Limit retries to avoid excessive processing on truly instrumental
            # sections (e.g. a real 2-minute guitar solo).
            # -----------------------------------------------------------------
            GAP_RETRY_THRESHOLD  = 8.0  # seconds — retry any gap over 8s
            GAP_RETRY_MAX_SLICES = 4    # max 4 retries — each is a full Whisper pass on CPU
            total_duration_secs  = round(getattr(info, 'duration', 0.0), 2)
            slice_temps = []  # track slice files for cleanup

            if all_words and total_duration_secs > 0:
                # Build candidate gaps from the word list
                gap_ranges = []

                # Gap before the first word (intro vocals missed?)
                if all_words[0]['start'] > GAP_RETRY_THRESHOLD:
                    gap_ranges.append((0.0, all_words[0]['start']))

                # Gaps between consecutive words
                for gi in range(len(all_words) - 1):
                    g_end   = all_words[gi]['end']
                    g_start = all_words[gi + 1]['start']
                    if g_start - g_end > GAP_RETRY_THRESHOLD:
                        gap_ranges.append((g_end, g_start))

                # Gap after the last word (outro vocals missed?)
                if total_duration_secs - all_words[-1]['end'] > GAP_RETRY_THRESHOLD:
                    gap_ranges.append((all_words[-1]['end'], total_duration_secs))

                # Process up to GAP_RETRY_MAX_SLICES gaps, largest first
                gap_ranges.sort(key=lambda r: r[1] - r[0], reverse=True)
                recovered_words = []

                for gap_start, gap_end in gap_ranges[:GAP_RETRY_MAX_SLICES]:
                    # Add a small overlap on each side so words right at the
                    # boundary aren't missed by the slice extraction
                    slice_start = max(0.0, gap_start - 0.5)
                    slice_end   = min(total_duration_secs, gap_end + 0.5)
                    t_offset    = slice_start

                    slice_path = _extract_slice(audio_path, slice_start, slice_end)
                    if not slice_path:
                        continue
                    slice_temps.append(slice_path)

                    new_words = _transcribe_slice(slice_path, t_offset=t_offset)
                    # Only keep words that fall inside the gap window
                    # (the overlap padding can produce duplicates at the edges)
                    for w in new_words:
                        if w['start'] >= gap_start - 0.1 and w['end'] <= gap_end + 0.1:
                            recovered_words.append(w)

                if recovered_words:
                    all_words.extend(recovered_words)
                    all_words.sort(key=lambda w: w['start'])

            # Format timestamped words into clean, short song lyric bars.
            # With a clean Demucs stem we allow slightly longer bars (8 words /
            # 42 chars) because word boundaries are crisp and reliable.
            if use_demucs and vocals_wav:
                lyrics_data = cls.format_words_into_lyric_bars(all_words, max_words=8, max_chars=42)
            else:
                lyrics_data = cls.format_words_into_lyric_bars(all_words, max_words=7, max_chars=36)
            plain_lines = [b['line'] for b in lyrics_data]

            result = {
                'success': True,
                'lyrics_data': lyrics_data,
                'plain_lyrics': "\n".join(plain_lines),
                'detected_language': info.language,
                'language_probability': round(getattr(info, 'language_probability', 1.0), 2),
                'duration': round(getattr(info, 'duration', 0.0), 2),
                # Surface Demucs status so the API can report it accurately
                'demucs_applied': bool(use_demucs and vocals_wav),
                'demucs_failed': bool(use_demucs and not vocals_wav),
                'demucs_error': demucs_diag.get('error') if (use_demucs and not vocals_wav) else None,
                'whisper_model': effective_model,
            }
            return result
        finally:
            # Clean up temp extracted WAV if we created one from a video file
            if extracted_wav and os.path.exists(extracted_wav):
                try:
                    os.remove(extracted_wav)
                except Exception:
                    pass
            # Clean up the entire Demucs temp directory (vocals.wav + no_vocals.wav)
            if demucs_dir and os.path.isdir(demucs_dir):
                try:
                    shutil.rmtree(demucs_dir, ignore_errors=True)
                except Exception:
                    pass
            # Clean up gap-retry slice temp files
            for _sp in slice_temps if 'slice_temps' in dir() else []:
                try:
                    if os.path.exists(_sp):
                        os.remove(_sp)
                except Exception:
                    pass

    @classmethod
    def hex_to_ass_color(cls, hex_str, alpha=0):
        """
        Converts hex color (e.g. #00E5FF or #FFFFFF) to ASS color format &HAABBGGRR&.
        """
        hex_str = hex_str.lstrip('#')
        if len(hex_str) == 3:
            hex_str = "".join([c * 2 for c in hex_str])
        if len(hex_str) != 6:
            hex_str = "FFFFFF"

        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        a = max(0, min(255, int(alpha)))

        # ASS uses &HAABBGGRR (Blue, Green, Red)
        return f"&H{a:02X}{b:02X}{g:02X}{r:02X}&"

    @classmethod
    def seconds_to_ass_time(cls, seconds):
        """
        Converts seconds float to ASS time format: H:MM:SS.cs
        """
        sec = max(0.0, float(seconds))
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        cs = int(round((sec - int(sec)) * 100))
        if cs >= 100:
            cs = 99
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    @classmethod
    def generate_ass_subtitles(
        cls,
        lyrics_data,
        output_ass_path,
        aspect_ratio='16:9',
        animation_style='KARAOKE_WIPE',
        font_family='Arial',
        font_size=48,
        highlight_color='#00E5FF',
        text_color='#FFFFFF',
        position_mode='CENTER',
        font_weight='bold',
        font_italic=False,
        letter_spacing=0.0,
        line_height=1.4,
        text_transform='none',
        text_stroke_width=2.5,
        text_shadow_depth=2.0,
        font_scale_x=100,
        font_scale_y=100,
        bg_opacity=0,
        title='',
        artist='',
        video_duration=0.0,
        time_offset=0.0,
    ):
        """
        Generates Advanced SubStation Alpha (.ass) subtitle file with karaoke wipes,
        glowing typography, drop shadows, and multi-line animations.
        """
        # Map CSS font-family name → (internal TTF name, safe filename stem).
        # FFmpeg's ass= filter matches on the internal font name, not the filename.
        # Mismatches cause silent fallback to Arial.
        _FONT_NAME_MAP = {
            'DynaPuff':       ('DynaPuff',        'DynaPuff'),
            'Ranchers':       ('Ranchers',         'Ranchers'),
            'Slackey':        ('Slackey',          'Slackey'),
            'Rubik 80s Fade': ('Rubik 80s Fade',   'Rubik80sFade'),
            'Rubik Iso':      ('Rubik Iso',         'RubikIso'),
            'Press Start 2P': ('Press Start 2P',    'PressStart2P'),
            'Limelight':      ('Limelight',         'Limelight'),
            'Monoton':        ('Monoton',           'Monoton'),
            'Nosifer':        ('Nosifer',           'Nosifer'),
            'Creepster':      ('Creepster',         'Creepster'),
        }
        if font_family in _FONT_NAME_MAP:
            _internal_name, _file_stem = _FONT_NAME_MAP[font_family]
            font_family = _internal_name
            # Auto-download the TTF if it was deleted or never downloaded
            try:
                _fonts_dir = os.path.join(settings.BASE_DIR, 'static', 'fonts')
                os.makedirs(_fonts_dir, exist_ok=True)
                _ttf_path = os.path.join(_fonts_dir, f'{_file_stem}.ttf')
                if not os.path.exists(_ttf_path):
                    import urllib.request
                    _api_url = f"https://fonts.googleapis.com/css2?family={font_family.replace(' ', '+')}&display=swap"
                    _req = urllib.request.Request(_api_url, headers={'User-Agent': 'Mozilla/5.0'})
                    _css = urllib.request.urlopen(_req, timeout=15).read().decode('utf-8')
                    import re as _re
                    _ttf_url = _re.search(r'url\((https://fonts\.gstatic\.com/[^)]+\.ttf)\)', _css)
                    if _ttf_url:
                        urllib.request.urlretrieve(_ttf_url.group(1), _ttf_path)
            except Exception:
                pass  # Missing font → FFmpeg falls back to Arial silently

        is_vertical = (aspect_ratio == '9:16')
        res_x = 1080 if is_vertical else 1920
        res_y = 1920 if is_vertical else 1080

        # Adjust default font size for canvas
        if is_vertical:
            actual_font_size = int(font_size * 1.15)
            margin_lr = 60
            if position_mode == 'BOTTOM':
                margin_v = 240
            elif position_mode == 'TOP':
                margin_v = 240
            else:
                margin_v = 450
        else:
            actual_font_size = int(font_size)
            margin_lr = 120
            if position_mode == 'BOTTOM':
                margin_v = 120
            elif position_mode == 'TOP':
                margin_v = 120
            else:
                margin_v = 280

        # ASS alignment: 2 = Bottom-Center, 5 = Mid-Center, 8 = Top-Center
        if position_mode == 'TOP':
            alignment = 8
        elif position_mode == 'CENTER':
            alignment = 5
        else:
            alignment = 2

        # Primary = Active Highlight Color; Secondary = Inactive Text Color (Wipe effect)
        primary_ass = cls.hex_to_ass_color(highlight_color, alpha=0)
        secondary_ass = cls.hex_to_ass_color(text_color, alpha=0)
        outline_ass = cls.hex_to_ass_color('#000000', alpha=40)
        shadow_ass = cls.hex_to_ass_color('#000000', alpha=120)
        dimmed_ass = cls.hex_to_ass_color(text_color, alpha=160)

        # Base weight/italic extraction
        italic_val = 1 if font_italic else 0
        bold_val = 1 if font_weight in ('bold', 'black') else 0
        
        # Override presets with new typography fields
        outline_width = text_stroke_width
        shadow_depth = text_shadow_depth
        bg_color_ass = cls.hex_to_ass_color('#000000', alpha=int(255 - (bg_opacity * 2.55)))
        border_style = 3 if bg_opacity > 0 else 1

        # Specific styling presets (adjusting defaults for certain styles if needed)
        if animation_style == 'CYBER_NEON':
            primary_ass = cls.hex_to_ass_color(highlight_color or '#00FFEA', alpha=0)
            secondary_ass = cls.hex_to_ass_color('#0F2A3F', alpha=20)
            outline_ass = cls.hex_to_ass_color(highlight_color or '#00FFEA', alpha=80)
            outline_width = max(3.5, text_stroke_width)
            shadow_depth = 0
        elif animation_style == 'CINEMATIC':
            primary_ass = cls.hex_to_ass_color(highlight_color or '#F8F9FA', alpha=0)
            secondary_ass = cls.hex_to_ass_color('#A0AEC0', alpha=60)
            outline_ass = cls.hex_to_ass_color('#000000', alpha=100)
            bold_val = 0 if font_weight in ('normal', 'light') else bold_val
            if font_family == 'Arial':
                font_family = 'Georgia'

        script_content = [
            "[Script Info]",
            "Title: YT Quid Synced Lyrics",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            f"PlayResX: {res_x}",
            f"PlayResY: {res_y}",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            # Main Style
            f"Style: Main,{font_family},{actual_font_size},{primary_ass},{secondary_ass},{outline_ass},{bg_color_ass},{bold_val},{italic_val},0,0,{font_scale_x},{font_scale_y},{letter_spacing},0,{border_style},{outline_width},{shadow_depth},{alignment},{margin_lr},{margin_lr},{margin_v},1",
            # Dimmed Rolling Style
            f"Style: RollingDim,{font_family},{int(actual_font_size * 0.78)},{dimmed_ass},{dimmed_ass},{outline_ass},{bg_color_ass},{bold_val},{italic_val},0,0,{font_scale_x},{font_scale_y},{letter_spacing},0,{border_style},{max(0.5, outline_width*0.5)},{max(0.5, shadow_depth*0.5)},{alignment},{margin_lr},{margin_lr},{margin_v},1",
            # Neon Glow Style
            f"Style: NeonGlow,{font_family},{actual_font_size},{primary_ass},{secondary_ass},{outline_ass},{bg_color_ass},{bold_val},{italic_val},0,0,{font_scale_x},{font_scale_y},{letter_spacing},0,{border_style},{outline_width},{shadow_depth},{alignment},{margin_lr},{margin_lr},{margin_v},1",
            # Title Card Style — same font family, smaller, top-left fixed position
            f"Style: TitleCard,{font_family},{max(22, int(actual_font_size * 0.45))},{secondary_ass},{secondary_ass},{outline_ass},{bg_color_ass},0,{italic_val},0,0,{font_scale_x},{font_scale_y},0,0,1,{max(1.0, outline_width * 0.6)},{max(0.5, shadow_depth * 0.6)},7,{margin_lr},{margin_lr},{int(margin_v * 0.5)},1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

        # Removed persistent title/artist card to avoid constantly displaying the song name

        # Pre-compute center positions used by both ♪ placeholders and regular lines
        x_center = res_x // 2
        if position_mode == 'TOP':
            y_center = margin_v + int(actual_font_size * 1.5)
        elif position_mode == 'CENTER':
            y_center = res_y // 2
        else:
            y_center = res_y - margin_v - int(actual_font_size * 0.5)

        # Generate Dialogue Events
        for idx, item in enumerate(lyrics_data):
            raw_line = str(item.get('line', '')).strip()
            if not raw_line:
                continue

            # Instrumental placeholder — render as a dimmed, centred ♪ with a
            # slow fade regardless of the chosen animation style. Skip transforms.
            if raw_line == '♪':
                instr_color = cls.hex_to_ass_color(text_color, alpha=160)
                p_start = max(0.0, float(item.get('start', 0.0)) + time_offset)
                p_end = max(0.0, float(item.get('end', p_start + 4.0)) + time_offset)
                script_content.append(
                    f"Dialogue: 0,{cls.seconds_to_ass_time(p_start)},"
                    f"{cls.seconds_to_ass_time(p_end)},"
                    f"RollingDim,,0,0,0,,"
                    f"{{\\pos({x_center},{y_center})\\c{instr_color}\\fad(400,400)}}♪"
                )
                continue

            # Apply Text Transform
            if text_transform == 'uppercase':
                raw_line = raw_line.upper()
            elif text_transform == 'lowercase':
                raw_line = raw_line.lower()
            elif text_transform == 'capitalize':
                raw_line = raw_line.title()

            start_sec = max(0.0, float(item.get('start', 0.0)) + time_offset)
            end_sec = max(0.0, float(item.get('end', start_sec + 4.0)) + time_offset)
            if end_sec <= start_sec:
                end_sec = start_sec + 2.0

            start_time_str = cls.seconds_to_ass_time(start_sec)
            end_time_str = cls.seconds_to_ass_time(end_sec)
            duration_cs = max(10, int((end_sec - start_sec) * 100))

            
            # Apply dynamic line_height setting
            y_prev = y_center - int(actual_font_size * line_height)
            y_next = y_center + int(actual_font_size * line_height)

            # Auto-synchronize word-level timestamps with edited line spellings
            line_words_tokens = raw_line.split()
            words_list = item.get('words')
            if line_words_tokens:
                if words_list and len(words_list) == len(line_words_tokens):
                    # Preserve exact Whisper millisecond timings while updating corrected spelling
                    words_list = [
                        {
                            'word': line_words_tokens[w_i],
                            'start': max(0.0, float(words_list[w_i].get('start', start_sec)) + time_offset),
                            'end': max(0.0, float(words_list[w_i].get('end', end_sec)) + time_offset)
                        }
                        for w_i in range(len(line_words_tokens))
                    ]
                elif words_list and len(words_list) != len(line_words_tokens):
                    # Resynchronize redistributed words across line duration
                    dur = max(0.4, end_sec - start_sec)
                    slot = dur / len(line_words_tokens)
                    words_list = [
                        {
                            'word': wt,
                            'start': round(start_sec + w_i * slot, 2),
                            'end': round(start_sec + (w_i + 1) * slot, 2)
                        }
                        for w_i, wt in enumerate(line_words_tokens)
                    ]

            if animation_style == 'KARAOKE_WIPE':
                # Entry: soft fade-in + blur that clears on the first beat so the
                # line doesn't hard-cut into view. Exit: short fade-out.
                # The \k wipe runs across the full duration as before.
                if words_list and len(words_list) > 0:
                    k_parts = []
                    for w in words_list:
                        w_text = w.get('word', '')
                        w_cs = max(5, int((float(w.get('end', float(w.get('start', start_sec)) + 0.3)) - float(w.get('start', start_sec))) * 100))
                        k_parts.append(f"{{\\k{w_cs}}}{w_text}")
                    karaoke_line = " ".join(k_parts)
                else:
                    words = raw_line.split(' ')
                    total_chars = max(1, sum(len(w) for w in words))
                    karaoke_line = " ".join([f"{{\\k{max(8, int(duration_cs * (len(w) / total_chars)))}}}{w}" for w in words])
                entry_tag = f"{{\\pos({x_center},{y_center})\\blur2\\t(0,120,\\blur0)\\fad(80,80)}}"
                script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{entry_tag}{karaoke_line}")

            elif animation_style == 'PLAYFUL_POP':
                # Spring bounce entry
                duration_ms = duration_cs * 10
                exit_start_ms = max(200, duration_ms - 200)
                s_x = font_scale_x
                s_y = font_scale_y
                pop_tag = (
                    f"{{\\pos({x_center},{y_center})"
                    f"\\fscx{int(s_x * 0.5)}\\fscy{int(s_y * 0.5)}\\alpha&HFF&"
                    f"\\t(0,120,\\fscx{int(s_x * 1.18)}\\fscy{int(s_y * 1.18)}\\alpha&H00&)"
                    f"\\t(120,220,\\fscx{int(s_x * 0.95)}\\fscy{int(s_y * 0.95)})"
                    f"\\t(220,300,\\fscx{s_x}\\fscy{s_y})"
                    f"\\t({exit_start_ms},{duration_ms},\\fscx{int(s_x * 0.4)}\\fscy{int(s_y * 0.4)}\\alpha&HFF&)}}"
                )
                script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{pop_tag}{raw_line}")

            elif animation_style == 'BUBBLE_BOUNCE':
                # Elastic bounce entry
                duration_ms = duration_cs * 10
                mid_ms = duration_ms // 2
                s_x = font_scale_x
                s_y = font_scale_y
                bubble_tag = (
                    f"{{\\move({x_center},{y_center + 22},{x_center},{y_center})"
                    f"\\fscx{int(s_x * 0.88)}\\fscy{int(s_y * 0.88)}"
                    f"\\t(0,400,\\fscx{s_x}\\fscy{s_y})"
                    f"\\t(400,{mid_ms},\\fscx{int(s_x * 1.04)}\\fscy{int(s_y * 1.04)})"
                    f"\\t({mid_ms},{duration_ms},\\fscx{s_x}\\fscy{s_y})"
                    f"\\fad(180,220)}}"
                )
                script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{bubble_tag}{raw_line}")

            elif animation_style == 'DREAMY_DRIFT':
                # Drift 
                duration_ms = duration_cs * 10
                exit_start_ms = max(300, duration_ms - 400)
                drift_tag = (
                    f"{{\\move({x_center},{y_center - 18},{x_center},{y_center + 22})"
                    f"\\blur6\\t(0,350,\\blur0)"
                    f"\\t({exit_start_ms},{duration_ms},\\blur4)"
                    f"\\fad(300,350)}}"
                )
                script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{drift_tag}{raw_line}")

            elif animation_style == 'NEON_GLOW':
                # Two-layer neon effect:
                # Layer 0 (bloom): a blurred copy of the whole line in the highlight
                #   color at 60% alpha — acts as the neon tube glow behind the text.
                #   Fades in/out with the line. blur5 gives a wide soft corona.
                # Layer 1 (wipe): the sharp karaoke wipe on top using \ko (outline
                #   karaoke) so the outline pulses as each word is highlighted,
                #   reinforcing the neon-on-dark-glass look.
                if words_list and len(words_list) > 0:
                    k_parts = []
                    for w in words_list:
                        w_text = w.get('word', '')
                        w_cs = max(5, int((float(w.get('end', float(w.get('start', start_sec)) + 0.3)) - float(w.get('start', start_sec))) * 100))
                        k_parts.append(f"{{\\ko{w_cs}}}{w_text}")
                    neon_line = " ".join(k_parts)
                else:
                    words = raw_line.split(' ')
                    total_chars = max(1, sum(len(w) for w in words))
                    neon_line = " ".join([f"{{\\ko{max(8, int(duration_cs * (len(w) / total_chars)))}}}{w}" for w in words])
                bloom_color = cls.hex_to_ass_color(highlight_color, alpha=100)
                # Bloom layer
                script_content.append(
                    f"Dialogue: 0,{start_time_str},{end_time_str},NeonGlow,,0,0,0,,"
                    f"{{\\pos({x_center},{y_center})\\blur5\\c{bloom_color}\\fad(120,120)}}{raw_line}"
                )
                # Sharp wipe layer on top
                script_content.append(
                    f"Dialogue: 1,{start_time_str},{end_time_str},NeonGlow,,0,0,0,,"
                    f"{{\\pos({x_center},{y_center})\\blur0\\fad(80,80)}}{neon_line}"
                )

                # Electric spark particles — ✦ ✧ scatter around the text,
                # each appearing at a staggered time and fading out quickly.
                # They use the highlight color to match the neon tube glow.
                spark_glyphs   = ['✦', '✧', '✦', '✧', '✦', '✧', '✦', '✧']
                spark_offsets  = [
                    (-170, -22), ( 175, -18), (-100, -38), ( 105, -35),
                    (-195,  10), ( 198,  14), ( -55,  32), (  60,  30),
                ]
                spark_scales   = [35, 28, 22, 32, 25, 20, 30, 18]
                spark_color    = cls.hex_to_ass_color(highlight_color, alpha=20)
                for sp_i, (glyph, (sx_off, sy_off), sscale) in enumerate(zip(
                    spark_glyphs, spark_offsets, spark_scales
                )):
                    sp_delay_ms  = sp_i * 60 + (idx * 23 % 40)
                    sp_start_sec = round(start_sec + sp_delay_ms / 1000.0, 3)
                    sp_end_sec   = round(sp_start_sec + 0.35, 3)
                    sp_end_sec   = min(sp_end_sec, end_sec)
                    if sp_end_sec <= sp_start_sec:
                        continue
                    sp_x = x_center + sx_off
                    sp_y = y_center + sy_off
                    script_content.append(
                        f"Dialogue: 0,{cls.seconds_to_ass_time(sp_start_sec)},"
                        f"{cls.seconds_to_ass_time(sp_end_sec)},NeonGlow,,0,0,0,,"
                        f"{{\\pos({sp_x},{sp_y})\\c{spark_color}"
                        f"\\fscx{sscale}\\fscy{sscale}"
                        f"\\t(0,120,\\fscx{sscale + 15}\\fscy{sscale + 15})"
                        f"\\fad(40,180)}}{glyph}"
                    )

            elif animation_style == 'HANDWRITTEN_INK':
                # \kf character-fill reveal + micro ink-splatter dots.
                # Small · dots appear just ahead of the writing position,
                # simulating ink hitting the page before the stroke arrives.
                chars = list(raw_line)
                if not chars:
                    script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{raw_line}")
                else:
                    char_dur = max(3, int(duration_cs / len(chars)))
                    ink_line = "".join(f"{{\\kf{char_dur}}}{c}" for c in chars)
                    ink_tag = f"{{\\move({x_center},{y_center + 8},{x_center},{y_center})\\fad(0,200)}}"
                    script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{ink_tag}{ink_line}")

                    # Ink splatter dots — · appear at staggered positions near
                    # the start of the line and fade quickly (80ms lifetime).
                    # X positions spread outward from center left, matching the
                    # left-to-right writing direction of the kf reveal.
                    n_chars = len(chars)
                    splat_color = cls.hex_to_ass_color(highlight_color, alpha=80)
                    splat_offsets = [-160, -120, -80, -40, 0, 40, 80, 120, 160]
                    splat_y_jitter = [-8, 10, -5, 12, -10, 7, -12, 5, -7]
                    for sp_i, (sx_off, sy_jit) in enumerate(zip(splat_offsets, splat_y_jitter)):
                        # Stagger: each dot appears as the \kf wipe reaches its x position
                        sp_frac = (sp_i / len(splat_offsets))
                        sp_start_sec = round(start_sec + sp_frac * (end_sec - start_sec) * 0.85, 3)
                        sp_end_sec   = round(sp_start_sec + 0.12, 3)
                        sp_end_sec   = min(sp_end_sec, end_sec)
                        if sp_end_sec <= sp_start_sec:
                            continue
                        sp_x = x_center + sx_off + ((idx * 5 + sp_i * 11) % 16) - 8
                        sp_y = y_center + sy_jit
                        sp_scale = 20 + (sp_i % 3) * 8
                        script_content.append(
                            f"Dialogue: 0,{cls.seconds_to_ass_time(sp_start_sec)},"
                            f"{cls.seconds_to_ass_time(sp_end_sec)},Main,,0,0,0,,"
                            f"{{\\pos({sp_x},{sp_y})\\c{splat_color}"
                            f"\\fscx{sp_scale}\\fscy{sp_scale}\\fad(0,60)}}·"
                        )

            elif animation_style == 'RETRO_VHS':
                # Three-layer RGB chromatic aberration + scanline noise overlay.
                # Layers 0-1: red/cyan channel shift (as before).
                # Layer 2: main white text.
                # Scanline layer: ░ block glyphs at very low alpha across the
                # text height — simulate VHS tracking noise / interlace lines.
                duration_ms = duration_cs * 10
                red_color  = cls.hex_to_ass_color('#FF4444', alpha=140)
                cyan_color = cls.hex_to_ass_color('#44FFEE', alpha=150)
                # Red channel drifts right: x+3→x+7 over the line duration
                script_content.append(
                    f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,"
                    f"{{\\move({x_center + 3},{y_center - 2},{x_center + 7},{y_center - 2})"
                    f"\\c{red_color}\\blur1\\fad(30,30)}}{raw_line}"
                )
                # Cyan channel: fixed offset left
                script_content.append(
                    f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,"
                    f"{{\\pos({x_center - 3},{y_center + 2})\\c{cyan_color}\\blur1"
                    f"\\fad(30,30)}}{raw_line}"
                )
                # Main white layer on top
                script_content.append(
                    f"Dialogue: 1,{start_time_str},{end_time_str},Main,,0,0,0,,"
                    f"{{\\pos({x_center},{y_center})\\fad(50,50)}}{raw_line}"
                )

                # Scanline noise — horizontal ░ strips at varying y offsets and
                # very low alpha, flickering in/out at different times.
                # Each strip is a wide, short-lived block at one of 6 y positions.
                noise_color = cls.hex_to_ass_color('#FFFFFF', alpha=210)
                noise_rows = [
                    # (y_offset, x_offset, fscx, fscy, delay_ms, lifetime_ms)
                    ( -18, -80, 280, 18,  0,   180),
                    (   8,  60, 220, 14,  90,  160),
                    (  18, -40, 300, 16,  40,  200),
                    ( -10,  20, 180, 12, 130,  150),
                    (  -4, -60, 240, 20,  70,  170),
                    (  14,  30, 260, 14, 160,  140),
                ]
                for nr_i, (ny_off, nx_off, nfscx, nfscy, delay_ms, lifetime_ms) in enumerate(noise_rows):
                    nr_start_sec = round(start_sec + delay_ms / 1000.0, 3)
                    nr_end_sec   = round(nr_start_sec + lifetime_ms / 1000.0, 3)
                    nr_end_sec   = min(nr_end_sec, end_sec)
                    if nr_end_sec <= nr_start_sec:
                        continue
                    nr_x = x_center + nx_off
                    nr_y = y_center + ny_off
                    script_content.append(
                        f"Dialogue: 0,{cls.seconds_to_ass_time(nr_start_sec)},"
                        f"{cls.seconds_to_ass_time(nr_end_sec)},Main,,0,0,0,,"
                        f"{{\\pos({nr_x},{nr_y})\\c{noise_color}"
                        f"\\fscx{nfscx}\\fscy{nfscy}\\fad(0,60)}}░"
                    )

            elif animation_style == 'VINTAGE_COUNTRY':
                # Warm slow fade — line fades in from nothing over 400ms,
                # holds clean, then fades out slowly over 500ms. The slow dissolve
                # feels warm and unhurried. Uses the main style with amber highlight.
                script_content.append(
                    f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,"
                    f"{{\\pos({x_center},{y_center})\\fad(400,500)}}{raw_line}"
                )

            elif animation_style == 'ROLLING_3LINE':
                # Slide-based rolling display — lines physically move rather than
                # fading in place, matching how Spotify/Apple Music render lyrics.
                #
                # Transition window: first 180ms of each line's display period.
                # Previous line: slides from y_center → y_prev (moves up) during entry.
                # Current line: slides from y_next → y_center (arrives from below).
                # Next line: appears statically at y_next, dim.
                slide_ms = 180  # transition duration in ms

                # 1. Previous line — slides upward as new line arrives
                if idx > 0:
                    prev_item = lyrics_data[idx - 1]
                    if prev_item.get('line') and prev_item['line'] != '♪':
                        prev_text = prev_item['line']
                        if text_transform == 'uppercase': prev_text = prev_text.upper()
                        elif text_transform == 'lowercase': prev_text = prev_text.lower()
                        elif text_transform == 'capitalize': prev_text = prev_text.title()
                        script_content.append(
                            f"Dialogue: 0,{start_time_str},{end_time_str},RollingDim,,0,0,0,,"
                            f"{{\\move({x_center},{y_center},{x_center},{y_prev})"
                            f"\\fad(0,200)}}{prev_text}"
                        )

                # 2. Current line — slides up from y_next into y_center
                script_content.append(
                    f"Dialogue: 1,{start_time_str},{end_time_str},Main,,0,0,0,,"
                    f"{{\\move({x_center},{y_next},{x_center},{y_center})"
                    f"\\fad(80,120)}}{raw_line}"
                )

                # 3. Next line — static at y_next, dim, no movement yet
                if idx + 1 < len(lyrics_data):
                    next_item = lyrics_data[idx + 1]
                    if next_item.get('line') and next_item['line'] != '♪':
                        next_text = next_item['line']
                        if text_transform == 'uppercase': next_text = next_text.upper()
                        elif text_transform == 'lowercase': next_text = next_text.lower()
                        elif text_transform == 'capitalize': next_text = next_text.title()
                        script_content.append(
                            f"Dialogue: 0,{start_time_str},{end_time_str},RollingDim,,0,0,0,,"
                            f"{{\\pos({x_center},{y_next})\\fad(200,100)}}{next_text}"
                        )

            elif animation_style == 'CYBER_NEON':
                # unchanged — already has blur + karaoke wipe on NeonGlow style
                words = raw_line.split(' ')
                total_chars = max(1, sum(len(w) for w in words))
                neon_line = " ".join([f"{{\\k{max(8, int(duration_cs * (len(w)/total_chars)))}}}{w}" for w in words])
                script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},NeonGlow,,0,0,0,,{{\\blur2\\fad(120,120)}}{neon_line}")

            elif animation_style == 'CINEMATIC':
                # Slow-burn reveal: entry blur 2 → 0 over 200ms + imperceptible
                # upward float (8px over full duration) — how film subtitles move.
                # A second dimmed shadow layer sits 2px below for depth.
                duration_ms = duration_cs * 10
                shadow_color = cls.hex_to_ass_color('#000000', alpha=160)
                script_content.append(
                    f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,"
                    f"{{\\pos({x_center},{y_center + 2})\\c{shadow_color}\\blur2"
                    f"\\fad(280,280)}}{raw_line}"
                )
                script_content.append(
                    f"Dialogue: 1,{start_time_str},{end_time_str},Main,,0,0,0,,"
                    f"{{\\move({x_center},{y_center + 4},{x_center},{y_center - 4})"
                    f"\\blur2\\t(0,200,\\blur0)\\fad(280,280)}}{raw_line}"
                )

            elif animation_style == 'BOUNCE_IN':
                # Entry: scale 50%→108%→100% spring (overshoot + settle).
                # Exit: scale down to 60% + fade — matches the entry energy.
                # Fixed \pos so position doesn't drift across renderers.
                duration_ms = duration_cs * 10
                exit_start_ms = max(200, duration_ms - 180)
                s_x = font_scale_x
                s_y = font_scale_y
                bounce_tag = (
                    f"{{\\pos({x_center},{y_center})"
                    f"\\fscx{int(s_x * 0.5)}\\fscy{int(s_y * 0.5)}"
                    f"\\t(0,180,\\fscx{int(s_x * 1.08)}\\fscy{int(s_y * 1.08)})"
                    f"\\t(180,280,\\fscx{s_x}\\fscy{s_y})"
                    f"\\t({exit_start_ms},{duration_ms},\\fscx{int(s_x * 0.6)}\\fscy{int(s_y * 0.6)}\\alpha&HFF&)"
                    f"\\fad(0,0)}}"
                )
                script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{bounce_tag}{raw_line}")

            elif animation_style == 'TYPEWRITER':
                # True character-by-character reveal using per-char Dialogue events.
                # Each character gets its own event starting at its reveal time,
                # so characters literally appear from nothing rather than color-wiping.
                # Previously used \k which does a color wipe — not a real typewriter.
                chars = list(raw_line)
                if not chars:
                    script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{raw_line}")
                else:
                    n = len(chars)
                    char_slot_sec = (end_sec - start_sec) / n
                    # Build incrementally: each event shows all revealed chars so far
                    for c_i in range(n):
                        c_start_sec = round(start_sec + c_i * char_slot_sec, 3)
                        c_start_str = cls.seconds_to_ass_time(c_start_sec)
                        revealed = "".join(chars[:c_i + 1])
                        script_content.append(
                            f"Dialogue: {c_i},{c_start_str},{end_time_str},Main,,0,0,0,,"
                            f"{{\\pos({x_center},{y_center})\\fad(0,120)}}{revealed}"
                        )

            elif animation_style == 'WAVE_PULSE':
                # Bug fix: \t uses milliseconds, not centiseconds.
                # Grow to 105% over first half, shrink back over second half.
                # Added \pos and a colour shift on the peak for extra visual punch.
                duration_ms = duration_cs * 10
                mid_ms = duration_ms // 2
                s_x_max = int(font_scale_x * 1.06)
                s_y_max = int(font_scale_y * 1.06)
                peak_color = cls.hex_to_ass_color(highlight_color, alpha=0)
                pulse_tag = (
                    f"{{\\pos({x_center},{y_center})"
                    f"\\t(0,{mid_ms},\\fscx{s_x_max}\\fscy{s_y_max}\\c{peak_color})"
                    f"\\t({mid_ms},{duration_ms},\\fscx{font_scale_x}\\fscy{font_scale_y})"
                    f"\\fad(150,150)}}"
                )
                script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{pulse_tag}{raw_line}")

            elif animation_style == 'SLIDE_UP':
                # Move from slightly below to center
                y_start = y_center + 60
                move_tag = f"{{\\move({x_center},{y_start},{x_center},{y_center})\\fad(200,200)}}"
                script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{move_tag}{raw_line}")

            else:
                script_content.append(f"Dialogue: 0,{start_time_str},{end_time_str},Main,,0,0,0,,{raw_line}")

        os.makedirs(os.path.dirname(os.path.abspath(output_ass_path)), exist_ok=True)
        with open(output_ass_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(script_content))

        return output_ass_path

    @classmethod
    def generate_ambient_background_frame(
        cls,
        background_image_path,
        width,
        height,
        output_frame_path,
        cover_layout='AMBIENT',
        cover_size=1.0,
        cover_blur=8.0,
        cover_opacity=1.0,
        cover_offset=0.0,
        cover_brightness=1.0,
        cover_contrast=1.0,
        cover_saturation=1.0,
        cover_vignette=0.0
    ):
        """
        Creates a high-resolution ambient background frame with smooth Gaussian blur,
        dark vignette enhancement, and either a centered cover or a full-bleed cover.
        """
        os.makedirs(os.path.dirname(os.path.abspath(output_frame_path)), exist_ok=True)

        if background_image_path and os.path.exists(background_image_path):
            img = Image.open(background_image_path).convert('RGB')

            if cover_layout == 'FULL':
                # Full cover background with the same effect stack as the preview.
                img_ratio = img.width / max(img.height, 1)
                canvas_ratio = width / max(height, 1)
                target_w = int(width * min(max(cover_size, 0.2), 1.8))
                target_h = int(height * min(max(cover_size, 0.2), 1.8))

                if img_ratio > canvas_ratio:
                    new_h = int(target_h)
                    new_w = int(target_h * img_ratio)
                    left = max((width - new_w) // 2 + int(cover_offset), 0)
                    top = max((height - new_h) // 2, 0)
                    bg = Image.new('RGB', (width, height), color=(12, 12, 18))
                    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    bg.paste(resized, (left, top))
                else:
                    new_w = int(target_w)
                    new_h = int(target_w / img_ratio)
                    left = max((width - new_w) // 2 + int(cover_offset), 0)
                    top = max((height - new_h) // 2, 0)
                    bg = Image.new('RGB', (width, height), color=(12, 12, 18))
                    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    bg.paste(resized, (left, top))

                # FULL layout: no blur — matches canvas preview which never
                # blurs in FULL mode regardless of the blur slider value.
                bg = ImageEnhance.Brightness(bg).enhance(max(cover_brightness, 0.1))
                bg = ImageEnhance.Contrast(bg).enhance(max(cover_contrast, 0.1))
                bg = ImageEnhance.Color(bg).enhance(max(cover_saturation, 0.0))

                if cover_opacity < 1.0:
                    bg = Image.blend(Image.new('RGB', bg.size, (12, 12, 18)), bg, float(max(min(cover_opacity, 1.0), 0.0)))

                if cover_vignette > 0:
                    vignette = Image.new('RGBA', bg.size, (0, 0, 0, 0))
                    draw = ImageDraw.Draw(vignette)
                    alpha = int(255 * min(max(cover_vignette, 0.0), 0.7))
                    draw.rectangle((0, 0, width, height), fill=(0, 0, 0, alpha))
                    bg = Image.alpha_composite(bg.convert('RGBA'), vignette).convert('RGB')
            else:
                # AMBIENT layout:
                # 1. Blurred + darkened full-canvas background using user controls
                bg = img.resize((width, height), Image.Resampling.LANCZOS)
                if cover_blur > 0:
                    bg = bg.filter(ImageFilter.GaussianBlur(radius=float(cover_blur)))
                # Apply brightness/contrast/saturation from user sliders
                dark_factor = max(0.1, min(cover_brightness, 2.0)) * 0.4  # ambient always darkened
                bg = ImageEnhance.Brightness(bg).enhance(dark_factor)
                bg = ImageEnhance.Contrast(bg).enhance(max(cover_contrast, 0.1))
                bg = ImageEnhance.Color(bg).enhance(max(cover_saturation, 0.0))

                if cover_opacity < 1.0:
                    bg = Image.blend(Image.new('RGB', bg.size, (12, 12, 18)), bg,
                                     float(max(min(cover_opacity, 1.0), 0.0)))

                # 2. Centered album artwork — preserve original aspect ratio,
                #    sized by cover_size (1.0 = 42% of shorter canvas dimension)
                base_size = int(min(width, height) * 0.42 * max(cover_size, 0.2))
                img_ratio = img.width / max(img.height, 1)
                if img_ratio >= 1.0:
                    art_w = base_size
                    art_h = int(base_size / img_ratio)
                else:
                    art_h = base_size
                    art_w = int(base_size * img_ratio)

                art_thumb = img.resize((art_w, art_h), Image.Resampling.LANCZOS)
                # Apply brightness to the art itself too
                art_thumb = ImageEnhance.Brightness(art_thumb).enhance(max(cover_brightness, 0.1))

                pos_x = (width  - art_w) // 2 + int(cover_offset)
                pos_y = int((height - art_h) * 0.30) if height > width else int((height - art_h) * 0.25)
                # Clamp so art never pastes outside the canvas
                pos_x = max(0, min(pos_x, width  - art_w))
                pos_y = max(0, min(pos_y, height - art_h))
                bg.paste(art_thumb, (pos_x, pos_y))

                if cover_vignette > 0:
                    vignette = Image.new('RGBA', bg.size, (0, 0, 0, 0))
                    draw = ImageDraw.Draw(vignette)
                    alpha = int(255 * min(max(cover_vignette, 0.0), 0.85))
                    draw.rectangle((0, 0, width, height), fill=(0, 0, 0, alpha))
                    bg = Image.alpha_composite(bg.convert('RGBA'), vignette).convert('RGB')

            # Save as PNG — lossless, no colour-space ambiguity.
            # FFmpeg's ass= subtitle filter composites cleanly over PNG;
            # JPEG's YCbCr encoding causes block-colour corruption on the
            # subtitle composite pass (the distorted cover art artifact).
            bg.save(output_frame_path, 'PNG')
        else:
            # Generate sleek dark gradient background
            img = Image.new('RGB', (width, height), color=(10, 14, 23))
            draw = ImageDraw.Draw(img)

            # Subtle radial glow
            center_x, center_y = width // 2, height // 2
            max_radius = int(math.hypot(center_x, center_y))

            for r in range(max_radius, 0, -15):
                alpha = int(35 * (1 - r / max_radius))
                color = (15 + alpha // 2, 22 + alpha, 38 + int(alpha * 1.5))
                draw.ellipse(
                    [center_x - r, center_y - r, center_x + r, center_y + r],
                    fill=color
                )

            img.save(output_frame_path, 'PNG')

        return output_frame_path

    @classmethod
    def render_lyrics_video(
        cls,
        audio_path=None,
        background_image_path=None,
        background_video_path=None,
        lyrics_data=None,
        output_video_path=None,
        aspect_ratio='16:9',
        animation_style='KARAOKE_WIPE',
        font_family='Arial',
        font_size=48,
        highlight_color='#00E5FF',
        text_color='#FFFFFF',
        position_mode='CENTER',
        cover_layout='AMBIENT',
        cover_size=1.0,
        cover_blur=8.0,
        cover_opacity=1.0,
        cover_offset=0.0,
        cover_brightness=1.0,
        cover_contrast=1.0,
        cover_saturation=1.0,
        cover_vignette=0.0,
        font_weight='bold',
        font_italic=False,
        letter_spacing=0.0,
        line_height=1.4,
        text_transform='none',
        text_stroke_width=2.5,
        text_shadow_depth=2.0,
        font_scale_x=100,
        font_scale_y=100,
        bg_opacity=0,
        title='Lyric Video',
        artist='',
        loop_video=True,
        project_id=None,
        time_offset=0.0,
    ):
        """
        Renders complete 1080p synchronized lyric video using local FFmpeg and ASS subtitle engine.
        Supports both:
        1. Source Video File (overlays lyrics onto existing video, preserving or replacing audio)
        2. Audio File + Cover Artwork / Ambient Dark Canvas
        """
        ffmpeg = cls.get_ffmpeg_binary()
        output_video_path = os.path.abspath(str(output_video_path))
        os.makedirs(os.path.dirname(output_video_path), exist_ok=True)

        if project_id:
            RenderProcessTracker.set_progress('lyrics', project_id, 10, "Inspecting media and preparing workspace...")
            if RenderProcessTracker.is_cancelled('lyrics', project_id):
                raise RuntimeError("Rendering cancelled by user.")

        # Validate input paths
        has_audio = bool(audio_path and os.path.exists(str(audio_path)))
        has_video = bool(background_video_path and os.path.exists(str(background_video_path)))

        if not has_audio and not has_video:
            raise FileNotFoundError("Neither a valid audio file nor a source video file was provided.")

        if has_audio:
            audio_path = os.path.abspath(str(audio_path))
            duration = cls.inspect_media_duration(audio_path)
        else:
            bg_vid = os.path.abspath(str(background_video_path))
            duration = cls.inspect_media_duration(bg_vid)

        # Ensure duration covers the entire lyrics timeline if looping enabled or audio present
        if lyrics_data:
            max_lyric_end = max([float(item.get('end', 0.0)) for item in lyrics_data if isinstance(item, dict) and 'end' in item] or [0.0])
            if max_lyric_end > duration and (loop_video or has_audio):
                duration = round(max_lyric_end, 2)

        if duration <= 0:
            duration = 60.0

        is_vertical = (aspect_ratio == '9:16')
        width = 1080 if is_vertical else 1920
        height = 1920 if is_vertical else 1080

        # Create temporary working directory for subtitle and frame assets
        temp_dir = os.path.join(os.path.dirname(output_video_path), f"temp_lyrics_{os.getpid()}")
        os.makedirs(temp_dir, exist_ok=True)

        try:
            if project_id:
                RenderProcessTracker.set_progress('lyrics', project_id, 25, "Generating synchronized ASS subtitle styling...")
                if RenderProcessTracker.is_cancelled('lyrics', project_id):
                    raise RuntimeError("Rendering cancelled by user.")

            # 1. Prepare ASS Subtitles
            ass_path = os.path.join(temp_dir, "lyrics.ass")
            cls.generate_ass_subtitles(
                lyrics_data=lyrics_data or [],
                output_ass_path=ass_path,
                aspect_ratio=aspect_ratio,
                animation_style=animation_style,
                font_family=font_family,
                font_size=font_size,
                highlight_color=highlight_color,
                text_color=text_color,
                position_mode=position_mode,
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
                title=title,
                artist=artist,
                video_duration=duration,
                time_offset=time_offset,
            )

            # Escape subtitle path for FFmpeg filter on Windows
            escaped_ass_path = ass_path.replace('\\', '/').replace(':', '\\:')

            # Fonts directory — contains downloaded Google Fonts TTFs so FFmpeg
            # can render them when the ass= filter looks up the Fontname field.
            _fonts_dir = os.path.join(settings.BASE_DIR, 'static', 'fonts')
            if os.path.isdir(_fonts_dir):
                _escaped_fonts_dir = _fonts_dir.replace('\\', '/').replace(':', '\\:')
                _ass_filter = f"ass='{escaped_ass_path}':fontsdir='{_escaped_fonts_dir}'"
            else:
                _ass_filter = f"ass='{escaped_ass_path}'"

            if project_id:
                RenderProcessTracker.set_progress('lyrics', project_id, 45, "Preparing visual composition & video layout...")
                if RenderProcessTracker.is_cancelled('lyrics', project_id):
                    raise RuntimeError("Rendering cancelled by user.")

            # 2. Build FFmpeg command depending on source media
            if has_video:
                bg_vid = os.path.abspath(str(background_video_path))
                vf_filter = (
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},"
                    f"{_ass_filter}"
                )

                stream_loop_args = ['-stream_loop', '-1'] if loop_video else []

                if has_audio:
                    # Video source + custom audio track replacement
                    cmd = [
                        ffmpeg, '-y'
                    ] + stream_loop_args + [
                        '-i', bg_vid,
                        '-i', audio_path,
                        '-map', '0:v:0',
                        '-map', '1:a:0',
                        '-vf', vf_filter,
                        '-c:v', 'libx264',
                        '-preset', 'veryfast',
                        '-threads', '0',
                        '-crf', '20',
                        '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac',
                        '-b:a', '192k',
                        '-t', str(duration),
                        '-movflags', '+faststart',
                        output_video_path
                    ]
                else:
                    # Source video file alone (overlays lyrics onto video, preserves video soundtrack)
                    cmd = [
                        ffmpeg, '-y'
                    ] + stream_loop_args + [
                        '-i', bg_vid,
                        '-vf', vf_filter,
                        '-map', '0:v:0',
                        '-map', '0:a?',
                        '-c:v', 'libx264',
                        '-preset', 'veryfast',
                        '-threads', '0',
                        '-crf', '20',
                        '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac',
                        '-b:a', '192k',
                        '-t', str(duration),
                        '-movflags', '+faststart',
                        output_video_path
                    ]
            else:
                # Prepare background image frame + audio
                frame_path = os.path.join(temp_dir, "bg_frame.png")
                cls.generate_ambient_background_frame(
                    background_image_path=background_image_path,
                    width=width,
                    height=height,
                    output_frame_path=frame_path,
                    cover_layout=cover_layout,
                    cover_size=cover_size,
                    cover_blur=cover_blur,
                    cover_opacity=cover_opacity,
                    cover_offset=cover_offset,
                    cover_brightness=cover_brightness,
                    cover_contrast=cover_contrast,
                    cover_saturation=cover_saturation,
                    cover_vignette=cover_vignette
                )

                vf_filter = _ass_filter
                cmd = [
                    ffmpeg, '-y',
                    '-loop', '1',
                    '-framerate', '30',
                    '-i', frame_path,
                    '-i', audio_path,
                    '-vf', vf_filter,
                    '-c:v', 'libx264',
                    '-tune', 'stillimage',
                    '-preset', 'veryfast',
                    '-threads', '0',
                    '-crf', '20',
                    '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac',
                    '-b:a', '192k',
                    '-shortest',
                    '-t', str(duration),
                    '-movflags', '+faststart',
                    output_video_path
                ]

            if project_id:
                RenderProcessTracker.set_progress('lyrics', project_id, 55, "Encoding 1080p synchronized video frames with FFmpeg...")
                if RenderProcessTracker.is_cancelled('lyrics', project_id):
                    raise RuntimeError("Rendering cancelled by user.")

            # Execute rendering command with live cancellation tracking
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors='ignore',
                **cls.get_subprocess_kwargs()
            )

            if project_id:
                RenderProcessTracker.register('lyrics', project_id, proc)

            stdout, stderr = "", ""
            try:
                while True:
                    try:
                        stdout, stderr = proc.communicate(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        if project_id and RenderProcessTracker.is_cancelled('lyrics', project_id):
                            proc.kill()
                            try:
                                proc.communicate()
                            except Exception:
                                pass
                            raise RuntimeError("Rendering cancelled by user.")
            finally:
                if project_id:
                    RenderProcessTracker.unregister('lyrics', project_id, proc)

            if proc.returncode != 0 or not os.path.exists(output_video_path) or os.path.getsize(output_video_path) == 0:
                if project_id and RenderProcessTracker.is_cancelled('lyrics', project_id):
                    raise RuntimeError("Rendering cancelled by user.")
                raise RuntimeError(f"FFmpeg lyric video render failed: {stderr[-1000:]}")

            if project_id:
                RenderProcessTracker.set_progress('lyrics', project_id, 95, "Finalizing MP4 video streams & metadata...")

            return {
                'success': True,
                'output_video': output_video_path,
                'duration': duration
            }

        finally:
            if project_id and not RenderProcessTracker.is_cancelled('lyrics', project_id):
                RenderProcessTracker.set_progress('lyrics', project_id, 100, "Rendering complete!")

            # Clean up temporary working directory
            try:
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
            except Exception:
                pass
