import os
from django.db import models

class VideoProject(models.Model):
    class VideoFormat(models.TextChoices):
        ONE_HOUR_LOOP = '1HOUR_LOOP', '1-Hour Study/Chill Loop (60 Min)'
        VISUALIZER = 'VISUALIZER', '1080p Official Visualizer (Full Song)'
        SHORT = 'SHORT', '15s YouTube Shorts Hook (9:16 Vertical)'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RENDERING = 'RENDERING', 'Rendering'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    title = models.CharField(max_length=255, default='My 1-Hour Chill Loop')
    audio_file = models.FileField(upload_to='studio/audio/')
    cover_image = models.ImageField(upload_to='studio/covers/')
    video_format = models.CharField(
        max_length=20,
        choices=VideoFormat.choices,
        default=VideoFormat.ONE_HOUR_LOOP
    )
    output_video = models.FileField(upload_to='studio/videos/', blank=True, null=True)
    duration_seconds = models.IntegerField(default=3600)
    render_status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    error_message = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Video Studio Project'
        verbose_name_plural = 'Video Studio Projects'

    def __str__(self):
        return f"{self.title} ({self.get_video_format_display()})"

    @property
    def youtube_title(self):
        if self.video_format == self.VideoFormat.ONE_HOUR_LOOP:
            return f"1 HOUR of {self.title} [Study / Deep Focus / Chill Beats Loop]"
        elif self.video_format == self.VideoFormat.SHORT:
            return f"{self.title} 🔥 #shorts #chillbeats #newmusic"
        else:
            return f"{self.title} (Official Audio / Visualizer)"

    @property
    def youtube_description(self):
        return (
            f"🎧 Listen to '{self.title}' on repeat!\n\n"
            f"✨ Perfect for studying, coding, gaming, relaxing, or sleeping.\n\n"
            f"⏱️ Chapters:\n"
            f"00:00 - {self.title} (Start)\n"
            f"15:00 - Deep Focus Phase\n"
            f"30:00 - Concentration Zone\n"
            f"45:00 - Final Session\n\n"
            f"👍 Leave a like & subscribe to support the channel!\n"
            f"#studybeats #chillvibes #1hourloop #focusmusic #ambient"
        )


