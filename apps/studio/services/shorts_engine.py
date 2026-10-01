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

class ShortsEngineService:

    @classmethod
    def get_subprocess_kwargs(cls):
        kwargs = {}
        if sys.platform == 'win32':
            kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        return kwargs

    @classmethod
    def get_ffmpeg_binary(cls):
        return VideoStudioRenderer.get_ffmpeg_binary()

    @classmethod
    def inspect_media_duration(cls, file_path):
        """
        Inspects video or audio file and returns duration in seconds as float.
        """
        ffmpeg = cls.get_ffmpeg_binary()
        file_path = os.path.abspath(str(file_path))
        
        try:
            cmd = [ffmpeg, '-i', file_path]
            proc = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, errors='ignore', **cls.get_subprocess_kwargs())
            
            # Look for Duration: 00:03:45.67
            match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', proc.stderr)
            if match:
                hours = int(match.group(1))
                mins = int(match.group(2))
                secs = float(match.group(3))
                total_seconds = hours * 3600 + mins * 60 + secs
                return max(1.0, round(total_seconds, 2))
        except Exception:
            pass

        return 60.0  # Safe default 60s

    CAPTION_STYLES = {
        'BEAST_YELLOW': {
            'name': 'MrBeast Yellow Highlight',
            'font_name': 'Arial Black',
            'primary_color': '&H00FFFFFF&',    # White for inactive words
            'highlight_color': '&H0000E5FF&',  # Vibrant Gold / Yellow for active word (&HAABBGGRR&: B=00, G=E5, R=FF)
            'outline_color': '&H00000000&',    # Pure black outline
            'back_color': '&H80000000&',       # Semi-transparent shadow
            'outline_width': 6,
            'shadow_depth': 3,
            'margin_v': 360,
        },
        'NEON_CYAN': {
            'name': 'Cyber Neon Cyan',
            'font_name': 'Arial Black',
            'primary_color': '&H00FFFFFF&',
            'highlight_color': '&H00FFFF00&',  # Electric Cyan (&HAABBGGRR&: B=FF, G=FF, R=00)
            'outline_color': '&H00000000&',
            'back_color': '&H80000000&',
            'outline_width': 6,
            'shadow_depth': 3,
            'margin_v': 360,
        },
        'FIRE_PUNCH': {
            'name': 'Fire Punch Orange/Red',
            'font_name': 'Arial Black',
            'primary_color': '&H00FFFFFF&',
            'highlight_color': '&H002060FF&',  # Neon Orange / Red (&HAABBGGRR&: B=20, G=60, R=FF)
            'outline_color': '&H00000000&',
            'back_color': '&H80000000&',
            'outline_width': 6,
            'shadow_depth': 3,
            'margin_v': 360,
        },
        'CLEAN_WHITE': {
            'name': 'Classic Bold White',
            'font_name': 'Arial Black',
            'primary_color': '&H00FFFFFF&',
            'highlight_color': '&H0000FF00&',  # Neon green accent
            'outline_color': '&H00000000&',
            'back_color': '&H80000000&',
            'outline_width': 5,
            'shadow_depth': 2,
            'margin_v': 360,
        },
    }

    @classmethod
    def format_ass_timestamp(cls, seconds):
        seconds = max(0.0, float(seconds))
        centiseconds = int(round((seconds % 1.0) * 100))
        if centiseconds >= 100:
            seconds += 1.0
            centiseconds = 0
        total_secs = int(seconds)
        hours = total_secs // 3600
        minutes = (total_secs % 3600) // 60
        secs = total_secs % 60
        return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"

    @classmethod
    def write_caption_ass(cls, words, duration_seconds, output_ass_path, style_name='BEAST_YELLOW', font_size=64):
        """
        Generates dynamic ASS subtitles with kinetic word-by-word active highlight animation.
        """
        duration = max(0.0, float(duration_seconds))
        style_config = cls.CAPTION_STYLES.get(style_name) or cls.CAPTION_STYLES['BEAST_YELLOW']

        font_name = style_config.get('font_name', 'Arial Black')
        primary_color = style_config.get('primary_color', '&H00FFFFFF&')
        highlight_color = style_config.get('highlight_color', '&H0000E5FF&')
        outline_color = style_config.get('outline_color', '&H00000000&')
        back_color = style_config.get('back_color', '&H80000000&')
        outline_w = style_config.get('outline_width', 6)
        shadow_d = style_config.get('shadow_depth', 3)
        margin_v = style_config.get('margin_v', 360)

        try:
            font_size = max(32, min(96, int(font_size)))
        except (TypeError, ValueError):
            font_size = 64

        # Group words into short punchy lines (3-5 words max for viral mobile retention)
        groups = []
        current = []

        def flush_group():
            if current:
                groups.append(current[:])
                current.clear()

        for item in words:
            text = str(item.get('word', '')).strip().upper()
            start = max(0.0, float(item.get('start', 0.0)))
            end = min(duration, float(item.get('end', start + 0.3)))
            if not text or start >= duration or end <= start:
                continue

            current_text = ' '.join(w['word'] for w in current)
            if current and (
                len(current) >= 4
                or len(current_text) + len(text) + 1 > 28
                or start - current[-1]['end'] > 0.6
            ):
                flush_group()
            current.append({'word': text, 'start': start, 'end': end})
        flush_group()

        if not groups:
            return None

        os.makedirs(os.path.dirname(os.path.abspath(output_ass_path)), exist_ok=True)

        lines = [
            "[Script Info]",
            "Title: Viral Shorts Dynamic Captions",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "PlayResX: 1080",
            "PlayResY: 1920",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,{font_name},{font_size},{primary_color},{highlight_color},{outline_color},{back_color},-1,0,0,0,100,100,1,0,1,{outline_w},{shadow_d},2,60,60,{margin_v},1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

        for group in groups:
            for idx, active_w in enumerate(group):
                w_start = active_w['start']
                if idx < len(group) - 1:
                    w_end = min(group[idx + 1]['start'], max(active_w['end'], w_start + 0.15))
                else:
                    w_end = min(duration, max(active_w['end'], w_start + 0.15))

                if w_end <= w_start:
                    continue

                line_words = []
                for j, w in enumerate(group):
                    if j == idx:
                        line_words.append(f"{{\\c{highlight_color}\\b1}}{w['word']}{{\\r}}")
                    else:
                        line_words.append(f"{{\\c{primary_color}}}{w['word']}")

                dialogue_text = ' '.join(line_words)
                start_ts = cls.format_ass_timestamp(w_start)
                end_ts = cls.format_ass_timestamp(w_end)
                lines.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{dialogue_text}")

        with open(output_ass_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

        return output_ass_path

    @classmethod
    def write_caption_srt(cls, words, duration_seconds, output_srt_path):
        duration = max(0.0, float(duration_seconds))
        groups = []
        current = []

        def flush_group():
            if current:
                groups.append(current[:])
                current.clear()

        for word in words:
            text = str(word.get('word', '')).strip()
            start = max(0.0, float(word.get('start', 0.0)))
            end = min(duration, float(word.get('end', start)))
            if not text or start >= duration or end <= start:
                continue

            current_text = ' '.join(item['word'] for item in current)
            if current and (
                len(current) >= 5
                or len(current_text) + len(text) + 1 > 38
                or start - current[-1]['end'] > 0.8
            ):
                flush_group()
            current.append({'word': text, 'start': start, 'end': end})
        flush_group()

        if not groups:
            return None

        def timestamp(seconds):
            milliseconds = max(0, int(round(seconds * 1000)))
            hours, milliseconds = divmod(milliseconds, 3_600_000)
            minutes, milliseconds = divmod(milliseconds, 60_000)
            seconds, milliseconds = divmod(milliseconds, 1000)
            return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'

        os.makedirs(os.path.dirname(os.path.abspath(output_srt_path)), exist_ok=True)
        with open(output_srt_path, 'w', encoding='utf-8') as subtitle_file:
            for index, group in enumerate(groups, start=1):
                text = ' '.join(item['word'] for item in group)
                start = min(duration, group[0]['start'])
                end = min(duration, max(group[-1]['end'], start + 0.15))
                if end <= start:
                    continue
                subtitle_file.write(
                    f'{index}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n\n'
                )
        return output_srt_path

    @classmethod
    def transcribe_clip_captions(cls, source_path, start_seconds, duration_seconds, output_path, model, style_name='BEAST_YELLOW', font_size=64):
        audio_path = output_path.rsplit('.', 1)[0] + '.wav'
        ffmpeg = cls.get_ffmpeg_binary()
        start = max(0.0, float(start_seconds))
        duration = max(0.1, float(duration_seconds))
        try:
            proc = subprocess.run(
                [
                    ffmpeg, '-y', '-ss', f'{start:.3f}', '-t', f'{duration:.3f}',
                    '-i', os.path.abspath(str(source_path)), '-vn', '-ac', '1',
                    '-ar', '16000', '-c:a', 'pcm_s16le', audio_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors='ignore',
                **cls.get_subprocess_kwargs(),
            )
            if proc.returncode != 0 or not os.path.exists(audio_path):
                raise RuntimeError(f'Could not extract clip audio for captions: {proc.stderr[-300:]}')

            segments, _ = model.transcribe(
                audio_path,
                beam_size=1,
                word_timestamps=True,
                vad_filter=True,
            )
            words = []
            for segment in segments:
                if segment.words:
                    words.extend(
                        {
                            'word': word.word,
                            'start': word.start,
                            'end': word.end,
                        }
                        for word in segment.words
                    )
                    continue

                segment_words = (segment.text or '').split()
                segment_duration = max(0.1, segment.end - segment.start)
                word_duration = segment_duration / max(1, len(segment_words))
                words.extend(
                    {
                        'word': word,
                        'start': segment.start + index * word_duration,
                        'end': segment.start + (index + 1) * word_duration,
                    }
                    for index, word in enumerate(segment_words)
                )

            if output_path.lower().endswith('.srt'):
                return cls.write_caption_srt(words, duration, output_path)
            else:
                return cls.write_caption_ass(words, duration, output_path, style_name=style_name, font_size=font_size)
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)

    @classmethod
    def _caption_filter(cls, subtitle_path, font_size=64):
        escaped_path = os.path.abspath(str(subtitle_path)).replace('\\', '/').replace(':', r'\:').replace("'", r"\'")
        if str(subtitle_path).lower().endswith('.ass'):
            return f"ass=filename='{escaped_path}'"
        try:
            font_size = max(18, min(72, int(font_size)))
        except (TypeError, ValueError):
            font_size = 30
        style = f'FontName=Arial Black,FontSize={font_size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=4,Shadow=1,Alignment=2,MarginV=320'
        return f"subtitles=filename='{escaped_path}':force_style='{style}'"

    @classmethod
    def generate_chop_splits(cls, total_duration, interval_seconds=15.0, hook_prefix="Wait for the end... 🔥"):
        """
        Generates automatic even chops for a given total duration.
        """
        interval = max(5.0, float(interval_seconds))
        total = float(total_duration)
        if total <= 0:
            total = 60.0

        chops = []
        current_start = 0.0
        idx = 1

        while current_start < total:
            current_end = min(total, current_start + interval)
            dur = round(current_end - current_start, 2)
            
            if dur >= 3.0:  # Ignore fragments smaller than 3s
                chops.append({
                    'id': idx,
                    'title': f"Part {idx}",
                    'hook_text': f"{hook_prefix}" if idx == 1 else f"Part {idx} 🔥",
                    'start_seconds': round(current_start, 2),
                    'end_seconds': round(current_end, 2),
                    'duration': dur,
                    'output_file': '',
                    'status': 'PENDING',
                })
                idx += 1
            
            current_start = current_end

        if not chops:
            chops.append({
                'id': 1,
                'title': 'Part 1',
                'hook_text': hook_prefix,
                'start_seconds': 0.0,
                'end_seconds': min(total, 15.0),
                'duration': min(total, 15.0),
                'output_file': '',
                'status': 'PENDING',
            })

        return chops

    @classmethod
    def prepare_overlay_banner(cls, hook_text="", part_label="", theme="VIRAL_HOOK", hook_position="TOP", show_cta_badge=True, cta_text="▶ Full Video on YouTube  •  Subscribe", width=1080, height=1920, output_png_path=None):
        """
        Creates a high-resolution transparent RGBA PNG overlay with modern typography,
        viral hook banner badges, and call-to-action pills for vertical 9:16 shorts.
        Supports hook_position: 'TOP', 'CENTER', or 'BOTTOM'.
        Supports toggling the bottom call-to-action pill (show_cta_badge).
        """
        has_hook = bool(hook_text or part_label)
        if (theme == 'CLEAN' or not has_hook) and not show_cta_badge:
            return None

        # Create transparent canvas
        img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Try to load standard TrueType font, fallback to default font
        font_large = None
        font_small = None
        font_badge = None

        font_names = [
            "arialbd.ttf", "segoeuib.ttf", "calibrib.ttf", "impact.ttf",
            "DejaVuSans-Bold.ttf", "arial.ttf", "calibri.ttf"
        ]
        
        for fn in font_names:
            try:
                font_large = ImageFont.truetype(fn, 44)
                font_small = ImageFont.truetype(fn, 28)
                font_badge = ImageFont.truetype(fn, 22)
                break
            except Exception:
                continue

        if not font_large:
            font_large = ImageFont.load_default()
            font_small = font_large
            font_badge = font_large

        # 1. Part / Viral Hook Banner
        if hook_text or part_label:
            banner_text = hook_text.strip() if hook_text else (part_label.strip() if part_label else "WAIT FOR IT... 🔥")
            
            # Measure text size
            try:
                bbox = draw.textbbox((0, 0), banner_text, font=font_large)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
            except Exception:
                text_w = len(banner_text) * 24
                text_h = 44

            box_padding_x = 45
            box_padding_y = 20
            box_w = min(width - 80, text_w + box_padding_x * 2)
            box_h = text_h + box_padding_y * 2
            box_x1 = (width - box_w) // 2

            pos_upper = str(hook_position or 'TOP').upper()
            if pos_upper == 'CENTER':
                box_y1 = (height - box_h) // 2
            elif pos_upper == 'BOTTOM':
                box_y1 = height - 400
            else:  # TOP (default)
                box_y1 = 160

            box_x2 = box_x1 + box_w
            box_y2 = box_y1 + box_h

            # Draw background pill with glow effect
            if theme == 'GLOW_NEON':
                pill_bg = (10, 15, 30, 220)
                border_color = (0, 240, 255, 255)
                text_color = (255, 255, 255, 255)
            elif theme == 'CHILL_LOFI':
                pill_bg = (30, 20, 35, 210)
                border_color = (230, 150, 210, 240)
                text_color = (255, 245, 250, 255)
            else:  # VIRAL_HOOK
                pill_bg = (15, 15, 20, 230)
                border_color = (255, 50, 80, 255)
                text_color = (255, 255, 255, 255)

            # Draw rounded rectangle pill
            draw.rounded_rectangle([box_x1, box_y1, box_x2, box_y2], radius=24, fill=pill_bg, outline=border_color, width=3)

            # Part Mini Tag above pill if part_label exists
            if part_label:
                part_tag = f"● {part_label.upper()} ●"
                try:
                    p_bbox = draw.textbbox((0, 0), part_tag, font=font_badge)
                    p_w = p_bbox[2] - p_bbox[0]
                except Exception:
                    p_w = len(part_tag) * 12
                
                tag_x1 = (width - p_w) // 2 - 16
                tag_y1 = box_y1 - 18
                tag_x2 = tag_x1 + p_w + 32
                tag_y2 = tag_y1 + 28
                draw.rounded_rectangle([tag_x1, tag_y1, tag_x2, tag_y2], radius=10, fill=(255, 50, 80, 255))
                draw.text((tag_x1 + 16, tag_y1 + 4), part_tag, fill=(255, 255, 255, 255), font=font_badge)

            # Draw banner main text
            text_x = (width - text_w) // 2
            text_y = box_y1 + (box_h - text_h) // 2 - 2
            draw.text((text_x, text_y), banner_text, fill=text_color, font=font_large)

        # 2. Bottom Call to Action Pill (Toggleable)
        if show_cta_badge:
            cta_banner_text = cta_text.strip() if cta_text else "▶ Full Video on YouTube  •  Subscribe"
            try:
                c_bbox = draw.textbbox((0, 0), cta_banner_text, font=font_small)
                c_w = c_bbox[2] - c_bbox[0]
                c_h = c_bbox[3] - c_bbox[1]
            except Exception:
                c_w = len(cta_banner_text) * 15
                c_h = 28

            c_box_w = c_w + 50
            c_box_h = c_h + 24
            c_box_x1 = (width - c_box_w) // 2
            c_box_y1 = height - 240
            c_box_x2 = c_box_x1 + c_box_w
            c_box_y2 = c_box_y1 + c_box_h

            draw.rounded_rectangle([c_box_x1, c_box_y1, c_box_x2, c_box_y2], radius=20, fill=(10, 10, 15, 200), outline=(255, 255, 255, 80), width=2)
            draw.text(((width - c_w) // 2, c_box_y1 + 12), cta_banner_text, fill=(255, 255, 255, 240), font=font_small)

        if output_png_path:
            os.makedirs(os.path.dirname(os.path.abspath(output_png_path)), exist_ok=True)
            img.save(output_png_path, format='PNG')
            return output_png_path

        return img

    @classmethod
    def render_video_chop(cls, source_video_path, output_mp4_path, start_seconds, duration_seconds, aspect_mode='BLURRED_FIT', overlay_png_path=None, crop_focal_percent=50, project_id=None, subtitle_path=None, caption_font_size=64, visual_progress_bar=True, audio_normalize=True):
        """
        Extracts and converts a video segment to 1080x1920 (9:16) with subject framing, overlay banners, dynamic captions, and retention bar.
        """
        ffmpeg = cls.get_ffmpeg_binary()
        source_video_path = os.path.abspath(str(source_video_path))
        output_mp4_path = os.path.abspath(str(output_mp4_path))
        os.makedirs(os.path.dirname(output_mp4_path), exist_ok=True)

        start_s = max(0.0, float(start_seconds))
        dur_s = max(1.0, float(duration_seconds))
        focal_pct = max(0.0, min(1.0, float(crop_focal_percent) / 100.0))

        if aspect_mode == 'CENTER_CROP':
            crop_x_expr = f"(iw-1080)*{focal_pct:.3f}"
            vf_base = f"[0:v]scale=-1:1920,crop=1080:1920:{crop_x_expr}:(ih-1920)/2[base]"
        elif aspect_mode == 'LETTERBOX':
            vf_base = "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black[base]"
        else:  # BLURRED_FIT (Default & Recommended - High Performance Downscaled Blur)
            crop_x_small = f"(iw-270)*{focal_pct:.3f}"
            vf_base = (
                f"[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480:{crop_x_small}:0,boxblur=8:2,eq=brightness=-0.25,scale=1080:1920:flags=fast_bilinear[bg]; "
                "[0:v]scale=1080:-1[fg]; "
                "[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
            )

        inputs = [
            '-ss', f"{start_s:.3f}",
            '-t', f"{dur_s:.3f}",
            '-i', source_video_path
        ]

        filter_parts = [vf_base]
        current_v = '[base]'

        if subtitle_path and os.path.exists(str(subtitle_path)):
            filter_parts.append(f"{current_v}{cls._caption_filter(subtitle_path, caption_font_size)}[captioned]")
            current_v = '[captioned]'

        if overlay_png_path and os.path.exists(str(overlay_png_path)):
            # Main video is always input [0]; overlay PNG is the next -i → always [1]
            overlay_idx = inputs.count('-i')
            inputs.extend(['-i', os.path.abspath(str(overlay_png_path))])
            filter_parts.append(f"{current_v}[{overlay_idx}:v]overlay=0:0[overlayed]")
            current_v = '[overlayed]'

        if visual_progress_bar:
            filter_parts.append(
                f"{current_v}drawbox=x=0:y=ih-12:w=iw:h=12:color=0x000000@0.5:t=fill,"
                f"drawbox=x=0:y=ih-12:w='iw*(t/{dur_s:.3f})':h=12:color=0x10B981@1:t=fill[prog]"
            )
            current_v = '[prog]'

        filter_complex = '; '.join(filter_parts)

        audio_args = ['-c:a', 'aac', '-b:a', '128k']
        if audio_normalize:
            # dynaudnorm: single-pass peak normalization — ~3x faster than loudnorm's 2-pass EBU R128
            audio_args = ['-af', 'dynaudnorm=p=0.95:m=100:s=5', *audio_args]

        cmd = [
            ffmpeg, '-y',
            # Faster input probing (skip unnecessary stream analysis)
            '-probesize', '5000000',
            '-analyzeduration', '1000000',
            *inputs,
            '-filter_complex', filter_complex,
            '-map', current_v,
            '-map', '0:a?',
            '-c:v', 'libx264',
            '-preset', 'ultrafast',   # ~50% faster than veryfast; fine for shorts
            '-tune', 'fastdecode',    # optimize bitstream for fast playback & encoding
            '-threads', '0',
            '-pix_fmt', 'yuv420p',
            *audio_args,
            '-movflags', '+faststart',
            output_mp4_path
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='ignore', **cls.get_subprocess_kwargs())
        if project_id:
            RenderProcessTracker.register('shorts', project_id, proc)

        stdout, stderr = "", ""
        try:
            while True:
                try:
                    stdout, stderr = proc.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if project_id and RenderProcessTracker.is_cancelled('shorts', project_id):
                        proc.kill()
                        try:
                            proc.communicate()
                        except Exception:
                            pass
                        raise RuntimeError("Rendering cancelled by user.")
        finally:
            if project_id:
                RenderProcessTracker.unregister('shorts', project_id, proc)

        if proc.returncode != 0:
            if project_id and RenderProcessTracker.is_cancelled('shorts', project_id):
                raise RuntimeError("Rendering cancelled by user.")
            raise RuntimeError(f"FFmpeg video chop rendering failed: {stderr[-400:]}")

        return output_mp4_path

    @classmethod
    def render_audio_cover_chop(cls, cover_path, audio_path, output_mp4_path, start_seconds, duration_seconds, overlay_png_path=None, project_id=None, subtitle_path=None, caption_font_size=64, visual_progress_bar=True, audio_normalize=True):
        """
        Renders a 1080x1920 vertical video from an audio file and cover image with dynamic captions & retention bar.
        """
        ffmpeg = cls.get_ffmpeg_binary()
        cover_path = os.path.abspath(str(cover_path))
        audio_path = os.path.abspath(str(audio_path))
        output_mp4_path = os.path.abspath(str(output_mp4_path))
        os.makedirs(os.path.dirname(output_mp4_path), exist_ok=True)

        start_s = max(0.0, float(start_seconds))
        dur_s = max(1.0, float(duration_seconds))

        temp_bg = output_mp4_path.replace('.mp4', '_canvas_bg.jpg')

        try:
            VideoStudioRenderer.prepare_9_16_background(cover_path, temp_bg)

            inputs = [
                '-loop', '1',
                '-i', temp_bg,
                '-ss', f"{start_s:.3f}",
                '-t', f"{dur_s:.3f}",
                '-i', audio_path
            ]

            filter_parts = []
            video_label = '[0:v]'
            if subtitle_path and os.path.exists(str(subtitle_path)):
                filter_parts.append(f"{video_label}{cls._caption_filter(subtitle_path, caption_font_size)}[captioned]")
                video_label = '[captioned]'

            if overlay_png_path and os.path.exists(str(overlay_png_path)):
                overlay_idx = inputs.count('-i')  # cover=[0], audio=[1], overlay=[2]
                inputs.extend(['-i', os.path.abspath(str(overlay_png_path))])
                filter_parts.append(f"{video_label}[{overlay_idx}:v]overlay=0:0[overlayed]")
                video_label = '[overlayed]'

            if visual_progress_bar:
                filter_parts.append(
                    f"{video_label}drawbox=x=0:y=ih-12:w=iw:h=12:color=0x000000@0.5:t=fill,"
                    f"drawbox=x=0:y=ih-12:w='iw*(t/{dur_s:.3f})':h=12:color=0x10B981@1:t=fill[prog]"
                )
                video_label = '[prog]'

            filter_complex = '; '.join(filter_parts) if filter_parts else None
            map_args = ['-map', video_label, '-map', '1:a']

            audio_args = ['-c:a', 'aac', '-b:a', '128k']
            if audio_normalize:
                audio_args = ['-af', 'dynaudnorm=p=0.95:m=100:s=5', *audio_args]

            cmd = [
                ffmpeg, '-y',
                '-probesize', '5000000',
                '-analyzeduration', '1000000',
                *inputs,
            ]
            if filter_complex:
                cmd.extend(['-filter_complex', filter_complex])
            cmd.extend([
                *map_args,
                '-c:v', 'libx264',
                '-tune', 'stillimage',
                '-preset', 'ultrafast',
                '-threads', '0',
                '-pix_fmt', 'yuv420p',
                *audio_args,
                '-shortest',
                '-movflags', '+faststart',
                output_mp4_path
            ])

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='ignore', **cls.get_subprocess_kwargs())
            if project_id:
                RenderProcessTracker.register('shorts', project_id, proc)

            stdout, stderr = "", ""
            try:
                while True:
                    try:
                        stdout, stderr = proc.communicate(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        if project_id and RenderProcessTracker.is_cancelled('shorts', project_id):
                            proc.kill()
                            try:
                                proc.communicate()
                            except Exception:
                                pass
                            raise RuntimeError("Rendering cancelled by user.")
            finally:
                if project_id:
                    RenderProcessTracker.unregister('shorts', project_id, proc)

            if proc.returncode != 0:
                if project_id and RenderProcessTracker.is_cancelled('shorts', project_id):
                    raise RuntimeError("Rendering cancelled by user.")
                raise RuntimeError(f"FFmpeg audio/cover chop rendering failed: {stderr[-400:]}")

            return output_mp4_path
        finally:
            if os.path.exists(temp_bg):
                try:
                    os.remove(temp_bg)
                except Exception:
                    pass

    @classmethod
    def download_youtube_video(cls, url, output_dir):
        """
        Downloads a YouTube video at best quality using yt-dlp + ffmpeg.
        Merges video+audio into a single MP4 file.
        Also extracts metadata: title, description, tags, channel_name, duration.

        Returns a dict:
        {
            'path': str (absolute path to downloaded MP4),
            'title': str,
            'description': str,
            'tags': list[str],
            'channel_name': str,
            'duration': float (seconds),
            'video_id': str,
        }
        """
        import yt_dlp
        import glob
        os.makedirs(output_dir, exist_ok=True)

        if not url.startswith('http'):
            url = f"https://www.youtube.com/watch?v={url}"

        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
            'outtmpl': os.path.join(output_dir, '%(id)s_%(title).80s.%(ext)s'),
            'merge_output_format': 'mp4',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'writeinfojson': False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title = info.get('title', 'YouTube Video')
        description = info.get('description', '')
        tags = info.get('tags', []) or []
        channel_name = info.get('channel', '') or info.get('uploader', '') or ''
        duration = float(info.get('duration', 0.0))
        video_id = info.get('id', 'video')

        # Sanitise title for filename matching (yt-dlp truncates/cleans the title)
        safe_title = title[:80].replace('/', '_').replace('\\', '_')

        # Find the merged MP4
        pattern = os.path.join(output_dir, f"{video_id}_*.mp4")
        matches = glob.glob(pattern)
        if matches:
            # Pick the largest file (most likely the fully merged output)
            matches.sort(key=lambda p: os.path.getsize(p), reverse=True)
            output_path = matches[0]
        else:
            # Fallback: search all mp4 files in dir
            mp4s = [f for f in os.listdir(output_dir) if f.startswith(video_id) and f.endswith('.mp4')]
            if not mp4s:
                raise FileNotFoundError(f"Could not find downloaded MP4 for URL: {url} (video_id={video_id})")
            mp4s.sort(key=lambda f: os.path.getsize(os.path.join(output_dir, f)), reverse=True)
            output_path = os.path.join(output_dir, mp4s[0])

        return {
            'path': os.path.abspath(output_path),
            'title': title,
            'description': description or '',
            'tags': [t for t in tags if t] if isinstance(tags, list) else [],
            'channel_name': channel_name,
            'duration': duration,
            'video_id': video_id,
        }
