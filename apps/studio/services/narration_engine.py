"""Kokoro narration and image-scene rendering for POV/story videos."""

import os
import subprocess
import tempfile
import wave

import numpy as np

from .renderer import VideoStudioRenderer


class NarrationEngineError(RuntimeError):
    pass


class NarrationEngineService:
    SAMPLE_RATE = 24000

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
    def synthesize_scene(cls, text, voice, output_path):
        """Create one WAV per scene using Kokoro's local Python pipeline."""
        try:
            from kokoro import KPipeline
        except ImportError as exc:
            raise NarrationEngineError('Kokoro is not installed. Add the kokoro package to enable voice narration.') from exc
        if not str(text).strip():
            raise NarrationEngineError('A scene cannot have empty narration text.')
        try:
            pipeline = KPipeline(lang_code='a')
            chunks = [np.asarray(audio, dtype=np.float32) for _, _, audio in pipeline(str(text).strip(), voice=voice)]
        except Exception as exc:
            raise NarrationEngineError(f'Kokoro could not generate narration: {exc}') from exc
        if not chunks:
            raise NarrationEngineError('Kokoro returned no narration audio.')
        cls._write_wav(np.concatenate(chunks), output_path)
        return output_path

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
    def render(cls, scenes, voice, aspect_ratio, output_video_path, output_audio_path, progress=None):
        """Narrate scenes, give every image a Ken-Burns movement, then concatenate."""
        if not scenes:
            raise NarrationEngineError('Add at least one image scene before rendering.')
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
                audio_path = os.path.join(temp_dir, f'scene_{index + 1}.wav')
                cls.synthesize_scene(text, voice, audio_path)
                duration = cls._duration(audio_path)
                scene['duration_seconds'] = round(duration, 2)
                frame_count = max(1, round(duration * 30))
                segment_path = os.path.join(temp_dir, f'scene_{index + 1}.mp4')
                movement = scene.get('movement', 'zoom_in')
                zoom = "min(zoom+0.0007,1.14)" if movement != 'zoom_out' else "max(zoom-0.0007,1.0)"
                effect = scene.get('effect', 'cinematic')
                grade = 'eq=contrast=1.06:saturation=1.12' if effect == 'cinematic' else 'eq=brightness=0.03:saturation=0.92'
                fade_out_at = max(0, duration - 0.25)
                video_filter = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},zoompan=z='{zoom}':d={frame_count}:s={width}x{height}:fps=30,{grade},fade=t=in:st=0:d=0.25,fade=t=out:st={fade_out_at:.3f}:d=0.25,format=yuv420p"
                cls._run([ffmpeg, '-y', '-loop', '1', '-i', image_path, '-i', audio_path, '-filter_complex', f'[0:v]{video_filter}[v]', '-map', '[v]', '-map', '1:a', '-t', f'{duration:.3f}', *VideoStudioRenderer.get_optimal_encoder_args(), '-c:a', 'aac', '-b:a', '192k', '-shortest', segment_path])
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