class LongMixProject(models.Model):
    class TransitionCurve(models.TextChoices):
        QSIN = 'qsin', 'Smooth Quarter-Sine / Equal-Power (Recommended)'
        TRI = 'tri', 'Triangular Linear Fade'
        LIN = 'lin', 'Linear Crossfade'
        EXP = 'exp', 'Exponential Fade (Smooth Fade-Out)'
        FAST = 'fast', 'Quick Club Cut (1.5s Overlap)'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RENDERING = 'RENDERING', 'Rendering'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    title = models.CharField(max_length=255, default='My Non-Stop Music Mix')
    description = models.TextField(blank=True, default='')
    cover_image = models.ImageField(upload_to='studio/mix_covers/', blank=True, null=True)
    transition_curve = models.CharField(
        max_length=20,
        choices=TransitionCurve.choices,
        default=TransitionCurve.QSIN
    )
    crossfade_seconds = models.IntegerField(default=6)
    render_video = models.BooleanField(default=True)
    output_audio = models.FileField(upload_to='studio/mix_audio/', blank=True, null=True)
    output_video = models.FileField(upload_to='studio/mix_videos/', blank=True, null=True)
    duration_seconds = models.IntegerField(default=0)
    track_count = models.IntegerField(default=0)
    tracklist_data = models.JSONField(default=list, blank=True)
    render_status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    error_message = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Non-Stop Long Mix Project'
        verbose_name_plural = 'Non-Stop Long Mix Projects'

    def __str__(self):
        return f"{self.title} ({self.track_count} tracks • {self.duration_formatted})"

    @property
    def duration_formatted(self):
        secs = int(self.duration_seconds or 0)
        hours = secs // 3600
        minutes = (secs % 3600) // 60
        seconds = secs % 60
        if hours > 0:
            return f"{hours}h {minutes:02d}m {seconds:02d}s"
        return f"{minutes:02d}m {seconds:02d}s"

    @property
    def youtube_chapters_text(self):
        """
        Formats tracklist_data into standard YouTube chapters with timestamps.
        Example:
        00:00 Track 1 - Artist A
        03:42 Track 2 - Artist B
        """
        lines = []
        for track in (self.tracklist_data or []):
            start_sec = int(track.get('start_seconds', 0))
            hours = start_sec // 3600
            minutes = (start_sec % 3600) // 60
            seconds = start_sec % 60
            
            if hours > 0:
                time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                time_str = f"{minutes:02d}:{seconds:02d}"
            
            title = track.get('title', 'Unknown Track')
            artist = track.get('artist', '')
            if artist:
                lines.append(f"{time_str} - {title} ({artist})")
            else:
                lines.append(f"{time_str} - {title}")

        if not lines:
            return "00:00 - Continuous Mix Start"
        return "\n".join(lines)

    @property
    def export_filename(self):
        title_clean = "".join(c for c in self.title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        return f"{title_clean}_Continuous_Mix.mp4" if title_clean else f"Mix_{self.id}.mp4"

    @property
    def export_audio_filename(self):
        title_clean = "".join(c for c in self.title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        return f"{title_clean}_Continuous_Mix.mp3" if title_clean else f"Mix_{self.id}.mp3"

    @property
    def youtube_description(self):
        chapters = self.youtube_chapters_text
        return (
            f"🎵 {self.title}\n\n"
            f"Enjoy this seamless non-stop continuous music mix! Perfect for study sessions, driving, gaming, and relaxing.\n\n"
            f"⏱️ Tracklist & Chapters:\n"
            f"{chapters}\n\n"
            f"🎧 Mixed with smooth equal-power crossfades.\n"
            f"👍 Like and subscribe for more non-stop DJ mixes!\n\n"
            f"#nonstopmix #longmix #chillmix #djmix #studybeats #continuousmix"
        )


class ShortVideoProject(models.Model):
    class SourceType(models.TextChoices):
        VIDEO = 'VIDEO', 'Long Video Upload (.mp4, .mov, .mkv)'
        AUDIO_COVER = 'AUDIO_COVER', 'Audio + Cover Art Track'

    class AspectMode(models.TextChoices):
        BLURRED_FIT = 'BLURRED_FIT', 'Blurred Ambient Background (9:16 Vertical)'
        CENTER_CROP = 'CENTER_CROP', 'Center Crop 9:16 (Fill Full Screen)'
        LETTERBOX = 'LETTERBOX', 'Letterbox (Black Bars)'

    class ThemeStyle(models.TextChoices):
        VIRAL_HOOK = 'VIRAL_HOOK', 'Viral Hook Banner + Smart Badges'
        CLEAN = 'CLEAN', 'Modern Minimal (No Overlays)'
        GLOW_NEON = 'GLOW_NEON', 'Cyber Neon Glow'
        CHILL_LOFI = 'CHILL_LOFI', 'Aesthetic Lo-Fi / Ambient'

    class HookPosition(models.TextChoices):
        TOP = 'TOP', 'Top (Header Overlay)'
        CENTER = 'CENTER', 'Center (Focal Drop)'
        BOTTOM = 'BOTTOM', 'Bottom (Lower Third / CTA Area)'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RENDERING = 'RENDERING', 'Rendering'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    title = models.CharField(max_length=255, default='My Viral Shorts & Reels')
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
        default=SourceType.VIDEO
    )
    source_video = models.FileField(upload_to='studio/shorts_source/', blank=True, null=True)
    audio_file = models.FileField(upload_to='studio/shorts_audio/', blank=True, null=True)
    cover_image = models.ImageField(upload_to='studio/shorts_covers/', blank=True, null=True)

    aspect_mode = models.CharField(
        max_length=20,
        choices=AspectMode.choices,
        default=AspectMode.BLURRED_FIT
    )
    crop_focal_percent = models.IntegerField(default=50)
    theme_style = models.CharField(
        max_length=30,
        choices=ThemeStyle.choices,
        default=ThemeStyle.VIRAL_HOOK
    )
    hook_position = models.CharField(
        max_length=20,
        choices=HookPosition.choices,
        default=HookPosition.TOP
    )

    duration_seconds = models.FloatField(default=0.0)
    chop_count = models.IntegerField(default=0)
    chops_data = models.JSONField(default=list, blank=True)
    output_video = models.FileField(upload_to='studio/shorts_output/', blank=True, null=True)

    render_status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    error_message = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Short Video Project'
        verbose_name_plural = 'Short Video Projects'

    def __str__(self):
        return f"{self.title} ({self.chop_count} chops • {self.get_source_type_display()})"

    @property
    def duration_formatted(self):
        secs = int(self.duration_seconds or 0)
        minutes = (secs % 3600) // 60
        seconds = secs % 60
        hours = secs // 3600
        if hours > 0:
            return f"{hours}h {minutes:02d}m {seconds:02d}s"
        return f"{minutes:02d}m {seconds:02d}s"

    @property
    def completed_chops_count(self):
        return sum(1 for c in (self.chops_data or []) if c.get('status') == 'COMPLETED' or c.get('output_url'))

    def export_filename_for_chop(self, chop_index=0):
        chops = self.chops_data or []
        chop = chops[chop_index] if chop_index < len(chops) else {}
        part_num = chop.get('id', chop_index + 1)
        title_clean = "".join(c for c in self.title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        return f"{title_clean}_Part_{part_num}.mp4" if title_clean else f"Short_{self.id}_Part_{part_num}.mp4"

    @property
    def export_zip_filename(self):
        title_clean = "".join(c for c in self.title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        return f"{title_clean}_Shorts_Package.zip" if title_clean else f"Shorts_Package_{self.id}.zip"

    def youtube_title_for_chop(self, chop_index=0):
        chops = self.chops_data or []
        chop = chops[chop_index] if chop_index < len(chops) else {}
        part_label = f"Part {chop_index + 1}"
        hook = chop.get('hook_text', '').strip()
        if hook:
            return f"{self.title} ({part_label}) - {hook} 🔥 #shorts #fyp #viral"
        return f"{self.title} - {part_label} 🔥 #shorts #trending #newvideo"

    def youtube_description_for_chop(self, chop_index=0):
        chops = self.chops_data or []
        chop = chops[chop_index] if chop_index < len(chops) else {}
        part_label = f"Part {chop_index + 1}"
        start_sec = chop.get('start_seconds', 0.0)
        end_sec = chop.get('end_seconds', 0.0)
        dur = round(end_sec - start_sec, 1)

        return (
            f"🔥 {self.title} - {part_label}\n\n"
            f"Segment: {start_sec}s to {end_sec}s ({dur}s clip)\n"
            f"Watch full version on our channel!\n\n"
            f"👍 Like, Share & Subscribe for more daily shorts!\n\n"
            f"#shorts #shortsvideo #reels #tiktok #viralvideo #trending #fyp"
        )


class LyricVideoProject(models.Model):
    class SourceType(models.TextChoices):
        AUDIO_IMAGE = 'AUDIO_IMAGE', 'Audio Track + Cover Art'
        VIDEO = 'VIDEO', 'Source Video File (.mp4, .mov, .webm)'

    class AnimationStyle(models.TextChoices):
        KARAOKE_WIPE    = 'KARAOKE_WIPE',    'Karaoke Color Wipe & Glow'
        ROLLING_3LINE   = 'ROLLING_3LINE',   'Smooth 3-Line Rolling Display'
        PLAYFUL_POP     = 'PLAYFUL_POP',     'Playful Pop'
        BUBBLE_BOUNCE   = 'BUBBLE_BOUNCE',   'Bubble Bounce'
        DREAMY_DRIFT    = 'DREAMY_DRIFT',    'Dreamy Drift'
        NEON_GLOW       = 'NEON_GLOW',       'Neon Glow'
        HANDWRITTEN_INK = 'HANDWRITTEN_INK', 'Handwritten Ink'
        RETRO_VHS       = 'RETRO_VHS',       'Retro VHS'
        VINTAGE_COUNTRY = 'VINTAGE_COUNTRY', 'Vintage Country'
        CYBER_NEON      = 'CYBER_NEON',      'Cyber Neon Glow'
        CINEMATIC       = 'CINEMATIC',       'Cinematic Minimal Serif'
        BOUNCE_IN       = 'BOUNCE_IN',       'Bounce Pop-In'
        TYPEWRITER      = 'TYPEWRITER',      'Typewriter Reveal'
        WAVE_PULSE      = 'WAVE_PULSE',      'Wave Color Pulse'
        SLIDE_UP        = 'SLIDE_UP',        'Smooth Slide Up'

    class AspectRatio(models.TextChoices):
        LANDSCAPE_16_9 = '16:9', '16:9 Landscape (YouTube Full HD 1080p)'
        VERTICAL_9_16 = '9:16', '9:16 Vertical (Shorts / Reels / TikTok)'

    class PositionMode(models.TextChoices):
        CENTER = 'CENTER', 'Center'
        BOTTOM = 'BOTTOM', 'Bottom Third'
        TOP = 'TOP', 'Top'

    class CoverLayout(models.TextChoices):
        AMBIENT = 'AMBIENT', 'Ambient / Centered Artwork'
        FULL = 'FULL', 'Full Cover Background'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RENDERING = 'RENDERING', 'Rendering'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    title = models.CharField(max_length=255, default='My Song Lyrics')
    artist_name = models.CharField(max_length=255, blank=True, default='')
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
        default=SourceType.AUDIO_IMAGE
    )
    audio_file = models.FileField(upload_to='studio/lyrics_audio/', blank=True, null=True)
    background_image = models.ImageField(upload_to='studio/lyrics_bg/', blank=True, null=True)
    background_video = models.FileField(upload_to='studio/lyrics_bg_video/', blank=True, null=True)

    lyrics_raw_text = models.TextField(blank=True, default='')
    lyrics_data = models.JSONField(default=list, blank=True)

    animation_style = models.CharField(
        max_length=30,
        choices=AnimationStyle.choices,
        default=AnimationStyle.KARAOKE_WIPE
    )
    aspect_ratio = models.CharField(
        max_length=10,
        choices=AspectRatio.choices,
        default=AspectRatio.LANDSCAPE_16_9
    )
    font_family = models.CharField(max_length=50, default='Arial')
    font_size = models.IntegerField(default=48)
    highlight_color = models.CharField(max_length=20, default='#00E5FF')
    text_color = models.CharField(max_length=20, default='#FFFFFF')
    position_mode = models.CharField(max_length=20, choices=PositionMode.choices, default=PositionMode.CENTER)
    cover_layout = models.CharField(
        max_length=20,
        choices=CoverLayout.choices,
        default=CoverLayout.AMBIENT
    )
    cover_size = models.FloatField(default=1.0)
    cover_blur = models.FloatField(default=8.0)
    cover_opacity = models.FloatField(default=1.0)
    cover_offset = models.FloatField(default=0.0)
    cover_brightness = models.FloatField(default=1.0)
    cover_contrast = models.FloatField(default=1.0)
    cover_saturation = models.FloatField(default=1.0)
    cover_vignette = models.FloatField(default=0.0)

    # Advanced Typography Settings
    font_weight = models.CharField(max_length=10, default='bold')
    font_italic = models.BooleanField(default=False)
    letter_spacing = models.FloatField(default=0.0)
    line_height = models.FloatField(default=1.4)
    text_transform = models.CharField(max_length=15, default='none')
    text_stroke_width = models.FloatField(default=2.5)
    text_shadow_depth = models.FloatField(default=2.0)
    font_scale_x = models.IntegerField(default=100)
    font_scale_y = models.IntegerField(default=100)
    bg_opacity = models.IntegerField(default=0)
    time_offset = models.FloatField(default=0.0)  # seconds to shift subtitle timing (negative = earlier)

    output_video = models.FileField(upload_to='studio/lyrics_output/', blank=True, null=True)
    duration_seconds = models.FloatField(default=0.0)
    render_status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    error_message = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Lyrics Video Project'
        verbose_name_plural = 'Lyrics Video Projects'

    def __str__(self):
        artist = f" - {self.artist_name}" if self.artist_name else ""
        return f"{self.title}{artist} ({self.get_animation_style_display()})"

    @property
    def duration_formatted(self):
        secs = int(self.duration_seconds or 0)
        minutes = (secs % 3600) // 60
        seconds = secs % 60
        hours = secs // 3600
        if hours > 0:
            return f"{hours}h {minutes:02d}m {seconds:02d}s"
        return f"{minutes:02d}m {seconds:02d}s"

    @property
    def thumbnail_image_url(self):
        """
        Returns the best available thumbnail URL for dashboard display.
        Priority: background_image → extracted frame from output_video → None.
        Extracted frames are cached in media/studio/lyrics_thumbs/ by project id.
        """
        if self.background_image:
            try:
                return self.background_image.url
            except Exception:
                pass

        if not self.output_video:
            return None

        try:
            video_path = self.output_video.path
        except Exception:
            return None

        if not os.path.exists(video_path):
            return None

        import subprocess
        from django.conf import settings

        thumb_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'lyrics_thumbs')
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_filename = f"lyric_{self.id}_thumb.jpg"
        thumb_path = os.path.join(thumb_dir, thumb_filename)

        if not os.path.exists(thumb_path):
            try:
                subprocess.run(
                    [
                        'ffmpeg', '-y',
                        '-ss', '3',
                        '-i', video_path,
                        '-frames:v', '1',
                        '-q:v', '3',
                        thumb_path,
                    ],
                    capture_output=True,
                    timeout=15,
                )
            except Exception:
                return None

        if os.path.exists(thumb_path):
            return f"{settings.MEDIA_URL}studio/lyrics_thumbs/{thumb_filename}"

        return None

    @property
    def artwork_layout_display(self):
        return self.get_cover_layout_display()

    @property
    def lrc_content(self):
        from .services.lyrics_engine import LyricsEngineService
        return LyricsEngineService.export_lrc_string(self.lyrics_data or [], self.title, self.artist_name)

    @property
    def export_filename(self):
        title_clean = "".join(c for c in self.title if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        artist_clean = "".join(c for c in self.artist_name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        if artist_clean and title_clean:
            return f"{artist_clean}_{title_clean}_Lyric_Video.mp4"
        elif title_clean:
            return f"{title_clean}_Lyric_Video.mp4"
        return f"Lyric_Video_{self.id}.mp4"

    @property
    def youtube_title(self):
        artist_str = f"{self.artist_name} - " if self.artist_name else ""
        if self.aspect_ratio == self.AspectRatio.VERTICAL_9_16:
            return f"{artist_str}{self.title} (Lyrics) 🔥 #shorts #lyrics #newmusic"
        return f"{artist_str}{self.title} (Official Lyric Video)"

    @property
    def youtube_description(self):
        artist_str = f"Artist: {self.artist_name}\n" if self.artist_name else ""
        raw_lines = "\n".join([line.get('line', '') for line in (self.lyrics_data or []) if line.get('line')])
        if not raw_lines and self.lyrics_raw_text:
            raw_lines = self.lyrics_raw_text
        return (
            f"🎵 {self.title}\n"
            f"{artist_str}\n"
            f"Official Synced Lyric Video.\n\n"
            f"📜 Lyrics:\n{raw_lines}\n\n"
            f"👍 Like, comment and subscribe for more lyrics videos!\n"
            f"#lyrics #lyricvideo #karaoke #newmusic"
        )



class AutomationPipeline(models.Model):
    class Status(models.TextChoices):
        IDLE = 'IDLE', 'Idle'
        RUNNING = 'RUNNING', 'Running'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    # Pipeline identity
    name = models.CharField(max_length=255, default='My Automation')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IDLE)

    # List of YouTube URLs to process
    youtube_urls = models.JSONField(default=list)

    # Shared render + publish preset applied to every song
    shared_config = models.JSONField(default=dict)

    # Per-song results — list of dicts, one per URL
    # Each dict: {url, title, artist, status, log, download_job_id,
    #             lyric_project_id, publishing_job_id, youtube_url}
    results = models.JSONField(default=list)

    # Overall log + progress
    log = models.TextField(blank=True, default='')
    current_song_index = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Automation Pipeline'

    def __str__(self):
        return f"{self.name} [{self.status}] — {len(self.youtube_urls)} song(s)"
