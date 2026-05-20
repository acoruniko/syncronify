# editar_playlist/views.py
import json
from datetime import datetime
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse, HttpResponseRedirect
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from usuarios.models import Usuario
from django.contrib import messages
from datetime import timedelta
from conexion.auth import build_authorize_url
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token, check_credentials, check_rate_limit, handle_429
from playlists.models import Playlist, Cancion, PlaylistCancion, Tarea
import requests
from django.views.decorators.http import require_GET
import json
from conexion.services import check_rate_limit
from django.shortcuts import redirect
import requests
from django.db import transaction
from playlists.services import procesar_consecuencias_tarea_eliminada


def mensajes_bar(request):
    return render(request, "partials/mensajes_bar.html")

@login_required
def editar_playlist_home(request, playlist_id):
    playlist = get_object_or_404(Playlist, id_playlist=playlist_id)

    relaciones = (
        PlaylistCancion.objects
        .filter(playlist_id=playlist_id, estado__in=["activo", "pendiente"])
        .select_related('cancion')
        .order_by('posicion')
    )

    total_con_pendientes = relaciones.count()

    canciones = []
    for rel in relaciones:
        c = rel.cancion
        duracion_str = None
        if c.duracion_ms:
            minutos = c.duracion_ms // 60000
            segundos = (c.duracion_ms % 60000) // 1000
            duracion_str = f"{minutos}:{segundos:02d}"

        tareas_qs = (
            Tarea.objects.filter(relacion=rel)
            .select_related("usuario")
            .order_by('fecha_ejecucion', '-fecha_creacion')
        )

        tareas = [{
            "id_tarea": t.id_tarea,
            "tipo": t.tipo,
            "estado": t.estado,
            "fecha_ejecucion": t.fecha_ejecucion.isoformat(),
            "posicion": t.posicion,
            "usuario": t.usuario.nombre_completo if t.usuario else None
        } for t in tareas_qs]

        canciones.append({
            "id": c.id_cancion,
            "titulo": c.nombre,
            "artistas": c.artistas,
            "album": c.album,
            "duracion": duracion_str,
            "fecha_agregado": rel.fecha_agregado.isoformat() if rel.fecha_agregado else None,
            "posicion": rel.posicion,
            "cover_url": getattr(c, "cover_url", None),
            "id_relacion": rel.id_relacion,
            "tareas": tareas,
        })

    # ⚠️ Verificar rate limit usando servicio
    cred = CredencialesSpotify.objects.first()
    seconds_remaining = 0
    rate_limited = False

    if cred:
        seconds_remaining = check_rate_limit(request, cred, show_message=False) or 0
        rate_limited = seconds_remaining > 0

    return render(request, "editar_playlist/editar_playlist.html", {
        "playlist": playlist,
        "canciones": canciones,
        "canciones_json": json.dumps(canciones, ensure_ascii=False, cls=DjangoJSONEncoder),
        "rate_limited": rate_limited,
        "seconds_remaining": seconds_remaining,
        "total_con_pendientes": total_con_pendientes,
    })




@require_GET
def obtener_tareas(request, playlist_id, relacion_id):
    relacion = get_object_or_404(PlaylistCancion, id_relacion=relacion_id, playlist_id=playlist_id)

    tareas_qs = (
        Tarea.objects.filter(relacion=relacion)
        .select_related("usuario")
        .order_by('fecha_ejecucion', '-fecha_creacion')
    )

    tareas = [{
        "id_tarea": t.id_tarea,
        "tipo": t.tipo,
        "estado": t.estado,
        "fecha_ejecucion": t.fecha_ejecucion.isoformat(),
        "posicion": t.posicion,
        "usuario": t.usuario.nombre_completo if t.usuario else None
    } for t in tareas_qs]

    return JsonResponse({"ok": True, "tareas": tareas})


from datetime import timedelta # Necesario para sumar el día

