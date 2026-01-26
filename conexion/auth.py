# conexion/auth.py
import base64, datetime
import requests
from django.conf import settings
from django.utils import timezone
from .models import CredencialesSpotify
from .logs import log_event

def build_authorize_url(state: str = "sync") -> str:
    SCOPES = [
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-public",
        "playlist-modify-private",
        "user-read-email",
        "user-read-private",
    ]
    params = {
        "client_id": settings.SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "show_dialog": "true",
    }
    from urllib.parse import urlencode
    return f"{settings.SPOTIFY_AUTH_URL}?{urlencode(params)}"



def _basic_auth_header() -> str:
    raw = f"{settings.SPOTIFY_CLIENT_ID}:{settings.SPOTIFY_CLIENT_SECRET}".encode()
    return base64.b64encode(raw).decode()

def exchange_code_for_tokens(code: str) -> CredencialesSpotify:
    headers = {
        "Authorization": f"Basic {_basic_auth_header()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
    }
    resp = requests.post(settings.SPOTIFY_TOKEN_URL, headers=headers, data=data, timeout=12)
    if resp.status_code != 200:
        log_event("token_error", f"Exchange code error {resp.status_code}: {resp.text[:200]}")
        raise RuntimeError(f"Spotify token exchange error: {resp.status_code}")

    payload = resp.json()
    expires_in = int(payload.get("expires_in", 3600))
    expires_at = timezone.now() + datetime.timedelta(seconds=expires_in)

    # Crear o actualizar único registro
    obj, _ = CredencialesSpotify.objects.update_or_create(
        id=1,  # fuerza un único registro
        defaults={
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", ""),
            "token_type": payload.get("token_type", "Bearer"),
            "scope": payload.get("scope", ""),
            "expires_at": expires_at,
        }
    )
    log_event("token_ok", "Authorization Code intercambiado y guardado")
    return obj

def refresh_access_token(auth: CredencialesSpotify) -> CredencialesSpotify:
    headers = {
        "Authorization": f"Basic {_basic_auth_header()}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": auth.refresh_token,
    }
    resp = requests.post(settings.SPOTIFY_TOKEN_URL, headers=headers, data=data, timeout=12)
    if resp.status_code != 200:
        log_event("token_error", f"Refresh error {resp.status_code}: {resp.text[:200]}")
        raise RuntimeError(f"Spotify refresh error: {resp.status_code}")

    payload = resp.json()
    expires_in = int(payload.get("expires_in", 3600))
    auth.access_token = payload.get("access_token", auth.access_token)
    auth.expires_at = timezone.now() + datetime.timedelta(seconds=expires_in)
    auth.token_type = payload.get("token_type", auth.token_type)
    if "scope" in payload:
        auth.scope = payload["scope"]
    auth.save(update_fields=["access_token", "expires_at", "token_type", "scope", "actualizado"])
    log_event("token_ok", "Access token refrescado")
    return auth
