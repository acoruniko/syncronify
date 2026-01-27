from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from datetime import datetime, timedelta
from calendar import monthrange
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db.models import F
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token
from playlists.models import Playlist, Cancion, PlaylistCancion, Tarea
import requests


@login_required
def sincronizar_playlist_home(request):
    today = timezone.localdate()

    month_year = request.GET.get("month_year")
    if month_year:
        try:
            year, month = map(int, month_year.split("-"))
        except ValueError:
            year, month = today.year, today.month
    else:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))

    start = datetime(year, month, 1, tzinfo=timezone.get_current_timezone())
    last_day = monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.get_current_timezone())

    tareas_qs = (
        Tarea.objects.filter(fecha_ejecucion__gte=start, fecha_ejecucion__lte=end)
        .select_related("relacion", "relacion__cancion", "relacion__playlist", "usuario")
        .order_by("fecha_ejecucion")
    )

    tareas = []
    for t in tareas_qs:
        tareas.append({
            "id_tarea": t.id_tarea,
            "accion": t.tipo,
            "posicion": t.posicion,
            "titulo": t.relacion.cancion.nombre if t.relacion and t.relacion.cancion else None,
            "album": t.relacion.cancion.album if t.relacion and t.relacion.cancion else None,
            "cover_url": t.relacion.cancion.cover_url if t.relacion and t.relacion.cancion else None,
            "playlist": t.relacion.playlist.nombre if t.relacion and t.relacion.playlist else None,
            "playlist_id": t.relacion.playlist.id_playlist if t.relacion and t.relacion.playlist else None,
            "estado": t.estado,
            "usuario": t.usuario.nombre_completo if t.usuario else None,
            "fecha_ejecucion": t.fecha_ejecucion.strftime("%d-%m-%Y"),
        })

    # ⚠️ Verificar rate limit
    cred = CredencialesSpotify.objects.first()
    rate_limited = False
    seconds_remaining = 0
    if cred and cred.rate_limit_until and cred.rate_limit_until > timezone.now():
        rate_limited = True
        seconds_remaining = int((cred.rate_limit_until - timezone.now()).total_seconds())
        messages.warning(request, f"Rate limit activo. Espera {seconds_remaining} segundos.")

    return render(request, "sincronizar_playlist/sincronizar_playlist.html", {
        "tareas": tareas,
        "current_year": year,
        "current_month": month,
        "rate_limited": rate_limited,
        "seconds_remaining": seconds_remaining,
    })


@login_required
@require_POST
def eliminar_tarea(request, tarea_id):
    try:
        tarea = Tarea.objects.get(id_tarea=tarea_id)
        tarea.delete()
        messages.success(request, "Tarea eliminada correctamente")
        return JsonResponse({"ok": True})
    except Tarea.DoesNotExist:
        messages.error(request, "La tarea ya fue eliminada o no existe")
        return JsonResponse({"ok": False, "error": "Tarea no encontrada"}, status=404)
    except Exception as e:
        messages.error(request, f"Error al eliminar la tarea: {str(e)}")
        return JsonResponse({"ok": False, "error": "Error interno"}, status=500)