@login_required
def crear_tarea(request, playlist_id):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'Método no permitido'}, status=405)

    relacion_id = request.POST.get('relacion_id')
    tipo = request.POST.get('tipo')
    posicion = request.POST.get('posicion')
    fecha_str = request.POST.get('fecha')

    if not relacion_id or not tipo or not fecha_str:
        return JsonResponse({'ok': False, 'error': 'Faltan campos obligatorios'}, status=400)

    relacion = get_object_or_404(PlaylistCancion, id_relacion=relacion_id, playlist_id=playlist_id)

    if relacion.estado not in ["activo", "pendiente"]:
        messages.error(request, "No puedes crear tareas sobre una relación eliminada.")
        return JsonResponse({'ok': False, 'error': 'Relación no activa'}, status=400)

    try:
        fecha_ejecucion = datetime.strptime(fecha_str, '%Y-%m-%d')
        fecha_ejecucion = timezone.make_aware(fecha_ejecucion, timezone.get_current_timezone())
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Fecha inválida'}, status=400)

    # --- INICIO VALIDACIONES TIPO 'POSICIONAR' ---
    if tipo == 'Posicionar':
        # 1. Validación de choque con 'Agregar' (No mover lo que no existe en esa fecha)
        if relacion.estado == 'pendiente':
            tarea_agregar = Tarea.objects.filter(relacion=relacion, tipo='Agregar', estado='Pendiente').first()
            if tarea_agregar and fecha_ejecucion.date() <= tarea_agregar.fecha_ejecucion.date():
                mensaje_error = (
                    f"Conflicto: La canción '{relacion.cancion.nombre}' se agrega el "
                    f"{tarea_agregar.fecha_ejecucion.strftime('%d/%m/%Y')}. "
                    f"Solo puede posicionarla a partir del día siguiente."
                )
                messages.error(request, mensaje_error)
                return JsonResponse({'ok': False, 'error': 'Choque con Agregar'}, status=400)

        # 2. Validación de choque con 'Eliminar' (No mover lo que se va a borrar en esa fecha)
        tarea_eliminar = Tarea.objects.filter(relacion=relacion, tipo='Eliminar', estado='Pendiente').first()
        if tarea_eliminar and fecha_ejecucion.date() >= tarea_eliminar.fecha_ejecucion.date():
            mensaje_error = (
                f"Conflicto: La canción '{relacion.cancion.nombre}' tiene una eliminación programada "
                f"para el {tarea_eliminar.fecha_ejecucion.strftime('%d/%m/%Y')}. "
                f"Debe posicionarla en una fecha anterior."
            )
            messages.error(request, mensaje_error)
            return JsonResponse({'ok': False, 'error': 'Choque con Eliminar'}, status=400)

        # 3. Validación de Rango (Estrictamente sobre canciones ACTIVAS hoy)
        if not posicion:
            return JsonResponse({'ok': False, 'error': 'Posición requerida'}, status=400)
        
        posicion_int = int(posicion)
        total_activas = PlaylistCancion.objects.filter(
            playlist_id=playlist_id, 
            estado='activo'
        ).count()

        # Si no hay canciones activas (playlist vacía), el límite es 1
        limite_maximo = max(1, total_activas)

        if posicion_int < 1 or posicion_int > limite_maximo:
            mensaje_error = (
                f"Rango inválido: La playlist '{relacion.playlist.nombre}' tiene {total_activas} "
                f"canciones activas. La posición {posicion_int} está fuera de rango."
            )
            messages.error(request, mensaje_error)
            return JsonResponse({'ok': False, 'error': 'Rango fuera de límites'}, status=400)
    # --- FIN VALIDACIONES TIPO 'POSICIONAR' ---

    # --- INICIO VALIDACIONES TIPO 'ELIMINAR' ---
    if tipo == 'Eliminar':
        # 1. Unicidad: Solo una tarea de eliminación a la vez
        # Buscamos tareas de tipo 'Eliminar' que no hayan fallado ni se hayan cancelado (Pendientes o en proceso)
        if Tarea.objects.filter(relacion=relacion, tipo='Eliminar', estado='Pendiente').exists():
            mensaje_error = (
                f"La canción '{relacion.cancion.nombre}' en la playlist '{relacion.playlist.nombre}' "
                f"ya tiene una tarea de 'Eliminar' pendiente."
            )
            messages.error(request, mensaje_error)
            return JsonResponse({'ok': False, 'error': 'Duplicado'}, status=400)

        # 2. Validación de choque con 'Agregar'
        if relacion.estado == 'pendiente':
            tarea_agregar = Tarea.objects.filter(relacion=relacion, tipo='Agregar', estado='Pendiente').first()
            if tarea_agregar:
                # NORMALIZACIÓN: Comparamos solo las fechas (date objects) 
                if fecha_ejecucion.date() <= tarea_agregar.fecha_ejecucion.date():
                    mensaje_error = (
                        f"Conflicto: La canción '{relacion.cancion.nombre}' se agrega el "
                        f"{tarea_agregar.fecha_ejecucion.strftime('%d/%m/%Y')} en '{relacion.playlist.nombre}'. "
                        f"Debe programar la eliminación al menos para el día siguiente."
                    )
                    messages.error(request, mensaje_error)
                    return JsonResponse({'ok': False, 'error': 'Choque con Agregar'}, status=400)
    # --- FIN VALIDACIONES TIPO 'ELIMINAR' ---

    # Creación del objeto
    tarea = Tarea(
        relacion=relacion,
        tipo=tipo,
        estado='Pendiente',
        fecha_ejecucion=fecha_ejecucion,
        usuario=request.user if request.user.is_authenticated else None
    )

    if tipo == 'Posicionar':
        if not posicion:
            return JsonResponse({'ok': False, 'error': 'Posición requerida'}, status=400)
        tarea.posicion = int(posicion)
    elif tipo == 'Eliminar':
        tarea.posicion = None
    else:
        return JsonResponse({'ok': False, 'error': 'Tipo de tarea inválido'}, status=400)

    tarea.save()

    messages.success(
        request,
        f"La tarea {tarea.tipo} de '{relacion.cancion.nombre}' "
        f"en la playlist '{relacion.playlist.nombre}' "
        f"para el {tarea.fecha_ejecucion.strftime('%d/%m/%Y')} "
        f"se agregó correctamente."
    )

    return JsonResponse({
        'ok': True,
        'tarea': {
            'id_tarea': tarea.id_tarea,
            'tipo': tarea.tipo,
            'estado': tarea.estado,
            'fecha_ejecucion': tarea.fecha_ejecucion.isoformat(),
            'posicion': tarea.posicion,
            'usuario': tarea.usuario.nombre_completo if tarea.usuario else None
        }
})


