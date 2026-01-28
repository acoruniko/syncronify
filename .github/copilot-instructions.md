# Guía para agentes IA — Syncronify

Objetivo corto
- Ayudar a un agente IA a entender rápidamente la arquitectura, convenciones y flujos operativos del proyecto para producir cambios seguros y consistentes.

Arquitectura (visión general)
- Proyecto Django mono-repo con apps por responsabilidad: `conexion` (integraciones con Spotify, credenciales, logs), `playlists` (modelo de playlists, canciones y tareas), `sincronizar_playlist` (lógica de ejecución de tareas y endpoints), `importar`, `editar_playlist`, `lista_playlist`, `usuarios`.
- Archivo principal de configuración: [syncronify/settings.py](syncronify/settings.py#L1).
- Flujo clave: acciones del usuario → se crea una `Tarea` (modelo en `playlists.models`) → `sincronizar_playlist.views` toma la tarea, llama a servicios en `conexion.services` → llama a la API de Spotify con token obtenido desde `CredencialesSpotify` o desde `conexion.spotify` según el caso.

Puntos de integración críticos
- Credenciales y tokens: modelo `CredencialesSpotify` en `conexion/models.py` y helpers en `conexion/services.py` y `conexion/auth.py`.
- Token de aplicación (client credentials) se maneja y cachea en `conexion/spotify.py` (función `get_token`).
- Todas las llamadas a la API de Spotify se hacen con `requests` y esperan `timeout` cortos (ej. 10–12s). Ver ejemplos en `sincronizar_playlist/views.py`.
- Rate limiting: `CredencialesSpotify.rate_limit_until` se comprueba antes de ejecutar tareas; si está activo, las vistas devuelven JSON con `rate_limited` y `seconds_remaining`.

Convenciones de código y patrones observables
- Idioma: mensajes y tipos de tareas usan español (`'posicionar'`, `'eliminar'`, `'agregar'`). Normalizar cadenas con `.strip().lower()` antes de comparar (ejemplo en `sincronizar_playlist/views.py`).
- Timezones: usar `django.utils.timezone` y datetimes con `tzinfo` (ver `sincronizar_playlist.views.sincronizar_playlist_home`).
- Actualizaciones masivas en BD: usan `QuerySet.update()` + `F()` para desplazar posiciones sin cargar filas (ver reordenado en `sincronizar_playlist/views.py`).
- Manejo de errores: vistas usan `messages` para notificar al usuario y devuelven `JsonResponse` con `{ok: False, error: ...}`.

Variables de entorno y ejecución
- Variables esperadas (definidas en `settings.py`): `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`, `SPOTIFY_API_BASE`, `SPOTIFY_TOKEN_URL`, `SPOTIFY_AUTH_URL`. Se leen con `environ` y `.env` en el `BASE_DIR`.
- Desarrollo local: instalar dependencias `pip install -r requirements.txt` y ejecutar `python manage.py runserver` (README). Migraciones estándar con `python manage.py migrate` y tests con `python manage.py test`.

Ejemplos concretos (usar exactamente estas rutas/imports)
- Obtener token y llamar API (usar `conexion.services.get_spotify_token()` cuando la acción implique usuario maestro):

```py
from conexion.services import get_spotify_token
token = get_spotify_token()
headers = {"Authorization": f"Bearer {token}"}
resp = requests.get("https://api.spotify.com/v1/me", headers=headers, timeout=12)
```

- Para token de aplicación (client credentials) ver `conexion/spotify.py::get_token()` (cache en `_token_cache`).

Qué evitar o tener en cuenta
- No asumir que la DB local es sqlite: `settings.py` está configurado para PostgreSQL. Verifica entorno antes de ejecutar migraciones.
- Evitar llamadas largas sin `timeout` y respetar `rate_limit_until` en `CredencialesSpotify`.
- Los tipos de tarea son strings en la BD; comparar siempre en minúscula y sin espacios.

Archivos relevantes (ejemplos rápidos)
- `syncronify/settings.py` — configuración, env vars.
- `conexion/models.py`, `conexion/services.py`, `conexion/auth.py`, `conexion/spotify.py` — token, refresh y logs.
- `playlists/models.py` — `Tarea`, `PlaylistCancion`, `Playlist`, `Cancion`.
- `sincronizar_playlist/views.py` — lógica de ejecución, reordenado, rate-limit handling.

Pruebas y flujo de desarrollo
- Tests por app: cada app incluye un `tests.py`. Ejecutar `python manage.py test`.
- Herramientas incluidas: `django_extensions` está en `INSTALLED_APPS` (útil: `shell_plus`, `runserver_plus`).

Si algo falta o es ambiguo
- Pide acceso a `conexion/models.py` y `playlists/models.py` si necesitas confirmar campos (p. ej. `rate_limit_until`, `id_spotify`, `posicion`).
- Pregunta si quieres que incluya snippets de refactor o tests unitarios para `sincronizar_playlist`.

-- Fin de la guía rápida
