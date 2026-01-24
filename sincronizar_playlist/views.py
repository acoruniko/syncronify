from django.shortcuts import render
from django.utils import timezone
from datetime import datetime
from calendar import monthrange
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from playlists.models import Tarea
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages


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
            "estado": t.estado,
            "usuario": t.usuario.nombre_completo if t.usuario else None,
            "fecha_ejecucion": t.fecha_ejecucion.strftime("%Y-%m-%d"),
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




