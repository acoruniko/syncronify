from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from datetime import datetime, timedelta
from calendar import monthrange
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseRedirect
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db.models import F
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token, check_credentials, check_rate_limit, handle_429
from playlists.models import Playlist, Cancion, PlaylistCancion, Tarea
import requests, json
from sincronizar_playlist.services import execute_tarea
from conexion.auth import build_authorize_url
from django.db import transaction
from django.db.models import Case, When, IntegerField
from playlists.services import procesar_consecuencias_tarea_eliminada


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
            "accion": t.tipo,   # Mantiene el string para la columna 'Acción' ("Agregar", "Eliminar")
            "posicion": t.posicion,
            "titulo": t.relacion.cancion.nombre if t.relacion and t.relacion.cancion else None,
            "album": t.relacion.cancion.album if t.relacion and t.relacion.cancion else None,
            "cover_url": t.relacion.cancion.cover_url if t.relacion and t.relacion.cancion else None,
            "playlist": t.relacion.playlist.nombre if t.relacion and t.relacion.playlist else None,
            "playlist_id": t.relacion.playlist.id_playlist if t.relacion and t.relacion.playlist else None,
            "estado": t.estado,
            "usuario": t.usuario.nombre_completo if t.usuario else None,
            "fecha_ejecucion": t.fecha_ejecucion.strftime("%d-%m-%Y"),
            
            # ==========================================
            # 🚀 LOS ENLACES PERDIDOS CON EL FRONTEND:
            # ==========================================
            "id_lote": t.id_lote if hasattr(t, "id_lote") else None,  # Ajusta el nombre si en tu modelo Tarea se llama distinto
            "tipo": t.tipo, # Lo necesita data-tipo en el HTML para saber si es "agregar"
        })

    # ⚠️ Verificar rate limit pero sin mensajes
    cred = CredencialesSpotify.objects.first()
    seconds_remaining = 0
    rate_limited = False
    if cred:
        seconds_remaining = check_rate_limit(request, cred, show_message=False) or 0
        rate_limited = seconds_remaining > 0

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
        # 1. Obtenemos la tarea con sus relaciones
        tarea = Tarea.objects.select_related(
            'relacion', 
            'relacion__cancion', 
            'relacion__playlist'
        ).get(id_tarea=tarea_id)

        with transaction.atomic():
            relacion = tarea.relacion
            tipo_tarea_original = tarea.tipo
            tipo_tarea_lower = tipo_tarea_original.strip().lower()
            
            # Guardamos info para el mensaje antes de borrar
            cancion = relacion.cancion.nombre if relacion and relacion.cancion else "Canción desconocida"
            playlist_nombre = relacion.playlist.nombre if relacion and relacion.playlist else "Playlist desconocida"
            fecha = tarea.fecha_ejecucion.strftime('%d/%m/%Y') if tarea.fecha_ejecucion else "sin fecha"

            # 2. Si es 'Agregar' pendiente, marcamos la relación para que el servicio la limpie
            if tipo_tarea_lower == "agregar" and relacion and relacion.estado == "pendiente":
                relacion.estado = "eliminado"
                relacion.save(update_fields=["estado"])

            # 3. Eliminamos la tarea físicamente
            tarea.delete()

            # 4. LLAMADA AL SERVICIO: Manejo de consecuencias (borrado en cascada)
            # Aquí el servicio se encargará de borrar las tareas huérfanas y poner los mensajes info
            procesar_consecuencias_tarea_eliminada(request, relacion, tipo_tarea_lower)

        # Mensaje principal validado
        messages.success(
            request,
            f"La tarea {tipo_tarea_original} de '{cancion}' en la playlist '{playlist_nombre}' "
            f"para el {fecha} fue eliminada correctamente."
        )

        return JsonResponse({"ok": True})

    except Tarea.DoesNotExist:
        messages.error(request, "La tarea ya fue eliminada o no existe.")
        return JsonResponse({"ok": False, "error": "Tarea no encontrada"}, status=404)

    except Exception as e:
        messages.error(request, f"Error al eliminar la tarea: {str(e)}")
        return JsonResponse({"ok": False, "error": "Error interno"}, status=500)