@login_required
def agregar_cancion(request, playlist_id):
    if request.method != "POST":
        messages.error(request, "Método no permitido")
        return JsonResponse({"ok": False, "error": "Método no permitido"}, status=405)

    try:
        url = request.POST.get("url")
        posicion = request.POST.get("posicion")
        fecha = request.POST.get("fecha")

        if not url or not posicion or not fecha:
            messages.error(request, "Datos incompletos para agregar canción")
            return JsonResponse({"ok": False, "error": "Datos incompletos"}, status=400)
        
        # 1. Extraer track ID
        track_id = None
        if url:
            # Intento 1: URL estándar de Spotify (ej: .../track/ID?...)
            if "/track/" in url:
                track_id = url.split("/track/")[1].split("?")[0]
            # Intento 2: URL de tu entorno (ej: .../spotify.com/ID)
            elif "spotify.com/" in url:
                track_id = url.split("spotify.com/")[1].split("/")[0].split("?")[0]
            # Intento 3: Es solo el ID puro
            else:
                track_id = url.strip()

        if not track_id:
            messages.error(request, "La URL de la canción no es válida")
            return JsonResponse({"ok": False, "error": "URL inválida"}, status=400)

        # 2. VALIDACIÓN DE DUPLICADOS (La regla de oro)
        # Primero intentamos obtener el objeto Cancion para tener su nombre
        cancion_existente = Cancion.objects.filter(id_spotify=track_id).first()
        nombre_cancion = cancion_existente.nombre if cancion_existente else f"con ID {track_id}"
        
        # Obtenemos el objeto playlist para el nombre (ya lo tienes en el punto 3, lo subimos)
        playlist = get_object_or_404(Playlist, id_playlist=playlist_id)

        existe_en_db = PlaylistCancion.objects.filter(
            playlist=playlist, 
            cancion__id_spotify=track_id,
            estado__in=["activo", "pendiente"]
        ).exists()

        if existe_en_db:
            mensaje_error = (
                f"La canción '{nombre_cancion}' ya existe en la playlist '{playlist.nombre}' "
                f"o tiene una tarea de 'Agregar' pendiente."
            )
            messages.error(request, mensaje_error)
            return JsonResponse({"ok": False, "error": "Canción duplicada"}, status=400)

        # 3. VALIDACIÓN DE RANGO (Solo para números positivos)
        try:
            posicion_raw = request.POST.get("posicion")
            posicion_int = int(posicion_raw)
            
            # Si NO es el flag de "último", validamos el rango actual
            if posicion_int != -1:
                playlist = get_object_or_404(Playlist, id_playlist=playlist_id)
                total_actual = PlaylistCancion.objects.filter(
                    playlist=playlist, 
                    estado__in=["activo", "pendiente"]
                ).count()

                if posicion_int < 1 or posicion_int > (total_actual + 1):
                    messages.error(request, f"Posición fuera de rango.")
                    return JsonResponse({"ok": False, "error": "Rango inválido"}, status=400)
            
            # Si es -1, no validamos rango contra el total actual, 
            # simplemente dejamos que pase para grabarse en la DB.
                
        except (ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "Formato de posición inválido"}, status=400)
        

        # 4. Credenciales y Rate Limit (Checkeos de seguridad)
        cred = check_credentials(request)
        if isinstance(cred, HttpResponseRedirect):
            return JsonResponse({
                "ok": False,
                "requires_auth": True,
                "auth_url": build_authorize_url(state=f"editar_playlist:{playlist_id}")
            }, status=401)

        seconds_remaining = check_rate_limit(request, cred)
        if seconds_remaining:
            return JsonResponse({
                "ok": False,
                "error": f"Rate limit activo. Espera {seconds_remaining} segundos."
            }, status=429)

        # 5. Token y Datos de Spotify
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers=headers,
            timeout=12
        )
        
        # Manejo de error 429 de la API
        retry_after = handle_429(resp, cred, request)
        if retry_after:
            return JsonResponse({"ok": False, "error": "Rate limit Spotify"}, status=429)

        if resp.status_code != 200:
            messages.error(request, "La API de Spotify no devolvió datos de la canción")
            return JsonResponse({"ok": False, "error": "Error API Spotify"}, status=500)

        data = resp.json()

        # 6. PERSISTENCIA ATÓMICA
        with transaction.atomic():
            # Crear/obtener canción
            cover_url = data["album"]["images"][0]["url"] if data["album"].get("images") else None
            cancion_obj, _ = Cancion.objects.get_or_create(
                id_spotify=track_id,
                defaults={
                    "nombre": data["name"],
                    "artistas": ", ".join([a["name"] for a in data["artists"]]),
                    "album": data["album"]["name"],
                    "duracion_ms": data["duration_ms"],
                    "popularidad": data.get("popularity"),
                    "cover_url": cover_url,
                }
            )

            # Crear relación (Estado PENDIENTE)
            # Nota: fecha_sincronizacion se deja en NULL hasta la ejecución real en Spotify
            relacion = PlaylistCancion.objects.create(
                playlist=playlist,
                cancion=cancion_obj,
                posicion=None,  # No tiene posición real en Spotify todavía
                fecha_agregado=timezone.now(),
                agregado_por=request.user.username,
                estado="pendiente"
            )

            # Validar y convertir fecha
            try:
                fecha_ejecucion = datetime.strptime(fecha, "%Y-%m-%d")
                fecha_dt = timezone.make_aware(fecha_ejecucion, timezone.get_current_timezone())
            except ValueError:
                return JsonResponse({"ok": False, "error": "Fecha inválida"}, status=400)

            # Crear la tarea automática para el Worker
            tarea = Tarea.objects.create(
                relacion=relacion,
                tipo="Agregar",
                posicion=posicion_int,
                estado="Pendiente",
                fecha_ejecucion=fecha_dt,
                usuario=request.user,
                url_cancion=url
            )

        # 7. Respuesta de éxito
        messages.success(
            request,
            f"La tarea {tarea.tipo} de '{relacion.cancion.nombre}' "
            f"en la playlist '{relacion.playlist.nombre}' "
            f"para el {tarea.fecha_ejecucion.strftime('%d/%m/%Y')} "
            f"se agregó correctamente."
        )
        
        return JsonResponse({
            "ok": True, 
            "relacion_id": relacion.id_relacion,
            "tarea_id": tarea.id_tarea
        })

    except Exception as e:
        messages.error(request, f"Error crítico al agregar canción: {str(e)}")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

