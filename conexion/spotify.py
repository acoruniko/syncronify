# conexion/spotify.py
import base64
import time
import requests
from django.conf import settings
from .logs import log_event

_token_cache = {"access_token": None, "expires_at": 0}

def _encode_basic(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return base64.b64encode(raw).decode()


def get_token(force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh and _token_cache["access_token"] and _token_cache["expires_at"] > now + 30:
        return _token_cache["access_token"]

    headers = {
        "Authorization": f"Basic {_encode_basic(settings.SPOTIFY_CLIENT_ID, settings.SPOTIFY_CLIENT_SECRET)}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}
    resp = requests.post(settings.SPOTIFY_TOKEN_URL, headers=headers, data=data, timeout=10)

    if resp.status_code != 200:
        log_event("token_error", f"Error {resp.status_code}: {resp.text[:200]}")
        raise RuntimeError(f"Spotify token error: {resp.status_code}")

    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + int(payload.get("expires_in", 3600))
    log_event("token_ok", "Access token obtenido y cacheado")
    return _token_cache["access_token"]