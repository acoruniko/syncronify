from django.db import transaction
from .models import Tarea, PlaylistCancion
from django.contrib import messages
from django.db.models import F
from django.utils import timezone
from logs.models import LogEvento


from django.db import transaction
from django.db.models import F
from django.utils import timezone
from logs.models import LogEvento
from .models import Tarea

def recalcular_posiciones_tareas_pendientes(tarea_ejecutada, posicion_anterior_movimiento=None):
    """
    Servicio para procesar cambios matemáticos secundarios en tareas del futuro.
    Corregido el filtro de estado para soportar 'Pendiente' con mayúsculas.
    """
    playlist_id = tarea_ejecutada.relacion.playlist_id
    tipo_ejecutado = tarea_ejecutada.tipo.strip().lower()
    usuario_username = getattr(tarea_ejecutada.usuario, "username", "sistema")
    
    cambios_detectados = []

    # Determinar el punto de origen matemático (N) según el tipo de tarea
    if tipo_ejecutado == "eliminar":
        N = tarea_ejecutada.relacion.posicion
    else:
        N = tarea_ejecutada.posicion

    if N is None:
        return cambios_detectados

    with transaction.atomic():
        # USAMOS __iexact PARA QUE DETECTE "Pendiente" O "pendiente" SIN DISTINCIÓN
        queryset_base = Tarea.objects.select_for_update().filter(
            relacion__playlist_id=playlist_id,
            estado__iexact="pendiente"
        ).select_related("relacion", "relacion__cancion", "relacion__playlist")

        tareas_pendientes_lista = list(queryset_base)

        def registrar_consecuencia(t, detalle):
            cancion_nom = t.relacion.cancion.nombre if t.relacion and t.relacion.cancion else "Canción"
            playlist_nom = t.relacion.playlist.nombre if t.relacion and t.relacion.playlist else "Playlist"
            fecha_fmt = t.fecha_ejecucion.strftime('%d/%m/%Y') if t.fecha_ejecucion else "sin fecha"
            
            msg = f"Consecuencia: La tarea '{t.tipo}' de '{cancion_nom}' en la playlist '{playlist_nom}' programada para el {fecha_fmt} {detalle}."
            
            LogEvento.objects.create(
                fecha=timezone.now(),
                nivel="INFO",
                usuario=usuario_username,
                modulo="consecuencias_servicio",
                mensaje=msg
            )
            cambios_detectados.append(msg)

        # ==========================================
        # CASO 1: CONSECUENCIAS DE UN "AGREGAR"
        # ==========================================
        if tipo_ejecutado == "agregar":
            for t in tareas_pendientes_lista:
                condicion_pos = t.posicion is not None and t.posicion >= N
                condicion_ant = t.posicion_anterior is not None and t.posicion_anterior >= N
                if condicion_pos or condicion_ant:
                    registrar_consecuencia(t, "se modificó desplazando sus posiciones (+1)")
            
            queryset_base.filter(posicion__isnull=False, posicion__gte=N).update(posicion=F("posicion") + 1)
            queryset_base.filter(posicion_anterior__isnull=False, posicion_anterior__gte=N).update(posicion_anterior=F("posicion_anterior") + 1)

        # ==========================================
        # CASO 2: CONSECUENCIAS DE UN "ELIMINAR"
        # ==========================================
        elif tipo_ejecutado == "eliminar":
            for t in tareas_pendientes_lista:
                if t.relacion_id == tarea_ejecutada.relacion_id:
                    registrar_consecuencia(t, "fue ANULADA porque la canción fue removida de la playlist")
                else:
                    condicion_pos = t.posicion is not None and t.posicion > N
                    condicion_ant = t.posicion_anterior is not None and t.posicion_anterior > N
                    if condicion_pos or condicion_ant:
                        registrar_consecuencia(t, "se modificó reduciendo sus posiciones (-1)")

            queryset_base.filter(posicion__isnull=False, posicion__gt=N).update(posicion=F("posicion") - 1)
            queryset_base.filter(posicion_anterior__isnull=False, posicion_anterior__gt=N).update(posicion_anterior=F("posicion_anterior") - 1)
            
            queryset_base.filter(relacion_id=tarea_ejecutada.relacion_id).update(
                estado="Anulada",
                mensaje_error="Canción eliminada en una ejecución previa."
            )

        # ==========================================
        # CASO 3: CONSECUENCIAS DE UN "POSICIONAR"
        # ==========================================
        elif tipo_ejecutado == "posicionar":
            X = posicion_anterior_movimiento
            Y = N

            if X is not None and Y is not None:
                if Y < X:  # Subió
                    for t in tareas_pendientes_lista:
                        condicion_pos = t.posicion is not None and Y <= t.posicion <= X - 1
                        condicion_ant = t.posicion_anterior is not None and Y <= t.posicion_anterior <= X - 1
                        if condicion_pos or condicion_ant:
                            registrar_consecuencia(t, f"se modificó reajustando posiciones (+1) por movimiento a la posición {Y}")

                    queryset_base.filter(posicion__isnull=False, posicion__gte=Y, posicion__lte=X - 1).update(posicion=F("posicion") + 1)
                    queryset_base.filter(posicion_anterior__isnull=False, posicion_anterior__gte=Y, posicion_anterior__lte=X - 1).update(posicion_anterior=F("posicion_anterior") + 1)
                    
                elif Y > X:  # Bajó
                    for t in tareas_pendientes_lista:
                        condicion_pos = t.posicion is not None and X + 1 <= t.posicion <= Y
                        condicion_ant = t.posicion_anterior is not None and X + 1 <= t.posicion_anterior <= Y
                        if condicion_pos or condicion_ant:
                            registrar_consecuencia(t, f"se modificó reajustando posiciones (-1) por movimiento a la posición {Y}")

                    queryset_base.filter(posicion__isnull=False, posicion__gt=X, posicion__lte=Y).update(posicion=F("posicion") - 1)
                    queryset_base.filter(posicion_anterior__isnull=False, posicion_anterior__gt=X, posicion_anterior__lte=Y).update(posicion_anterior=F("posicion_anterior") - 1)

    return cambios_detectados

def procesar_consecuencias_tarea_eliminada(request, relacion, tipo_tarea_eliminada):
    """
    Se encarga de limpiar el desastre que queda tras borrar una tarea.
    """
    requiere_reload = False
    
    # Caso 1: Si eliminamos el 'Agregar', matamos todo el futuro de esa relación
    if tipo_tarea_eliminada.lower() == "agregar" and relacion.estado == "eliminado":
        # Buscamos tareas que se quedaron 'zombis'
        tareas_zombis = Tarea.objects.filter(relacion=relacion)
        count = tareas_zombis.count()
        
        if count > 0:
            for t in tareas_zombis:
                # Log específico para cada tarea muerta en cascada
                messages.info(
                    request, 
                    f"Consecuencia: Se eliminó la tarea '{t.tipo}' programada para el "
                    f"{t.fecha_ejecucion.strftime('%d/%m/%Y')} porque ya no se agregará la canción."
                )
            tareas_zombis.delete()
        
        requiere_reload = True
        
    # Aquí podrías añadir Caso 2, Caso 3 en el futuro...
    
    return requiere_reload