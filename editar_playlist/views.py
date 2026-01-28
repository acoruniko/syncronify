# editar_playlist/views.py
import json
from datetime import datetime
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from usuarios.models import Usuario
from django.contrib import messages
from django.shortcuts import render
from datetime import timedelta
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token
from playlists.models import Playlist, Cancion, PlaylistCancion, Tarea
import requests
from django.views.decorators.http import require_GET

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

    # ⚠️ Verificar rate limit
    cred = CredencialesSpotify.objects.first()
    rate_limited = False
    seconds_remaining = 0

    if cred and cred.rate_limit_until and cred.rate_limit_until > timezone.now():
        rate_limited = True
        seconds_remaining = int((cred.rate_limit_until - timezone.now()).total_seconds())
        messages.warning(
            request,
            f"Muchas peticiones a la API de Spotify. Espera {seconds_remaining} segundos."
        )

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
    messages.success(request, "Tarea agregada correctamente")

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
        return JsonResponse({"ok": False, "error": "Método no permitido"})

    try:
        url = request.POST.get("url")
        posicion = request.POST.get("posicion")
        fecha = request.POST.get("fecha")

        if not url or not posicion or not fecha:
            messages.error(request, "Datos incompletos para agregar canción")
            return JsonResponse({"ok": False, "error": "Datos incompletos"})

        # 1. Extraer track ID
        track_id = None
        if "/track/" in url:
            try:
                track_id = url.split("/track/")[1].split("?")[0]
            except Exception:
                pass

        if not track_id:
            messages.error(request, "La URL de la canción no es válida")
            return JsonResponse({"ok": False, "error": "URL inválida"})

        # 2. Obtener credenciales
        cred = CredencialesSpotify.objects.first()
        if not cred:
            messages.error(request, "No hay credenciales de Spotify configuradas")
            return JsonResponse({"ok": False, "error": "No hay credenciales de Spotify"})

        # Verificar rate limit
        if cred.rate_limit_until and cred.rate_limit_until > timezone.now():
            seconds_remaining = int((cred.rate_limit_until - timezone.now()).total_seconds())
            messages.warning(request, f"Rate limit activo. Espera {seconds_remaining} segundos.")
            return JsonResponse({
                "ok": False,
                "error": f"Rate limit activo. Espera {seconds_remaining} segundos."
            })

        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}

        # 3. Llamar a la API de Spotify
        resp = requests.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers=headers,
            timeout=12
        )

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 30))
            cred.rate_limit_until = timezone.now() + timedelta(seconds=retry_after)
            cred.save()
            messages.warning(request, f"Rate limit. Intenta en {retry_after} segundos.")
            return JsonResponse({
                "ok": False,
                "error": f"Rate limit. Intenta en {retry_after} segundos."
            })

        if resp.status_code != 200:
            messages.error(request, "La API de Spotify no devolvió datos")
            return JsonResponse({"ok": False, "error": "La API no devolvió datos"})

        data = resp.json()

        # 4. Crear canción si no existe
        cover_url = None
        if data["album"].get("images"):
            cover_url = data["album"]["images"][0]["url"]

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

        # 5. Crear relación en estado pendiente
        playlist = Playlist.objects.get(id_playlist=playlist_id)
        agregado_por=request.user.username

        relacion = PlaylistCancion.objects.create(
            playlist=playlist,
            cancion=cancion_obj,
            posicion=None,                  # 👈 posición inválida hasta que se ejecute la tarea
            fecha_agregado=timezone.now(),
            agregado_por=agregado_por,      # 👈 correcto según API (si disponible)
            estado="pendiente"              # 👈 nuevo campo
        )

        # 6. Crear tarea automática
        Tarea.objects.create(
            relacion=relacion,
            tipo="Agregar",
            posicion=posicion,              # 👈 aquí sí guardamos la posición solicitada
            estado="Pendiente",
            fecha_ejecucion=fecha,
            usuario=request.user,
            url_cancion=url
        )

        # 👉 mensaje de éxito
        messages.success(request, f"Canción '{cancion_obj.nombre}' registrada como pendiente en la playlist")

        return JsonResponse({
            "ok": True,
            "relacion_id": relacion.id_relacion
        })

    except Exception as e:
        messages.error(request, f"Error al agregar canción: {str(e)}")
        return JsonResponse({"ok": False, "error": str(e)})



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
    tarea.delete()

    # 👉 mensaje en la barra superior
    messages.success(request, "Tarea eliminada correctamente")

    return JsonResponse({"ok": True})