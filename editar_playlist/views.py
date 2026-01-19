# editar_playlist/views.py
import json
from django.shortcuts import render, get_object_or_404
from playlists.models import Playlist, PlaylistCancion

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

        canciones.append({
            "id": c.id_cancion,
            "titulo": c.nombre,
            "artistas": c.artistas,
            "album": c.album,
            "duracion": duracion_str,
            "fecha_agregado": rel.fecha_agregado.isoformat() if rel.fecha_agregado else None,
            "posicion": rel.posicion,
            "cover_url": getattr(c, "cover_url", None),
        })

    return render(request, "editar_playlist/editar_playlist.html", {
        "playlist": playlist,
        "canciones": canciones,
        "canciones_json": json.dumps(canciones, ensure_ascii=False), 
    })