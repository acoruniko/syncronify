# lista_playlist/views.py
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.utils import timezone
from playlists.models import Playlist
from conexion.models import CredencialesSpotify
from django.contrib import messages
from django.http import JsonResponse



@login_required
def lista_playlist_home(request):
    # 1. Obtener playlists almacenadas en la BD (todas, sin filtrar por usuario)
    playlists = Playlist.objects.all()

    # 2. Verificar si hay rate limit activo
    cred = CredencialesSpotify.objects.first()
    rate_limited = False
    seconds_remaining = 0

    if cred and cred.rate_limit_until and cred.rate_limit_until > timezone.now():
        rate_limited = True
        seconds_remaining = int((cred.rate_limit_until - timezone.now()).total_seconds())

    # 3. Renderizar con el template
    return render(request, "lista_playlist/home.html", {
        "playlists": playlists,
        "rate_limited": rate_limited,
        "seconds_remaining": seconds_remaining,
    })


@require_POST
def eliminar_playlist(request, playlist_id):
    playlist = get_object_or_404(Playlist, id_playlist=playlist_id)
    nombre = playlist.nombre
    playlist.delete()
    messages.success(request, f'Playlist "{nombre}" eliminada correctamente.')
    return JsonResponse({'status': 'ok'})
