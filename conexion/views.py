# conexion/views.py
from django.shortcuts import redirect
from django.contrib import messages

from django.http import HttpResponse
from .auth import build_authorize_url, exchange_code_for_tokens

def login_spotify(request):
    # Por defecto, si no se especifica, volvemos a lista_playlist
    url = build_authorize_url(state="lista_playlist")
    return redirect(url)


def spotify_callback(request):
    code = request.GET.get("code")
    error = request.GET.get("error")
    state = request.GET.get("state", "lista_playlist")  # 👈 capturamos el state

    if error:
        messages.error(request, f"Spotify error: {error}")
        return redirect("lista_playlist_home")

    if not code:
        messages.error(request, "No se recibió el código de autorización de Spotify")
        return redirect("lista_playlist_home")

    try:
        exchange_code_for_tokens(code)
        messages.success(request, "Conexión con Spotify establecida correctamente")

        # ✅ Redirigir según el state
        if state.startswith("editar_playlist:"):
            playlist_id = state.split(":")[1]
            return redirect("editar_playlist_home", playlist_id=playlist_id)
        elif state == "importar_playlists":
            return redirect("importar_playlists")
        elif state == "sincronizar_playlist":
            return redirect("sincronizar_playlist_home")
        else:
            return redirect("lista_playlist_home")

    except Exception as e:
        messages.error(request, f"No se pudo conectar con Spotify: {str(e)}")
        return redirect("lista_playlist_home")


def kick_callback(request):
    code = request.GET.get("code")
    error = request.GET.get("error")
    print("Kick callback recibido:", code, error)  # 👈 imprime en consola
    return HttpResponse(f"Kick code: {code}, error: {error}")

