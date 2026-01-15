# conexion/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("login_spotify/", views.login_spotify, name="login_spotify"),
    path("spotify_callback/", views.spotify_callback, name="spotify_callback"),
]