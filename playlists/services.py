from django.db import transaction
from .models import Tarea, PlaylistCancion, Cancion, Playlist
from django.contrib import messages
from django.db.models import F
from django.utils import timezone
from logs.models import LogEvento
import requests

def recalcular_posiciones_tareas_pendientes(tarea_ejecutada, posicion_anterior_movimiento=None, desplazamiento=1):
    """
    Servicio para procesar cambios matemáticos secundarios en tareas del futuro.
    Ahora soporta un 'desplazamiento' variable para inyecciones o remociones en lote (K).
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

        # =====================================================================
        # CASO 1: CONSECUENCIAS DE UN "AGREGAR" (Mueve el futuro hacia adelante)
        # =====================================================================
        if tipo_ejecutado == "agregar":
            for t in tareas_pendientes_lista:
                condicion_pos = t.posicion is not None and t.posicion >= N
                condicion_ant = t.posicion_anterior is not None and t.posicion_anterior >= N
                if condicion_pos or condicion_ant:
                    registrar_consecuencia(t, f"se modificó desplazando sus posiciones (+{desplazamiento})")
            
            queryset_base.filter(posicion__isnull=False, posicion__gte=N).update(posicion=F("posicion") + desplazamiento)
            queryset_base.filter(posicion_anterior__isnull=False, posicion_anterior__gte=N).update(posicion_anterior=F("posicion_anterior") + desplazamiento)

        # =====================================================================
        # CASO 2: CONSECUENCIAS DE UN "ELIMINAR" (Atrae el futuro hacia atrás)
        # =====================================================================
        elif tipo_ejecutado == "eliminar":
            for t in tareas_pendientes_lista:
                if t.relacion_id == tarea_ejecutada.relacion_id:
                    registrar_consecuencia(t, "fue ANULADA porque la canción fue removida de la playlist")
                else:
                    condicion_pos = t.posicion is not None and t.posicion > N
                    condicion_ant = t.posicion_anterior is not None and t.posicion_anterior > N
                    if condicion_pos or condicion_ant:
                        registrar_consecuencia(t, f"se modificó reduciendo sus posiciones (-{desplazamiento})")

            # AJUSTE MATEMÁTICO EN LOTE: Restamos la magnitud exacta del lote
            queryset_base.filter(posicion__isnull=False, posicion__gt=N).update(posicion=F("posicion") - desplazamiento)
            queryset_base.filter(posicion_anterior__isnull=False, posicion_anterior__gt=N).update(posicion_anterior=F("posicion_anterior") - desplazamiento)
            
            # ESCUDO DE NÚMEROS NEGATIVOS O LÍMITES INVALIDADOS POST-ELIMINACIÓN:
            # Si tras la reducción alguna posición cae por debajo del índice mínimo (1), la anulamos.
            tareas_invalidas = queryset_base.filter(posicion__isnull=False, posicion__lt=1)
            for t_inv in tareas_invalidas:
                registrar_consecuencia(t_inv, "fue ANULADA debido a un reajuste de posiciones negativo o fuera de rango")
            tareas_invalidas.update(estado="Anulada", mensaje_error="Posición invalidada por eliminación previa de elementos en lote.")

            queryset_base.filter(relacion_id=tarea_ejecutada.relacion_id).update(
                estado="Anulada",
                mensaje_error="Canción eliminada en una ejecución previa."
            )

        # =====================================================================
        # CASO 3: CONSECUENCIAS DE UN "POSICIONAR" (Inalterado, no opera en lotes)
        # =====================================================================
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
    """Se encarga de limpiar el desastre que queda tras borrar una tarea de forma manual."""
    requiere_reload = False
    
    if tipo_tarea_eliminada.lower() == "agregar" and relacion.estado == "eliminado":
        tareas_zombis = Tarea.objects.filter(relacion=relacion)
        count = tareas_zombis.count()
        
        if count > 0:
            for t in tareas_zombis:
                messages.info(
                    request, 
                    f"Consecuencia: Se eliminó la tarea '{t.tipo}' programada para el "
                    f"{t.fecha_ejecucion.strftime('%d/%m/%Y')} porque ya no se agregará la canción."
                )
            tareas_zombis.delete()
        
        requiere_reload = True
        
    return requiere_reload


def conciliar_playlist_con_spotify(id_playlist_local, spotify_token=None, request_user=None):
    """
    Servicio de ingeniería lineal.
    Clona el estado físico actual de Spotify en la BD local de un solo golpe
    y audita las tareas pendientes del futuro aplicando reglas de acotación y anulación.
    """
    try:
        playlist = Playlist.objects.get(id_playlist=id_playlist_local)
        playlist.refresh_from_db()
    except Playlist.DoesNotExist:
        return {"ok": False, "error": f"La playlist con ID {id_playlist_local} no existe en la BD."}

    headers = {"Authorization": f"Bearer {spotify_token}"}

    items_spotify = obtener_todas_las_canciones(playlist.id_spotify, headers)

    if not items_spotify:
        return {"ok": False, "error": "No se pudieron recuperar canciones o la playlist está vacía."}
    
    url_playlist_base = f"https://api.spotify.com/v1/playlists/{playlist.id_spotify}?fields=snapshot_id"
    try:
        resp_snap = requests.get(url_playlist_base, headers=headers, timeout=10)
        snapshot_spotify = resp_snap.json().get("snapshot_id") if resp_snap.status_code == 200 else None
    except Exception:
        snapshot_spotify = None

    log_alertas_futuro = []

    with transaction.atomic():
        id_relaciones_sobrevivientes = []
        total_canciones_actuales = 0
        
        for idx, item in enumerate(items_spotify):
            if not item.get("track") or not item["track"].get("id"): continue
            t = item["track"]
            pos_real = idx + 1
            total_canciones_actuales += 1

            cancion_obj, _ = Cancion.objects.get_or_create(
                id_spotify=t["id"],
                defaults={
                    "nombre": t["name"],
                    "artistas": ", ".join([a["name"] for a in t["artists"]]) if t.get("artists") else "Desconocido",
                    "album": t["album"]["name"] if t.get("album") else "Desconocido",
                    "duracion_ms": t.get("duration_ms", 0),
                    "popularidad": t.get("popularity", 0),
                    "cover_url": t["album"]["images"][0]["url"] if t.get("album") and t["album"].get("images") else None
                }
            )

            relacion_activa = PlaylistCancion.objects.filter(
                playlist=playlist, 
                cancion=cancion_obj,
                estado="activo"
            ).first()

            if relacion_activa:
                relacion_activa.posicion = pos_real
                relacion_activa.save(update_fields=["posicion"])
                id_relaciones_sobrevivientes.append(relacion_activa.id_relacion)
            else:
                nueva_relacion = PlaylistCancion.objects.create(
                    playlist=playlist,
                    cancion=cancion_obj,
                    posicion=pos_real,
                    estado="activo",
                    fecha_agregado=timezone.now(),
                    agregado_por="Sincronizacion_Externa"
                )
                id_relaciones_sobrevivientes.append(nueva_relacion.id_relacion)

        relaciones_ausentes = PlaylistCancion.objects.filter(
            playlist=playlist,
            estado="activo"
        ).exclude(id_relacion__in=id_relaciones_sobrevivientes)

        id_relaciones_borradas_manualmente = []
        for rel in relaciones_ausentes:
            tiene_agregar_pendiente = Tarea.objects.filter(
                relacion=rel,
                tipo="Agregar",
                estado__in=["Pendiente", "En progreso"]
            ).exists()
            
            if not tiene_agregar_pendiente:
                id_relaciones_borradas_manualmente.append(rel.id_relacion)

        relaciones_a_eliminar = PlaylistCancion.objects.filter(id_relacion__in=id_relaciones_borradas_manualmente)

        if relaciones_a_eliminar.exists():
            tareas_huerfanas = Tarea.objects.filter(
                relacion__in=relaciones_a_eliminar,
                estado="Pendiente"
            ).select_related("relacion__cancion")

            for tarea_fantasma in tareas_huerfanas:
                fecha_formateada = tarea_fantasma.fecha_ejecucion.strftime('%d/%m/%Y')
                nombre_cancion = tarea_fantasma.relacion.cancion.nombre
                msg = f"Se eliminó la tarea '{tarea_fantasma.tipo}' de '{nombre_cancion}' programada para el '{fecha_formateada}' porque se eliminó la canción."
                log_alertas_futuro.append(msg)

            tareas_huerfanas.update(estado="Anulada")
            relaciones_a_eliminar.update(estado="eliminado", posicion=None)

        # ───────── PASO B: AUDITORÍA DEL RETRACTO EN CASO DE DESBORDAMIENTOS ─────────
        tareas_pendientes = Tarea.objects.filter(
            relacion__playlist=playlist, 
            estado="Pendiente"
        ).select_related("relacion__cancion")

        for tarea_futura in tareas_pendientes:
            if tarea_futura.tipo == "Agregar":
                limite_permitido = total_canciones_actuales + 1
                if tarea_futura.posicion >= limite_permitido:
                    tarea_futura.posicion = -1  
                    tarea_futura.save(update_fields=["posicion"])
                    msg = f"La tarea 'Agregar' de '{tarea_futura.relacion.cancion.nombre}' se reajustó a la ultima posición.."
                    log_alertas_futuro.append(msg)

            elif tarea_futura.tipo == "Posicionar":
                # 🛡️ REGLA CRÍTICA DE BORDE: Si la posición requerida quedó fuera del límite actual 
                # o el cálculo la arrojó a un número negativo o cero, se anula.
                if tarea_futura.posicion > total_canciones_actuales or tarea_futura.posicion < 1:
                    tiene_agregar_previo = Tarea.objects.filter(
                        relacion=tarea_futura.relacion,
                        tipo="Agregar",
                        estado__in=["Pendiente", "En progreso"]
                    ).exists()
                    
                    if not tiene_agregar_previo:
                        tarea_futura.estado = "Anulada"
                        tarea_futura.save(update_fields=["estado"])
                        msg = f"La tarea 'Posicionar' de '{tarea_futura.relacion.cancion.nombre}' se anuló porque su nueva posición ({tarea_futura.posicion}) quedará fuera de los límites de la playlist."
                        log_alertas_futuro.append(msg)
            
            elif tarea_futura.tipo == "Eliminar":
                tiene_agregar_en_cola = Tarea.objects.filter(
                    relacion=tarea_futura.relacion,
                    tipo="Agregar",
                    estado__in=["Pendiente", "En progreso"]
                ).exists()

                if tarea_futura.relacion.estado != "activo" and not tiene_agregar_en_cola:
                    tarea_futura.estado = "Anulada"
                    tarea_futura.save(update_fields=["estado"])
                    msg = f"La tarea 'Eliminar' de '{tarea_futura.relacion.cancion.nombre}' se anuló porque la canción ya fue removida de Spotify."
                    log_alertas_futuro.append(msg)

        # ───────── PASO C: PASAMANOS MATEMÁTICO UNIVERSAL ─────────
        playlist.total_canciones = total_canciones_actuales
        playlist.save(update_fields=["total_canciones"])
        
        if snapshot_spotify:
            playlist.snapshot_anterior = playlist.snapshot_ahorita
            playlist.snapshot_ahorita = snapshot_spotify
            playlist.save(update_fields=["snapshot_anterior", "snapshot_ahorita"])

    base_msg = f'Actualización de la Playlist "{playlist.nombre}" completada correctamente.'
    msg_final = f"{base_msg}\nConsecuencias:\n" + "\n".join(log_alertas_futuro) if log_alertas_futuro else f"{base_msg} Las tareas pendientes no se modificaron."

    return {
        "ok": True, 
        "cambios_detectados": True, 
        "nombre_playlist": playlist.nombre, 
        "mensaje": msg_final
    }

def obtener_todas_las_canciones(playlist_id, headers):
    """
    Función auxiliar para paginar las canciones de Spotify.
    """
    todos_los_items = []
    # Usamos limit=100 que es el máximo permitido por la API
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?fields=items(track(id,name,album(name,images),artists(name),duration_ms,popularity)),next&limit=100"
    
    while url:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            break
        
        data = resp.json()
        todos_los_items.extend(data.get("items", []))
        url = data.get("next") 
        
    return todos_los_items