@login_required
def sincronizar_tarea(request, playlist_id, tarea_id):
    if request.method != "POST":
        messages.error(request, "Método no permitido para sincronizar tarea.")
        return JsonResponse({"ok": False, "error": "Método no permitido"}, status=405)

    tarea = get_object_or_404(Tarea, id_tarea=tarea_id)

    # Validación de seguridad opcional por si acaso mutó la playlist en el backend:
    playlist_actual_id = tarea.relacion.playlist_id if tarea.relacion else None
    if playlist_actual_id and playlist_actual_id != int(playlist_id):
        print(f"[WARN VISTA] La tarea {tarea_id} mutó de playlist. Original de URL: {playlist_id} -> Actual en DB: {playlist_actual_id}")

    # 📥 EXTRAER PARÁMETRO DE LOTE DESDE EL JSON ENVIADO POR JS
    ejecutar_como_lote = False
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body)
            ejecutar_como_lote = data.get("ejecutar_como_lote", False)
        except json.JSONDecodeError:
            pass
    else:
        ejecutar_como_lote = request.POST.get("ejecutar_como_lote") in ["true", "True", "1", True]

    # ⚠️ Validación de Credenciales de Spotify
    cred = check_credentials(request)
    if isinstance(cred, HttpResponseRedirect):
        return JsonResponse({
            "ok": False,
            "requires_auth": True,
            "auth_url": build_authorize_url(state="sincronizar_playlist")
        }, status=401)

    # ⚠️ Estados de control (Completada / No operable)
    estado_lower = tarea.estado.strip().lower() if tarea.estado else ""
    if estado_lower == "completado":
        messages.info(request, f"La tarea {tarea.tipo} de '{tarea.relacion.cancion.nombre}' ya fue completada.")
        return JsonResponse({"ok": False, "error": "La tarea ya está completada.", "estado": tarea.estado})
        
    if estado_lower in ["anulada", "cancelada"]:
        return JsonResponse({"ok": False, "error": f"Tarea en estado no operable: {tarea.estado}", "estado": tarea.estado}, status=400)

    # ⚠️ Control de Rate Limit
    seconds_remaining = check_rate_limit(request, cred, show_message=True)
    if seconds_remaining:
        return JsonResponse({"ok": False, "error": f"Rate limit activo.", "rate_limited": True, "seconds_remaining": seconds_remaining})

    # 🛡️ VALIDACIONES DE COHERENCIA CRONOLÓGICA
    tipo_actual = tarea.tipo.strip().lower()
    relacion_estado = tarea.relacion.estado.strip().lower() if tarea.relacion and tarea.relacion.estado else ""

    if tipo_actual in ["eliminar", "posicionar"] and relacion_estado == "pendiente":
        messages.error(request, f"No puedes ejecutar '{tarea.tipo}' porque la canción aún no ha sido agregada a Spotify.")
        return JsonResponse({"ok": False, "error": "Canción no agregada aún en Spotify"}, status=400)

    # =====================================================================
    # 👉 EJECUCIÓN SOBERANA: Selección de flujo basada EN LA ORDEN DEL JS
    # =====================================================================
    
    # 🛠️ AGREGAR Y ELIMINAR: Absorben el lote completo de la BD en una sola iteración de Python
    if ejecutar_como_lote and tipo_actual in ["agregar", "eliminar"]:
        print(f"\n=== [DEBUG VISTA] INICIO PROCESAMIENTO LOTE ABSORBIDO [{tipo_actual.upper()}] ===")
        
        tareas_lote = Tarea.objects.filter(
            id_lote=tarea.id_lote, 
            tipo__iexact=tipo_actual, 
            estado__in=["Pendiente", "En progreso"]
        )
        total_tareas_pendientes = tareas_lote.count()

        if total_tareas_pendientes == 0:
            return JsonResponse({"ok": False, "error": f"No quedan tareas de {tipo_actual} pendientes."}, status=400)

        estado, concilio, mensaje_telemetria = execute_tarea(
            tarea.id_tarea, source="manual", ejecutar_como_lote=True, request=request
        )
        print(f"=== [DEBUG VISTA] FIN PROCESAMIENTO LOTE [{tipo_actual.upper()}] ===\n")
        
        lote_absorbido = True  # Le avisa al JS que rompa el bucle de inmediato
        total_procesadas = total_tareas_pendientes

    # 🛠️ POSICIONAR (en lote o individual) O CUALQUIER TAREA UNITARIA STANDARD
    else:
        # Si viene de un switch de lote pero es posicionar, se envía ejecutar_como_lote=True 
        # para que la Capa 2 use el sufijo de lote correcto, pero procesando uno por uno de forma secuencial.
        modo_lote_capa2 = True if (ejecutar_como_lote and tipo_actual == "posicionar") else False
        
        print(f"\n=== [DEBUG VISTA] EJECUCIÓN SECUENCIAL (ID Tarea: {tarea.id_tarea} | Tipo: {tarea.tipo} | Modo Lote: {modo_lote_capa2}) ===")
        
        estado, concilio, mensaje_telemetria = execute_tarea(
            tarea.id_tarea, source="manual", ejecutar_como_lote=modo_lote_capa2, request=request
        )
        
        lote_absorbido = False  # Le avisa al JS que continúe con la siguiente tarea de la cola
        total_procesadas = 1
        total_tareas_pendientes = 1
    
    # Refrescamos el estado final después de procesar
    tarea.refresh_from_db()

    if concilio and mensaje_telemetria:
        messages.info(request, mensaje_telemetria)

    # 🎯 CORRECCIÓN QUIRÚRGICA: Extraer el string del mensaje generado para retornarlo al JS
    texto_notificacion = ""
    if estado == "Completado" and tarea.relacion and tarea.relacion.cancion and tarea.relacion.playlist:
        tipo_str = tarea.tipo.strip().capitalize()
        sufijo_lote = " en lote." if (ejecutar_como_lote and tipo_actual == "posicionar") else "."
        
        if tipo_actual == "posicionar":
            texto_notificacion = f"La tarea Posicionar de '{tarea.relacion.cancion.nombre}' (Nueva posición: {tarea.posicion}) en la playlist '{tarea.relacion.playlist.nombre}' se ejecutó correctamente{sufijo_lote}"
        else:
            texto_notificacion = f"La tarea {tipo_str} de '{tarea.relacion.cancion.nombre}' en la playlist '{tarea.relacion.playlist.nombre}' se ejecutó correctamente."

    if estado == "Completado":
        return JsonResponse({
            "ok": True, 
            "estado": tarea.estado, 
            "intentos": tarea.intentos,
            "rate_limited": False, 
            "seconds_remaining": 0, 
            "lote_procesado": ejecutar_como_lote,
            "lote_absorbido": lote_absorbido,  # 🚀 LLAVE INTEGRAL PARA EL ORQUESTADOR JS
            "total_lote": total_tareas_pendientes,
            "mensaje_interfaz": texto_notificacion  # 📦 Pasamos el texto limpio al front-end
        })
    else:
        error_msg = tarea.mensaje_error or "Ocurrió un error en el procesamiento."
        return JsonResponse({"ok": False, "error": error_msg, "estado": tarea.estado})