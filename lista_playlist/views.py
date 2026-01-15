# lista_playlist/views.py
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from playlists.models import Playlist
from conexion.models import CredencialesSpotify

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