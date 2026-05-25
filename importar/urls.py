from django.urls import path
from . import views

urlpatterns = [
    path("playlists/", views.importar_playlists, name="importar_playlists"),
    path("<str:playlist_id>/confirmar/", views.importar_playlist_confirmar, name="importar_playlist_confirmar"),

    path("generos/crear/", views.crear_genero_ajax, name="crear_genero_ajax"),
    path("generos/eliminar/<int:id_genero>/", views.eliminar_genero_ajax, name="eliminar_genero_ajax"),
]