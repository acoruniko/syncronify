from celery import shared_task
from django.utils import timezone
from playlists.models import Playlist, Cancion, PlaylistCancion, Tarea
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token
from .services import execute_tarea
from django.db.models import F

# 👉 Ejecuta una tarea individual
@shared_task(bind=True, max_retries=5, default_retry_delay=300)  # 5 min entre reintentos
def process_tarea(self, tarea_id):
    try:
        return execute_tarea(tarea_id)

    except Exception as exc:
        # 👉 Registrar el intento y el error en la BD
        tarea = Tarea.objects.get(id_tarea=tarea_id)
        tarea.estado = "Error temporal"
        tarea.mensaje_error = str(exc)
        tarea.intentos = F("intentos") + 1
        tarea.save(update_fields=["estado", "mensaje_error", "intentos"])

        # 👉 Luego dejar que Celery reprograme el reintento
        raise self.retry(exc=exc)



# 👉 Revisa las tareas pendientes del día y las encola
@shared_task
def process_due_tasks():
    now = timezone.now()

    # Verificar si hay rate limit global en las credenciales
    cred = CredencialesSpotify.objects.first()
    if cred and cred.rate_limit_until and cred.rate_limit_until > now:
        # Si todavía estamos en rate limit, marcar todas las tareas del día como Reprogramadas
        Tarea.objects.filter(
            estado__in=["Pendiente", "Error temporal"],
            fecha_ejecucion__date=now.date()
        ).update(
            estado="Reprogramada",
            mensaje_error=f"Rate limit activo hasta {cred.rate_limit_until}",
            intentos=F("intentos") + 1
        )
        return

    # Si no hay rate limit, encolar todas las tareas del día que no estén completadas
    pendientes = Tarea.objects.filter(
        estado__in=["Pendiente", "Error temporal", "Reprogramada"],
        fecha_ejecucion__date=now.date()
    )

    for tarea in pendientes:
        process_tarea.delay(tarea.id_tarea)
