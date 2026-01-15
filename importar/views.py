from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token
from playlists.models import Playlist, Cancion, PlaylistCancion
import requests


@login_required
def importar_playlist_confirmar(request, playlist_id):
    try:
        cred = CredencialesSpotify.objects.first()
        if not cred:
            return redirect("login_spotify")

        # ⚠️ Verificar rate limit antes de importar
        if cred.rate_limit_until and cred.rate_limit_until > timezone.now():
            rate_limited = True
            seconds_remaining = int((cred.rate_limit_until - timezone.now()).total_seconds())
            messages.error(
                request,
                f"Muchas peticiones a la API de Spotify. Espera {seconds_remaining} segundos antes de volver a intentar."
            )
            return redirect("lista_playlist_home")

        token = get_spotify_token()
        current_page = request.GET.get("page", 1)

        # 1. Verificar si la playlist ya existe
        if Playlist.objects.filter(id_spotify=playlist_id).exists():
            messages.warning(request, f"La playlist ya fue importada previamente.")
            return redirect(f"/importar/playlists/?page={current_page}")

        # 2. Obtener canciones de la playlist
        canciones_guardadas = []
        url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100&offset=0"
        headers = {"Authorization": f"Bearer {token}"}

        while url:
            resp = requests.get(url, headers=headers, timeout=12)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                cred.rate_limit_until = timezone.now() + timedelta(seconds=retry_after)
                cred.save()
                rate_limited = True
                messages.error(
                    request,
                    f"Muchas peticiones a la API de Spotify. Espera {retry_after} segundos antes de volver a intentar."
                )
                return redirect("lista_playlist_home")

            resp.raise_for_status()
            data = resp.json()

            for item in data["items"]:
                track = item["track"]
                if not track:
                    continue

                cancion_obj, _ = Cancion.objects.get_or_create(
                    id_spotify=track["id"],
                    defaults={
                        "nombre": track["name"],
                        "artistas": ", ".join([a["name"] for a in track["artists"]]),
                        "album": track["album"]["name"],
                        "duracion_ms": track["duration_ms"],
                        "popularidad": track.get("popularity"),
                    }
                )
                canciones_guardadas.append((cancion_obj, item))

            url = data.get("next")

        # 3. Guardar playlist
        playlist_resp = requests.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}",
            headers=headers,
            timeout=12
        )

        if playlist_resp.status_code == 429:
            retry_after = int(playlist_resp.headers.get("Retry-After", 30))
            cred.rate_limit_until = timezone.now() + timedelta(seconds=retry_after)
            cred.save()
            rate_limited = True
            messages.error(
                request,
                f"Muchas peticiones a la API de Spotify. Espera {retry_after} segundos antes de volver a intentar."
            )
            return redirect("lista_playlist_home")

        playlist_resp.raise_for_status()
        playlist_data = playlist_resp.json()

        descripcion = playlist_data.get("description", "")
        if descripcion and len(descripcion) > 1000:
            descripcion = descripcion[:1000]

        playlist_obj = Playlist.objects.create(
            id_spotify=playlist_data["id"],
            nombre=playlist_data["name"],
            descripcion=descripcion,
            propietario=playlist_data["owner"]["display_name"],
            total_canciones=playlist_data["tracks"]["total"],
            cover_url=playlist_data["images"][0]["url"] if playlist_data.get("images") else None,
            usuario_importo=request.user
        )

        # 4. Guardar relaciones playlist ↔ canciones
        for cancion_obj, item in canciones_guardadas:
            PlaylistCancion.objects.get_or_create(
                playlist=playlist_obj,
                cancion=cancion_obj,
                defaults={
                    "posicion": item["track"]["track_number"],
                    "fecha_agregado": item.get("added_at"),
                    "agregado_por": item["added_by"]["id"] if item.get("added_by") else None,
                }
            )

        # 5. Mensaje de éxito
        messages.success(request, f"La playlist '{playlist_obj.nombre}' fue importada con éxito.")
        return redirect(f"/importar/playlists/?page={current_page}")

    except Exception as e:
        messages.error(request, f"Error al importar la playlist: {str(e)}")
        current_page = request.GET.get("page", 1)
        return redirect(f"/importar/playlists/?page={current_page}")


@login_required
def importar_playlists(request):
    cred = CredencialesSpotify.objects.first()
    if not cred:
        return redirect("login_spotify")

    # Inicializar variables
    rate_limited = False
    seconds_remaining = 0

    # ⚠️ Simulación manual de 429 si pasas ?force429=1 en la URL
    #if request.GET.get("force429") == "1":
    #    retry_after = 30
    #    cred.rate_limit_until = timezone.now() + timedelta(seconds=retry_after)
    #    cred.save()
    #    rate_limited = True
    #    seconds_remaining = retry_after
    #    messages.error(
    #        request,
    #        f"[SIMULACIÓN] Muchas peticiones a la API de Spotify. Espera {retry_after} segundos antes de volver a intentar."
    #    )
    #    return redirect("lista_playlist_home")

    # ⚠️ Revisar rate limit real antes de listar
    if cred.rate_limit_until and cred.rate_limit_until > timezone.now():
        rate_limited = True
        seconds_remaining = int((cred.rate_limit_until - timezone.now()).total_seconds())
        messages.error(
            request,
            f"Muchas peticiones a la API de Spotify. Espera {seconds_remaining} segundos antes de volver a intentar."
        )
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

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 30))
            cred.rate_limit_until = timezone.now() + timedelta(seconds=retry_after)
            cred.save()
            rate_limited = True
            messages.error(
                request,
                f"Muchas peticiones a la API de Spotify. Espera {retry_after} segundos antes de volver a intentar."
            )
            return redirect("lista_playlist_home")

        resp.raise_for_status()
        data = resp.json()
        playlists = data.get("items", [])

        return render(request, "importar/importar_playlist.html", {
            "playlists": playlists,
            "page_number": page_number,
            "has_next": data.get("next") is not None,
            "rate_limited": rate_limited,
            "seconds_remaining": seconds_remaining,
        })

    except Exception as e:
        messages.error(request, f"Error al importar playlists: {str(e)}")
        return redirect("lista_playlist_home")