from django.urls import path
from .views import editar_playlist_home

urlpatterns = [
    path('<int:playlist_id>/', editar_playlist_home, name='editar_playlist_home'),
]
