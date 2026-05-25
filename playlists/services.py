from django.db import transaction
from .models import Tarea, PlaylistCancion, Cancion, Playlist
from django.contrib import messages
from django.db.models import F
from django.utils import timezone
from logs.models import LogEvento
import requests

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


def conciliar_playlist_con_spotify(id_playlist_local, forzar_actualizacion=False, spotify_token=None, request_user=None):
    """
    Servicio de ingeniería lineal.
    Clona el estado físico actual de Spotify en la BD local de un solo golpe
    y audita las tareas pendientes del futuro aplicando reglas de acotación y anulación.
    """
    from django.utils import timezone
    
    try:
        playlist = Playlist.objects.get(id_playlist=id_playlist_local)
        playlist.refresh_from_db()
        snapshot_local_fisco = playlist.snapshot_id
    except Playlist.DoesNotExist:
        return {"ok": False, "error": f"La playlist con ID {id_playlist_local} no existe en la BD."}

    headers = {"Authorization": f"Bearer {spotify_token}"}
    
    # 📡 1. EXTRAER SNAPSHOT ACTUAL DE SPOTIFY
    url_playlist_completa = f"https://api.spotify.com/v1/playlists/{playlist.id_spotify}"
    try:
        resp_master = requests.get(url_playlist_completa, headers=headers, timeout=12)
        if resp_master.status_code != 200:
            return {"ok": False, "error": f"Spotify API Maestra devolvió status {resp_master.status_code}."}
        snapshot_spotify = resp_master.json().get("snapshot_id")
    except Exception as e:
        return {"ok": False, "error": f"Error de conexión maestra: {str(e)}"}
    
    # 🔍 2. CONTROL DE SNAPSHOTS
    if not forzar_actualizacion and snapshot_local_fisco == snapshot_spotify:
        return {"ok": True, "cambios_detectados": False, "nombre_playlist": playlist.nombre, "mensaje": None}

    # ⚙️ 3. DESCARGAR TRACKS DE SPOTIFY (Verdad Absoluta)
    url_tracks = f"https://api.spotify.com/v1/playlists/{playlist.id_spotify}/tracks?fields=items(track(id,name,album(name,images),artists(name),duration_ms,popularity))"
    try:
        resp_tracks = requests.get(url_tracks, headers=headers, timeout=15)
        if resp_tracks.status_code != 200:
            return {"ok": False, "error": f"Error al descargar tracks (Status {resp_tracks.status_code})"}
        items_spotify = resp_tracks.json().get("items", [])
    except Exception as e:
        return {"ok": False, "error": f"Error de conexión: {str(e)}"}

    log_alertas_futuro = []

    with transaction.atomic():
        # ───────── PASO A: CLONACIÓN FÍSICA INMEDIATA ─────────
        id_relaciones_sobrevivientes = []
        total_canciones_actuales = 0
        
        for idx, item in enumerate(items_spotify):
            if not item.get("track") or not item["track"].get("id"): continue
            t = item["track"]
            pos_real = idx + 1
            total_canciones_actuales += 1

            # Buscamos u obtenemos la canción en el catálogo global
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

            # ESTRATEGIA LARRY: Buscamos únicamente si existe una relación ya ACTIVA
            relacion_activa = PlaylistCancion.objects.filter(
                playlist=playlist, 
                cancion=cancion_obj,
                estado="activo"
            ).first()

            if relacion_activa:
                # Si está activa, solo actualizamos su posición física en el nuevo tablero
                relacion_activa.posicion = pos_real
                relacion_activa.save(update_fields=["posicion"])
                id_relaciones_sobrevivientes.append(relacion_activa.id_relacion)
            else:
                # Si no hay relación activa (está en 'eliminado' o no existe), forzamos una NUEVA fila.
                # De esta forma las tareas viejas se quedan pegadas al ID anterior para auditoría.
                nueva_relacion = PlaylistCancion.objects.create(
                    playlist=playlist,
                    cancion=cancion_obj,
                    posicion=pos_real,
                    estado="activo",
                    fecha_agregado=timezone.now(),
                    agregado_por="Sincronizacion_Externa"
                )
                id_relaciones_sobrevivientes.append(nueva_relacion.id_relacion)

        # ───────── CIERRE DE REMANENTES (INTEGRIDAD REFERENCIAL PROTEGIDA) ─────────
        # Identificamos qué relaciones estaban activas localmente pero ya NO vinieron en Spotify
        relaciones_a_eliminar = PlaylistCancion.objects.filter(
            playlist=playlist,
            estado="activo"
        ).exclude(id_relacion__in=id_relaciones_sobrevivientes)

        if relaciones_a_eliminar.exists():
            # Buscamos todas las tareas pendientes amarradas a estas canciones que se van a eliminar
            tareas_huerfanas = Tarea.objects.filter(
                relacion__in=relaciones_a_eliminar,
                estado="Pendiente"
            ).select_related("relacion__cancion")

            # Registramos la consecuencia exacta solicitada
            for tarea_fantasma in tareas_huerfanas:
                fecha_formateada = tarea_fantasma.fecha_ejecucion.strftime('%d/%m/%Y')
                nombre_cancion = tarea_fantasma.relacion.cancion.nombre
                
                msg = f"Se eliminó la tarea '{tarea_fantasma.tipo}' de '{nombre_cancion}' programada para el '{fecha_formateada}' porque se eliminó la canción."
                log_alertas_futuro.append(msg)

            # Las pendientes mueren de forma segura, pero mantenemos las FKs intactas hacia la relación eliminada
            tareas_huerfanas.update(estado="Anulada")

            # Pasamos las relaciones locales a estado eliminado
            relaciones_a_eliminar.update(estado="eliminado", posicion=None)

        # ───────── PASO B: AUDITORÍA DEL RETRACTO (EL FUTURO) ─────────
        tareas_pendientes = Tarea.objects.filter(
            relacion__playlist=playlist, 
            estado="Pendiente"
        ).select_related("relacion__cancion")

        for tarea in tareas_pendientes:
            
            # Caso 1: AGREGAR (Muta a -1 si se desfasa por >= límite)
            if tarea.tipo == "Agregar":
                limite_permitido = total_canciones_actuales + 1
                if tarea.posicion >= limite_permitido:
                    tarea.posicion = -1  
                    tarea.save(update_fields=["posicion"])
                    msg = f"La tarea 'Agregar' de '{tarea.relacion.cancion.nombre}' se reajustó a la ultima posición.."
                    log_alertas_futuro.append(msg)

            # Caso 2: POSICIONAR (Anulación estricta por rango fuera de índice)
            elif tarea.tipo == "Posicionar":
                if tarea.posicion > total_canciones_actuales:
                    tarea.estado = "Anulada"
                    tarea.save(update_fields=["estado"])
                    msg = f"La tarea 'Posicionar' de '{tarea.relacion.cancion.nombre}' se anuló porque su nueva posición ({tarea.posicion}) quedará fuera del playlist."
                    log_alertas_futuro.append(msg)
            
            # Caso 3: ELIMINAR 
            elif tarea.tipo == "Eliminar":
                if tarea.relacion.estado != "activo":
                    tarea.estado = "Anulada"
                    tarea.save(update_fields=["estado"])
                    msg = f"La tarea 'Eliminar' de '{tarea.relacion.cancion.nombre}' se anuló porque la canción ya fue removida manualmente de Spotify."
                    log_alertas_futuro.append(msg)

        # ───────── PASO C: GUARDAR SNAPSHOT META ─────────
        playlist.snapshot_id = snapshot_spotify
        playlist.total_canciones = total_canciones_actuales
        playlist.save(update_fields=["snapshot_id", "total_canciones"])

    # 📝 FORMATEO DEL REPORTE PROFESIONAL PARA LA INTERFAZ
    base_msg = f'Actualización de la Playlist "{playlist.nombre}" completada correctamente.'
    
    if log_alertas_futuro:
        consecuencias = "\n".join(log_alertas_futuro)
        msg_final = f"{base_msg}\nConsecuencias:\n{consecuencias}"
    else:
        msg_final = f"{base_msg} Las tareas pendientes no se modificaron."

    return {
        "ok": True, 
        "cambios_detectados": True, 
        "nombre_playlist": playlist.nombre, 
        "mensaje": msg_final
    }