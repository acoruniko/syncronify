from django.urls import path
from .views import configuracion_home_view, actualizar_usuario_ajax, eliminar_usuario_ajax, obtener_tiempo_restante_ajax

urlpatterns = [
    path('', configuracion_home_view, name='configuracion_home'),
    path('actualizar-usuario/', actualizar_usuario_ajax, name='actualizar_usuario_ajax'),
    path('eliminar-usuario/', eliminar_usuario_ajax, name='eliminar_usuario_ajax'),
    path('obtener-tiempo-ajax/', obtener_tiempo_restante_ajax, name='obtener_tiempo_ajax'),
]