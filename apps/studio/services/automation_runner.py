import os
import threading
import datetime
import logging

logger = logging.getLogger(__name__)


class AutomationRunnerService:

    @classmethod
    def run_pipeline_async(cls, pipeline_id):
        t = threading.Thread(target=cls._run, args=(pipeline_id,), daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Main orchestrator
    # ------------------------------------------------------------------
    @classmethod
    def _run(cls, pipeline_id):
        from apps.studio.models import AutomationPipeline
        from django.db import connection
        try:
            pipeline = AutomationPipeline.objects.get(pk=pipeline_id)
            urls = pipeline.youtube_urls or []
            cfg = pipeline.shared_config or {}

            # Merge existing song_config data (written by the view) with fresh runtime fields.
            existing = pipeline.results or []
            results = []
            for i, url in enumerate(urls):
                prev = existing[i] if i < len(existing) else {}
                results.append({
                    'url': url,
                    'index': i,
                    'title': '',
                    'artist': '',
                    'status': 'PENDING',
                    'log': '',
                    'progress': 0,
                    'download_job_id': None,
                    'lyric_project_id': None,
                    'publishing_job_id': None,
                    'youtube_video_id': None,
                    'youtube_url': None,
                    # Preserve per-song config carried from the form submission
                    'song_config': prev.get('song_config', {}),
                })

            pipeline.status = AutomationPipeline.Status.RUNNING
            pipeline.results = results
            pipeline.log = ''
            pipeline.current_song_index = 0
            pipeline.save()

            cls._global_log(pipeline, f"▶ Starting automation: {len(urls)} song(s)")

            failed_count = 0
            for i, url in enumerate(urls):
                pipeline.current_song_index = i
                pipeline.save(update_fields=['current_song_index'])

                cls._global_log(pipeline, f"\n{'─'*50}\n[{i+1}/{len(urls)}] {url}")

                # Per-song config stored in results[i]['song_config'] by the view.
                # Fall back to shared_config (Song 0 master) if not present.
                results_snapshot = pipeline.results or []
                if i < len(results_snapshot) and results_snapshot[i].get('song_config'):
                    song_cfg = {**cfg, **results_snapshot[i]['song_config']}
                else:
                    song_cfg = cfg

                try:
                    cls._process_song(pipeline, i, url, song_cfg)
                    cls._song_log(pipeline, i, '✓ Song completed successfully.')
                    cls._set_song_status(pipeline, i, 'DONE', 100)
                except Exception as e:
                    logger.error(f"Pipeline {pipeline_id} song {i} failed: {e}", exc_info=True)
                    cls._song_log(pipeline, i, f'✗ Failed: {e}')
                    cls._set_song_status(pipeline, i, 'FAILED', 0)
                    failed_count += 1
                    if cfg.get('stop_on_error', False):
                        cls._global_log(pipeline, '⛔ Stopping pipeline due to error (stop_on_error is set).')
                        break

            if failed_count == 0:
                pipeline.status = AutomationPipeline.Status.COMPLETED
                cls._global_log(pipeline, f'\n✅ All {len(urls)} song(s) completed successfully!')
            elif failed_count < len(urls):
                pipeline.status = AutomationPipeline.Status.COMPLETED
                cls._global_log(pipeline, f'\n⚠ Completed with {failed_count} failure(s).')
            else:
                pipeline.status = AutomationPipeline.Status.FAILED
                cls._global_log(pipeline, f'\n✗ All songs failed.')
            pipeline.save()

        except Exception as e:
            logger.error(f"Pipeline {pipeline_id} crashed: {e}", exc_info=True)
            try:
                from apps.studio.models import AutomationPipeline
                pipeline = AutomationPipeline.objects.get(pk=pipeline_id)
                pipeline.status = AutomationPipeline.Status.FAILED
                cls._global_log(pipeline, f'✗ Pipeline crash: {e}')
                pipeline.save()
            except Exception:
                pass
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Per-song pipeline: download → render → publish
    # ------------------------------------------------------------------
    @classmethod
    def _process_song(cls, pipeline, idx, url, cfg):
        # ── 1. DOWNLOAD ────────────────────────────────────────────────
        cls._set_song_status(pipeline, idx, 'DOWNLOADING', 0)
        cls._song_log(pipeline, idx, f'⬇ Downloading audio from: {url}')

        audio_path, title, artist, yt_thumb_path = cls._download_audio(pipeline, idx, url, cfg)

        cls._song_log(pipeline, idx, f'✓ Downloaded: {title or url}')
        cls._set_song_field(pipeline, idx, 'title', title)
        cls._set_song_field(pipeline, idx, 'artist', artist)

        # ── 2. RENDER LYRIC VIDEO ──────────────────────────────────────
        do_render = cfg.get('do_render', True)
        if not do_render:
            cls._song_log(pipeline, idx, '⏭ Skipping render (do_render=false).')
            return

        cls._set_song_status(pipeline, idx, 'RENDERING', 30)
        cls._song_log(pipeline, idx, f'🎬 Rendering lyric video for: {title}')

        video_path, lyric_project_id = cls._render_lyrics(pipeline, idx, audio_path, title, artist, yt_thumb_path, cfg)

        cls._song_log(pipeline, idx, f'✓ Rendered: {os.path.basename(video_path)}')

        # ── 3. GENERATE ATTRACTIVE THUMBNAIL ───────────────────────────
        custom_thumb_path = None
        do_gen_thumb = cfg.get('generate_thumbnail', True)
        if do_gen_thumb:
            try:
                from apps.studio.services.thumbnail_generator import ThumbnailGeneratorService
                cls._song_log(pipeline, idx, '🎨 Generating attractive custom thumbnail...')
                thumb_style = cfg.get('thumbnail_style', 'FROSTED_SCRIM')
                badge_text = cfg.get('thumbnail_badge', 'OFFICIAL LYRIC VIDEO')
                theme_palette = cfg.get('thumbnail_theme', 'DARK_STUDIO')
                bg_source = cfg.get('background_image_path') or yt_thumb_path

                custom_thumb_path = ThumbnailGeneratorService.generate_thumbnail(
                    title=title,
                    artist=artist,
                    background_image_path=bg_source,
                    style=thumb_style,
                    badge_text=badge_text,
                    theme_palette=theme_palette,
                    project_id=lyric_project_id,
                )
                cls._set_song_field(pipeline, idx, 'thumbnail_path', custom_thumb_path)
                cls._song_log(pipeline, idx, f'✓ Thumbnail created ({thumb_style} style).')
            except Exception as te:
                logger.warning(f"Thumbnail generation notice for song {idx}: {te}")
                cls._song_log(pipeline, idx, f'⚠ Thumbnail generation notice: {te}')

        # ── 4. PUBLISH ─────────────────────────────────────────────────
        do_publish = cfg.get('do_publish', True)
        if not do_publish:
            cls._song_log(pipeline, idx, '⏭ Skipping publish (do_publish=false).')
            return

        cls._set_song_status(pipeline, idx, 'PUBLISHING', 70)
        cls._song_log(pipeline, idx, f'🚀 Publishing to YouTube: {title}')

        cls._publish_video(pipeline, idx, video_path, title, artist, custom_thumb_path, cfg)

    # ------------------------------------------------------------------
    # Step implementations
    # ------------------------------------------------------------------
    @classmethod
    def _download_audio(cls, pipeline, idx, url, cfg):
        import time
        from apps.downloader.models import DownloadJob
        from apps.downloader import services as dl_services
        import re as _re

        fmt = cfg.get('audio_format', 'mp3_320')

        # Fetch metadata first for title/artist/thumbnail
        raw_title, thumbnail = '', ''
        try:
            info = dl_services.fetch_video_info(url)
            raw_title = info.get('title', '')
            thumbnail = info.get('thumbnail', '')
        except Exception as e:
            cls._song_log(pipeline, idx, f'  ⚠ Could not fetch metadata: {e}')

        # Parse title/artist from YouTube video title
        title, artist = cls._parse_yt_title(raw_title)

        job = DownloadJob.objects.create(
            url=url,
            title=raw_title,
            thumbnail_url=thumbnail,
            format=fmt,
            status=DownloadJob.Status.DOWNLOADING,
            progress=0,
        )
        cls._set_song_field(pipeline, idx, 'download_job_id', job.pk)
        cls._save_results(pipeline)

        # Cache high-res thumbnail locally if available
        downloaded_thumb = ''
        if thumbnail:
            try:
                import urllib.request
                thumb_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'automation_covers')
                os.makedirs(thumb_dir, exist_ok=True)
                thumb_dest = os.path.join(thumb_dir, f"yt_thumb_{job.pk}.jpg")
                urllib.request.urlretrieve(thumbnail, thumb_dest)
                if os.path.exists(thumb_dest) and os.path.getsize(thumb_dest) > 1000:
                    downloaded_thumb = thumb_dest
            except Exception as te:
                logger.warning(f"Could not download remote thumbnail {thumbnail}: {te}")

        done_event = threading.Event()
        result = {}

        def _on_progress(pct, text):
            DownloadJob.objects.filter(pk=job.pk).update(progress=pct, progress_text=text)
            cls._set_song_field(pipeline, idx, 'progress', int(5 + pct * 0.25))
            cls._save_results(pipeline)

        def _on_done(abs_path, file_size):
            from django.conf import settings as _s
            rel = os.path.relpath(abs_path, str(_s.MEDIA_ROOT))
            DownloadJob.objects.filter(pk=job.pk).update(
                status=DownloadJob.Status.COMPLETED,
                output_file=rel,
                file_size_bytes=file_size,
                progress=100,
            )
            result['path'] = abs_path
            done_event.set()

        def _on_error(msg):
            DownloadJob.objects.filter(pk=job.pk).update(
                status=DownloadJob.Status.FAILED,
                error_message=msg,
            )
            result['error'] = msg
            done_event.set()

        dl_services.download_video(
            job_id=job.pk, url=url, fmt=fmt, title=raw_title,
            on_progress=_on_progress, on_done=_on_done, on_error=_on_error,
        )
        done_event.wait(timeout=600)

        if result.get('error'):
            raise RuntimeError(f"Download failed: {result['error']}")
        if not result.get('path'):
            raise RuntimeError("Download timed out after 10 minutes.")

        return result['path'], title, artist, downloaded_thumb

    @classmethod
    def _render_lyrics(cls, pipeline, idx, audio_path, title, artist, yt_thumb_path, cfg):
        from django.conf import settings
        from django.core.files import File
        from apps.studio.models import LyricVideoProject
        from apps.studio.services.lyrics_engine import LyricsEngineService
        from apps.studio.services.process_tracker import RenderProcessTracker

        animation_style = cfg.get('animation_style', 'KARAOKE_WIPE')
        font_family = cfg.get('font_family', 'Arial')
        highlight_color = cfg.get('highlight_color', '#00E5FF')
        text_color = cfg.get('text_color', '#FFFFFF')
        aspect_ratio = cfg.get('aspect_ratio', '16:9')
        position_mode = cfg.get('position_mode', 'CENTER')
        font_size = int(cfg.get('font_size', 48))
        font_weight = cfg.get('font_weight', 'bold')
        text_stroke_width = float(cfg.get('text_stroke_width', 2.5))
        text_shadow_depth = float(cfg.get('text_shadow_depth', 2.0))
        lyrics_text = cfg.get('lyrics_text', '')
        bg_img_path = cfg.get('background_image_path', '') or yt_thumb_path or ''
        bg_vid_path = cfg.get('background_video_path', '')

        duration = LyricsEngineService.inspect_media_duration(audio_path)

        lyrics_data = []
        use_whisper = cfg.get('use_whisper', True)
        use_demucs = cfg.get('use_demucs', False)
        whisper_model = cfg.get('whisper_model', 'base')
        
        # 1. Use Whisper AI if enabled
        if use_whisper and not lyrics_text:
            try:
                cls._song_log(pipeline, idx, f'  🎙 Transcribing with Whisper AI ({whisper_model} model)...')
                whisper_result = LyricsEngineService.transcribe_and_sync_with_whisper(
                    audio_path,
                    model_size=whisper_model,
                    use_demucs=use_demucs,
                )
                w_bars = whisper_result.get('lyrics_data') or []
                if w_bars:
                    lyrics_data = w_bars
                    cls._song_log(pipeline, idx, f'  ✓ Whisper transcribed {len(lyrics_data)} lines.')
                else:
                    cls._song_log(pipeline, idx, '  ⚠ Whisper found no speech — falling back to instrumental placeholder.')
            except ImportError:
                cls._song_log(pipeline, idx, '  ⚠ faster-whisper not installed — skipping AI transcription.')
            except Exception as e:
                cls._song_log(pipeline, idx, f'  ⚠ Whisper failed ({e}) — falling back to instrumental placeholder.')

        # 2. Auto-fetch synced lyrics from LRCLIB online database if Whisper disabled
        if not lyrics_data and not lyrics_text and title:
            try:
                online = LyricsEngineService.fetch_online_synced_lyrics(title, artist)
                if online.get('success') and online.get('lyrics_data'):
                    lyrics_data = online['lyrics_data']
                    cls._song_log(pipeline, idx, f'  ✓ Synced lyrics found online ({len(lyrics_data)} lines).')
            except Exception:
                pass

        # 3. Fall back to pasted lyrics or instrumental placeholder
        if not lyrics_data:
            if lyrics_text:
                lyrics_data = LyricsEngineService.auto_distribute_raw_lyrics(lyrics_text, total_duration=duration)
                cls._song_log(pipeline, idx, f'  ✓ Using pasted lyrics ({len(lyrics_data)} lines).')
            else:
                lyrics_data = LyricsEngineService.auto_distribute_raw_lyrics('♪', total_duration=duration)
                cls._song_log(pipeline, idx, '  ℹ No lyrics found — rendering instrumental placeholder.')

        project = LyricVideoProject.objects.create(
            title=title or 'Untitled',
            artist_name=artist,
            source_type=(
                LyricVideoProject.SourceType.VIDEO
                if bg_vid_path and os.path.exists(bg_vid_path)
                else LyricVideoProject.SourceType.AUDIO_IMAGE
            ),
            lyrics_raw_text=lyrics_text,
            lyrics_data=lyrics_data,
            animation_style=animation_style,
            font_family=font_family,
            highlight_color=highlight_color,
            text_color=text_color,
            aspect_ratio=aspect_ratio,
            position_mode=position_mode,
            font_size=font_size,
            font_weight=font_weight,
            text_stroke_width=text_stroke_width,
            text_shadow_depth=text_shadow_depth,
            render_status=LyricVideoProject.Status.RENDERING,
        )

        # Link background image if provided
        if bg_img_path and os.path.exists(bg_img_path):
            with open(bg_img_path, 'rb') as f:
                project.background_image.save(os.path.basename(bg_img_path), File(f), save=False)

        # Link audio
        with open(audio_path, 'rb') as af:
            project.audio_file.save(os.path.basename(audio_path), File(af), save=False)

        project.save()

        cls._set_song_field(pipeline, idx, 'lyric_project_id', project.pk)
        cls._set_song_field(pipeline, idx, 'progress', 40)
        cls._save_results(pipeline)

        out_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'lyrics_output')
        os.makedirs(out_dir, exist_ok=True)
        safe = "".join(c for c in title if c.isalnum() or c in ' _-').strip().replace(' ', '_')[:30]
        out_filename = f"auto_{project.id}_{safe}.mp4"
        out_path = os.path.join(out_dir, out_filename)

        def _progress_cb(pct, step_text):
            mapped = int(40 + pct * 0.3)
            cls._set_song_field(pipeline, idx, 'progress', mapped)
            cls._song_log(pipeline, idx, f'  🎞 Render {pct}%: {step_text}')
            cls._save_results(pipeline)

        RenderProcessTracker.set_progress('lyrics', project.id, 0, 'Starting...')

        render_res = LyricsEngineService.render_lyrics_video(
            audio_path=audio_path,
            background_image_path=bg_img_path if bg_img_path and os.path.exists(bg_img_path) else None,
            background_video_path=bg_vid_path if bg_vid_path and os.path.exists(bg_vid_path) else None,
            lyrics_data=lyrics_data,
            output_video_path=out_path,
            animation_style=animation_style,
            font_family=font_family,
            font_size=font_size,
            highlight_color=highlight_color,
            text_color=text_color,
            position_mode=position_mode,
            aspect_ratio=aspect_ratio,
            font_weight=font_weight,
            text_stroke_width=text_stroke_width,
            text_shadow_depth=text_shadow_depth,
            title=title,
            artist=artist,
            project_id=project.id,
        )

        project.output_video.name = f"studio/lyrics_output/{out_filename}"
        project.duration_seconds = render_res.get('duration', duration)
        project.render_status = LyricVideoProject.Status.COMPLETED
        project.save()

        cls._set_song_field(pipeline, idx, 'progress', 70)
        cls._save_results(pipeline)
        return out_path, project.pk

    @classmethod
    def _publish_video(cls, pipeline, idx, video_path, title, artist, custom_thumb_path, cfg):
        import time
        from apps.publishing.models import PublishingJob, YouTubeOAuthAccount
        from apps.publishing.services.uploader_service import YouTubeUploaderService

        # Use the per-song account_id if provided, else fall back to default
        account_id = cfg.get('account_id', '')
        if account_id:
            account = YouTubeOAuthAccount.objects.filter(pk=account_id, is_active=True).first()
        else:
            account = None
        if not account:
            account = (
                YouTubeOAuthAccount.objects.filter(is_default=True, is_active=True).first()
                or YouTubeOAuthAccount.objects.filter(is_active=True).first()
            )
        if not account:
            raise RuntimeError("No YouTube channel connected. Connect one in YouTube Publishing first.")

        privacy = cfg.get('privacy_status', 'private')
        description = cfg.get('description', '')
        tags_raw = cfg.get('tags', '')
        tags = [t.strip() for t in tags_raw.split(',') if t.strip()] if isinstance(tags_raw, str) else (tags_raw or [])
        category_id = cfg.get('category_id', '10')
        lyric_project_id = cls._get_song_field(pipeline, idx, 'lyric_project_id')

        # Build YouTube title: "Artist - Title (Official Lyric Video)"
        yt_title = f"{artist} - {title} (Official Lyric Video)" if artist else f"{title} (Official Lyric Video)"
        yt_title = yt_title[:100]

        if not description:
            description = f"🎵 {title}\n" + (f"Artist: {artist}\n\n" if artist else "\n") + \
                "Official Synced Lyric Video.\n\n#lyrics #lyricvideo #newmusic"

        job = PublishingJob.objects.create(
            account=account,
            title=yt_title,
            description=description[:5000],
            tags=tags or ['lyrics', 'lyricvideo', 'music'],
            category_id=category_id,
            privacy_status=privacy,
            video_file_path=video_path,
            thumbnail_path=custom_thumb_path or '',
            source_type=PublishingJob.SourceType.LYRIC_VIDEO,
            source_id=lyric_project_id,
            status=PublishingJob.Status.QUEUED,
        )
        cls._set_song_field(pipeline, idx, 'publishing_job_id', job.pk)
        cls._set_song_field(pipeline, idx, 'progress', 75)
        cls._save_results(pipeline)

        YouTubeUploaderService.start_upload_async(job.pk)

        # Poll upload progress (every 4s, up to 60min)
        for tick in range(900):
            time.sleep(4)
            job.refresh_from_db()

            upload_pct = job.progress_percent or 0
            mapped = int(75 + upload_pct * 0.24)
            cls._set_song_field(pipeline, idx, 'progress', min(99, mapped))
            cls._song_log(pipeline, idx, f'  ☁ Upload {upload_pct}%{" — " + job.error_message if job.error_message else ""}')
            cls._save_results(pipeline)

            if job.status == PublishingJob.Status.SUCCESS:
                cls._set_song_field(pipeline, idx, 'youtube_video_id', job.youtube_video_id)
                cls._set_song_field(pipeline, idx, 'youtube_url', job.youtube_url)
                cls._song_log(pipeline, idx, f'  ✓ Published! https://youtu.be/{job.youtube_video_id}')
                return
            elif job.status in [PublishingJob.Status.FAILED, PublishingJob.Status.CANCELLED]:
                raise RuntimeError(f"Upload failed: {job.error_message}")

        raise RuntimeError("Upload timed out after 60 minutes.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @classmethod
    def _parse_yt_title(cls, raw):
        """Split 'Artist - Song Title (Official Video)' → (title, artist)."""
        import re
        if not raw:
            return '', ''
        noise = re.compile(
            r'\s*[\(\[](official\s*(music\s*)?video|official\s*audio|lyrics?\s*video|'
            r'lyric\s*video|visualizer|hd|4k|remastered|full\s*song|audio|official|live|'
            r'feat\.?.*?|ft\.?.*?)[\)\]]\s*$', re.IGNORECASE
        )
        cleaned = noise.sub('', raw).strip()
        sep = re.compile(r'\s*(?:–|—|-|:|\|)\s*', re.UNICODE)
        parts = sep.split(cleaned, maxsplit=1)
        if len(parts) == 2:
            title, artist = parts[0].strip(), parts[1].strip()
        else:
            title, artist = cleaned, ''
        title = noise.sub('', title).strip()
        return title, artist

    @classmethod
    def _global_log(cls, pipeline, msg):
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        pipeline.log = (pipeline.log or '') + f"[{ts}] {msg}\n"
        pipeline.save(update_fields=['log', 'updated_at'])

    @classmethod
    def _song_log(cls, pipeline, idx, msg):
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        results = pipeline.results or []
        if idx < len(results):
            results[idx]['log'] = results[idx].get('log', '') + f"[{ts}] {msg}\n"
        pipeline.results = results
        pipeline.save(update_fields=['results', 'updated_at'])

    @classmethod
    def _set_song_status(cls, pipeline, idx, status, progress=None):
        results = pipeline.results or []
        if idx < len(results):
            results[idx]['status'] = status
            if progress is not None:
                results[idx]['progress'] = progress
        pipeline.results = results
        pipeline.save(update_fields=['results', 'updated_at'])

    @classmethod
    def _set_song_field(cls, pipeline, idx, field, value):
        results = pipeline.results or []
        if idx < len(results):
            results[idx][field] = value
        pipeline.results = results

    @classmethod
    def _get_song_field(cls, pipeline, idx, field):
        results = pipeline.results or []
        if idx < len(results):
            return results[idx].get(field)
        return None

    @classmethod
    def _save_results(cls, pipeline):
        pipeline.save(update_fields=['results', 'updated_at'])
