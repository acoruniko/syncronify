# lista_playlist/views.py
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.utils import timezone
from playlists.models import Playlist
from conexion.models import CredencialesSpotify
from conexion.services import check_rate_limit
from django.contrib import messages
from django.http import JsonResponse


@login_required
def lista_playlist_home(request):
    # 1. Obtener playlists almacenadas en la BD
    playlists = Playlist.objects.all()

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
        "rate_limited": rate_limited,
        "seconds_remaining": seconds_remaining or 0,
    })



@require_POST
def eliminar_playlist(request, playlist_id):
    playlist = get_object_or_404(Playlist, id_playlist=playlist_id)
    nombre = playlist.nombre
    playlist.delete()
    messages.success(request, f'Playlist "{nombre}" eliminada correctamente.')
    return JsonResponse({'status': 'ok'})
