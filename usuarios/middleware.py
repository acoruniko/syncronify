# usuarios/middleware.py
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth import logout
from .models import Sesion

class ValidarSesionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Solo validar si el usuario está autenticado
        if request.user.is_authenticated:
            sesion_valida = Sesion.objects.filter(
                id_usuario=request.user.id_usuario,
                token_sesion=request.session.session_key,
                estado="activo"
            ).exists()

            if not sesion_valida:
                logout(request)
                messages.error(request, "Tu sesión fue cerrada porque iniciaste sesión en otro dispositivo")
                return redirect("login")

        return self.get_response(request)
