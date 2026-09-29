import logging
import urllib.parse
import requests
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from apps.publishing.models import TikTokAccount

logger = logging.getLogger(__name__)


class TikTokOAuthService:
    """
    Handles TikTok OAuth 2.0 (Login Kit) authorization flow and token management.

    Required env vars:
        TIKTOK_CLIENT_KEY      — your app's Client Key (shown as "Client Key" in TikTok dev portal)
        TIKTOK_CLIENT_SECRET   — your app's Client Secret

    Required TikTok app products:
        - Login Kit
        - Content Posting API  (for video.publish / video.upload scopes)
    """

    AUTH_URI = "https://www.tiktok.com/v2/auth/authorize/"
    TOKEN_URI = "https://open.tiktokapis.com/v2/oauth/token/"
    REVOKE_URI = "https://open.tiktokapis.com/v2/oauth/revoke/"
    USER_INFO_URI = "https://open.tiktokapis.com/v2/user/info/"

    # video.publish  → Direct Post (goes live / to inbox depending on privacy)
    # video.upload   → Upload-only (creates a draft in the app)
    SCOPES = ["user.info.basic", "user.info.stats", "video.publish", "video.upload"]

    @classmethod
    def get_client_credentials(cls):
        client_key = getattr(settings, "TIKTOK_CLIENT_KEY", "").strip()
        client_secret = getattr(settings, "TIKTOK_CLIENT_SECRET", "").strip()
        return client_key, client_secret

    @classmethod
    def is_configured(cls):
        key, secret = cls.get_client_credentials()
        return bool(key and secret)

    @classmethod
    def get_authorization_url(cls, redirect_uri, state=None):
        """Builds the TikTok OAuth authorization URL."""
        client_key, _ = cls.get_client_credentials()
        if not client_key:
            raise ValueError("TIKTOK_CLIENT_KEY is not configured.")

        params = {
            "client_key": client_key,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(cls.SCOPES),
        }
        if state:
            params["state"] = state

        return f"{cls.AUTH_URI}?{urllib.parse.urlencode(params)}"

    @classmethod
    def exchange_code_for_tokens(cls, code, redirect_uri):
        """Exchanges an authorization code for access + refresh tokens."""
        client_key, client_secret = cls.get_client_credentials()
        if not client_key or not client_secret:
            raise ValueError("TikTok OAuth credentials are not configured.")

        payload = {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        resp = requests.post(cls.TOKEN_URI, data=payload, timeout=15)
        data = resp.json()
        if resp.status_code != 200 or data.get("error"):
            err = data.get("error_description") or data.get("error") or resp.text
            raise RuntimeError(f"TikTok token exchange failed: {err}")

        return data  # contains access_token, refresh_token, expires_in, open_id, scope

    @classmethod
    def refresh_access_token(cls, account: TikTokAccount) -> TikTokAccount:
        """Refreshes an expired access token and saves it back to the account."""
        client_key, client_secret = cls.get_client_credentials()
        payload = {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": account.refresh_token,
        }
        resp = requests.post(cls.TOKEN_URI, data=payload, timeout=15)
        data = resp.json()
        if resp.status_code != 200 or data.get("error"):
            err = data.get("error_description") or data.get("error") or resp.text
            raise RuntimeError(f"TikTok token refresh failed: {err}")

        expires_in = int(data.get("expires_in", 86400))
        account.access_token = data["access_token"]
        account.refresh_token = data.get("refresh_token", account.refresh_token)
        account.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
        account.save(update_fields=["access_token", "refresh_token", "token_expires_at", "updated_at"])
        logger.info(f"Refreshed TikTok token for @{account.display_name}")
        return account

    @classmethod
    def get_valid_token(cls, account: TikTokAccount) -> str:
        """Returns a valid access token, refreshing first if it is expired."""
        if account.is_token_expired:
            account = cls.refresh_access_token(account)
        return account.access_token

    @classmethod
    def fetch_user_info(cls, access_token: str, open_id: str) -> dict:
        """Fetches basic profile + stats from TikTok user info endpoint."""
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {
            "fields": "open_id,union_id,avatar_url,display_name,bio_description,profile_deep_link,follower_count,following_count,video_count",
        }
        resp = requests.get(cls.USER_INFO_URI, headers=headers, params=params, timeout=15)
        data = resp.json()
        if resp.status_code != 200 or data.get("error", {}).get("code", "ok") != "ok":
            err = data.get("error", {}).get("message", resp.text)
            raise RuntimeError(f"TikTok user info fetch failed: {err}")
        return data.get("data", {}).get("user", {})

    @classmethod
    def register_or_update_account(cls, token_data: dict) -> TikTokAccount:
        """
        Takes raw token data from code exchange, fetches user profile,
        then creates or updates the TikTokAccount record.
        """
        access_token = token_data["access_token"]
        open_id = token_data["open_id"]
        expires_in = int(token_data.get("expires_in", 86400))

        user_info = cls.fetch_user_info(access_token, open_id)

        account = TikTokAccount.objects.filter(open_id=open_id).first()
        token_expires_at = timezone.now() + timedelta(seconds=expires_in)

        fields = dict(
            display_name=user_info.get("display_name", "TikTok User"),
            union_id=user_info.get("union_id", ""),
            avatar_url=user_info.get("avatar_url", ""),
            bio_description=user_info.get("bio_description", ""),
            profile_url=user_info.get("profile_deep_link", ""),
            follower_count=int(user_info.get("follower_count", 0)),
            following_count=int(user_info.get("following_count", 0)),
            video_count=int(user_info.get("video_count", 0)),
            access_token=access_token,
            refresh_token=token_data.get("refresh_token", ""),
            token_expires_at=token_expires_at,
            token_scope=token_data.get("scope", ""),
            is_active=True,
        )

        if account:
            for k, v in fields.items():
                setattr(account, k, v)
            account.save()
            logger.info(f"Updated TikTok account: @{account.display_name} ({open_id})")
        else:
            is_first = not TikTokAccount.objects.exists()
            account = TikTokAccount.objects.create(
                open_id=open_id,
                is_default=is_first,
                **fields,
            )
            logger.info(f"Registered new TikTok account: @{account.display_name} ({open_id})")

        return account

    @classmethod
    def disconnect_account(cls, account_id: int) -> str:
        """Revokes the token and deletes the account record."""
        account = TikTokAccount.objects.get(pk=account_id)
        client_key, client_secret = cls.get_client_credentials()
        try:
            requests.post(
                cls.REVOKE_URI,
                data={"client_key": client_key, "client_secret": client_secret, "token": account.access_token},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"Could not revoke TikTok token: {e}")

        name = account.display_name
        account.delete()

        remaining = TikTokAccount.objects.first()
        if remaining and not TikTokAccount.objects.filter(is_default=True).exists():
            remaining.is_default = True
            remaining.save()

        return name