@login_required
def obtener_canciones(request, playlist_id):
    playlist = Playlist.objects.get(id_playlist=playlist_id)
    relaciones = ( 
        playlist.playlistcancion_set 
        .filter(estado__in=["activo", "pendiente"])
        .select_related("cancion") 
        .order_by("posicion") 
        )
    
    total_con_pendientes = relaciones.count()

    canciones = []
    for rel in relaciones:
        c = rel.cancion
        duracion_str = None
        if c.duracion_ms:
            minutos = c.duracion_ms // 60000
            segundos = (c.duracion_ms % 60000) // 1000
            duracion_str = f"{minutos}:{segundos:02d}"

        tareas_qs = (
            Tarea.objects.filter(relacion=rel)
            .select_related("usuario")
            .order_by('fecha_ejecucion', '-fecha_creacion')
        )

        tareas = [{
            "id_tarea": t.id_tarea,
            "tipo": t.tipo,
            "estado": t.estado,
            "fecha_ejecucion": t.fecha_ejecucion.isoformat(),
            "posicion": t.posicion,
            "usuario": t.usuario.nombre_completo if t.usuario else None
        } for t in tareas_qs]

        canciones.append({
            "id": c.id_cancion,
            "titulo": c.nombre,
            "artistas": c.artistas,
            "album": c.album,
            "duracion": duracion_str,
            "fecha_agregado": rel.fecha_agregado.isoformat() if rel.fecha_agregado else None,
            "posicion": rel.posicion,
            "cover_url": getattr(c, "cover_url", None),
            "id_relacion": rel.id_relacion,
            "tareas": tareas,
        })

    return JsonResponse({"ok": True, "canciones": canciones, "total_con_pendientes": total_con_pendientes,})


