# editar_playlist/views.py
import json, requests,uuid
from django.urls import reverse
from datetime import datetime, timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseRedirect
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_protect
from django.core.serializers.json import DjangoJSONEncoder
from usuarios.models import Usuario
from django.contrib import messages
from conexion.auth import build_authorize_url
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token, check_credentials, check_rate_limit, handle_429
from playlists.models import Playlist, Cancion, PlaylistCancion, Tarea, Genero
from django.db import transaction
from playlists.services import procesar_consecuencias_tarea_eliminada, conciliar_playlist_con_spotify
from django.db import models

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
    ahora = timezone.now()  # 🕒 Captura del tiempo actual del sistema

    canciones = []
    for rel in relaciones:
        c = rel.cancion
        duracion_str = None
        if c.duracion_ms:
            minutos = c.duracion_ms // 60000
            segundos = (c.duracion_ms % 60000) // 1000
            duracion_str = f"{minutos}:{segundos:02d}"

        # Cálculo de días en el sistema limpio y real
        if rel.fecha_sincronizacion:
            dias_sistema = (ahora - rel.fecha_sincronizacion).days
            dias_en_sistema = max(0, dias_sistema) 
        else:
            dias_en_sistema = None 

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
            "dias_en_sistema": dias_en_sistema,  
        })

    # ⚠️ Verificar rate limit usando servicio
    cred = CredencialesSpotify.objects.first()
    seconds_remaining = 0
    rate_limited = False

    if cred:
        seconds_remaining = check_rate_limit(request, cred, show_message=False) or 0
        rate_limited = seconds_remaining > 0

    generos = Genero.objects.all().order_by('nombre')
    generos_asignados_ids = set(playlist.generos.values_list('id_genero', flat=True))

    return render(request, "editar_playlist/editar_playlist.html", {
        "playlist": playlist,
        "canciones": canciones,
        "canciones_json": json.dumps(canciones, ensure_ascii=False, cls=DjangoJSONEncoder),
        "rate_limited": rate_limited,
        "seconds_remaining": seconds_remaining,
        "total_con_pendientes": total_con_pendientes,
        "generos": generos,
        "generos_asignados_ids": generos_asignados_ids,
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

   # Reconstrucción exacta con la URL de la Playlist
    playlist_obj = relacion.playlist
    url_playlist = f"https://open.spotify.com/playlist/{playlist_obj.id_spotify}"
    artistas_str = getattr(relacion.cancion, 'artistas', 'Artista Desconocido')

    # 1. Construimos el string con los datos que irán al portapapeles
    data_copiar = (
        f"Tarea de Syncronify ({tarea.tipo}) | "
        f"Canción: {relacion.cancion.nombre} | "
        f"Artista: {artistas_str} | "
        f"Ejecución: {tarea.fecha_ejecucion.strftime('%d/%m/%Y')} | "
        f"Playlist: {playlist_obj.nombre} | "
        f"URL: {url_playlist}"
    )

    # 2. 🎯 INYECCIÓN CRUCIAL: Creamos el mensaje de éxito en Django con los corchetes estructurados
    messages.success(
        request,
        f"La tarea {tarea.tipo} de '{relacion.cancion.nombre}' "
        f"en la playlist '{playlist_obj.nombre}' "
        f"para el {tarea.fecha_ejecucion.strftime('%d/%m/%Y')} "
        f"se agregó correctamente. [[[{data_copiar}]]]"
    )

    # 3. Retornamos la respuesta Json de éxito estándar
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
        dias_proyeccion_raw = request.POST.get("dias_proyeccion")

        if not url or not posicion or not fecha:
            messages.error(request, "Datos incompletos para agregar canción")
            return JsonResponse({"ok": False, "error": "Datos incompletos"}, status=400)
        
        # 1. Extraer track ID
        track_id = None
        if url:
            url_clean = url.strip()
            if "open/spotify.com/" in url_clean:
                formatos_validos = [
                    "spotify.com/ID",
                    "spotify.com/",
                    "https://api.spotify.com/v1/tracks/",
                    "https://open.spotify.com/intl-es/track/2302lUwfZ4S4dVyPOCDFnQ"
                ]
                formato_encontrado = next((f for f in formatos_validos if f in url_clean), None)
                if formato_encontrado:
                    track_id = url_clean.split(formato_encontrado)[1].split("/")[0].split("?")[0]
                else:
                    messages.error(request, "El formato de URL del entorno de pruebas no está soportado.")
                    return JsonResponse({"ok": False, "error": "Formato de entorno inválido"}, status=400)
            elif "/track/" in url_clean:
                track_id = url_clean.split("/track/")[1].split("?")[0]
            elif "/" not in url_clean:
                track_id = url_clean
            else:
                messages.error(request, "La URL proporcionada no corresponde a un format válido.")
                return JsonResponse({"ok": False, "error": "Estructura de URL desconocida"}, status=400)

        if not track_id:
            messages.error(request, "No se pudo extraer un ID válido de la canción.")
            return JsonResponse({"ok": False, "error": "URL inválida"}, status=400)

        # 2. VALIDACIÓN DE DUPLICADOS
        cancion_existente = Cancion.objects.filter(id_spotify=track_id).first()
        nombre_cancion = cancion_existente.nombre if cancion_existente else f"con ID {track_id}"
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

        # 3. VALIDACIÓN DE RANGO
        try:
            posicion_int = int(posicion)
            if posicion_int != -1:
                total_actual = PlaylistCancion.objects.filter(
                    playlist=playlist, 
                    estado__in=["activo", "pendiente"]
                ).count()

                if posicion_int < 1 or posicion_int > (total_actual + 1):
                    messages.error(request, f"Posición fuera de rango.")
                    return JsonResponse({"ok": False, "error": "Rango inválido"}, status=400)
        except (ValueError, TypeError):
            return JsonResponse({"ok": False, "error": "Formato de posición inválido"}, status=400)
        
        # 4. Credenciales y Rate Limit
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
        
        retry_after = handle_429(resp, cred, request)
        if retry_after:
            return JsonResponse({"ok": False, "error": "Rate limit Spotify"}, status=429)

        if resp.status_code != 200:
            messages.error(request, "La API de Spotify no devolvió datos de la canción")
            return JsonResponse({"ok": False, "error": "Error API Spotify"}, status=500)

        data = resp.json()

        # 6. PERSISTENCIA ATÓMICA
        with transaction.atomic():
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

            relacion = PlaylistCancion.objects.create(
                playlist=playlist,
                cancion=cancion_obj,
                posicion=None,
                fecha_agregado=timezone.now(),
                agregado_por=request.user.username,
                estado="pendiente"
            )

            try:
                fecha_ejecucion = datetime.strptime(fecha, "%Y-%m-%d")
                fecha_dt = timezone.make_aware(fecha_ejecucion, timezone.get_current_timezone())
            except ValueError:
                return JsonResponse({"ok": False, "error": "Fecha inválida"}, status=400)

            # Crear la tarea automática de inserción para el Worker
            tarea = Tarea.objects.create(
                relacion=relacion,
                tipo="Agregar",
                posicion=posicion_int,
                estado="Pendiente",
                fecha_ejecucion=fecha_dt,
                usuario=request.user,
                url_cancion=url
            )

            tarea_eliminar_creada = False
            fecha_eliminar_str = ""

            if dias_proyeccion_raw and dias_proyeccion_raw != "-":
                try:
                    dias_int = int(str(dias_proyeccion_raw).strip())
                    if dias_int >= 1:
                        # Calculamos la fecha de eliminación (mantiene el timezone de fecha_dt)
                        fecha_eliminar_dt = fecha_dt + timedelta(days=dias_int)
                        fecha_eliminar_str = fecha_eliminar_dt.strftime('%d/%m/%Y')
                        
                        # Guardamos explícitamente en la base de datos
                        Tarea.objects.create(
                            relacion=relacion,
                            tipo="Eliminar",
                            posicion=None,
                            estado="Pendiente",
                            fecha_ejecucion=fecha_eliminar_dt,
                            usuario=request.user
                        )
                        tarea_eliminar_creada = True
                except (ValueError, TypeError) as e:
                    pass

        # 7. Respuesta de éxito e inyección de Mensajes en el Sistema de Django
        url_playlist = f"https://open.spotify.com/playlist/{playlist.id_spotify}"
        
        # Data para Agregar
        data_copiar_agregar = (
            f"Tarea de Syncronify (Agregar) | "
            f"Canción: {cancion_obj.nombre} | "
            f"Artista: {cancion_obj.artistas} | "
            f"Ejecución: {tarea.fecha_ejecucion.strftime('%d/%m/%Y')} | "
            f"Playlist: {playlist.nombre} | "
            f"URL: {url_playlist}"
        )

        messages.success(
            request,
            f"La tarea {tarea.tipo} de '{relacion.cancion.nombre}' "
            f"en la playlist '{playlist.nombre}' "
            f"para el {tarea.fecha_ejecucion.strftime('%d/%m/%Y')} "
            f"se agregó correctamente. [[[{data_copiar_agregar}]]]"
        )
        
        if tarea_eliminar_creada:
            # Data para Eliminar
            data_copiar_eliminar = (
                f"Tarea de Syncronify (Eliminar) | "
                f"Canción: {cancion_obj.nombre} | "
                f"Artista: {cancion_obj.artistas} | "
                f"Ejecución: {fecha_eliminar_str} | "
                f"Playlist: {playlist.nombre} | "
                f"URL: {url_playlist}"
            )

            messages.success(
                request,
                f"La tarea Eliminar de '{relacion.cancion.nombre}' "
                f"en la playlist '{playlist.nombre}' "
                f"para el {fecha_eliminar_str} "
                f"se agregó correctamente. [[[{data_copiar_eliminar}]]]"
            )
        
        return JsonResponse({
            "ok": True, 
            "relacion_id": relacion.id_relacion,
            "tarea_id": tarea.id_tarea,
            "eliminar_creada": tarea_eliminar_creada
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

@login_required
@transaction.non_atomic_requests
def actualizar_playlist_spotify_view(request, playlist_id):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método no permitido"}, status=405)

    try:
        data = json.loads(request.body)
        forzar = data.get("forzar", False)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"ok": False, "error": "Cuerpo de petición inválido"}, status=400)

    # 1. Obtener la playlist local y leer el PRESENTE físico en nuestra BD
    try:
        playlist = Playlist.objects.get(id_playlist=playlist_id)
        snapshot_local_presente = playlist.snapshot_ahorita  # 🎯 Cambiado al campo correcto
    except Playlist.DoesNotExist:
        return JsonResponse({"ok": False, "error": "La playlist no existe en la BD local"}, status=404)

    # 2. Checkeos de Seguridad y Autenticación de Spotify
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

    # 3. Obtener Token Válido
    token = get_spotify_token()
    if not token:
        return JsonResponse({"ok": False, "error": "No se pudo obtener el token de acceso a Spotify"}, status=401)

    headers = {"Authorization": f"Bearer {token}"}

    if not forzar:
        url_playlist_base = f"https://api.spotify.com/v1/playlists/{playlist.id_spotify}?fields=snapshot_id"
        try:
            resp_master = requests.get(url_playlist_base, headers=headers, timeout=12)
            if resp_master.status_code == 200:
                snapshot_spotify = resp_master.json().get("snapshot_id")
                
                # Comparamos el "ahorita" de la BD con el de los servidores de Spotify
                if snapshot_local_presente == snapshot_spotify:
                    msg_no_cambios = f'La playlist "{playlist.nombre}" ya está actualizada. No hay cambios externos detectados.'
                    messages.info(request, msg_no_cambios)
                    return JsonResponse({
                        "ok": True,
                        "cambios_detectados": False,
                        "nombre_playlist": playlist.nombre,
                        "mensaje": msg_no_cambios
                    })
        except Exception:
            pass

    # 4. Invocar el servicio de conciliación (va ciego y ejecuta el pasamanos adentro)
    resultado = conciliar_playlist_con_spotify(
        id_playlist_local=playlist_id,
        spotify_token=token,
        request_user=request.user
    )

    if resultado["ok"]:
        messages.success(request, resultado["mensaje"])
    else:
        messages.error(request, resultado.get("error", "Error desconocido en la conciliación."))

    return JsonResponse(resultado)

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


@login_required
@require_POST
def asociar_genero_ajax(request):
    playlist_id = request.POST.get('playlist_id')
    genero_id = request.POST.get('genero_id')
    
    if not playlist_id or not genero_id:
        return JsonResponse({'ok': False, 'error': 'Faltan parámetros requeridos.'}, status=400)
        
    playlist = get_object_or_404(Playlist, id_playlist=playlist_id)
    genero = get_object_or_404(Genero, id_genero=genero_id)
    
    playlist.generos.add(genero)
    
    return JsonResponse({'ok': True, 'msg': f'Género {genero.nombre} asociado con éxito.'})


@login_required
@require_POST
def desasociar_genero_ajax(request):
    playlist_id = request.POST.get('playlist_id')
    genero_id = request.POST.get('genero_id')
    
    if not playlist_id or not genero_id:
        return JsonResponse({'ok': False, 'error': 'Faltan parámetros requeridos.'}, status=400)
        
    playlist = get_object_or_404(Playlist, id_playlist=playlist_id)
    genero = get_object_or_404(Genero, id_genero=genero_id)
    
    playlist.generos.remove(genero)
    
    return JsonResponse({'ok': True, 'msg': f'Género {genero.nombre} desasociado con éxito.'})

@login_required
def agregar_tareas_multiples_home(request):
    return render(request, "editar_playlist/agregar_tareas_multiples.html")


@login_required
@require_POST
def consultar_track_spotify_ajax(request):
    try:
        data = json.loads(request.body)
        url_clean = data.get('url', '').replace(' ', '').strip()
        
        # 1. Extracción elástica del Track ID (Fiel a tu estándar en agregar_cancion)
        track_id = None
        if url_clean:
            if "open/spotify.com/" in url_clean:
                formatos_validos = [
                    "spotify.com/ID",
                    "spotify.com/",
                    "https://api.spotify.com/v1/tracks/",
                    "https://open.spotify.com/intl-es/track/2302lUwfZ4S4dVyPOCDFnQ"
                ]
                formato_encontrado = next((f for f in formatos_validos if f in url_clean), None)
                if formato_encontrado:
                    track_id = url_clean.split(formato_encontrado)[1].split("/")[0].split("?")[0]
            elif "/track/" in url_clean:
                track_id = url_clean.split("/track/")[1].split("?")[0]
            elif "/" not in url_clean:
                track_id = url_clean

        if not track_id:
            return JsonResponse({'ok': False, 'error': 'URL o ID de Spotify inválido o no soportado.'})

        # 2. Chequeo de Seguridad y Autenticación del Ecosistema Local
        cred = check_credentials(request)
        if isinstance(cred, HttpResponseRedirect):
            return JsonResponse({
                "ok": False,
                "requires_auth": True,
                "auth_url": build_authorize_url(state="agregar_tareas_multiples")
            }, status=401)

        # 3. Control de Rate Limit Local
        seconds_remaining = check_rate_limit(request, cred)
        if seconds_remaining:
            return JsonResponse({
                "ok": False,
                "error": f"Rate limit activo. Espera {seconds_remaining} segundos."
            }, status=429)

        # 4. Obtención de Token de Acceso
        token = get_spotify_token()
        if not token:
            return JsonResponse({"ok": False, "error": "No se pudo obtener el token de acceso a Spotify."}, status=401)

        # 5. Consumo Real de la API (Usa tu endpoint espejo del entorno de pruebas)
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers=headers,
            timeout=12
        )
        
        # 6. Manejo de Rate Limit Externo (429)
        retry_after = handle_429(resp, cred, request)
        if retry_after:
            return JsonResponse({"ok": False, "error": f"Rate limit de Spotify activo. Reintenta en {retry_after}s."}, status=429)

        if resp.status_code != 200:
            return JsonResponse({"ok": False, "error": "La API de Spotify no devolvió datos para este track."}, status=resp.status_code)

        track_data = resp.json()

        # 7. Mapeo y Limpieza de Metadata Real para el Frontend
        titulo = track_data.get("name", "Sin título")
        artistas = ", ".join([a["name"] for a in track_data.get("artists", [])])
        album = track_data.get("album", {}).get("name", "Sin álbum")
        
        # Extracción segura del Cover URL (Buscamos la resolución más pequeña o la que haya)
        images = track_data.get("album", {}).get("images", [])
        cover_url = ""
        if images:
            cover_url = images[0]["url"]  # O images[-1]["url"] si prefieres la miniatura ligera de 64x64

        # Retorno exitoso estructurado directo a tu tabla dinámica
        return JsonResponse({
            'ok': True,
            'track': {
                'id_spotify': track_id,
                'titulo': titulo,
                'artistas': artistas,
                'album': album,
                'cover_url': cover_url
            }
        })

    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Error crítico en el servidor: {str(e)}'}, status=500)
    

@login_required
@require_GET
def buscar_cancion_local_ajax(request):
    """
    Busca canciones en la base de datos local por nombre o artista 
    y devuelve la metadata estructurada para la grilla de lotes.
    """
    query = request.GET.get('q', '').strip()
    
    if not query or len(query) < 2:
        return JsonResponse({'ok': True, 'resultados': []})
        
    try:
        # Buscamos coincidencias parciales (case-insensitive) limitando a 15 resultados por rendimiento
        canciones_qs = Cancion.objects.filter(
            models.Q(nombre__icontains=query) | models.Q(artistas__icontains=query)
        )[:15]
        
        resultados = []
        for c in canciones_qs:
            resultados.append({
                'id_spotify': c.id_spotify,
                'titulo': c.nombre,
                'artistas': c.artistas,
                'album': c.album or "Sin álbum",
                'cover_url': c.cover_url or "https://picsum.photos/64"  # Fallback si no tiene imagen
            })
            
        return JsonResponse({'ok': True, 'resultados': resultados})
        
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Error en la búsqueda local: {str(e)}'}, status=500)
    
def seleccionar_destinos_lote_view(request):
    # Traemos los géneros ordenados de la base de datos local para el grid horizontal
    generos = Genero.objects.all().order_by('nombre')
    
    # Traemos las playlists iniciales que se renderizan por defecto al cargar la página
    playlists = Playlist.objects.all().order_by('-id_playlist')[:20] # O tu paginación por defecto

    context = {
        'generos': generos,
        'playlists': playlists,
    }
    return render(request, 'editar_playlist/seleccionar_destinos_lote.html', context)

@login_required
def planificar_tareas_lote_ajax(request):
    """
    Fábrica transaccional masiva. Genera tareas individuales de tipo 'Agregar' 
    y 'Eliminar' bajo un mismo id_lote global para todas las canciones y playlists,
    consolidando los mensajes por canción para optimizar el copiado de URLs.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método no permitido"}, status=405)

    try:
        payload = json.loads(request.body)
        canciones_lote = payload.get("canciones", [])
        playlists_destino = payload.get("playlists", [])

        if not canciones_lote or not playlists_destino:
            return JsonResponse({"ok": False, "error": "Datos del lote incompletos o vacíos."}, status=400)

        duplicados_omitidos = 0
        logs_copiado = {}
        id_lote_global = uuid.uuid4()

        # OPERACIÓN ATÓMICA DE PERSISTENCIA
        with transaction.atomic():
            
            for p_dest in playlists_destino:
                playlist_id = int(p_dest["id_playlist"])
                playlist_obj = get_object_or_404(Playlist, id_playlist=playlist_id)
                url_playlist = f"https://open.spotify.com/playlist/{playlist_obj.id_spotify}"
                
                total_actual_playlist = PlaylistCancion.objects.filter(
                    playlist=playlist_obj,
                    estado__in=["activo", "pendiente"]
                ).count()

                for track in canciones_lote:
                    track_id = track.get("id_spotify")
                    posicion_raw = track.get("posicion", -1)
                    fecha_raw = track.get("fecha_ejecucion")
                    dias_proyeccion_raw = track.get("dias_proyeccion")

                    cancion_obj = Cancion.objects.filter(id_spotify=track_id).first()
                    if not cancion_obj:
                        continue

                    if track_id not in logs_copiado:
                        logs_copiado[track_id] = {
                            'nombre': cancion_obj.nombre,
                            'artistas': cancion_obj.artistas,
                            'destinos_agregar': [],
                            'destinos_eliminar': []
                        }

                    existe_en_db = PlaylistCancion.objects.filter(
                        playlist=playlist_obj,
                        cancion=cancion_obj,
                        estado__in=["activo", "pendiente"]
                    ).exists()

                    if existe_en_db:
                        duplicados_omitidos += 1
                        messages.warning(
                            request,
                            f"Se omitió '{cancion_obj.nombre}' en '{playlist_obj.nombre}': Ya existe o tiene un Agregar pendiente."
                        )
                        continue

                    try:
                        posicion_int = int(posicion_raw)
                        if posicion_int != -1:
                            if posicion_int < 1 or posicion_int > (total_actual_playlist + 1):
                                posicion_int = -1
                    except (ValueError, TypeError):
                        posicion_int = -1
                    
                    try:
                        fecha_ejecucion = datetime.strptime(fecha_raw, "%Y-%m-%d")
                        fecha_dt = timezone.make_aware(fecha_ejecucion, timezone.get_current_timezone())
                    except (ValueError, TypeError):
                        return JsonResponse({"ok": False, "error": f"Fecha inválida en track {track_id}"}, status=400)

                    relacion = PlaylistCancion.objects.create(
                        playlist=playlist_obj,
                        cancion=cancion_obj,
                        posicion=None,
                        fecha_agregado=timezone.now(),
                        agregado_por=request.user.username,
                        estado="pendiente"
                    )

                    tarea_agregar = Tarea.objects.create(
                        relacion=relacion,
                        tipo="Agregar",
                        posicion=posicion_int, 
                        estado="Pendiente",
                        fecha_ejecucion=fecha_dt,
                        usuario=request.user,
                        url_cancion=f"https://open.spotify.com/track/{track_id}",
                        id_lote=id_lote_global  # 👈 Usamos el lote global unificado
                    )
                    total_actual_playlist += 1

                    f_agregar_str = tarea_agregar.fecha_ejecucion.strftime('%d/%m/%Y')
                    logs_copiado[track_id]['fecha_agregar'] = f_agregar_str
                    logs_copiado[track_id]['destinos_agregar'].append(f"- Playlist: {playlist_obj.nombre} | URL: {url_playlist}")

                    if dias_proyeccion_raw and dias_proyeccion_raw != "-":
                        try:
                            dias_int = int(str(dias_proyeccion_raw).strip())
                            if dias_int >= 1:
                                fecha_eliminar_dt = fecha_dt + timedelta(days=dias_int)
                                
                                Tarea.objects.create(
                                    relacion=relacion,
                                    tipo="Eliminar",
                                    posicion=None,
                                    estado="Pendiente",
                                    fecha_ejecucion=fecha_eliminar_dt,
                                    usuario=request.user,
                                    id_lote=id_lote_global  # 👈 Mismo lote global unificado
                                )
                                f_eliminar_str = fecha_eliminar_dt.strftime('%d/%m/%Y')
                                logs_copiado[track_id]['fecha_eliminar'] = f_eliminar_str
                                logs_copiado[track_id]['destinos_eliminar'].append(f"- Playlist: {playlist_obj.nombre} | URL: {url_playlist}")
                        except (ValueError, TypeError):
                            pass

        # GENERACIÓN DE MENSAJES CONSOLIDADOS POR CANCIÓN (Sin cambios)
        for t_id, info in logs_copiado.items():
            if info['destinos_agregar']:
                destinos_str = " | ".join(info['destinos_agregar'])
                data_copiar_add = (
                    f"Tarea de Syncronify por Lote (Agregar) | "
                    f"Canción: {info['nombre']} | "
                    f"Artista: {info['artistas']} | "
                    f"Ejecución: {info['fecha_agregar']} | "
                    f"Destinos: | {destinos_str}"
                )
                messages.success(
                    request,
                    f"La tarea Agregar por lote para '{info['nombre']}' se programó correctamente en {len(info['destinos_agregar'])} playlists. [[[{data_copiar_add}]]]"
                )

            if info['destinos_eliminar']:
                destinos_del_str = " | ".join(info['destinos_eliminar'])
                data_copiar_del = (
                    f"Tarea de Syncronify por Lote (Eliminar) | "
                    f"Canción: {info['nombre']} | "
                    f"Artista: {info['artistas']} | "
                    f"Ejecución: {info['fecha_eliminar']} | "
                    f"Destinos: | {destinos_del_str}"
                )
                messages.success(
                    request,
                    f"La tarea Eliminación proyectada por lote para '{info['nombre']}' se programó correctamente en {len(info['destinos_eliminar'])} playlists. [[[{data_copiar_del}]]]"
                )

        return JsonResponse({
            "ok": True,
            "duplicados_omitidos": duplicados_omitidos
        })

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Fallo crítico en el motor de persistencia: {str(e)}"}, status=500)
    
@login_required
def eliminar_tareas_multiples_home(request):
    """
    Renderiza el entorno de trabajo principal para la eliminación múltiple.
    Envia las playlists iniciales para el selector derecho del Estado A.
    """
    playlists = Playlist.objects.all().order_by('-id_playlist')
    generos = Genero.objects.all().order_by('nombre')
    
    context = {
        'playlists': playlists,
        'generos': generos,
    }
    return render(request, "editar_playlist/eliminar_tareas_multiples.html", context)

@login_required
def posicionar_tareas_multiples_home(request):
    """
    Renderiza el entorno de trabajo principal para el posicionamiento múltiple.
    Envia las playlists iniciales para el selector derecho del Estado A.
    """
    playlists = Playlist.objects.all().order_by('-id_playlist')
    generos = Genero.objects.all().order_by('nombre')
    
    context = {
        'playlists': playlists,
        'generos': generos,
    }
    return render(request, "editar_playlist/posicionar_tareas_multiples.html", context)


@login_required
@require_GET
def obtener_canciones_playlist_eliminar_ajax(request, playlist_id):
    """
    Estado B (Columna Derecha): Devuelve todas las canciones mapeando correctamente 
    las posiciones programadas de cualquier tipo de tarea pendiente (Agregar/Eliminar/Posicionar).
    """
    playlist = get_object_or_404(Playlist, id_playlist=playlist_id)
    
    # 1. Total de posiciones válidas basándose SOLO en tracks activos
    total_playlist = PlaylistCancion.objects.filter(
        playlist=playlist, 
        estado="activo"
    ).count()
    
    # 2. Traemos TODAS las relaciones operables (activas y pendientes)
    relaciones = (
        PlaylistCancion.objects
        .filter(playlist=playlist, estado__in=["activo", "pendiente"])
        .select_related("cancion")
        .order_by("posicion")
    )
    
    canciones = []
    for rel in relaciones:
        tarea_pendiente = Tarea.objects.filter(
            relacion=rel, 
            estado="Pendiente"
        ).first()
        
        # Bandera específica para saber si tiene una eliminación programada en la UI
        tiene_eliminar_pendiente = tarea_pendiente is not None and tarea_pendiente.tipo == "Eliminar"

        # Determinar la posición real según el estado en el ecosistema
        if rel.estado == "activo":
            posicion_real = rel.posicion
        else:
            # Si está pendiente, extraemos el número real que guardaste en el modelo Tarea (1, -1, etc.)
            if tarea_pendiente and tarea_pendiente.posicion is not None:
                posicion_real = tarea_pendiente.posicion
            else:
                posicion_real = -1  # Fallback seguro solo si la relación quedó huérfana de tarea

        canciones.append({
            "id_relacion": rel.id_relacion,
            "titulo": rel.cancion.nombre,
            "artistas": rel.cancion.artistas,
            "cover_url": getattr(rel.cancion, "cover_url", None),
            "playlist_origen_nombre": playlist.nombre,
            "eliminacion_programada": tiene_eliminar_pendiente,
            "posicion_actual": posicion_real,  # 👈 Enviará 1 o -1 de forma exacta según tu registro
            "total_playlist": max(1, total_playlist)
        })
        
    return JsonResponse({
        "ok": True,
        "playlist_nombre": playlist.nombre,
        "canciones": canciones
    })


@login_required
@require_POST
def planificar_eliminacion_lote_ajax(request):
    """
    Fábrica masiva de eliminaciones con un id_lote unificado para toda la cesta de la interfaz.
    """
    try:
        data = json.loads(request.body)
        cesta = data.get('cesta', [])
        
        if not cesta:
            return JsonResponse({'ok': False, 'error': 'La cesta de eliminación está vacía.'}, status=400)
        
        id_lote_global = uuid.uuid4()
        
        duplicados_omitidos = 0
        errores_colision = 0
        tareas_creadas_contador = 0
        logs_copiado = {}

        for item in cesta:
            relacion_id = item.get('relacion_id')
            playlist_id = item.get('playlist_id')
            fecha_str = item.get('fecha')

            if not relacion_id or not playlist_id or not fecha_str:
                continue

            try:
                with transaction.atomic():
                    relacion = PlaylistCancion.objects.select_related('cancion', 'playlist').get(
                        id_relacion=relacion_id, 
                        playlist_id=playlist_id
                    )

                    if relacion.estado not in ["activo", "pendiente"]:
                        continue  

                    track_id = relacion.cancion.id_spotify
                    if track_id not in logs_copiado:
                        logs_copiado[track_id] = {
                            'nombre': relacion.cancion.nombre,
                            'artistas': relacion.cancion.artistas,
                            'destinos': []
                        }

                    fecha_ejecucion = datetime.strptime(fecha_str, '%Y-%m-%d')
                    fecha_dt = timezone.make_aware(fecha_ejecucion, timezone.get_current_timezone())

                    if Tarea.objects.filter(relacion=relacion, tipo='Eliminar', estado='Pendiente').exists():
                        duplicados_omitidos += 1
                        continue

                    if relacion.estado == 'pendiente':
                        tarea_agregar = Tarea.objects.filter(relacion=relacion, tipo='Agregar', estado='Pendiente').first()
                        if tarea_agregar and fecha_dt.date() <= tarea_agregar.fecha_ejecucion.date():
                            mensaje_error = (
                                f"No se pudo programar: La canción '{relacion.cancion.nombre}' está PENDIENTE "
                                f"porque se agregará el {tarea_agregar.fecha_ejecucion.strftime('%d/%m/%Y')} "
                                f"en '{relacion.playlist.nombre}'."
                            )
                            messages.error(request, mensaje_error)
                            errores_colision += 1
                            continue  

                    # Usamos directamente el id_lote_global para romper la separación de playlists
                    tarea_eliminar = Tarea.objects.create(
                        relacion=relacion,
                        tipo="Eliminar",
                        posicion=None,
                        estado="Pendiente",
                        fecha_ejecucion=fecha_dt,
                        usuario=request.user,
                        id_lote=id_lote_global
                    )
                    
                    tareas_creadas_contador += 1
                    
                    url_playlist = f"https://open.spotify.com/playlist/{relacion.playlist.id_spotify}"
                    logs_copiado[track_id]['fecha_ejecucion'] = tarea_eliminar.fecha_ejecucion.strftime('%d/%m/%Y')
                    logs_copiado[track_id]['destinos'].append(f"- Playlist: {relacion.playlist.nombre} | URL: {url_playlist}")

            except PlaylistCancion.DoesNotExist:
                continue
            except Exception as e:
                print(f"Error procesando item del lote: {str(e)}")
                continue

        # DISPARO DE MENSAJES CONSOLIDADOS (Sin cambios)
        for t_id, info in logs_copiado.items():
            if info['destinos']:
                destinos_str = " | ".join(info['destinos'])
                data_copiar_lote = (
                    f"Tarea de Syncronify por Lote (Eliminar) | "
                    f"Canción: {info['nombre']} | "
                    f"Artista: {info['artistas']} | "
                    f"Ejecución: {info['fecha_ejecucion']} | "
                    f"Destinos: | {destinos_str}"
                )
                messages.success(
                    request,
                    f"La tarea de Eliminación por lote para '{info['nombre']}' se programó en {len(info['destinos'])} playlists. [[[{data_copiar_lote}]]]"
                )

        return JsonResponse({
            "ok": True,
            "tareas_creadas": tareas_creadas_contador,
            "duplicados_omitidos": duplicados_omitidos,
            "errores_colision": errores_colision
        })

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Fallo crítico en el motor por lote: {str(e)}"}, status=500)
    

@login_required
@require_POST
def planificar_posicionamiento_lote_ajax(request):
    """
    Fábrica masiva de posicionamientos con consolidación de destinos y posiciones específicas por canción.
    """
    try:
        data = json.loads(request.body)
        cesta = data.get('cesta', [])
        
        if not cesta:
            return JsonResponse({'ok': False, 'error': 'La cesta de posicionamiento está vacía.'}, status=400)
        
        id_lote_global = str(uuid.uuid4())
        errores_colision = 0
        tareas_creadas_contador = 0

        # Diccionario para agrupar: { cancion_id: { ... 'destinos': [] } }
        logs_copiado = {}

        for item in cesta:
            relacion_id = item.get('relacion_id')
            playlist_id = item.get('playlist_id')
            fecha_str = item.get('fecha')
            nueva_posicion = item.get('nueva_posicion')

            if not relacion_id or not playlist_id or not fecha_str or nueva_posicion is None:
                continue

            try:
                with transaction.atomic():
                    relacion = PlaylistCancion.objects.select_related('cancion', 'playlist').get(
                        id_relacion=relacion_id, 
                        playlist_id=playlist_id
                    )

                    if relacion.estado not in ["activo", "pendiente"]:
                        continue  

                    track_id = relacion.cancion.id_spotify
                    if track_id not in logs_copiado:
                        logs_copiado[track_id] = {
                            'nombre': relacion.cancion.nombre,
                            'artistas': relacion.cancion.artistas,
                            'destinos': []
                        }

                    fecha_ejecucion = datetime.strptime(fecha_str, '%Y-%m-%d')
                    fecha_dt = timezone.make_aware(fecha_ejecucion, timezone.get_current_timezone())

                    if relacion.estado == 'pendiente':
                        tarea_agregar = Tarea.objects.filter(relacion=relacion, tipo='Agregar', estado='Pendiente').first()
                        if tarea_agregar and fecha_dt.date() <= tarea_agregar.fecha_ejecucion.date():
                            continue

                    tarea_eliminar = Tarea.objects.filter(relacion=relacion, tipo='Eliminar', estado='Pendiente').first()
                    if tarea_eliminar and fecha_dt.date() >= tarea_eliminar.fecha_ejecucion.date():
                        continue

                    posicion_int = int(nueva_posicion)
                    total_activas = PlaylistCancion.objects.filter(
                        playlist_id=playlist_id, 
                        estado='activo'
                    ).count()
                    
                    limite_maximo = max(1, total_activas)

                    if posicion_int < 1 or posicion_int > limite_maximo:
                        continue

                    tarea_posicionar = Tarea.objects.create(
                        relacion=relacion,
                        tipo="Posicionar",
                        posicion=posicion_int,
                        estado="Pendiente",
                        fecha_ejecucion=fecha_dt,
                        usuario=request.user,
                        id_lote=id_lote_global
                    )
                    
                    tareas_creadas_contador += 1
                    
                    url_playlist = f"https://open.spotify.com/playlist/{relacion.playlist.id_spotify}"
                    logs_copiado[track_id]['fecha_ejecucion'] = tarea_posicionar.fecha_ejecucion.strftime('%d/%m/%Y')
                    # Guardamos la URL y también a qué posición se movió en este destino específico
                    logs_copiado[track_id]['destinos'].append(
                        f"- Playlist: {relacion.playlist.nombre} (Posición: {posicion_int}) | URL: {url_playlist}"
                    )

            except PlaylistCancion.DoesNotExist:
                continue
            except Exception as e:
                print(f"Error procesando item de posicionamiento: {str(e)}")
                continue

        # DISPARO DE MENSAJES CONSOLIDADOS
        for t_id, info in logs_copiado.items():
            if info['destinos']:
                destinos_str = " | ".join(info['destinos'])
                data_copiar_lote = (
                    f"Tarea de Syncronify por Lote (Posicionar) | "
                    f"Canción: {info['nombre']} | "
                    f"Artista: {info['artistas']} | "
                    f"Ejecución: {info['fecha_ejecucion']} | "
                    f"Destinos: | {destinos_str}"
                )
                messages.success(
                    request,
                    f"La tarea de Posicionamiento por lote para '{info['nombre']}' se programó en {len(info['destinos'])} playlists. [[[{data_copiar_lote}]]]"
                )

        return JsonResponse({
            "ok": True,
            "tareas_creadas": tareas_creadas_contador,
            "errores_colision": errores_colision
        })

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Fallo crítico en el motor de lote: {str(e)}"}, status=500)