# importar/tests_debug.py (o temporalmente en importar/views.py)
import requests
from django.contrib import messages
from conexion.services import get_spotify_token

def debug_spotify_token():
    token = get_spotify_token()
    print(">>> Token obtenido:", token[:20], "...")  # imprime solo un prefijo por seguridad

    # 1) Prueba /v1/me — debería devolver 200 si el token es válido
    me = requests.get(
        "https://api.spotify.com/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=12
    )
    print(">>> /me status:", me.status_code)
    print(">>> /me body:", me.text)

    # 2) Prueba /v1/me/playlists — puede devolver 403 si faltan scopes
    pls = requests.get(
        "https://api.spotify.com/v1/me/playlists",
        headers={"Authorization": f"Bearer {token}"},
        timeout=12
    )
    print(">>> /me/playlists status:", pls.status_code)
    print(">>> /me/playlists body:", pls.text)