import requests
from django.utils import timezone
from django.db.models import F
from django.db import transaction
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token
from playlists.models import Tarea, PlaylistCancion

def execute_tarea(tarea_id):
    tarea = Tarea.objects.select_related("relacion", "relacion__playlist", "relacion__cancion").get(id_tarea=tarea_id)
    cred = CredencialesSpotify.objects.first()

    tarea.intentos += 1
    tarea.estado = "En progreso"
    tarea.save(update_fields=["estado", "intentos"])

    # ⚠️ Verificar rate limit
    if cred and cred.rate_limit_until and cred.rate_limit_until > timezone.now():
        tarea.estado = "Reprogramada"
        tarea.mensaje_error = f"Rate limit activo hasta {cred.rate_limit_until}"
        tarea.save(update_fields=["estado", "mensaje_error"])
        return tarea.estado

    # ⚠️ Verificar si la relación ya está eliminada
    if tarea.relacion.estado == "eliminado":
        tarea.estado = "Error"
        tarea.mensaje_error = "No se puede ejecutar la tarea: la canción ya está eliminada."
        tarea.save(update_fields=["estado", "mensaje_error"])
        return tarea.estado

    try:
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}
        playlist_spotify_id = tarea.relacion.playlist.id_spotify
        track_spotify_id = tarea.relacion.cancion.id_spotify
        tipo = tarea.tipo.strip().lower()

        # 👉 POSICIONAR
        if tipo == "posicionar":
            old_pos = tarea.posicion_anterior or tarea.relacion.posicion
            new_pos = tarea.posicion
            total_items = PlaylistCancion.objects.filter(
                playlist=tarea.relacion.playlist,
                estado="activo"
            ).count()

            range_start = old_pos - 1
            if new_pos == total_items:
                insert_before = total_items
            elif new_pos > old_pos:
                insert_before = new_pos
            else:
                insert_before = new_pos - 1

            payload = {
                "range_start": range_start,
                "insert_before": insert_before,
                "range_length": 1
            }
            resp = requests.put(
                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                headers=headers,
                json=payload,
                timeout=12
            )
            resp.raise_for_status()

            # Actualizar BD
            if new_pos > old_pos:
                PlaylistCancion.objects.filter(
                    playlist=tarea.relacion.playlist,
                    estado="activo",
                    posicion__gt=old_pos,
                    posicion__lte=new_pos
                ).update(posicion=F("posicion") - 1)
            elif new_pos < old_pos:
                PlaylistCancion.objects.filter(
                    playlist=tarea.relacion.playlist,
                    estado="activo",
                    posicion__gte=new_pos,
                    posicion__lt=old_pos
                ).update(posicion=F("posicion") + 1)

            tarea.relacion.posicion = new_pos
            tarea.posicion_anterior = old_pos
            tarea.save(update_fields=["posicion_anterior"])
            tarea.relacion.save(update_fields=["posicion"])

        # 👉 ELIMINAR
        elif tipo == "eliminar":
            payload = {"tracks": [{"uri": f"spotify:track:{track_spotify_id}"}]}
            resp = requests.delete(
                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                headers=headers,
                json=payload,
                timeout=12
            )
            resp.raise_for_status()

            PlaylistCancion.objects.filter(
                playlist=tarea.relacion.playlist,
                cancion__id_spotify=track_spotify_id,
                estado="activo"
            ).update(estado="eliminado")

            playlist = tarea.relacion.playlist
            activas = PlaylistCancion.objects.filter(playlist=playlist, estado="activo").order_by("posicion")
            for i, rel in enumerate(activas, start=1):
                rel.posicion = i
                rel.save(update_fields=["posicion"])

            playlist.total_canciones = activas.count()
            playlist.save(update_fields=["total_canciones"])

        # 👉 AGREGAR
        elif tipo == "agregar":
            playlist = tarea.relacion.playlist
            new_pos = tarea.posicion

            payload_add = {"uris": [f"spotify:track:{track_spotify_id}"]}
            resp_add = requests.post(
                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                headers=headers,
                json=payload_add,
                timeout=12
            )
            resp_add.raise_for_status()

            total_items = PlaylistCancion.objects.filter(playlist=playlist, estado="activo").count() + 1
            range_start = total_items - 1
            insert_before = new_pos - 1

            payload_move = {
                "range_start": range_start,
                "insert_before": insert_before,
                "range_length": 1
            }
            resp_move = requests.put(
                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                headers=headers,
                json=payload_move,
                timeout=12
            )
            resp_move.raise_for_status()

            PlaylistCancion.objects.filter(
                playlist=playlist,
                estado="activo",
                posicion__gte=new_pos
            ).update(posicion=F("posicion") + 1)

            tarea.relacion.estado = "activo"
            tarea.relacion.posicion = new_pos
            tarea.relacion.save(update_fields=["estado", "posicion"])

            playlist.total_canciones = PlaylistCancion.objects.filter(playlist=playlist, estado="activo").count()
            playlist.save(update_fields=["total_canciones"])

        else:
            tarea.estado = "Error"
            tarea.mensaje_error = f"Tipo de tarea inválido: {tarea.tipo}"
            tarea.save(update_fields=["estado", "mensaje_error"])
            return tarea.estado

        tarea.estado = "Completado"
        tarea.mensaje_error = None
        tarea.save(update_fields=["estado", "mensaje_error"])
        return tarea.estado

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code

        if status == 429:
            # 👉 Rate limit → Reprogramada
            retry_after = int(e.response.headers.get("Retry-After", 60))
            cred.rate_limit_until = timezone.now() + timezone.timedelta(seconds=retry_after)
            cred.save(update_fields=["rate_limit_until"])
            tarea.estado = "Reprogramada"
            tarea.mensaje_error = f"Rate limit, reintentar en {retry_after}s"

        elif status in (400, 403, 404):
            # 👉 Errores definitivos → Error
            tarea.estado = "Error"
            tarea.mensaje_error = f"Error definitivo {status}: {e.response.text}"

        else:
            # 👉 Errores externos → Error temporal
            tarea.estado = "Error temporal"
            tarea.mensaje_error = f"Error temporal {status}: {str(e)}"

        tarea.save(update_fields=["estado", "mensaje_error"])
        return tarea.estado


    except Exception as e:
        tarea.estado = "Error"
        tarea.mensaje_error = str(e)
        tarea.save(update_fields=["estado", "mensaje_error"])
        return tarea.estado
