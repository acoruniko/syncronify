from django.shortcuts import render
from django.utils import timezone
from datetime import datetime
from calendar import monthrange
from datetime import timedelta
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db.models import F
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token
from playlists.models import Playlist, Cancion, PlaylistCancion, Tarea
import requests


@login_required
def sincronizar_playlist_home(request):
    from django.utils import timezone
    from datetime import datetime
    from calendar import monthrange

    # Año y mes desde GET
    today = timezone.localdate()

    # Si viene el parámetro month_year (ej: "2026-01"), lo usamos
    month_year = request.GET.get("month_year")
    if month_year:
        try:
            year, month = map(int, month_year.split("-"))
        except ValueError:
            year, month = today.year, today.month
    else:
        # fallback a year y month separados
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))

    # límites del mes
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

    return render(request, "sincronizar_playlist/sincronizar_playlist.html", {
        "tareas": tareas,
        "current_year": year,
        "current_month": month,
    })

@login_required
@require_POST
def eliminar_tarea(request, tarea_id):
    try:
        tarea = Tarea.objects.get(id_tarea=tarea_id)
        tarea.delete()

        # Mensaje de éxito
        messages.success(request, "Tarea eliminada correctamente")
        return JsonResponse({"ok": True})

    except Tarea.DoesNotExist:
        # Mensaje de error si la tarea ya no existe
        messages.error(request, "La tarea ya fue eliminada o no existe")
        return JsonResponse({"ok": False, "error": "Tarea no encontrada"}, status=404)

    except Exception as e:
        # Cualquier otro error inesperado
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
            f"⏳ Penalización activa. Espera {seconds_remaining} segundos antes de sincronizar."
        )
        return JsonResponse({
            "ok": False,
            "error": f"Rate limit activo. Espera {seconds_remaining} segundos."
        })

    # Incrementar intentos
    tarea.intentos += 1

    try:
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}
        playlist_spotify_id = tarea.relacion.playlist.id_spotify
        track_spotify_id = tarea.relacion.cancion.id_spotify

        if tarea.tipo.lower() == "posicionar":
            old_pos = tarea.relacion.posicion       # posición humana actual
            new_pos = tarea.posicion                # posición humana destino
            total_items = PlaylistCancion.objects.filter(
                playlist=tarea.relacion.playlist
            ).count()

            # Spotify usa índices 0-based
            range_start = old_pos - 1

            # ⚠️ Lógica final para insert_before
            if new_pos == total_items:
                insert_before = total_items
            elif new_pos > old_pos:
                # mover hacia adelante → insert_before = new_pos
                insert_before = new_pos
            else:
                # mover hacia atrás → insert_before = new_pos - 1
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

        elif tarea.tipo.lower() == "eliminar":
            payload = {"tracks": [{"uri": f"spotify:track:{track_spotify_id}"}]}
            resp = requests.delete(
                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                headers=headers,
                json=payload,
                timeout=12
            )

        elif tarea.tipo.lower() == "agregar":
            payload = {"uris": [f"spotify:track:{track_spotify_id}"]}
            resp = requests.post(
                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                headers=headers,
                json=payload,
                timeout=12
            )
        else:
            messages.error(request, "Tipo de tarea inválido.")
            return JsonResponse({"ok": False, "error": "Tipo de tarea inválido"}, status=400)

        # ⚠️ Manejo de rate limit
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 30))
            cred.rate_limit_until = timezone.now() + timedelta(seconds=retry_after)
            cred.save()
            tarea.estado = "Error"
            tarea.mensaje_error = f"Muchas peticiones a la API de Spotify. Espera {retry_after} segundos."
            tarea.save()
            messages.error(request, tarea.mensaje_error)
            return JsonResponse({"ok": False, "error": tarea.mensaje_error})

        resp.raise_for_status()

        # ✅ Si todo salió bien
        tarea.estado = "Completado"
        tarea.mensaje_error = None
        tarea.save()

        # 👉 Actualizar posiciones en la base de datos imitando Spotify
        if tarea.tipo.lower() == "posicionar":
            if new_pos > old_pos:
                PlaylistCancion.objects.filter(
                    playlist=tarea.relacion.playlist,
                    posicion__gt=old_pos,
                    posicion__lte=new_pos
                ).update(posicion=F("posicion") - 1)

            elif new_pos < old_pos:
                PlaylistCancion.objects.filter(
                    playlist=tarea.relacion.playlist,
                    posicion__gte=new_pos,
                    posicion__lt=old_pos
                ).update(posicion=F("posicion") + 1)

            tarea.relacion.posicion = new_pos
            tarea.relacion.save(update_fields=["posicion"])

        messages.success(
            request,
            f"Tarea '{tarea.tipo}' ejecutada correctamente. Intentos acumulados: {tarea.intentos}"
        )

        return JsonResponse({
            "ok": True,
            "estado": tarea.estado,
            "intentos": tarea.intentos
        })

    except Exception as e:
        tarea.estado = "Error"
        tarea.mensaje_error = str(e)
        tarea.save()
        messages.error(request, f"Error al ejecutar tarea: {tarea.mensaje_error}")
        return JsonResponse({"ok": False, "error": str(e), "intentos": tarea.intentos})












