from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
from conexion.auth import build_authorize_url
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token, check_credentials, check_rate_limit, handle_429
from playlists.models import Playlist, Cancion, PlaylistCancion, Genero, PlaylistGenero
from django.http import HttpResponseRedirect
from django.db import transaction
from django.http import JsonResponse
import json
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
        
        generos_ids = request.GET.get("generos", "")
        lista_generos_ids = [int(x) for x in generos_ids.split(",") if x.isdigit()]

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

        # 6. Guardar playlist (Capturamos metadata completa de Spotify)
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

        snapshot_fresco = playlist_data.get("snapshot_id")

        # Aislamiento atómico para persistir la estructura completa sin corrupción parcial
        with transaction.atomic():
            playlist_obj = Playlist.objects.create(
                id_spotify=playlist_data["id"],
                nombre=playlist_data["name"],
                descripcion=descripcion,
                propietario=playlist_data["owner"]["display_name"],
                total_canciones=playlist_data["tracks"]["total"],
                cover_url=playlist_data["images"][0]["url"] if playlist_data.get("images") else None,
                usuario_importo=request.user,

                snapshot_ahorita=snapshot_fresco,   # Guardamos el estado actual capturado
                snapshot_anterior=None               # Importación inicial: queda explícitamente vacío
            )

            # 7. Guardar relaciones de canciones
            for idx, (cancion_obj, item) in enumerate(canciones_guardadas, start=1):
                PlaylistCancion.objects.create(
                    playlist=playlist_obj,
                    cancion=cancion_obj,
                    posicion=idx,
                    fecha_agregado=item.get("added_at"),
                    agregado_por=item["added_by"]["id"] if item.get("added_by") else None,
                    estado="activo"
                )
                
            # ASOCIAR GÉNEROS SELECCIONADOS
            for g_id in lista_generos_ids:
                PlaylistGenero.objects.create(
                    playlist_id=playlist_obj.pk,  
                    genero_id=g_id                
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

        # EXTRAEMOS TODOS LOS GÉNEROS DISPONIBLES
        generos_disponibles = Genero.objects.all().order_by('nombre')

        return render(request, "importar/importar_playlist.html", {
            "playlists": playlists,
            "page_number": page_number,
            "has_next": data.get("next") is not None,
            "rate_limited": False,
            "seconds_remaining": 0,
            "generos": generos_disponibles, # 👈 Enviamos los géneros a la plantilla
        })

    except Exception as e:
        messages.error(request, f"Error al importar playlists: {str(e)}")
        return redirect("lista_playlist_home")
    

@login_required
def crear_genero_ajax(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            nombre_genero = data.get("nombre", "").strip()
            
            if not nombre_genero:
                return JsonResponse({"ok": False, "error": "El nombre no puede estar vacío."})
            
            # Verificamos duplicados de forma limpia
            genero_obj, creado = Genero.objects.get_or_create(nombre=nombre_genero)
            
            return JsonResponse({
                "ok": True, 
                "id_genero": genero_obj.id_genero, 
                "nombre": genero_obj.nombre,
                "nuevo": creado
            })
        except Exception as e:
            return JsonResponse({"ok": False, "error": str(e)})
    return JsonResponse({"ok": False, "error": "Método no permitido."})

@login_required
def eliminar_genero_ajax(request, id_genero):
    if request.method == "POST":
        try:
            # Al tener ON DELETE CASCADE en la BD, MySQL limpia la tabla 'playlist_genero' automáticamente.
            Genero.objects.filter(id_genero=id_genero).delete()
            return JsonResponse({"ok": True})
        except Exception as e:
            return JsonResponse({"ok": False, "error": str(e)})
    return JsonResponse({"ok": False, "error": "Método no permitido."})