@login_required
def eliminar_tarea(request, playlist_id, tarea_id):
    if request.method != 'POST':
        return JsonResponse({"ok": False, "error": "Método no permitido"}, status=405)

    # 1. Traemos la tarea con su relación y datos necesarios
    tarea = get_object_or_404(
        Tarea.objects.select_related('relacion', 'relacion__cancion', 'relacion__playlist'), 
        id_tarea=tarea_id, 
        relacion__playlist_id=playlist_id
    )

    with transaction.atomic():
        relacion = tarea.relacion
        tipo_tarea_original = tarea.tipo # Mantenemos el tipo original para el mensaje y el servicio
        tipo_tarea_lower = tipo_tarea_original.strip().lower()
        
        # 2. REGLA ESPECIAL: Si la tarea es de tipo 'Agregar'
        # Marcamos como eliminado para que el servicio sepa qué limpiar
        if tipo_tarea_lower == "agregar" and relacion.estado == "pendiente":
            relacion.estado = "eliminado"
            relacion.save(update_fields=["estado"])

        # Guardamos datos para tu mensaje de éxito ANTES de eliminar
        cancion_nom = relacion.cancion.nombre
        playlist_nom = relacion.playlist.nombre
        fecha = tarea.fecha_ejecucion.strftime('%d/%m/%Y') if tarea.fecha_ejecucion else "sin fecha"

        # 3. Eliminamos la tarea principal
        tarea.delete()

        # 4. LLAMADA AL SERVICIO: Manejo de consecuencias (borrado en cascada)
        # Importante: Pasa 'relacion' y el tipo de la tarea que acabamos de borrar
        requiere_reload = procesar_consecuencias_tarea_eliminada(request, relacion, tipo_tarea_lower)

    # Mantenemos tu mensaje original tal cual lo validaste con el cliente
    messages.success(
        request, 
        f"La tarea {tipo_tarea_original} de '{cancion_nom}' en '{playlist_nom}' ({fecha}) fue eliminada correctamente."
    )

    # Devolvemos requiere_reload para que el JS sepa si hacer location.reload() o refresco suave
    return JsonResponse({
        "ok": True,
        "requiere_reload": requiere_reload
    })


