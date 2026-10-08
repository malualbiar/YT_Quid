import os
import time
import logging
from django.conf import settings
from django.utils import timezone
from apps.publishing.models import PublishingJob

logger = logging.getLogger(__name__)

class PlaywrightStudioUploader:
    """
    Automates YouTube Studio web uploads using Playwright.
    Bypasses Google API quota restrictions completely (0 quota cost).
    """

    @classmethod
    def get_user_data_dir(cls):
        """Returns persistent browser profile directory to retain YouTube Studio login session."""
        base_dir = getattr(settings, 'MEDIA_ROOT', os.path.join(settings.BASE_DIR, 'media'))
        profile_dir = os.path.join(base_dir, 'publishing', 'browser_profile')
        os.makedirs(profile_dir, exist_ok=True)
        return profile_dir

    @classmethod
    def execute_browser_upload(cls, job_id, headless=True):
        """
        Executes an automated YouTube Studio upload session.
        """
        from playwright.sync_api import sync_playwright

        try:
            job = PublishingJob.objects.get(pk=job_id)
        except PublishingJob.DoesNotExist:
            logger.error(f"Playwright upload failed: Job #{job_id} not found.")
            return

        if not job.video_file_path or not os.path.exists(job.video_file_path):
            job.status = PublishingJob.Status.FAILED
            job.error_message = f"Video file not found at: {job.video_file_path}"
            job.save(update_fields=['status', 'error_message'])
            return

        job.status = PublishingJob.Status.UPLOADING
        job.progress_percent = 10
        job.started_at = timezone.now()
        job.save(update_fields=['status', 'progress_percent', 'started_at'])

        user_data_dir = cls.get_user_data_dir()

        try:
            with sync_playwright() as p:
                logger.info(f"Launching Playwright browser context for Job #{job.id}...")

                # Pre-flight: verify Chromium is installed
                try:
                    browser_exe = p.chromium.executable_path
                    if not os.path.exists(browser_exe):
                        raise FileNotFoundError(browser_exe)
                except Exception:
                    raise RuntimeError(
                        "Chromium browser not installed. Fix: run  python -m playwright install chromium"
                    )

                # Launch persistent browser context (retains Google Studio login)
                context = p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=headless,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-setuid-sandbox'
                    ],
                    viewport={'width': 1280, 'height': 800}
                )

                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(45000)

                logger.info("Navigating to YouTube Studio...")
                page.goto('https://studio.youtube.com', wait_until='domcontentloaded')
                page.wait_for_timeout(3000)

                # Check if login is required
                if "accounts.google.com" in page.url or "signin" in page.url.lower():
                    raise RuntimeError(
                        "YouTube Studio requires login. Please log in once via the visible browser helper or use YouTube API v3 mode."
                    )

                job.progress_percent = 25
                job.save(update_fields=['progress_percent'])

                # Open Create -> Upload Videos
                logger.info("Opening Upload dialog...")
                try:
                    create_btn = page.locator('#create-icon, button:has-text("Create")').first
                    if create_btn.is_visible():
                        create_btn.click()
                        page.wait_for_timeout(1000)
                        page.locator('text="Upload videos", #text-item-0').first.click()
                    else:
                        page.goto('https://studio.youtube.com/channel/videos/upload?d=ud')
                except Exception:
                    page.goto('https://studio.youtube.com/channel/videos/upload?d=ud')

                page.wait_for_timeout(2000)

                # Upload video file input
                file_input = page.locator('input[type="file"]').first
                if not file_input.is_visible(timeout=10000):
                    file_input = page.locator('input[type="file"]')

                logger.info(f"Uploading file {job.video_file_path}...")
                file_input.set_input_files(job.video_file_path)

                job.progress_percent = 50
                job.save(update_fields=['progress_percent'])

                # Wait for title input to appear
                page.wait_for_selector('#textbox[aria-label*="title"], #textbox', timeout=30000)
                page.wait_for_timeout(2000)

                # Set Title
                clean_title = (job.title or 'My Video')[:100]
                title_box = page.locator('#textbox[aria-label*="title"], #title-textarea #textbox').first
                if title_box.is_visible():
                    title_box.fill('')
                    title_box.type(clean_title, delay=20)

                # Set Description
                if job.description:
                    desc_box = page.locator('#description-textarea #textbox, #textbox[aria-label*="description"]').first
                    if desc_box.is_visible():
                        desc_box.fill('')
                        desc_box.type(job.description[:5000], delay=10)

                # Set Made for Kids to NO
                try:
                    not_for_kids = page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"], [name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').first
                    if not_for_kids.is_visible():
                        not_for_kids.click()
                except Exception:
                    pass

                # Upload custom thumbnail if present
                if job.thumbnail_path and os.path.exists(job.thumbnail_path):
                    try:
                        thumb_input = page.locator('#file-loader, input[accept="image/jpeg,image/png"]').first
                        if thumb_input.is_visible():
                            thumb_input.set_input_files(job.thumbnail_path)
                            page.wait_for_timeout(1500)
                    except Exception as te:
                        logger.warning(f"Could not set thumbnail in browser: {te}")

                job.progress_percent = 75
                job.save(update_fields=['progress_percent'])

                # Extract Video link / ID if visible
                try:
                    video_link_elem = page.locator('a.ytcp-video-info, a.ytcp-video-metadata-info').first
                    if video_link_elem.is_visible():
                        href = video_link_elem.get_attribute('href')
                        if href and 'youtu.be/' in href:
                            vid = href.split('youtu.be/')[-1].split('?')[0]
                            job.youtube_video_id = vid
                            job.youtube_url = f"https://youtu.be/{vid}"
                except Exception:
                    pass

                # Step through Next buttons to Visibility tab
                for _ in range(3):
                    try:
                        next_btn = page.locator('#next-button').first
                        if next_btn.is_visible() and next_btn.is_enabled():
                            next_btn.click()
                            page.wait_for_timeout(1000)
                    except Exception:
                        break

                # Set Visibility
                try:
                    if job.privacy_status == PublishingJob.PrivacyStatus.PUBLIC:
                        page.locator('tp-yt-paper-radio-button[name="PUBLIC"]').first.click()
                    elif job.privacy_status == PublishingJob.PrivacyStatus.UNLISTED:
                        page.locator('tp-yt-paper-radio-button[name="UNLISTED"]').first.click()
                    else:
                        # Private / Draft (Default)
                        page.locator('tp-yt-paper-radio-button[name="PRIVATE"]').first.click()
                except Exception as ve:
                    logger.warning(f"Could not click visibility option: {ve}")

                # Click Done / Save
                try:
                    done_btn = page.locator('#done-button').first
                    if done_btn.is_visible() and done_btn.is_enabled():
                        done_btn.click()
                        page.wait_for_timeout(3000)
                except Exception as de:
                    logger.warning(f"Could not click done button: {de}")

                context.close()

                job.status = PublishingJob.Status.SUCCESS
                job.progress_percent = 100
                job.completed_at = timezone.now()
                job.save(update_fields=['status', 'progress_percent', 'completed_at', 'youtube_video_id', 'youtube_url'])
                logger.info(f"Browser upload for Job #{job.id} completed successfully!")

        except Exception as e:
            logger.error(f"Playwright upload failed for Job #{job.id}: {e}", exc_info=True)
            job.status = PublishingJob.Status.FAILED
            job.error_message = f"Browser Automation Error: {str(e)}"
            job.save(update_fields=['status', 'error_message'])
