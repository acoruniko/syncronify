from django.urls import path
from .views import editar_playlist_home, crear_tarea, eliminar_tarea, obtener_tareas, actualizar_playlist_spotify_view
from . import views

urlpatterns = [
    path('<int:playlist_id>/', editar_playlist_home, name='editar_playlist_home'),
    path('<int:playlist_id>/relacion/<int:relacion_id>/tareas/', obtener_tareas, name='obtener_tareas'),
    path('<int:playlist_id>/tarea/crear/', crear_tarea, name='crear_tarea'),
    path('<int:playlist_id>/tarea/<int:tarea_id>/eliminar/', eliminar_tarea, name='eliminar_tarea'),
    path("mensajes_bar/", views.mensajes_bar, name="mensajes_bar"),
    path("agregar_cancion/<int:playlist_id>/", views.agregar_cancion, name="agregar_cancion"),
    path("<int:playlist_id>/canciones/", views.obtener_canciones, name="obtener_canciones"),
    path("<int:playlist_id>/actualizar-spotify/", actualizar_playlist_spotify_view, name="actualizar_playlist_spotify"),

    path("generos/crear/", views.crear_genero_ajax, name="crear_genero_ajax"),
    path("generos/eliminar/<int:id_genero>/", views.eliminar_genero_ajax, name="eliminar_genero_ajax"),

    path('lista_playlist/ajax/asociar-genero/', views.asociar_genero_ajax, name='asociar_genero_ajax'),
    path('lista_playlist/ajax/desasociar-genero/', views.desasociar_genero_ajax, name='desasociar_genero_ajax'),
]