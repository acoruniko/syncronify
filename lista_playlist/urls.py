from django.urls import path
from . import views

urlpatterns = [
    path("home/", views.lista_playlist_home, name="lista_playlist_home"),
    path("playlists/<int:playlist_id>/eliminar/", views.eliminar_playlist, name="eliminar_playlist"),
]