@login_required
def sincronizar_tarea(request, playlist_id, tarea_id):
    if request.method != "POST":
        messages.error(request, "Método no permitido para sincronizar tarea.")
        return JsonResponse({"ok": False, "error": "Método no permitido"}, status=405)

    tarea = get_object_or_404(Tarea, id_tarea=tarea_id, relacion__playlist_id=playlist_id)
    cred = CredencialesSpotify.objects.first()

    # ⚠️ Verificar rate limit
    if cred and cred.rate_limit_until and cred.rate_limit_until > timezone.now():
        seconds_remaining = int((cred.rate_limit_until - timezone.now()).total_seconds())
        messages.error(
            request,
            f"Muchas peticiones a la API de Spotify. Espera {seconds_remaining} segundos antes de volver a intentar."
        )
        return JsonResponse({
            "ok": False,
            "error": f"Rate limit activo. Espera {seconds_remaining} segundos.",
            "rate_limited": True,
            "seconds_remaining": seconds_remaining,
        })

    tarea.intentos += 1

    try:
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}
        playlist_spotify_id = tarea.relacion.playlist.id_spotify
        track_spotify_id = tarea.relacion.cancion.id_spotify

        tipo = tarea.tipo.strip().lower()  # 👈 normalizamos para evitar espacios o mayúsculas raras

        # 👉 POSICIONAR
        if tipo == "posicionar":
            old_pos = tarea.relacion.posicion
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

            # ✅ Actualizar BD
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
            tarea.relacion.save(update_fields=["posicion"])

        # 👉 ELIMINAR
        elif tipo == "eliminar":
            # 1. Eliminar en Spotify (borra todas las ocurrencias del track)
            payload = {
                "tracks": [
                    {
                        "uri": f"spotify:track:{track_spotify_id}"
                    }
                ]
            }
            resp = requests.delete(
                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                headers=headers,
                json=payload,
                timeout=12
            )
            resp.raise_for_status()

            # 2. Actualizar BD: marcar todas las relaciones con ese track como eliminadas
            PlaylistCancion.objects.filter(
                playlist=tarea.relacion.playlist,
                cancion__id_spotify=track_spotify_id,
                estado="activo"
            ).update(estado="eliminado")

            playlist = tarea.relacion.playlist

            # 3. Reordenar posiciones de las canciones activas
            activas = PlaylistCancion.objects.filter(
                playlist=playlist,
                estado="activo"
            ).order_by("posicion")

            for i, rel in enumerate(activas, start=1):
                rel.posicion = i
                rel.save(update_fields=["posicion"])

            # 4. Actualizar total_canciones
            playlist.total_canciones = activas.count()
            playlist.save(update_fields=["total_canciones"])


        # 👉 AGREGAR
        elif tipo == "agregar":
            playlist = tarea.relacion.playlist
            new_pos = tarea.posicion

            # 1. Agregar en Spotify (queda al final)
            payload_add = {"uris": [f"spotify:track:{track_spotify_id}"]}
            resp_add = requests.post(
                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                headers=headers,
                json=payload_add,
                timeout=12
            )
            resp_add.raise_for_status()

            # 2. Reposicionar en Spotify (de último a la posición solicitada)
            total_items = PlaylistCancion.objects.filter(
                playlist=playlist,
                estado="activo"
            ).count() + 1  # incluye la nueva
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

            # ✅ Actualizar BD
            PlaylistCancion.objects.filter(
                playlist=playlist,
                estado="activo",
                posicion__gte=new_pos
            ).update(posicion=F("posicion") + 1)

            tarea.relacion.estado = "activo"
            tarea.relacion.posicion = new_pos
            tarea.relacion.save(update_fields=["estado", "posicion"])

            playlist.total_canciones = PlaylistCancion.objects.filter(
                playlist=playlist,
                estado="activo"
            ).count()
            playlist.save(update_fields=["total_canciones"])

        else:
            messages.error(request, f"Tipo de tarea inválido: {tarea.tipo}")
            return JsonResponse({"ok": False, "error": "Tipo de tarea inválido"}, status=400)

        # ✅ Si todo salió bien
        tarea.estado = "Completado"
        tarea.mensaje_error = None
        tarea.save()

        messages.success(request, f"Tarea '{tarea.tipo}' ejecutada correctamente.")
        return JsonResponse({
            "ok": True,
            "estado": tarea.estado,
            "intentos": tarea.intentos,
            "rate_limited": False,
            "seconds_remaining": 0,
        })

    except Exception as e:
        tarea.estado = "Error"
        tarea.mensaje_error = str(e)
        tarea.save()
        messages.error(request, f"Error al ejecutar tarea: {tarea.mensaje_error}")
        return JsonResponse({
            "ok": False,
            "error": str(e),
            "intentos": tarea.intentos,
            "rate_limited": False,
            "seconds_remaining": 0,
        })




