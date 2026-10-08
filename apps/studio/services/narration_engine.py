"""Edge-TTS narration and image-scene rendering for POV/story videos.

Uses Microsoft Edge's free neural TTS (edge-tts package) — no heavy ML
dependencies, works on Python 3.13, produces studio-quality voices.
"""

import asyncio
import os
import subprocess
import tempfile
import wave
import re

import numpy as np

from .renderer import VideoStudioRenderer


class NarrationEngineError(RuntimeError):
    pass


class NarrationEngineService:
    SAMPLE_RATE = 24000
    MAX_SCENES = 50
    IMPACT_ACTION_PATTERN = re.compile(
        r'\b(?:'
        r'thunder(?:\s+struck)?|lightning\s+struck|struck|'
        r'explod\w*|detonat\w*|crash(?:ed|es|ing)?|slam(?:med|s|ming)?|'
        r'impact|collid\w*|shatter\w*|smash(?:ed|es|ing)?|'
        r'blast(?:ed|s|ing)?|burst|erupt\w*|'
        r'punched|punches|punching|kicked|kicks|kicking|'
        r'struck|attacked|attack|attacks|'
        r'gunshot|shot|fired|fires|earthquake|'
        r'suddenly\s+(?:fell|hit|struck|crashed|exploded|slammed)'
        r')\b',
        re.IGNORECASE,
    )
    ATMOSPHERE_CUES = (
        ('lightning', re.compile(r'\b(?:lightning|thunder(?:storm)?|bolt of lightning)\b', re.I)),
        ('rain', re.compile(r'\b(?:rain|raining|rainstorm|downpour|pouring rain|wet streets?|storm|monsoon)\b', re.I)),
        ('snow', re.compile(r'\b(?:snow|snowing|snowfall|blizzard|snowflakes?)\b', re.I)),
        ('fog', re.compile(r'\b(?:fog|foggy|mist|misty|haze|hazy)\b', re.I)),
        ('dust', re.compile(r'\b(?:dust|dusty|desert|sandstorm|sandy|dry earth)\b', re.I)),
        ('sun_rays', re.compile(r'\b(?:sunlight|sunbeam|sunbeams|sun rays|sunrays|sunrise|sunset|sunlit)\b', re.I)),
        ('embers', re.compile(r'\b(?:embers?|sparks?|fire|flames?|burning|blazing)\b', re.I)),
        ('smoke', re.compile(r'\b(?:smoke|smoking|smoky|smoke plume)\b', re.I)),
        ('candle_flicker', re.compile(r'\b(?:candle|candles|candlelight|torchlight|torch-lit)\b', re.I)),
    )

    @classmethod
    def impact_shake_start_fraction(cls, text):
        """Return the narration position for a dramatic impact, if one is present."""
        match = cls.IMPACT_ACTION_PATTERN.search(str(text or ''))
        if not match:
            return None
        total_words = max(1, len(str(text).split()))
        words_before_impact = len(str(text)[:match.start()].split())
        return min(1.0, words_before_impact / total_words)

    @classmethod
    def resolve_atmosphere_effect(cls, effect, text):
        if effect != 'auto':
            return effect
        for name, cue_pattern in cls.ATMOSPHERE_CUES:
            if cue_pattern.search(str(text or '')):
                return name
        return 'none'

    @classmethod
    def atmosphere_filter_chain(cls, effect, width, height, fps=30):
        """Create restrained animated FFmpeg overlays for a scene atmosphere."""
        particles = []
        if effect in ('dust', 'embers', 'snow', 'rain'):
            count = {'dust': 16, 'embers': 12, 'snow': 18, 'rain': 24}[effect]
            for index in range(count):
                seed_x = (index * 173 + 47) % width
                seed_y = (index * 251 + 83) % height
                if effect == 'dust':
                    speed_x, speed_y = 5 + index % 7, 4 + index % 9
                    box_width, box_height, color = 3 + index % 3, 3 + index % 3, '0xd9c49a@0.30'
                    y = f'mod({seed_y}-t*{speed_y}+{height},{height})'
                    x = f'mod({seed_x}+t*{speed_x},{width})'
                elif effect == 'embers':
                    speed_x, speed_y = 3 + index % 8, 18 + index % 17
                    box_width, box_height, color = 3 + index % 3, 4 + index % 5, '0xff742e@0.72'
                    y = f'mod({seed_y}-t*{speed_y}+{height},{height})'
                    x = f'mod({seed_x}+sin(t*2+{index})*18+{width},{width})'
                elif effect == 'snow':
                    speed_x, speed_y = 4 + index % 9, 12 + index % 16
                    box_width = box_height = 4 + index % 5
                    color = 'white@0.62'
                    y = f'mod({seed_y}+t*{speed_y},{height})'
                    x = f'mod({seed_x}+sin(t+{index})*18+{width},{width})'
                else:
                    speed_x, speed_y = 24 + index % 16, 250 + index % 180
                    box_width, box_height, color = 2, 22 + index % 16, '0xc9e8ff@0.35'
                    y = f'mod({seed_y}+t*{speed_y},{height})'
                    x = f'mod({seed_x}-t*{speed_x}+{width},{width})'
                particles.append(
                    f"drawbox=x='{x}':y='{y}':w={box_width}:h={box_height}:"
                    f"color={color}:t=fill"
                )
        elif effect == 'fog':
            for index in range(6):
                y = (height * (index + 1)) // 8
                x = f'mod(t*{8 + index * 2}+{index * width // 6},{width})-{width // 3}'
                particles.append(
                    f"drawbox=x='{x}':y={y}:w={width // 2}:h={max(30, height // 16)}:"
                    'color=0xd9e3e8@0.055:t=fill'
                )
        elif effect == 'smoke':
            for index in range(8):
                x = f'mod(t*{4 + index}+{index * width // 8},{width})'
                y = f'mod({height - index * 31}-t*{8 + index * 2}+{height},{height})'
                particles.append(
                    f"drawbox=x='{x}':y='{y}':w={width // 9}:h={height // 18}:"
                    'color=0x96999d@0.10:t=fill'
                )
        elif effect == 'sun_rays':
            particles.append(f'drawbox=x=0:y=0:w=iw:h=ih:color=0xffd77a@0.025:t=fill')
            for index in range(5):
                x = f'mod({index * width // 5}+t*3,iw)'
                particles.append(
                    f"drawbox=x='{x}':y=0:w={max(12, width // 80)}:h=ih:"
                    'color=0xffedb0@0.075:t=fill'
                )
        elif effect == 'lightning':
            return (
                "drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:"
                "enable='between(t,0.12,0.19)+between(t,0.25,0.31)'"
            )
        elif effect == 'candle_flicker':
            return "eq=brightness='0.035+0.025*sin(17*t)+0.012*sin(31*t)':eval=frame"
        elif effect == 'glow':
            return 'eq=brightness=0.045:saturation=1.12'
        elif effect == 'vignette':
            return 'vignette=PI/5'
        elif effect == 'flash':
            return (
                "drawbox=x=0:y=0:w=iw:h=ih:color=white@0.38:t=fill:"
                "enable='between(t,0.16,0.24)'"
            )
        return ','.join(particles)

    @classmethod
    def _write_wav(cls, audio, output_path):
        samples = np.asarray(audio, dtype=np.float32)
        samples = np.clip(samples, -1, 1)
        with wave.open(output_path, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(cls.SAMPLE_RATE)
            wav_file.writeframes((samples * 32767).astype('<i2').tobytes())

    @classmethod
    def _mp3_to_wav(cls, mp3_path, wav_path):
        """Convert MP3 (from edge-tts) to WAV using ffmpeg."""
        ffmpeg = VideoStudioRenderer.get_ffmpeg_binary()
        result = subprocess.run(
            [ffmpeg, '-y', '-i', mp3_path, '-ar', str(cls.SAMPLE_RATE), '-ac', '1', wav_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **VideoStudioRenderer.get_subprocess_kwargs(),
        )
        if result.returncode:
            msg = result.stderr.decode('utf-8', errors='replace')[-600:]
            raise NarrationEngineError(f'FFmpeg MP3→WAV conversion failed: {msg}')

    @classmethod
    def _run_async(cls, coro):
        """Safely execute an async coroutine across Python versions and WSGI thread contexts."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result()
        elif loop and not loop.is_closed():
            return loop.run_until_complete(coro)
        else:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                return new_loop.run_until_complete(coro)
            finally:
                try:
                    new_loop.close()
                except Exception:
                    pass

    @classmethod
    def synthesize_scene(cls, text, voice, output_path):
        """Create one WAV per scene using edge-tts (Microsoft neural voices). Returns audio duration in seconds."""
        try:
            import edge_tts
        except ImportError as exc:
            raise NarrationEngineError(
                'edge-tts is not installed. Run: pip install edge-tts'
            ) from exc

        text = str(text).strip()
        if not text:
            raise NarrationEngineError('A scene cannot have empty narration text.')

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        mp3_path = output_path.replace('.wav', '.mp3')
        try:
            async def _speak():
                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(mp3_path)

            cls._run_async(_speak())
        except Exception as exc:
            raise NarrationEngineError(f'edge-tts could not generate narration: {exc}') from exc

        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
            raise NarrationEngineError('edge-tts returned no audio data.')

        cls._mp3_to_wav(mp3_path, output_path)
        try:
            os.remove(mp3_path)
        except OSError:
            pass
        return round(cls._duration(output_path), 2)

    @classmethod
    def synthesize_scenes_batch(cls, scenes, voice, output_dir):
        """Synthesize audio for multiple scenes concurrently using async gather."""
        try:
            import edge_tts
        except ImportError as exc:
            raise NarrationEngineError('edge-tts is not installed. Run: pip install edge-tts') from exc

        os.makedirs(output_dir, exist_ok=True)

        async def _speak_batch():
            tasks = []
            valid_indices = []

            for idx, scene in enumerate(scenes):
                text = str(scene.get('text', '') if isinstance(scene, dict) else scene).strip()
                if not text:
                    continue
                mp3_path = os.path.join(output_dir, f"scene_{idx + 1}.mp3")
                communicate = edge_tts.Communicate(text, voice)
                tasks.append(communicate.save(mp3_path))
                valid_indices.append((idx, text, mp3_path))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            results = []
            for idx, text, mp3_path in valid_indices:
                wav_path = mp3_path.replace('.mp3', '.wav')
                if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
                    try:
                        cls._mp3_to_wav(mp3_path, wav_path)
                        try:
                            os.remove(mp3_path)
                        except OSError:
                            pass
                        duration = round(cls._duration(wav_path), 2)
                        results.append({
                            'index': idx,
                            'text': text,
                            'audio_path': wav_path,
                            'duration_seconds': duration,
                        })
                    except Exception as e:
                        pass
            return results

        return cls._run_async(_speak_batch())

    @classmethod
    def _duration(cls, audio_path):
        with wave.open(audio_path, 'rb') as wav_file:
            return wav_file.getnframes() / float(wav_file.getframerate())

    @classmethod
    def _run(cls, command):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **VideoStudioRenderer.get_subprocess_kwargs())
        if result.returncode:
            message = result.stderr.decode('utf-8', errors='replace')[-800:]
            raise NarrationEngineError(f'Video render failed: {message}')

    @classmethod
    def get_word_timestamps(cls, text, audio_path=None, duration=4.0):
        """Extract exact word timestamps using faster-whisper or weighted timing."""
        words = str(text).strip().split()
        if not words:
            return []

        # Tier 1: Try faster-whisper alignment if audio_path exists
        if audio_path and os.path.exists(audio_path):
            try:
                from faster_whisper import WhisperModel
                model = WhisperModel('tiny', device='cpu', compute_type='int8')
                segments, _ = model.transcribe(audio_path, word_timestamps=True)
                extracted = []
                for seg in segments:
                    for w in seg.words:
                        clean = w.word.strip()
                        if clean:
                            extracted.append({'word': clean, 'start': round(w.start, 3), 'end': round(w.end, 3)})
                if extracted:
                    return extracted
            except Exception:
                pass

        # Tier 2: Weighted punctuation & character length alignment fallback
        weights = []
        for w in words:
            wt = max(1, len(w))
            if any(w.endswith(c) for c in (',', ';', ':', '-')):
                wt += 4
            elif any(w.endswith(c) for c in ('.', '?', '!')):
                wt += 7
            weights.append(wt)

        total_w = max(1, sum(weights))
        curr = 0.0
        res = []
        for w, wt in zip(words, weights):
            w_dur = (wt / total_w) * duration
            res.append({'word': w, 'start': round(curr, 3), 'end': round(curr + w_dur, 3)})
            curr += w_dur
        return res

    @classmethod
    def _build_drawtext_filter(cls, text, caption_options, width, height, duration=4.0, audio_path=None):
        if not caption_options or not caption_options.get('show_captions', True):
            return ""

        words_data = cls.get_word_timestamps(text, audio_path=audio_path, duration=duration)
        if not words_data:
            return ""

        style = caption_options.get('style', 'BEAST_YELLOW')
        position = caption_options.get('position', 'BOTTOM')
        font_size = int(caption_options.get('font_size', 44))

        # Break words into 2-3 word phrase chunks with precise timestamps
        chunk_size = 3
        chunks = []
        for i in range(0, len(words_data), chunk_size):
            group = words_data[i:i + chunk_size]
            chunk_text = " ".join(w['word'] for w in group)
            st = group[0]['start']
            et = group[-1]['end']
            chunks.append({
                'text': chunk_text,
                'start': st,
                'end': et
            })

        if not chunks:
            return ""

        # Ensure last chunk covers the full speech duration seamlessly
        chunks[-1]['end'] = max(chunks[-1]['end'], duration + 0.1)

        if position == 'TOP':
            y_expr = "h*0.12"
        elif position == 'CENTER':
            y_expr = "(h-text_h)/2"
        else:  # BOTTOM (default)
            y_expr = "h*0.82"

        if style == 'NEON_CYAN':
            font_color = "0x00E5FF"
            border_color = "black"
            border_w = 4
            box = "box=1:boxcolor=black@0.65:boxborderw=8"
        elif style == 'FIRE_PUNCH':
            font_color = "0xFF5722"
            border_color = "white"
            border_w = 4
            box = "box=1:boxcolor=black@0.75:boxborderw=10"
        elif style == 'CLEAN_WHITE':
            font_color = "white"
            border_color = "black"
            border_w = 3
            box = "box=1:boxcolor=black@0.7:boxborderw=8"
        elif style == 'CINEMATIC':
            font_color = "0xF5F5F5"
            border_color = "black@0.6"
            border_w = 2
            box = ""
        elif style == 'BOXED_DARK':
            font_color = "white"
            border_color = "black"
            border_w = 2
            box = "box=1:boxcolor=black@0.85:boxborderw=12"
        else:  # BEAST_YELLOW (default)
            font_color = "0xFFD700"
            border_color = "black"
            border_w = 5
            box = "box=1:boxcolor=black@0.7:boxborderw=10"

        box_part = f":{box}" if box else ""

        filter_parts = []
        for chunk in chunks:
            st = chunk['start']
            et = chunk['end']
            escaped = chunk['text'].replace("'", "'\\''").replace(":", "\\:").replace("%", "\\%")
            filter_parts.append(
                f"drawtext=text='{escaped}':fontsize={font_size}:fontcolor={font_color}:bordercolor={border_color}:"
                f"borderw={border_w}:x=(w-text_w)/2:y={y_expr}{box_part}:enable='between(t,{st:.3f},{et:.3f})'"
            )

        return "," + ",".join(filter_parts) if filter_parts else ""

    @classmethod
    def render(cls, scenes, voice, aspect_ratio, output_video_path, output_audio_path, progress=None, caption_options=None):
        """Narrate scenes, give every image a Ken-Burns movement, add timed phrase captions, then concatenate."""
        if not scenes:
            raise NarrationEngineError('Add at least one image scene before rendering.')
        if len(scenes) > cls.MAX_SCENES:
            raise NarrationEngineError(f'A POV narration can have no more than {cls.MAX_SCENES} image scenes.')
        ffmpeg = VideoStudioRenderer.get_ffmpeg_binary()
        width, height = (1080, 1920) if aspect_ratio == '9:16' else (1920, 1080)
        os.makedirs(os.path.dirname(output_video_path), exist_ok=True)
        os.makedirs(os.path.dirname(output_audio_path), exist_ok=True)

        with tempfile.TemporaryDirectory(prefix='narration_render_') as temp_dir:
            segment_paths, audio_paths = [], []
            for index, scene in enumerate(scenes):
                image_path, text = scene.get('image_path'), scene.get('text', '')
                if not image_path or not os.path.isfile(image_path):
                    raise NarrationEngineError(f'Scene {index + 1} is missing its image.')
                
                # Use existing audio if provided and valid, otherwise synthesize
                existing_audio = scene.get('audio_path')
                if existing_audio and os.path.isfile(existing_audio):
                    audio_path = existing_audio
                    duration = cls._duration(audio_path)
                else:
                    audio_path = os.path.join(temp_dir, f'scene_{index + 1}.wav')
                    duration = cls.synthesize_scene(text, voice, audio_path)
                
                scene['duration_seconds'] = round(duration, 2)
                frame_count = max(1, round(duration * 30))
                segment_path = os.path.join(temp_dir, f'scene_{index + 1}.mp4')
                movement = scene.get('movement', 'zoom_in')
                
                if movement == 'zoom_out':
                    zoom = "max(1.15-on*0.0015,1.0)"
                    pan = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                elif movement == 'pan_left':
                    zoom = "1.12"
                    pan = f"x='(1-on/{frame_count})*(iw-iw/zoom)':y='ih/2-(ih/zoom/2)'"
                elif movement == 'pan_right':
                    zoom = "1.12"
                    pan = f"x='(on/{frame_count})*(iw-iw/zoom)':y='ih/2-(ih/zoom/2)'"
                elif movement == 'static':
                    zoom = "1.0"
                    pan = "x='0':y='0'"
                else:  # zoom_in (default)
                    zoom = "min(zoom+0.0015,1.15)"
                    pan = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"

                camera_effect = scene.get('camera_effect', 'auto')
                impact_start = cls.impact_shake_start_fraction(text)
                if camera_effect == 'impact_shake' and impact_start is None:
                    impact_start = 0.25
                if camera_effect in ('none', 'handheld'):
                    impact_start = None
                if camera_effect == 'handheld':
                    pan_match = re.fullmatch(r"x='([^']+)':y='([^']+)'", pan)
                    base_x, base_y = pan_match.groups() if pan_match else (
                        'iw/2-(iw/zoom/2)',
                        'ih/2-(ih/zoom/2)',
                    )
                    pan = (
                        f"x='max(0,min(iw-iw/zoom,({base_x})+iw*0.004*sin(on*0.7)))':"
                        f"y='max(0,min(ih-ih/zoom,({base_y})+ih*0.004*sin(on*0.91)))'"
                    )
                    if movement == 'static':
                        zoom = '1.04'
                if impact_start is not None:
                    shake_start_frame = round(impact_start * frame_count)
                    shake_end_frame = shake_start_frame + 18
                    pan_match = re.fullmatch(r"x='([^']+)':y='([^']+)'", pan)
                    base_x, base_y = pan_match.groups() if pan_match else (
                        'iw/2-(iw/zoom/2)',
                        'ih/2-(ih/zoom/2)',
                    )
                    shake_x = (
                        f"if(between(on,{shake_start_frame},{shake_end_frame}),"
                        f"iw*0.012*sin((on-{shake_start_frame})*2.1)*exp(-(on-{shake_start_frame})*0.12),0)"
                    )
                    shake_y = (
                        f"if(between(on,{shake_start_frame},{shake_end_frame}),"
                        f"ih*0.012*sin((on-{shake_start_frame})*2.8)*exp(-(on-{shake_start_frame})*0.12),0)"
                    )
                    pan = (
                        f"x='max(0,min(iw-iw/zoom,({base_x})+({shake_x})))':"
                        f"y='max(0,min(ih-ih/zoom,({base_y})+({shake_y})))'"
                    )
                    if movement == 'static':
                        zoom = '1.04'

                effect = scene.get('effect', 'cinematic')
                if effect == 'soft':
                    grade = 'eq=brightness=0.03:saturation=0.92'
                elif effect == 'cyberpunk':
                    grade = 'eq=contrast=1.12:saturation=1.4,colorbalance=rs=0.15:bs=0.3'
                elif effect == 'vhs':
                    grade = 'eq=contrast=1.1:saturation=1.3'
                elif effect == 'dark_mood':
                    grade = 'eq=contrast=1.25:brightness=-0.06:saturation=0.8'
                elif effect == 'vintage':
                    grade = 'eq=contrast=1.05:saturation=0.85:brightness=0.02'
                else:  # cinematic (default)
                    grade = 'eq=contrast=1.06:saturation=1.12'

                caption_filter = cls._build_drawtext_filter(text, caption_options, width, height, duration=duration, audio_path=audio_path)
                atmosphere_effect = cls.resolve_atmosphere_effect(
                    scene.get('atmosphere_effect', 'auto'),
                    text,
                )
                atmosphere_filter = cls.atmosphere_filter_chain(atmosphere_effect, width, height)
                atmosphere_filter = f',{atmosphere_filter}' if atmosphere_filter else ''
                fade_out_at = max(0, duration - 0.25)
                video_filter = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},zoompan=z='{zoom}':{pan}:d={frame_count}:s={width}x{height}:fps=30,{grade}{atmosphere_filter},fade=t=in:st=0:d=0.25,fade=t=out:st={fade_out_at:.3f}:d=0.25{caption_filter},format=yuv420p"
                cls._run([ffmpeg, '-y', '-framerate', '30', '-loop', '1', '-i', image_path, '-i', audio_path, '-filter_complex', f'[0:v]{video_filter}[v]', '-map', '[v]', '-map', '1:a', '-t', f'{duration:.3f}', *VideoStudioRenderer.get_optimal_encoder_args(), '-c:a', 'aac', '-b:a', '192k', '-shortest', segment_path])
                segment_paths.append(segment_path)
                audio_paths.append(audio_path)
                if progress:
                    progress(index + 1, len(scenes), f'Animated scene {index + 1} of {len(scenes)}')

            for paths, list_name, output in ((segment_paths, 'segments.txt', output_video_path), (audio_paths, 'audio.txt', output_audio_path)):
                list_path = os.path.join(temp_dir, list_name)
                with open(list_path, 'w', encoding='utf-8') as file_list:
                    for path in paths:
                        file_list.write("file '" + path.replace("'", "'\\''") + "'\n")
                cls._run([ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', output])

        return sum(float(scene.get('duration_seconds', 0)) for scene in scenes)
