import os
import time
import json
import logging
import threading
from django.utils import timezone
from django.db import transaction
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from apps.publishing.models import PublishingJob, YouTubeOAuthAccount
from apps.youtube.models import YouTubeApiUsage

logger = logging.getLogger(__name__)

# In-memory tracking of running upload threads and cancellation flags
_ACTIVE_UPLOADS = {}
_CANCEL_FLAGS = {}
_UPLOAD_LOCK = threading.Lock()

class YouTubeUploaderService:
    """
    Handles chunked resumable video uploads, metadata injection, custom thumbnail uploads,
    and background worker task dispatch for YouTube Data API v3.
    """

    @classmethod
    def get_authenticated_service(cls, account):
        """
        Builds an authorized YouTube Resource object using the account's OAuth credentials.
        """
        creds = account.get_credentials()
        return build('youtube', 'v3', credentials=creds, cache_discovery=False)

    @staticmethod
    def _extract_http_error_text(exc):
        """Normalizes Google API errors into a readable string for fallback decisions."""
        payload = ''
        if hasattr(exc, 'content'):
            raw = exc.content
            if isinstance(raw, (bytes, bytearray)):
                payload = raw.decode('utf-8', errors='replace')
            else:
                payload = str(raw)

        resp = getattr(exc, 'resp', None)
        status = getattr(resp, 'status', None)
        reason = getattr(resp, 'reason', '') or ''
        reason_text = f' {reason}' if reason else ''
        return status, f"{reason_text} {payload}".strip()

    @classmethod
    def _should_retry_via_browser(cls, exc):
        """Identifies quota/network errors where the browser automation fallback is safer."""
        if isinstance(exc, HttpError):
            status, message = cls._extract_http_error_text(exc)
            combined = (message or str(exc)).lower()
            if status in [400, 403, 429, 500, 502, 503, 504]:
                if any(token in combined for token in [
                    'quota', 'daily limit', 'exceeded the number of videos', 'rate limit',
                    'too many requests', 'temporarily unavailable', 'service unavailable',
                    'backend error', 'unable to find the server', 'not resolved', 'network',
                    'connection', 'timed out', 'timeout', 'ssl', 'gaierror'
                ]):
                    return True
            return False

        message = str(exc).lower()
        return any(token in message for token in [
            'unable to find the server', 'youtube.googleapis.com', 'not resolved',
            'network is unreachable', 'connection aborted', 'timed out', 'timeout',
            'quota', 'exceeded the number of videos', 'daily limit', 'too many requests',
            'rate limit', 'temporarily unavailable', 'service unavailable'
        ])

    @classmethod
    def _fallback_to_browser_upload(cls, job):
        """Switch a failing API upload to the browser automation engine."""
        if job.upload_engine == PublishingJob.UploadEngine.BROWSER_AUTOMATION:
            return

        job.upload_engine = PublishingJob.UploadEngine.BROWSER_AUTOMATION
        job.status = PublishingJob.Status.QUEUED
        job.error_message = (
            'Google API upload is unavailable or quota-limited. Retrying via browser automation '
            'instead of the direct API connection.'
        )
        job.save(update_fields=['upload_engine', 'status', 'error_message'])

        from .playwright_uploader import PlaywrightStudioUploader
        logger.warning(f"Falling back to browser automation for Job #{job.id} after API error.")
        PlaywrightStudioUploader.execute_browser_upload(job.id)

    @classmethod
    def start_upload_async(cls, job_id):
        """
        Launches the upload job in a daemon background thread.
        """
        with _UPLOAD_LOCK:
            _CANCEL_FLAGS[job_id] = False
            t = threading.Thread(target=cls._upload_worker_wrapper, args=(job_id,), daemon=True)
            _ACTIVE_UPLOADS[job_id] = t
            t.start()
            logger.info(f"Launched background upload thread for Job #{job_id}")

    @classmethod
    def cancel_upload(cls, job_id):
        """
        Signals the worker thread to cancel and updates job status.
        """
        with _UPLOAD_LOCK:
            _CANCEL_FLAGS[job_id] = True

        try:
            job = PublishingJob.objects.get(pk=job_id)
            if job.status in [PublishingJob.Status.QUEUED, PublishingJob.Status.UPLOADING, PublishingJob.Status.PROCESSING]:
                job.status = PublishingJob.Status.CANCELLED
                job.error_message = "Upload cancelled by user."
                job.save(update_fields=['status', 'error_message'])
                return True
        except PublishingJob.DoesNotExist:
            pass
        return False

    @classmethod
    def _upload_worker_wrapper(cls, job_id):
        """
        Wrapper to execute upload safely inside Django thread context.
        """
        from django.db import connection
        try:
            cls.execute_upload_job(job_id)
        except Exception as e:
            logger.error(f"Unhandled error in upload worker for Job #{job_id}: {e}", exc_info=True)
        finally:
            with _UPLOAD_LOCK:
                _ACTIVE_UPLOADS.pop(job_id, None)
                _CANCEL_FLAGS.pop(job_id, None)
            connection.close()

    @classmethod
    def execute_upload_job(cls, job_id):
        """
        Main execution router for uploading a video file via selected engine.
        """
        try:
            job = PublishingJob.objects.select_related('account').get(pk=job_id)
        except PublishingJob.DoesNotExist:
            logger.error(f"Cannot execute upload: Job #{job_id} not found.")
            return

        # Route to Playwright Browser Automation if selected
        if job.upload_engine == PublishingJob.UploadEngine.BROWSER_AUTOMATION:
            from .playwright_uploader import PlaywrightStudioUploader
            PlaywrightStudioUploader.execute_browser_upload(job_id)
            return

        # Route to 1-Click Studio Assistant if selected
        if job.upload_engine == PublishingJob.UploadEngine.STUDIO_DISPATCHER:
            job.status = PublishingJob.Status.SUCCESS
            job.progress_percent = 100
            job.completed_at = timezone.now()
            job.save(update_fields=['status', 'progress_percent', 'completed_at'])
            logger.info(f"Job #{job.id} dispatched to 1-Click Studio Assistant.")
            return

        # Default / Official YouTube Data API v3 upload execution
        cls._execute_upload_job_api_v3(job)

    @classmethod
    def _execute_upload_job_api_v3(cls, job):
        """
        Official YouTube Data API v3 chunked resumable upload implementation.
        """
        job_id = job.id
        # Check target account
        account = job.account
        if not account:
            account = YouTubeOAuthAccount.objects.filter(is_default=True, is_active=True).first()
            if not account:
                account = YouTubeOAuthAccount.objects.filter(is_active=True).first()

            if not account:
                job.status = PublishingJob.Status.FAILED
                job.error_message = "No active YouTube OAuth account connected. Please connect a channel first or switch to Browser Automation mode."
                job.save(update_fields=['status', 'error_message'])
                return

            job.account = account
            job.save(update_fields=['account'])

        # Verify video file exists
        if not job.video_file_path or not os.path.exists(job.video_file_path):
            job.status = PublishingJob.Status.FAILED
            job.error_message = f"Video file not found at path: {job.video_file_path}"
            job.save(update_fields=['status', 'error_message'])
            return

        file_size = os.path.getsize(job.video_file_path)
        job.total_bytes = file_size
        job.status = PublishingJob.Status.UPLOADING
        job.progress_percent = 0
        job.started_at = timezone.now()
        job.error_message = ''
        job.save(update_fields=['total_bytes', 'status', 'progress_percent', 'started_at', 'error_message'])

        try:
            youtube = cls.get_authenticated_service(account)

            # Construct YouTube Video Resource Metadata
            tags_list = job.tags if isinstance(job.tags, list) else [t.strip() for t in str(job.tags).split(',') if t.strip()]
            
            # Formulate description (ensuring under 5000 chars)
            clean_desc = (job.description or '')[:5000]

            # Formulate title (ensuring under 100 chars)
            clean_title = (job.title or 'Untitled Video')[:100]

            # Privacy status and scheduling
            api_privacy_status = 'private' if job.privacy_status == PublishingJob.PrivacyStatus.SCHEDULED else job.privacy_status

            snippet = {
                'title': clean_title,
                'description': clean_desc,
                'tags': tags_list[:500], # max 500 tags
                'categoryId': str(job.category_id or '10'),
            }

            status_dict = {
                'privacyStatus': api_privacy_status,
                'selfDeclaredMadeForKids': bool(job.made_for_kids),
                'embeddable': bool(job.embeddable)
            }

            if job.privacy_status == PublishingJob.PrivacyStatus.SCHEDULED and job.publish_at:
                status_dict['publishAt'] = job.publish_at.strftime('%Y-%m-%dT%H:%M:%S.000Z')

            body = {
                'snippet': snippet,
                'status': status_dict
            }

            # 10 MB chunk size for smooth progress tracking and reliability
            chunk_size = 10 * 1024 * 1024
            media = MediaFileUpload(
                job.video_file_path,
                chunksize=chunk_size,
                resumable=True,
                mimetype='video/mp4'
            )

            request = youtube.videos().insert(
                part='snippet,status',
                body=body,
                media_body=media
            )

            logger.info(f"Starting chunked upload for Job #{job.id} ({clean_title}) - Total Size: {file_size / (1024*1024):.1f} MB")

            response = None
            last_save_time = time.time()

            while response is None:
                # Check for cancellation
                if _CANCEL_FLAGS.get(job_id, False):
                    logger.info(f"Upload for Job #{job_id} cancelled by user flag.")
                    job.status = PublishingJob.Status.CANCELLED
                    job.error_message = "Upload cancelled by user."
                    job.save(update_fields=['status', 'error_message'])
                    return

                status, response = request.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    job.progress_percent = min(99, progress)
                    job.bytes_uploaded = int(status.resumable_progress)
                    
                    # Update database at reasonable intervals to prevent excessive DB writes
                    now = time.time()
                    if now - last_save_time > 1.5 or progress >= 98:
                        job.save(update_fields=['progress_percent', 'bytes_uploaded'])
                        last_save_time = now

            # Video successfully uploaded!
            video_id = response.get('id', '')
            if not video_id:
                raise RuntimeError(f"YouTube did not return a valid video ID: {response}")

            job.youtube_video_id = video_id
            job.youtube_url = f"https://youtu.be/{video_id}"
            job.status = PublishingJob.Status.PROCESSING
            job.progress_percent = 99
            job.save(update_fields=['youtube_video_id', 'youtube_url', 'status', 'progress_percent'])

            # Record standard video upload quota (1600 units)
            try:
                YouTubeApiUsage.record_usage(units=1600)
            except Exception as qe:
                logger.warning(f"Could not record API quota for upload: {qe}")

            # Upload custom thumbnail if specified
            if job.thumbnail_path and os.path.exists(job.thumbnail_path):
                try:
                    logger.info(f"Uploading custom thumbnail for video {video_id}...")
                    thumb_media = MediaFileUpload(job.thumbnail_path, mimetype='image/jpeg', resumable=False)
                    youtube.thumbnails().set(
                        videoId=video_id,
                        media_body=thumb_media
                    ).execute()
                    
                    # Record thumbnail quota (50 units)
                    try:
                        YouTubeApiUsage.record_usage(units=50)
                    except Exception:
                        pass
                    logger.info(f"Custom thumbnail attached successfully for {video_id}.")
                except Exception as te:
                    logger.warning(f"Failed to attach thumbnail to video {video_id}: {te}")
                    # Don't fail the whole job if only thumbnail fails
                    job.error_message = f"Video uploaded successfully, but custom thumbnail failed: {te}"

            # Mark complete
            job.status = PublishingJob.Status.SUCCESS
            job.progress_percent = 100
            job.bytes_uploaded = file_size
            job.completed_at = timezone.now()
            job.save(update_fields=['status', 'progress_percent', 'bytes_uploaded', 'completed_at', 'error_message'])
            logger.info(f"Job #{job.id} completed successfully! Video ID: {video_id}")

        except HttpError as he:
            error_details = he.content.decode('utf-8', errors='replace') if hasattr(he, 'content') else str(he)
            logger.error(f"Google API HttpError for Job #{job.id}: {error_details}")

            if cls._should_retry_via_browser(he):
                cls._fallback_to_browser_upload(job)
                return

            job.status = PublishingJob.Status.FAILED
            job.error_message = f"YouTube API Error ({he.resp.status}): {he._get_reason()}"
            job.save(update_fields=['status', 'error_message'])

        except Exception as e:
            logger.error(f"Unexpected error executing Job #{job.id}: {e}", exc_info=True)
            if cls._should_retry_via_browser(e):
                cls._fallback_to_browser_upload(job)
                return

            job.status = PublishingJob.Status.FAILED
            job.error_message = str(e)
            job.save(update_fields=['status', 'error_message'])
