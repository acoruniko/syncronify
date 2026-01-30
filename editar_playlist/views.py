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

    # 👉 Registrar mensaje en la barra superior
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
        if "/track/" in url:
            try:
                track_id = url.split("/track/")[1].split("?")[0]
            except Exception:
                pass

        if not track_id:
            messages.error(request, "La URL de la canción no es válida")
            return JsonResponse({"ok": False, "error": "URL inválida"}, status=400)

        # 2. Credenciales
        cred = check_credentials(request)
        if isinstance(cred, HttpResponseRedirect):
            return JsonResponse({
                "ok": False,
                "requires_auth": True,
                "auth_url": build_authorize_url(state=f"editar_playlist:{playlist_id}")
            }, status=401)


        # 3. Rate limit
        seconds_remaining = check_rate_limit(request, cred)
        if seconds_remaining:
            return JsonResponse({
                "ok": False,
                "error": f"Rate limit activo. Espera {seconds_remaining} segundos."
            }, status=429)

        # 4. Token
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}

        # 5. Llamada a la API
        resp = requests.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers=headers,
            timeout=12
        )
        retry_after = handle_429(resp, cred, request)
        if retry_after:
            return JsonResponse({
                "ok": False,
                "error": f"Muchas peticiones a la API de Spotify. Espera {retry_after} segundos antes de volver a intentar."
            }, status=429)

        if resp.status_code != 200:
            messages.error(request, "La API de Spotify no devolvió datos")
            return JsonResponse({"ok": False, "error": "La API no devolvió datos"}, status=500)

        data = resp.json()

        # 6. Crear canción si no existe
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

        # 7. Crear relación en estado pendiente
        playlist = Playlist.objects.get(id_playlist=playlist_id)
        relacion = PlaylistCancion.objects.create(
            playlist=playlist,
            cancion=cancion_obj,
            posicion=None,
            fecha_agregado=timezone.now(),
            agregado_por=request.user.username,
            estado="pendiente"
        )

        # 8. Crear tarea automática

        try:
            fecha_ejecucion = datetime.strptime(fecha, "%Y-%m-%d")
            fecha_ejecucion = timezone.make_aware(fecha_ejecucion, timezone.get_current_timezone())
        except ValueError:
            return JsonResponse({"ok": False, "error": "Fecha inválida"}, status=400)

        tarea = Tarea.objects.create(
            relacion=relacion,
            tipo="Agregar",
            posicion=int(posicion),  # 👈 aseguramos que sea entero
            estado="Pendiente",
            fecha_ejecucion=fecha_ejecucion,  # 👈 ahora es datetime
            usuario=request.user,
            url_cancion=url
        )

        messages.success(request, f"Canción '{cancion_obj.nombre}' registrada como pendiente en la playlist")
        messages.success(
            request,
            f"La tarea {tarea.tipo} de '{relacion.cancion.nombre}' "
            f"en la playlist '{relacion.playlist.nombre}' "
            f"para el {tarea.fecha_ejecucion.strftime('%d/%m/%Y')} "
            f"se agregó correctamente."
        )
  

        return JsonResponse({"ok": True, "relacion_id": relacion.id_relacion})

    except Exception as e:
        messages.error(request, f"Error al agregar canción: {str(e)}")
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

    tarea = get_object_or_404(Tarea, id_tarea=tarea_id, relacion__playlist_id=playlist_id)

    # Guardamos info antes de eliminar
    tipo = tarea.tipo
    cancion = tarea.relacion.cancion.nombre
    playlist_nombre = tarea.relacion.playlist.nombre
    fecha = tarea.fecha_ejecucion.strftime('%d/%m/%Y') if tarea.fecha_ejecucion else "sin fecha"

    tarea.delete()

    # 👉 mensaje más específico
    messages.success(
        request,
        f"La tarea {tipo} de '{cancion}' en la playlist '{playlist_nombre}' "
        f"para el {fecha} fue eliminada correctamente."
    )

    return JsonResponse({"ok": True})
