from django.utils import timezone
from celery import shared_task
from playlists.models import Tarea
from conexion.models import CredencialesSpotify
from django.db.models import F
from .services import execute_tarea

@shared_task(bind=True, max_retries=5, default_retry_delay=300)
def process_tarea(self, tarea_id, ejecutar_como_lote=False):
    try:
        # Ejecución normal de tu orquestador principal
        estado, _, _ = execute_tarea(tarea_id, source="celery", ejecutar_como_lote=ejecutar_como_lote)
        return estado

    except Exception as exc:
        try:
            tarea = Tarea.objects.get(id_tarea=tarea_id)
            
            # Sincronización de Estados Celery <-> DB
            # Evaluamos si a Celery todavía le quedan cartuchos (intentos) disponibles
            if self.request.retries < self.max_retries:
                tarea.estado = "En espera de reintento"
                tarea.mensaje_error = f"Intento {self.request.retries + 1} fallido: {str(exc)}"
            else:
                # Si ya quemó los 5 intentos en los bloques de 5 minutos, pasa a Error definitivo
                tarea.estado = "Error"
                tarea.mensaje_error = f"Agotados los reintentos automáticos de Celery: {str(exc)}"
                
            tarea.intentos = self.request.retries + 1
            tarea.save(update_fields=["estado", "mensaje_error", "intentos"])
            
        except Tarea.DoesNotExist:
            pass
        
        # Lanza el reintento nativo de Celery (esperará los 300 segundos en la cola de Redis/Rabbit)
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

    # 2. Traer todo lo que toque ejecutar ya
    pendientes = Tarea.objects.filter(
        estado__in=["Pendiente", "Error temporal", "Reprogramada"],
        fecha_ejecucion__lte=now
    ).order_by('id_tarea')

    # Conjunto para registrar qué lotes masivos ya fueron enviados al worker en esta pasada
    lotes_absorbidos_en_vuelo = set()

    # 3. Despacho Orquestado
    for tarea in pendientes:
        id_lote_actual = getattr(tarea, "id_lote", None)
        tipo_actual = tarea.tipo.strip().lower()
        
        if id_lote_actual:
            # Operaciones MASIVAS (Agregar / Eliminar)
            if tipo_actual in ["agregar", "eliminar"]:
                # Si ya mandamos la tarea líder de este lote, ignoramos las hermanas
                if id_lote_actual in lotes_absorbidos_en_vuelo:
                    continue
                
                # Registramos el lote para que ninguna otra tarea de este grupo se vuelva a encolar
                lotes_absorbidos_en_vuelo.add(id_lote_actual)
                
                # Despachamos únicamente el líder con ejecutar_como_lote=True
                process_tarea.delay(tarea.id_tarea, ejecutar_como_lote=True)
            
            # Operación SECUENCIAL (Posicionar)
            elif tipo_actual == "posicionar":
                # Posicionar procesa de a una por una. No se agrega al set de exclusión.
                # Se manda con ejecutar_como_lote=True para asegurar el sufijo del mensaje en la Capa 2
                process_tarea.delay(tarea.id_tarea, ejecutar_como_lote=True)
        
        else:
            # Tarea estándar unitaria sin lote relacional
            process_tarea.delay(tarea.id_tarea, ejecutar_como_lote=False)