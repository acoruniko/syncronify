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
from sincronizar_playlist.services import execute_tarea


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

    # ⚠️ Verificar rate limit antes de ejecutar
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

    # 👉 Delegar ejecución al servicio
    estado = execute_tarea(tarea.id_tarea)

    if estado == "Completado":
        messages.success(request, f"Tarea '{tarea.tipo}' ejecutada correctamente.")
        return JsonResponse({
            "ok": True,
            "estado": tarea.estado,
            "intentos": tarea.intentos,
            "rate_limited": False,
            "seconds_remaining": 0,
        })
    else:
        messages.error(request, f"Error al ejecutar tarea: {tarea.mensaje_error}")
        return JsonResponse({
            "ok": False,
            "error": tarea.mensaje_error,
            "intentos": tarea.intentos,
            "rate_limited": False,
            "seconds_remaining": 0,
        })





