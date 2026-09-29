import os
import time
import logging
import threading
import requests
from django.utils import timezone
from django.db import connection

logger = logging.getLogger(__name__)

# In-memory tracking (shared with YouTube uploader pattern)
_ACTIVE_TIKTOK_UPLOADS = {}
_CANCEL_TIKTOK_FLAGS = {}
_TIKTOK_UPLOAD_LOCK = threading.Lock()

# TikTok Content Posting API base
_API_BASE = "https://open.tiktokapis.com/v2"


class TikTokUploaderService:
    """
    Handles TikTok video uploads via the Content Posting API v2.

    Flow (both Direct Post and Inbox Draft):
        1. POST /post/publish/video/init/  → get publish_id + upload_url
        2. PUT  <upload_url>               → upload the raw video bytes
        3. Poll POST /post/publish/status/fetch/  → wait for PUBLISH_COMPLETE

    Direct Post  (upload_engine = TIKTOK_DIRECT):
        - video goes directly to TikTok feed / inbox depending on privacy level
        - requires scope: video.publish

    Inbox Draft  (upload_engine = TIKTOK_INBOX):
        - video lands in the creator's TikTok app as an unsaved draft
        - requires scope: video.upload
    """

    @classmethod
    def start_upload_async(cls, job_id: int):
        with _TIKTOK_UPLOAD_LOCK:
            _CANCEL_TIKTOK_FLAGS[job_id] = False
            t = threading.Thread(target=cls._worker_wrapper, args=(job_id,), daemon=True)
            _ACTIVE_TIKTOK_UPLOADS[job_id] = t
            t.start()
            logger.info(f"[TikTok] Launched upload thread for Job #{job_id}")

    @classmethod
    def cancel_upload(cls, job_id: int):
        with _TIKTOK_UPLOAD_LOCK:
            _CANCEL_TIKTOK_FLAGS[job_id] = True
        try:
            from apps.publishing.models import PublishingJob
            job = PublishingJob.objects.get(pk=job_id)
            if job.status in [PublishingJob.Status.QUEUED, PublishingJob.Status.UPLOADING, PublishingJob.Status.PROCESSING]:
                job.status = PublishingJob.Status.CANCELLED
                job.error_message = "Upload cancelled by user."
                job.save(update_fields=["status", "error_message"])
        except Exception:
            pass

    @classmethod
    def _worker_wrapper(cls, job_id: int):
        try:
            cls.execute_upload_job(job_id)
        except Exception as e:
            logger.error(f"[TikTok] Unhandled error in upload worker for Job #{job_id}: {e}", exc_info=True)
        finally:
            with _TIKTOK_UPLOAD_LOCK:
                _ACTIVE_TIKTOK_UPLOADS.pop(job_id, None)
                _CANCEL_TIKTOK_FLAGS.pop(job_id, None)
            connection.close()

    @classmethod
    def execute_upload_job(cls, job_id: int):
        from apps.publishing.models import PublishingJob, TikTokAccount
        from apps.publishing.services.tiktok_oauth_service import TikTokOAuthService

        try:
            job = PublishingJob.objects.select_related("tiktok_account").get(pk=job_id)
        except PublishingJob.DoesNotExist:
            logger.error(f"[TikTok] Job #{job_id} not found.")
            return

        # Resolve account
        account = job.tiktok_account
        if not account:
            account = TikTokAccount.objects.filter(is_default=True, is_active=True).first() or \
                      TikTokAccount.objects.filter(is_active=True).first()
            if not account:
                cls._fail(job, "No active TikTok account connected. Please connect an account first.")
                return
            job.tiktok_account = account
            job.save(update_fields=["tiktok_account"])

        # Verify video file
        if not job.video_file_path or not os.path.exists(job.video_file_path):
            cls._fail(job, f"Video file not found: {job.video_file_path}")
            return

        file_size = os.path.getsize(job.video_file_path)
        if file_size > 4 * 1024 * 1024 * 1024:  # 4 GB TikTok limit
            cls._fail(job, f"Video file exceeds TikTok 4 GB limit ({file_size / (1024**3):.1f} GB).")
            return

        # Mark as uploading
        job.status = PublishingJob.Status.UPLOADING
        job.total_bytes = file_size
        job.progress_percent = 0
        job.started_at = timezone.now()
        job.error_message = ""
        job.save(update_fields=["status", "total_bytes", "progress_percent", "started_at", "error_message"])

        try:
            access_token = TikTokOAuthService.get_valid_token(account)
            is_inbox = (job.upload_engine == PublishingJob.UploadEngine.TIKTOK_INBOX)

            publish_id, upload_url = cls._init_upload(job, access_token, file_size, is_inbox)
            job.tiktok_publish_id = publish_id
            job.save(update_fields=["tiktok_publish_id"])

            cls._upload_file(job, upload_url, file_size)

            if _CANCEL_TIKTOK_FLAGS.get(job_id, False):
                return  # cancelled during upload

            # Poll for completion
            job.status = PublishingJob.Status.PROCESSING
            job.progress_percent = 99
            job.save(update_fields=["status", "progress_percent"])

            video_id = cls._poll_status(job, access_token, publish_id)

            job.tiktok_video_id = video_id or ""
            job.tiktok_share_url = f"https://www.tiktok.com/@{account.display_name}/video/{video_id}" if video_id else ""
            job.status = PublishingJob.Status.SUCCESS
            job.progress_percent = 100
            job.bytes_uploaded = file_size
            job.completed_at = timezone.now()
            job.save(update_fields=[
                "tiktok_video_id", "tiktok_share_url", "status",
                "progress_percent", "bytes_uploaded", "completed_at", "error_message",
            ])
            logger.info(f"[TikTok] Job #{job.id} completed. publish_id={publish_id} video_id={video_id}")

        except Exception as e:
            logger.error(f"[TikTok] Job #{job.id} failed: {e}", exc_info=True)
            cls._fail(job, str(e))

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _init_upload(cls, job, access_token: str, file_size: int, is_inbox: bool) -> tuple[str, str]:
        """
        Calls /post/publish/video/init/ and returns (publish_id, upload_url).
        """
        from apps.publishing.models import PublishingJob

        endpoint = f"{_API_BASE}/post/publish/inbox/video/init/" if is_inbox else f"{_API_BASE}/post/publish/video/init/"

        # Build caption: title + hashtags from tags
        tags_list = job.tags if isinstance(job.tags, list) else []
        hashtags = " ".join(f"#{t.lstrip('#')}" for t in tags_list[:20] if t.strip())
        caption_parts = [job.title or ""]
        if job.description:
            caption_parts.append(job.description)
        if hashtags:
            caption_parts.append(hashtags)
        caption = "\n".join(p for p in caption_parts if p).strip()[:2200]

        post_info = {"title": caption, "privacy_level": job.tiktok_privacy_level or "PUBLIC_TO_EVERYONE"}
        if not is_inbox:
            post_info["disable_duet"] = bool(job.tiktok_disable_duet)
            post_info["disable_comment"] = bool(job.tiktok_disable_comment)
            post_info["disable_stitch"] = bool(job.tiktok_disable_stitch)
            post_info["brand_content_toggle"] = bool(job.tiktok_branded_content)

        body = {
            "post_info": post_info,
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,  # single-chunk upload (≤ 4 GB)
                "total_chunk_count": 1,
            },
        }
        if not is_inbox:
            body["post_mode"] = "DIRECT_POST"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        resp = requests.post(endpoint, json=body, headers=headers, timeout=30)
        data = resp.json()
        err_code = data.get("error", {}).get("code", "ok")
        if resp.status_code != 200 or err_code != "ok":
            msg = data.get("error", {}).get("message", resp.text)
            raise RuntimeError(f"TikTok init upload failed [{err_code}]: {msg}")

        upload_data = data.get("data", {})
        publish_id = upload_data.get("publish_id", "")
        upload_url = upload_data.get("upload_url", "")
        if not publish_id or not upload_url:
            raise RuntimeError(f"TikTok init response missing publish_id/upload_url: {data}")

        logger.info(f"[TikTok] Init OK. publish_id={publish_id}")
        return publish_id, upload_url

    @classmethod
    def _upload_file(cls, job, upload_url: str, file_size: int):
        """
        PUTs the video file as a single chunk to TikTok's pre-signed S3 URL.
        Updates job.bytes_uploaded / progress_percent during the transfer.
        """
        job_id = job.id
        chunk_size = 4 * 1024 * 1024  # 4 MB read buffer
        bytes_sent = 0
        last_save = time.time()

        headers = {
            "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
            "Content-Type": "video/mp4",
            "Content-Length": str(file_size),
        }

        def _gen():
            nonlocal bytes_sent
            with open(job.video_file_path, "rb") as f:
                while True:
                    if _CANCEL_TIKTOK_FLAGS.get(job_id, False):
                        return
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    bytes_sent += len(chunk)
                    yield chunk

                    # Throttle DB writes
                    nonlocal last_save
                    now = time.time()
                    if now - last_save > 1.5:
                        pct = min(98, int(bytes_sent / file_size * 100))
                        job.progress_percent = pct
                        job.bytes_uploaded = bytes_sent
                        job.save(update_fields=["progress_percent", "bytes_uploaded"])
                        last_save = now

        resp = requests.put(upload_url, data=_gen(), headers=headers, timeout=3600)
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"TikTok S3 upload failed [{resp.status_code}]: {resp.text[:300]}")

        logger.info(f"[TikTok] File upload complete for Job #{job.id}. {bytes_sent} bytes sent.")

    @classmethod
    def _poll_status(cls, job, access_token: str, publish_id: str, max_wait: int = 300) -> str:
        """
        Polls /post/publish/status/fetch/ until PUBLISH_COMPLETE or timeout.
        Returns the video_id on success.
        """
        endpoint = f"{_API_BASE}/post/publish/status/fetch/"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }
        deadline = time.time() + max_wait
        poll_interval = 5

        while time.time() < deadline:
            if _CANCEL_TIKTOK_FLAGS.get(job.id, False):
                return ""

            resp = requests.post(endpoint, json={"publish_id": publish_id}, headers=headers, timeout=15)
            data = resp.json()
            status_data = data.get("data", {})
            status = status_data.get("status", "")
            fail_reason = status_data.get("fail_reason", "")

            logger.debug(f"[TikTok] Job #{job.id} publish status: {status}")

            if status == "PUBLISH_COMPLETE":
                return status_data.get("video_id", "")
            if status in ("FAILED", "PUBLISH_FAILED"):
                raise RuntimeError(f"TikTok publish failed: {fail_reason or status}")
            if status == "INBOX_SAVED":
                # Inbox draft mode — success without a live video_id
                return status_data.get("video_id", "")

            time.sleep(poll_interval)

        raise RuntimeError(f"TikTok publish status polling timed out after {max_wait}s (publish_id={publish_id})")

    @classmethod
    def _fail(cls, job, message: str):
        from apps.publishing.models import PublishingJob
        job.status = PublishingJob.Status.FAILED
        job.error_message = message
        job.save(update_fields=["status", "error_message"])
        logger.error(f"[TikTok] Job #{job.id} failed: {message}")
