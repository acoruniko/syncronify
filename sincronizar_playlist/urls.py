from django.urls import path
from . import views

urlpatterns = [
    path("tareas/", views.sincronizar_playlist_home, name="sincronizar_playlist_home"),
    path("tareas/<int:tarea_id>/eliminar/", views.eliminar_tarea, name="eliminar_tarea"),
    path("tareas/<int:playlist_id>/<int:tarea_id>/sincronizar/", views.sincronizar_tarea, name="sincronizar_tarea"),
]
