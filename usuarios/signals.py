# usuarios/signals.py
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from django.utils import timezone
from .models import Sesion

@receiver(user_logged_in)
def registrar_sesion(sender, request, user, **kwargs):
    Sesion.objects.create(
        id_usuario=user.id_usuario, 
        token_sesion=request.session.session_key,
        ip_origen=request.META.get("REMOTE_ADDR")
    )

@receiver(user_logged_out)
def cerrar_sesion(sender, request, user, **kwargs):
    Sesion.objects.filter(
        id_usuario=user.id_usuario,
        token_sesion=request.session.session_key,
        estado="activo"
    ).update(
        fecha_cierre=timezone.now(),
        estado="cerrada"
    )