from django.urls import path
from . import views

urlpatterns = [
    path("home/", views.lista_playlist_home, name="lista_playlist_home"),
]