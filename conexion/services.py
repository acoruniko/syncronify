# conexion/services.py
import requests
from django.utils import timezone
from .models import CredencialesSpotify
from .auth import refresh_access_token
from .logs import status_ok, status_err, log_event 
from django.shortcuts import redirect
from django.contrib import messages
from datetime import timedelta

def get_spotify_token() -> str:
    cred = CredencialesSpotify.objects.first()
    if not cred:
        log_event("auth_error", "No hay credenciales de Spotify almacenadas")
        raise RuntimeError("No hay credenciales de Spotify almacenadas")

    # Si expiró, refrescar
    if cred.expires_at <= timezone.now():
        cred = refresh_access_token(cred)
        log_event("token_ok", "Access token refrescado automáticamente en get_spotify_token")

    # Validar scopes
    if "playlist-read-private" not in cred.scope:
        log_event("auth_error", "Token sin permisos de playlists")
        raise RuntimeError("Token sin permisos de playlists. Reautoriza en Spotify.")

    return cred.access_token


def obtener_perfil_spotify():
    """
    Llama al endpoint /me de Spotify para obtener perfil del usuario
    asociado a la cuenta maestra.
    """
    try:
        token = get_spotify_token()
    except Exception as e:
        log_event("service_error", f"Error al obtener token: {str(e)}")
        return status_err("perfil_spotify", str(e))

    try:
        resp = requests.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=12
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log_event("service_error", f"Error al llamar /me: {str(e)}")
        return status_err("perfil_spotify", f"Error al llamar /me: {str(e)}")

    log_event("service_ok", "Perfil de Spotify obtenido correctamente")
    return status_ok("perfil_spotify", {
        "display_name": data.get("display_name", ""),
        "email": data.get("email", ""),
        "country": data.get("country", "")
    })


def check_credentials(request):
    cred = CredencialesSpotify.objects.first()
    if not cred:
        messages.error(request, "Debes autorizar tu cuenta de Spotify.")
        return redirect("login_spotify")
    return cred

def check_rate_limit(request, cred, show_message=True):
    if cred.rate_limit_until and cred.rate_limit_until > timezone.now():
        seconds_remaining = int((cred.rate_limit_until - timezone.now()).total_seconds())
        if show_message:
            messages.error(request, f"Rate limit activo. Espera {seconds_remaining} segundos.")
        return seconds_remaining
    return None


def handle_429(resp, cred, request):
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 30))
        cred.rate_limit_until = timezone.now() + timedelta(seconds=retry_after)
        cred.save()
        messages.error(request, f"Muchas peticiones a la API de Spotify. Espera {retry_after} segundos antes de volver a intentar.")
        return retry_after
    return None
