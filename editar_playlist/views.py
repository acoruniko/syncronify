# editar_playlist/views.py
import json
from datetime import datetime
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from playlists.models import Playlist, PlaylistCancion, Tarea
from usuarios.models import Usuario
from django.contrib import messages
from django.shortcuts import render

def mensajes_bar(request):
    return render(request, "partials/mensajes_bar.html")


def editar_playlist_home(request, playlist_id):
    playlist = get_object_or_404(Playlist, id_playlist=playlist_id)

    relaciones = (
        PlaylistCancion.objects
        .filter(playlist_id=playlist_id)
        .select_related('cancion')
        .order_by('posicion')
    )

    canciones = []
    for rel in relaciones:
        c = rel.cancion
        duracion_str = None
        if c.duracion_ms:
            minutos = c.duracion_ms // 60000
            segundos = (c.duracion_ms % 60000) // 1000
            duracion_str = f"{minutos}:{segundos:02d}"

        # 👉 ahora con usuario gracias al FK
        tareas_qs = (
            Tarea.objects.filter(relacion=rel)
            .select_related("usuario")  # ya funciona porque el modelo tiene FK
            .order_by('-fecha_creacion')
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

    return render(request, "editar_playlist/editar_playlist.html", {
        "playlist": playlist,
        "canciones": canciones,
        "canciones_json": json.dumps(canciones, ensure_ascii=False, cls=DjangoJSONEncoder),
    })

from django.views.decorators.http import require_GET


@require_GET
def obtener_tareas(request, playlist_id, relacion_id):
    relacion = get_object_or_404(PlaylistCancion, id_relacion=relacion_id, playlist_id=playlist_id)

    tareas_qs = (
        Tarea.objects.filter(relacion=relacion)
        .select_related("usuario")
        .order_by('-fecha_creacion')
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

    try:
        fecha_ejecucion = datetime.strptime(fecha_str, '%Y-%m-%d')
        fecha_ejecucion = timezone.make_aware(fecha_ejecucion, timezone.get_current_timezone())
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Fecha inválida'}, status=400)

    tarea = Tarea(
        relacion=relacion,
        tipo=tipo,
        estado='pendiente',
        fecha_ejecucion=fecha_ejecucion,
        usuario=request.user if request.user.is_authenticated else None
    )

    if tipo == 'posicionar':
        if not posicion:
            return JsonResponse({'ok': False, 'error': 'Posición requerida'}, status=400)
        tarea.posicion = int(posicion)
    elif tipo == 'eliminar':
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
def eliminar_tarea(request, playlist_id, tarea_id):
    if request.method != 'POST':
        return JsonResponse({"ok": False, "error": "Método no permitido"}, status=405)

    tarea = get_object_or_404(Tarea, id_tarea=tarea_id, relacion__playlist_id=playlist_id)
    tarea.delete()

    # 👉 mensaje en la barra superior
    messages.success(request, "Tarea eliminada correctamente")

    return JsonResponse({"ok": True})