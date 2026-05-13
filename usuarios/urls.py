from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"), 
    path('registro/', views.registro_view, name='registro'),
    path('perfil/', views.editar_perfil_view, name='editar_perfil'),
]