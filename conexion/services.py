# conexion/services.py
import requests
from django.utils import timezone
from .models import CredencialesSpotify
from .auth import refresh_access_token
from .logs import status_ok, status_err

def get_spotify_token() -> str:
    cred = CredencialesSpotify.objects.first()
    if not cred:
        raise RuntimeError("No hay credenciales de Spotify almacenadas")

    # Si expiró, refrescar
    if cred.expires_at <= timezone.now():
        cred = refresh_access_token(cred)

    # Validar scopes
    if "playlist-read-private" not in cred.scope:
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
        return status_err("perfil_spotify", f"Error al llamar /me: {str(e)}")

    return status_ok("perfil_spotify", {
        "display_name": data.get("display_name", ""),
        "email": data.get("email", ""),
        "country": data.get("country", "")
    })