from django.utils import timezone
from celery import shared_task
from playlists.models import Tarea
from conexion.models import CredencialesSpotify
from django.db.models import F
from .services import execute_tarea

@shared_task(bind=True, max_retries=5, default_retry_delay=300)
def process_tarea(self, tarea_id, ejecutar_como_lote=False):
    try:
        # Pasa la bandera tal cual lo hace tu botón manual
        estado, _, _ = execute_tarea(tarea_id, source="celery", ejecutar_como_lote=ejecutar_como_lote)
        return estado

    except Exception as exc:
        try:
            tarea = Tarea.objects.get(id_tarea=tarea_id)
            tarea.estado = "Error temporal"
            tarea.mensaje_error = str(exc)
            tarea.intentos = F("intentos") + 1
            tarea.save(update_fields=["estado", "mensaje_error", "intentos"])
        except Tarea.DoesNotExist:
            pass
        
        raise self.retry(exc=exc)


@shared_task
def process_due_tasks():
    now = timezone.now()

    # 1. Escudo de Rate Limit Global
    cred = CredencialesSpotify.objects.first()
    if cred and cred.rate_limit_until and cred.rate_limit_until > now:
        Tarea.objects.filter(
            estado__in=["Pendiente", "Error temporal"],
            fecha_ejecucion__lte=now
        ).update(
            estado="Reprogramada",
            mensaje_error=f"Rate limit activo hasta {cred.rate_limit_until}",
            intentos=F("intentos") + 1
        )
        return

    # 2. Traer TODO lo que esté vencido o toque ejecutar YA
    pendientes = Tarea.objects.filter(
        estado__in=["Pendiente", "Error temporal", "Reprogramada"],
        fecha_ejecucion__lte=now
    ).order_by('id_tarea')

    # 3. Encolar sin mente: Si tiene lote, va como lote.
    for tarea in pendientes:
        id_lote_actual = getattr(tarea, "id_lote", None)
        
        # Copia exacta del comportamiento del botón manual:
        # Si la fila tiene un id_lote en la BD, se manda con True. Si no, con False.
        tiene_lote = True if id_lote_actual else False
        
        process_tarea.delay(tarea.id_tarea, ejecutar_como_lote=tiene_lote)