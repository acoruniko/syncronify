from django.urls import path
from .views import editar_playlist_home, crear_tarea, eliminar_tarea, obtener_tareas, actualizar_playlist_spotify_view
from . import views

urlpatterns = [
    # 📌 1. ENDPOINTS PARA ELIMINACIÓN MÚLTIPLE (Puestos arriba para evitar conflictos de captura)
    path('eliminar-multiple/', views.eliminar_tareas_multiples_home, name='eliminar_tareas_multiples_home'),
    path('eliminar-multiple/obtener-canciones/<int:playlist_id>/', views.obtener_canciones_playlist_eliminar_ajax, name='obtener_canciones_playlist_eliminar_ajax'),
    path('eliminar-multiple/planificar-lote/', views.planificar_eliminacion_lote_ajax, name='planificar_eliminacion_lote_ajax'),

    path('posicionar-multiple/', views.posicionar_tareas_multiples_home, name='posicionar_tareas_multiples_home'),
    path('editar_playlist/posicionar-multiple/guardar/', views.planificar_posicionamiento_lote_ajax, name='planificar_posicionamiento_lote_ajax'
    ),

    # 📌 2. ENTORNO AGREGAR MÚLTIPLEEXISTENTE
    path('agregar-multiple/', views.agregar_tareas_multiples_home, name='agregar_tareas_multiples_home'),
    path('agregar-multiple/destinos/', views.seleccionar_destinos_lote_view, name='seleccionar_destinos_lote'),
    path('consultar-track/', views.consultar_track_spotify_ajax, name='consultar_track_spotify_ajax'),
    path('buscar-local/', views.buscar_cancion_local_ajax, name='buscar_cancion_local_ajax'),
    path("playlist/lote/planificar/", views.planificar_tareas_lote_ajax, name="planificar_tareas_lote_ajax"),

    # 📌 3. RUTAS CORE Y DINÁMICAS POR PLAYLIST
    path('<int:playlist_id>/', editar_playlist_home, name='editar_playlist_home'),
    path('<int:playlist_id>/relacion/<int:relacion_id>/tareas/', obtener_tareas, name='obtener_tareas'),
    path('<int:playlist_id>/tarea/crear/', crear_tarea, name='crear_tarea'),
    path('<int:playlist_id>/tarea/<int:tarea_id>/eliminar/', eliminar_tarea, name='eliminar_tarea'),
    path("<int:playlist_id>/canciones/", views.obtener_canciones, name="obtener_canciones"),
    path("<int:playlist_id>/actualizar-spotify/", actualizar_playlist_spotify_view, name="actualizar_playlist_spotify"),
    path("agregar_cancion/<int:playlist_id>/", views.agregar_cancion, name="agregar_cancion"),

    # 📌 4. MÓDULOS DE GÉNEROS Y MENSAJES
    path("mensajes_bar/", views.mensajes_bar, name="mensajes_bar"),
    path("generos/crear/", views.crear_genero_ajax, name="crear_genero_ajax"),
    path("generos/eliminar/<int:id_genero>/", views.eliminar_genero_ajax, name="eliminar_genero_ajax"),
    path('lista_playlist/ajax/asociar-genero/', views.asociar_genero_ajax, name='asociar_genero_ajax'),
    path('lista_playlist/ajax/desasociar-genero/', views.desasociar_genero_ajax, name='desasociar_genero_ajax'),
]