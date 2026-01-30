from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
from conexion.auth import build_authorize_url
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token, check_credentials, check_rate_limit, handle_429
from playlists.models import Playlist, Cancion, PlaylistCancion
from django.http import HttpResponseRedirect
import requests

@login_required
def importar_playlist_confirmar(request, playlist_id):
    try:
        # 1. Credenciales
        cred = check_credentials(request)
        if isinstance(cred, HttpResponseRedirect):
            return redirect(build_authorize_url(state="importar_playlists"))


        # 2. Rate limit
        seconds_remaining = check_rate_limit(request, cred)
        if seconds_remaining:
            return redirect("lista_playlist_home")

        # 3. Token
        token = get_spotify_token()
        current_page = request.GET.get("page", 1)

        # 4. Verificar si la playlist ya existe
        if Playlist.objects.filter(id_spotify=playlist_id).exists():
            messages.warning(request, f"La playlist ya fue importada previamente.")
            return redirect(f"/importar/playlists/?page={current_page}")

        # 5. Obtener canciones
        canciones_guardadas = []
        url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100&offset=0"
        headers = {"Authorization": f"Bearer {token}"}

        while url:
            resp = requests.get(url, headers=headers, timeout=12)
            retry_after = handle_429(resp, cred, request)
            if retry_after:
                return redirect("lista_playlist_home")

            resp.raise_for_status()
            data = resp.json()

            for item in data["items"]:
                track = item["track"]
                if not track:
                    continue

                cover_url = track["album"]["images"][0]["url"] if track["album"].get("images") else None
                cancion_obj, _ = Cancion.objects.get_or_create(
                    id_spotify=track["id"],
                    defaults={
                        "nombre": track["name"],
                        "artistas": ", ".join([a["name"] for a in track["artists"]]),
                        "album": track["album"]["name"],
                        "duracion_ms": track["duration_ms"],
                        "popularidad": track.get("popularity"),
                        "cover_url": cover_url,
                    }
                )
                canciones_guardadas.append((cancion_obj, item))

            url = data.get("next")

        # 6. Guardar playlist
        playlist_resp = requests.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}",
            headers=headers,
            timeout=12
        )
        retry_after = handle_429(playlist_resp, cred, request)
        if retry_after:
            return redirect("lista_playlist_home")

        playlist_resp.raise_for_status()
        playlist_data = playlist_resp.json()

        descripcion = playlist_data.get("description", "")[:1000] if playlist_data.get("description") else ""

        playlist_obj = Playlist.objects.create(
            id_spotify=playlist_data["id"],
            nombre=playlist_data["name"],
            descripcion=descripcion,
            propietario=playlist_data["owner"]["display_name"],
            total_canciones=playlist_data["tracks"]["total"],
            cover_url=playlist_data["images"][0]["url"] if playlist_data.get("images") else None,
            usuario_importo=request.user
        )

        # 7. Guardar relaciones
        for idx, (cancion_obj, item) in enumerate(canciones_guardadas, start=1):
            PlaylistCancion.objects.create(
                playlist=playlist_obj,
                cancion=cancion_obj,
                posicion=idx,
                fecha_agregado=item.get("added_at"),
                agregado_por=item["added_by"]["id"] if item.get("added_by") else None,
                estado="activo"
            )

        # 8. Mensaje de éxito
        messages.success(request, f"La playlist '{playlist_obj.nombre}' fue importada con éxito.")
        return redirect(f"/importar/playlists/?page={current_page}")

    except Exception as e:
        messages.error(request, f"Error al importar la playlist: {str(e)}")
        current_page = request.GET.get("page", 1)
        return redirect(f"/importar/playlists/?page={current_page}")

@login_required
def importar_playlists(request):
    cred = check_credentials(request)
    if isinstance(cred, HttpResponseRedirect):
        return redirect(build_authorize_url(state="importar_playlists"))


    seconds_remaining = check_rate_limit(request, cred)
    if seconds_remaining:
        return redirect("lista_playlist_home")

    try:
        token = get_spotify_token()
        page_number = int(request.GET.get("page", 1))
        limit = 20
        offset = (page_number - 1) * limit

        resp = requests.get(
            f"https://api.spotify.com/v1/me/playlists?limit={limit}&offset={offset}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=12
        )
        retry_after = handle_429(resp, cred, request)
        if retry_after:
            return redirect("lista_playlist_home")

        resp.raise_for_status()
        data = resp.json()
        playlists = data.get("items", [])

        return render(request, "importar/importar_playlist.html", {
            "playlists": playlists,
            "page_number": page_number,
            "has_next": data.get("next") is not None,
            "rate_limited": False,
            "seconds_remaining": 0,
        })

    except Exception as e:
        messages.error(request, f"Error al importar playlists: {str(e)}")
        return redirect("lista_playlist_home")
