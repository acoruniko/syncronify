# conexion/views.py
from django.shortcuts import redirect
from django.contrib import messages
from django.http import HttpResponse
from .auth import build_authorize_url, exchange_code_for_tokens

def login_spotify(request):
    url = build_authorize_url(state="lista_playlist")
    return redirect(url)


def spotify_callback(request):
    code = request.GET.get("code")
    error = request.GET.get("error")

    if error:
        messages.error(request, f"Spotify error: {error}")
        return redirect("lista_playlist_home")

    if not code:
        messages.error(request, "No se recibió el código de autorización de Spotify")
        return redirect("lista_playlist_home")

    try:
        exchange_code_for_tokens(code)
        messages.success(request, "Conexión con Spotify establecida correctamente")
        # ✅ Si la conexión es positiva → ir directo a importar
        return redirect("importar_playlists")
    except Exception as e:
        messages.error(request, f"No se pudo conectar con Spotify: {str(e)}")
        # ❌ Si falla → volver a lista_playlist
        return redirect("lista_playlist_home")
    
    