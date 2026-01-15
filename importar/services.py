# importar/services.py
import requests
from lista_playlist.models import Playlist

def importar_playlists_spotify(token):
    """
    Llama al endpoint /me/playlists de Spotify y guarda/actualiza en la DB local.
    """
    resp = requests.get(
        "https://api.spotify.com/v1/me/playlists",
        headers={"Authorization": f"Bearer {token}"},
        timeout=12
    )
    resp.raise_for_status()
    data = resp.json()

    for item in data.get("items", []):
        Playlist.objects.update_or_create(
            spotify_id=item["id"],
            defaults={"name": item["name"]}
        )