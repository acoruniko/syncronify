# lista_playlist/views.py
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.utils import timezone
from playlists.models import Playlist, Genero  # 🚀 Mantenemos la importación de Genero
from conexion.models import CredencialesSpotify
from conexion.services import check_rate_limit
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Q

@login_required
def lista_playlist_home(request):
    # 1. Obtener playlists almacenadas en la BD y el catálogo de géneros
    playlists = Playlist.objects.all()
    generos = Genero.objects.all().order_by('nombre')  # 🚀 Esto alimenta tu nueva caja de filtros

    # 2. Verificar rate limit usando servicio de conexion
    cred = CredencialesSpotify.objects.first()
    seconds_remaining = None
    rate_limited = False

    if cred:
        seconds_remaining = check_rate_limit(request, cred, show_message=False)
        rate_limited = seconds_remaining is not None

    # 3. Renderizar con el template
    return render(request, "lista_playlist/home.html", {
        "playlists": playlists,
        "generos": generos,  # 🚀 Enviado al template limpio
        "rate_limited": rate_limited,
        "seconds_remaining": seconds_remaining or 0,
    })


# 🚀 TU VISTA ORIGINAL RESTAURADA (Para sanar el AttributeError):
@require_POST
def eliminar_playlist(request, playlist_id):
    playlist = get_object_or_404(Playlist, id_playlist=playlist_id)
    nombre = playlist.nombre
    playlist.delete()
    messages.success(request, f'Playlist "{nombre}" eliminada correctamente.')
    return JsonResponse({'status': 'ok'})

@login_required
def filtrar_playlists(request):
    """
    Endpoint síncrono/asíncrono para filtrar playlists por géneros y texto (canción/artista).
    Devuelve un fragmento HTML con las filas correspondientes.
    """
    # 1. Recuperar parámetros de la petición GET
    generos_str = request.GET.get("generos", "")
    buscar_texto = request.GET.get("q", "").strip()

    # Query base limpia
    queryset = Playlist.objects.all()

    # 2. Filtro A: Por Géneros (Si hay IDs seleccionados)
    if generos_str:
        try:
            generos_ids = [int(x) for x in generos_str.split(",") if x.isdigit()]
            if generos_ids:
                # Filtramos las playlists que tengan los géneros seleccionados
                queryset = queryset.filter(generos__id_genero__in=generos_ids)
        except ValueError:
            pass

    # 3. Filtro B: Por Cadena de Texto en Canciones o Artistas
    if buscar_texto:
        # Buscamos a través de la relación inversa que provee el modelo intermedio PlaylistCancion
        # Filtra si el nombre de la canción o el nombre del artista contiene el texto (case-insensitive)
        queryset = queryset.filter(
            Q(playlistcancion__cancion__nombre__icontains=buscar_texto) |
            Q(playlistcancion__cancion__artistas__icontains=buscar_texto)
        )

    # 4. Limpieza de duplicados generada por los Joins relacionales
    playlists_filtradas = queryset.distinct()

    # 5. Renderizar únicamente el pedazo de HTML que va dentro del contenedor central
    return render(request, "lista_playlist/partials/playlist_rows.html", {
        "playlists": playlists_filtradas
    })