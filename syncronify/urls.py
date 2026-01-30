"""
URL configuration for syncronify project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from conexion.views import login_spotify, spotify_callback

urlpatterns = [
    path("admin/", admin.site.urls),
    path("usuarios/", include("usuarios.urls")),  # conecta las rutas de la app usuarios
    path("lista_playlist/", include("lista_playlist.urls")),
    path('editar_playlist/', include('editar_playlist.urls')),
    path("sincronizar/", include("sincronizar_playlist.urls")),
    path('logs/', include('logs.urls')),
    path("spotify/login", login_spotify, name="spotify_login"),
    path("spotify/callback", spotify_callback, name="spotify_callback"),
    path("conexion/", include("conexion.urls")),
    path("importar/", include("importar.urls")), 
    ]
