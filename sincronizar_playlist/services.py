import requests
from django.utils import timezone
from django.db import transaction
from django.db.models import F, Case, When, IntegerField
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token
from playlists.models import Tarea, PlaylistCancion, Playlist
from logs.models import LogEvento
from playlists.models import PlaylistSnapshotHistorial
from playlists.services import recalcular_posiciones_tareas_pendientes, conciliar_playlist_con_spotify
from collections import defaultdict


# =====================================================================
# SERVICIOS AUXILIARES / HELPERS ATÓMICOS
# =====================================================================

def log_evento(nivel, usuario, modulo, mensaje, source="manual"):
    if source == "celery":
        LogEvento.objects.create(
            fecha=timezone.now(),
            nivel=nivel,
            usuario=usuario or "celery",
            modulo=modulo,
            mensaje=mensaje
        )

  # Asegúrate de importar tu nuevo modelo

def _save_snapshot_sucesion(obj_playlist, nuevo_snap):
    """
    Aplica el desplazamiento temporal guardando el snapshot en la lista blanca
    histórica (máximo 15) y manteniendo los campos de caché en la playlist.
    """
    if not nuevo_snap:
        return

    with transaction.atomic():
        # 1. Insertamos el nuevo snapshot en el historial
        PlaylistSnapshotHistorial.objects.create(
            playlist=obj_playlist,
            snapshot_id=nuevo_snap
        )

        # 2. Mantener el buffer circular: Identificamos los IDs de los 15 más recientes
        ids_a_conservar = PlaylistSnapshotHistorial.objects.filter(
            playlist=obj_playlist
        ).values_list('id_historial', flat=True)[:15]

        # 3. Podar la tabla: Eliminamos cualquier registro que no esté en ese Top 15
        PlaylistSnapshotHistorial.objects.filter(
            playlist=obj_playlist
        ).exclude(id_historial__in=list(ids_a_conservar)).delete()

        # 4. Sucesión y persistencia en los campos de caché tradicionales por compatibilidad
        obj_playlist.snapshot_anterior = obj_playlist.snapshot_ahorita
        obj_playlist.snapshot_ahorita = nuevo_snap
        obj_playlist.save(update_fields=["snapshot_anterior", "snapshot_ahorita"])


# =====================================================================
# CAPA 1: PASAPORTE Y ALCABALAS DE CONCILIACIÓN
# =====================================================================
from django.db import transaction

def _verificar_y_conciliar_playlist(tarea, playlist_obj, token, headers, source):
    """
    Escudo de lista blanca histórico puro.
    Utiliza bloqueo de base de datos para evitar que la ráfaga de tareas
    genere falsos positivos de actualización por lag de red.
    """
    # 🔐 BLOQUEO DE SEGURIDAD: Obliga a las tareas del lote a pasar de una en una
    # Evita que la tarea 2 consulte a Spotify antes de que la tarea 1 guarde su snapshot.
    with transaction.atomic():
        playlist_bloqueada = Playlist.objects.select_for_update().get(id_playlist=playlist_obj.id_playlist)
        
        url_playlist_master = f"https://api.spotify.com/v1/playlists/{playlist_bloqueada.id_spotify}"
        resp_snapshot = requests.get(url_playlist_master, headers=headers, timeout=12)
        resp_snapshot.raise_for_status()
        snapshot_spotify_real = resp_snapshot.json().get("snapshot_id")

        tarea._telemetria_sincronizacion = {
            "desfase_detectado": False,
            "snapshot_spotify": snapshot_spotify_real,
            "conciliacion_ejecutada": False,
            "mensaje": f"Sincronización limpia ({snapshot_spotify_real})."
        }

        # 🛡️ REGLA ESTRUCTURAL DE LISTA BLANCA PURA
        # Buscamos en el historial de los últimos 15 si el snapshot ya es conocido por nuestro sistema
        existe_en_lista_blanca = PlaylistSnapshotHistorial.objects.filter(
            playlist=playlist_bloqueada,
            snapshot_id=snapshot_spotify_real
        ).exists()

        if existe_en_lista_blanca:
            # Si el snapshot ya existía en la lista blanca, significa que este estado
            # fue generado por nosotros en una tarea previa del lote. Pasamos de largo de forma segura.
            tarea._telemetria_sincronizacion["mensaje"] = "Sincronización validada por amortiguador histórico puro."
            return False, None

        # CASO B: Desfase real externo (Cambio manual desde la app de Spotify)
        tarea._telemetria_sincronizacion["desfase_detectado"] = True
        log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea",
                   f"Cambio manual detectado en Spotify para '{playlist_bloqueada.nombre}'. Sincronizando...", source)
        
        # Se ejecuta tu actualizador lineal que clona el estado de Spotify en la BD
        resultado_conciliacion = conciliar_playlist_con_spotify(
            id_playlist_local=playlist_bloqueada.id_playlist,
            spotify_token=token,
            request_user=tarea.usuario
        )

        if not resultado_conciliacion.get("ok"):
            raise Exception(f"Fallo crítico en servicio de conciliación: {resultado_conciliacion.get('error')}")

        msg_servicio = resultado_conciliacion.get("mensaje")
        tarea._telemetria_sincronizacion["conciliacion_ejecutada"] = True
        tarea._telemetria_sincronizacion["mensaje"] = msg_servicio

        # Refrescamos las instancias locales para la ejecución física de la tarea actual
        tarea.refresh_from_db()
        playlist_obj.refresh_from_db()
        
        # Registramos de inmediato el snapshot manual como punto legítimo en la lista blanca
        _save_snapshot_sucesion(playlist_obj, snapshot_spotify_real)
        
        return True, msg_servicio


# =====================================================================
# CAPA 2: MOTOR DE ACCIONES FÍSICAS Y MUTACIONES (EL "FIERRO")
# =====================================================================
# =====================================================================
# CAPA 2: MOTOR DE ACCIONES FÍSICAS Y MUTACIONES (EL "FIERRO")
# =====================================================================
def _ejecutar_accion_fisica_spotify(tarea, playlist_obj, headers, tipo, token, source, ejecutar_como_lote=False, request=None):
    """
    Contiene la lógica de payload y peticiones REST específicas por tipo de tarea.
    Maneja sus propias transacciones SQL locales atómicas.
    Almacena CADA snapshot generado en caliente para blindar la lista blanca contra ráfagas.
    Soporta el procesamiento de lotes multi-playlist mediante segmentación por bucle.
    """
    track_spotify_id = tarea.relacion.cancion.id_spotify
    playlist_spotify_id = playlist_obj.id_spotify
    url_base_spotify = "https://api.spotify.com/v1/playlists/"

    # =====================================================================
    # ---- OPERACIÓN: POSICIONAR (No opera en lotes, se mantiene lineal) ----
    # =====================================================================
    if tipo == "posicionar":
        url_tracks = f"{url_base_spotify}{playlist_spotify_id}/tracks"
        
        tarea.refresh_from_db()
        if tarea.relacion:
            tarea.relacion.refresh_from_db()

        old_pos = tarea.posicion_anterior or tarea.relacion.posicion
        if old_pos is None:
            raise ValueError("No se puede posicionar: la canción no cuenta con una posición de origen válida.")

        new_pos = tarea.posicion
        
        # Escudo de Identidad / Verificación de Índice
        indice_verificacion = old_pos - 1
        url_verificar = f"{url_tracks}?offset={indice_verificacion}&limit=1"
        resp_verif = requests.get(url_verificar, headers=headers, timeout=10)
        resp_verif.raise_for_status()
        items_verif = resp_verif.json().get("items", [])
        
        if not items_verif or items_verif[0].get("track", {}).get("id") != track_spotify_id:
            log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea_desfase_fantasma", "Desfase fantasma detectado en índices.", source)
            fake_response = requests.Response()
            fake_response.status_code = 409
            raise requests.exceptions.HTTPError("Desfase de índice fantasma detectado.", response=fake_response)

        total_items = PlaylistCancion.objects.filter(playlist=playlist_obj, estado="activo").count()
        insert_before = total_items if new_pos == total_items else (new_pos if new_pos > old_pos else new_pos - 1)

        payload = {"range_start": old_pos - 1, "insert_before": insert_before, "range_length": 1}
        resp = requests.put(url_tracks, headers=headers, json=payload, timeout=12)
        resp.raise_for_status()
        
        snap_posicionar = resp.json().get("snapshot_id")
        if snap_posicionar:
            _save_snapshot_sucesion(playlist_obj, snap_posicionar)

        with transaction.atomic():
            if new_pos > old_pos:
                PlaylistCancion.objects.filter(playlist=playlist_obj, estado="activo", posicion__gt=old_pos, posicion__lte=new_pos).update(posicion=F("posicion") - 1)
            elif new_pos < old_pos:
                PlaylistCancion.objects.filter(playlist=playlist_obj, estado="activo", posicion__gte=new_pos, posicion__lt=old_pos).update(posicion=F("posicion") + 1)
            
            tarea.relacion.posicion = new_pos
            tarea.relacion.fecha_sincronizacion = timezone.now()
            tarea.relacion.save(update_fields=["posicion", "fecha_sincronizacion"])

            recalcular_posiciones_tareas_pendientes(
                tarea_ejecutada=tarea,
                posicion_anterior_movimiento=old_pos,
                desplazamiento=1
            )

            tarea.posicion_anterior = old_pos
            tarea.estado = "Completado"
            tarea.mensaje_error = None
            tarea.save(update_fields=["posicion_anterior", "estado", "mensaje_error"])

            if source == "manual" and request:
                from django.contrib import messages as django_messages
                sufijo_lote = " en lote." if ejecutar_como_lote else "."
                msg_texto = f"La tarea Posicionar de '{tarea.relacion.cancion.nombre}' (Nueva posición: {new_pos}) en la playlist '{playlist_obj.nombre}' se ejecutó correctamente{sufijo_lote}"
                django_messages.success(request, msg_texto)

    # =====================================================================
    # ---- OPERACIÓN: ELIMINAR --------------------------------------------
    # =====================================================================
    elif tipo == "eliminar":
        id_lote_actual = getattr(tarea, "id_lote", None)

        # 1. Separar las tareas del lote por playlist (si aplica)
        sub_lotes_playlist = defaultdict(list)
        if ejecutar_como_lote and id_lote_actual:
            lote_global = Tarea.objects.filter(
                id_lote=id_lote_actual,
                tipo__iexact="eliminar",
                estado__in=["Pendiente", "En progreso"]
            ).select_related("relacion", "relacion__cancion", "relacion__playlist")
            
            for t_hermano in lote_global:
                sub_lotes_playlist[t_hermano.relacion.playlist_id].append(t_hermano)
        else:
            sub_lotes_playlist[playlist_obj.id_playlist].append(tarea)

        if not sub_lotes_playlist:
            raise ValueError("No se encontraron tareas de eliminación pendientes en este lote.")

        # 2. El Bucle: Recorrer cada playlist ejecutando la lógica con variables locales aisladas
        for id_playlist_local, lote_tareas in sub_lotes_playlist.items():
            playlist_actual = lote_tareas[0].relacion.playlist
            url_tracks_actual = f"{url_base_spotify}{playlist_actual.id_spotify}/tracks"
            
            for t_hermano in lote_tareas:
                t_hermano.refresh_from_db()
                if t_hermano.relacion:
                    t_hermano.relacion.refresh_from_db()

            sub_leader = lote_tareas[0]

            # 🛠️ ALCABALA EN CALIENTE ORIGINAL (Sin mutaciones forzadas de estado en lote)
            if id_playlist_local != playlist_obj.id_playlist:
                url_snap = f"{url_base_spotify}{playlist_actual.id_spotify}?fields=snapshot_id"
                try:
                    r_snap = requests.get(url_snap, headers=headers, timeout=10)
                    if r_snap.status_code == 200:
                        snap_real = r_snap.json().get("snapshot_id")
                        existe_en_buffer = PlaylistSnapshotHistorial.objects.filter(playlist=playlist_actual, snapshot_id=snap_real).exists()
                        if not existe_en_buffer:
                            log_evento("WARNING", getattr(sub_leader.usuario, "username", None), "execute_tarea_lote_desfase_secundario", f"Desfase detectado en playlist secundaria '{playlist_actual.nombre}'. Corrigiendo...", source)
                            conciliar_playlist_con_spotify(id_playlist_local=playlist_actual.id_playlist, spotify_token=token, request_user=sub_leader.usuario)
                            playlist_actual.refresh_from_db()
                            for t_hermano in lote_tareas:
                                t_hermano.refresh_from_db()
                                if t_hermano.relacion: t_hermano.relacion.refresh_from_db()
                except Exception:
                    pass

            posiciones_reales = [t.relacion.posicion for t in lote_tareas if t.relacion and t.relacion.posicion]
            _posicion_pivote_N = min(posiciones_reales) if posiciones_reales else None
            _cantidad_eliminaciones = len(lote_tareas)

            sub_leader._posicion_real_eliminar = _posicion_pivote_N
            sub_leader._ancho_lote = _cantidad_eliminaciones

            tracks_payload = [{"uri": f"spotify:track:{t.relacion.cancion.id_spotify}"} for t in lote_tareas]
            log_evento("INFO", getattr(sub_leader.usuario, "username", None), "execute_tarea_lote_eliminar", 
                       f"Procesando sub-lote de eliminación para '{playlist_actual.nombre}' con {len(tracks_payload)} canciones. Pivote: {_posicion_pivote_N}", source)

            payload = {"tracks": tracks_payload}
            resp = requests.delete(url_tracks_actual, headers=headers, json=payload, timeout=12)
            resp.raise_for_status()
            
            snap_eliminar = resp.json().get("snapshot_id")
            if snap_eliminar:
                _save_snapshot_sucesion(playlist_actual, snap_eliminar)

            # Asentamiento Atómico por Playlist
            with transaction.atomic():
                if _posicion_pivote_N is not None:
                    recalcular_posiciones_tareas_pendientes(
                        tarea_ejecutada=sub_leader, 
                        posicion_anterior_movimiento=None, 
                        desplazamiento=_cantidad_eliminaciones
                    )

                ids_spotify_a_borrar = [t.relacion.cancion.id_spotify for t in lote_tareas]
                PlaylistCancion.objects.filter(
                    playlist=playlist_actual, 
                    cancion__id_spotify__in=ids_spotify_a_borrar, 
                    estado="activo"
                ).update(estado="eliminado", posicion=None)

                activas = PlaylistCancion.objects.filter(playlist=playlist_actual, estado="activo").order_by("posicion")
                for i, rel in enumerate(activas, start=1):
                    rel.posicion = i
                    rel.save(update_fields=["posicion"])

                for t_lote in lote_tareas:
                    if source == "manual" and request:
                        from django.contrib import messages as django_messages
                        sufijo_lote = " en lote." if ejecutar_como_lote else "."
                        msg_texto = f"La tarea Eliminar de '{t_lote.relacion.cancion.nombre}' en la playlist '{playlist_actual.nombre}' se ejecutó correctamente{sufijo_lote}"
                        django_messages.success(request, msg_texto)

                    t_lote.estado = "Completado"
                    t_lote.mensaje_error = None
                    if t_lote.relacion:
                        t_lote.relacion.fecha_sincronizacion = timezone.now()
                        t_lote.relacion.save(update_fields=["fecha_sincronizacion"])
                    t_lote.save(update_fields=["estado", "mensaje_error"])

    # =====================================================================
    # ---- OPERACIÓN: AGREGAR (Código 100% Original Sano) -----------------
    # =====================================================================
    elif tipo == "agregar":
        id_lote_actual = getattr(tarea, "id_lote", None)
        
        # 1. Separar las tareas del lote por playlist (si aplica)
        sub_lotes_playlist = defaultdict(list)
        if ejecutar_como_lote and id_lote_actual:
            lote_global = Tarea.objects.filter(
                id_lote=id_lote_actual,
                tipo__iexact="agregar",
                estado__in=["Pendiente", "En progreso"]
            ).annotate(
                orden_posicion=Case(
                    When(posicion=-1, then=99999),
                    When(posicion__isnull=True, then=99999),
                    default='posicion',
                    output_field=IntegerField(),
                )
            ).order_by('orden_posicion', 'id_tarea').select_related("relacion", "relacion__cancion", "relacion__playlist")
            
            for t_hermano in lote_global:
                sub_lotes_playlist[t_hermano.relacion.playlist_id].append(t_hermano)
        else:
            sub_lotes_playlist[playlist_obj.id_playlist].append(tarea)

        # 2. El Bucle: Recorrer cada playlist ejecutando la lógica con variables locales aisladas
        for id_playlist_local, lote_tareas in sub_lotes_playlist.items():
            playlist_actual = lote_tareas[0].relacion.playlist
            url_tracks_actual = f"{url_base_spotify}{playlist_actual.id_spotify}/tracks"
            
            for t_hermano in lote_tareas:
                t_hermano.refresh_from_db()
                if t_hermano.relacion:
                    t_hermano.relacion.refresh_from_db()

            tarea_guia = lote_tareas[0]
            tarea_guia._ancho_lote = len(lote_tareas)

            # Determinar posición de inyección
            tareas_con_posicion = [t for t in lote_tareas if t.posicion is not None and t.posicion > 0]
            posicion_guia = tareas_con_posicion[0].posicion if tareas_con_posicion else -1

            total_activas = PlaylistCancion.objects.filter(playlist=playlist_actual, estado="activo").count()
            new_pos = total_activas + 1 if posicion_guia == -1 else posicion_guia
            tarea_guia.posicion = new_pos

            # Armar URIs del lote de esta playlist
            uris_lote = [f"spotify:track:{t.relacion.cancion.id_spotify}" for t in lote_tareas]
            total_canciones_lote = len(uris_lote)
            
            # Peticiones físicas a Spotify usando los datos de la vuelta actual
            payload_add = {"uris": uris_lote}
            resp_add = requests.post(url_tracks_actual, headers=headers, json=payload_add, timeout=15)
            resp_add.raise_for_status()
            
            snap_post = resp_add.json().get("snapshot_id")
            if snap_post:
                _save_snapshot_sucesion(playlist_actual, snap_post)

            if new_pos <= total_activas:
                payload_move = {
                    "range_start": total_activas, 
                    "insert_before": new_pos - 1, 
                    "range_length": total_canciones_lote
                }
                resp_move = requests.put(url_tracks_actual, headers=headers, json=payload_move, timeout=15)
                resp_move.raise_for_status()
                
                snap_put = resp_move.json().get("snapshot_id")
                if snap_put:
                    _save_snapshot_sucesion(playlist_actual, snap_put)

            # Asentamiento en Base de Datos Local
            with transaction.atomic():
                PlaylistCancion.objects.filter(
                    playlist=playlist_actual, 
                    estado="activo", 
                    posicion__gte=new_pos
                ).update(posicion=F("posicion") + total_canciones_lote)

                for indice, t_lote in enumerate(lote_tareas):
                    posicion_cancion_lote = new_pos + indice
                    
                    t_lote.relacion.estado = "activo"
                    t_lote.relacion.posicion = posicion_cancion_lote
                    t_lote.relacion.save(update_fields=["estado", "posicion"])
                    
                    t_lote.posicion = -1 if posicion_guia == -1 else posicion_cancion_lote

                    if source == "manual" and request:
                        from django.contrib import messages as django_messages
                        sufijo_lote = " en lote." if ejecutar_como_lote else "."
                        msg_texto = f"La tarea Agregar de '{t_lote.relacion.cancion.nombre}' en la playlist '{playlist_actual.nombre}' se ejecutó correctamente{sufijo_lote}"
                        django_messages.success(request, msg_texto)

                    t_lote.estado = "Completado"
                    t_lote.mensaje_error = None
                    t_lote.relacion.fecha_sincronizacion = timezone.now()
                    t_lote.relacion.save(update_fields=["fecha_sincronizacion"])
                    t_lote.save(update_fields=["estado", "mensaje_error", "posicion"])

                if not (posicion_guia == -1):
                    recalcular_posiciones_tareas_pendientes(
                        tarea_ejecutada=tarea_guia,
                        posicion_anterior_movimiento=None,
                        desplazamiento=total_canciones_lote
                    )
                
                playlist_actual.total_canciones = PlaylistCancion.objects.filter(playlist=playlist_actual, estado="activo").count()
                playlist_actual.save(update_fields=["total_canciones"])

# =====================================================================
# CAPA 3: ORQUESTADOR PRINCIPAL (EL CAPITÁN)
# =====================================================================

def execute_tarea(tarea_id, source="manual", ejecutar_como_lote=False, request=None):
    """Orquestador maestro encargado de transiciones de estado, reintentos y captura de errores."""
    try:
        tarea = Tarea.objects.select_related(
            "usuario", "relacion", "relacion__playlist", "relacion__cancion"
        ).get(id_tarea=tarea_id)
    except Tarea.DoesNotExist:
        log_evento("ERROR", None, "execute_tarea", f"La tarea con ID {tarea_id} no existe.", source)
        return "Error", False, None

    playlist_obj = tarea.relacion.playlist
    cred = CredencialesSpotify.objects.first()

    if cred and cred.rate_limit_until and cred.rate_limit_until > timezone.now():
        tarea.estado = "Reprogramada"
        tarea.mensaje_error = f"Rate limit activo hasta {cred.rate_limit_until}"
        tarea.save(update_fields=["estado", "mensaje_error"])
        log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
        return tarea.estado, False, None

    token = get_spotify_token()
    headers = {"Authorization": f"Bearer {token}"}
    hubo_conciliacion_previa = False
    msg_final_conciliacion = None

    try:
        # ────────── PUNTO A: ALCABALA DE CONCILIACIÓN INICIAL ──────────
        hubo_conciliacion_previa, msg_final_conciliacion = _verificar_y_conciliar_playlist(
            tarea, playlist_obj, token, headers, source
        )

        # 🎯 SI HUBO CAMBIOS FÍSICOS Y ES MANUAL, MOSTRAMOS EL REPORTE EN LA INTERFAZ
        if hubo_conciliacion_previa and msg_final_conciliacion and source == "manual" and request:
            from django.contrib import messages as django_messages
            django_messages.info(request, msg_final_conciliacion)

        # Refrescos de contexto obligatorios post-conciliación
        tarea.refresh_from_db()
        if tarea.relacion:
            tarea.relacion.refresh_from_db()

        if tarea.estado != "Pendiente":
            return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion

        if tarea.relacion.estado == "eliminado":
            tarea.estado = "Error"
            tarea.mensaje_error = "No se puede ejecutar la tarea: el registro ya está eliminado localmente."
            tarea.save(update_fields=["estado", "mensaje_error"])
            return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion

        tarea.intentos += 1
        tarea.estado = "En progreso"
        tarea.save(update_fields=["estado", "intentos"])

        autocuracion_intentada = False
        tipo = tarea.tipo.strip().lower()

        while True:
            try:
                _ejecutar_accion_fisica_spotify(
                    tarea, playlist_obj, headers, tipo, token, source, 
                    ejecutar_como_lote=ejecutar_como_lote, request=request
                )
                break

            except (requests.exceptions.HTTPError, ValueError) as physical_error:
                status = physical_error.response.status_code if hasattr(physical_error, 'response') and physical_error.response else 400
                if status in (400, 403, 404, 409) and not autocuracion_intentada:
                    log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea_autocuracion", f"Fallo físico {status}. Ejecutando autocuración de emergencia...", source)
                    
                    resultado_emergencia = conciliar_playlist_con_spotify(id_playlist_local=playlist_obj.id_playlist, spotify_token=token, request_user=tarea.usuario)
                    tarea.refresh_from_db()
                    playlist_obj.refresh_from_db()

                    autocuracion_intentada = True
                    hubo_conciliacion_previa = True
                    msg_final_conciliacion = resultado_emergencia.get("mensaje")

                    # 🎯 SI LA AUTOCURACIÓN GENERÓ CAMBIOS, LOS DETALLAMOS EN LA UI
                    if msg_final_conciliacion and source == "manual" and request:
                        from django.contrib import messages as django_messages
                        django_messages.warning(request, f"[Autocuración] {msg_final_conciliacion}")

                    tarea.refresh_from_db()
                    playlist_obj.refresh_from_db()

                    autocuracion_intentada = True
                    hubo_conciliacion_previa = True

                    if tarea.estado != "En progreso" and tarea.estado != "Pendiente":
                        return tarea.estado, True, msg_final_conciliacion
                    
                    tarea.estado = "En progreso"
                    continue 
                else:
                    raise physical_error

        # =====================================================================
        # ASENTAMIENTO FINAL EXITOSO Y CÁLCULO DE DESPLAZAMIENTO DINÁMICO
        # =====================================================================
        posicion_original_db = Tarea.objects.filter(id_tarea=tarea_id).values_list('posicion', flat=True).first()

        # 1. 🛡️ CAPTURA DE EMERGENCIA PRE-REFRESH PARA ELIMINACIONES UNITARIAS
        # Si es una tarea individual de eliminar, salvamos la posición de la relación 
        # antes de que el refresh_from_db() la convierta en None.
        posicion_relacion_pre_refresh = tarea.relacion.posicion if (tipo == "eliminar" and tarea.relacion) else None

        # REFRESH OBLIGATORIO: Sincroniza estados de la Capa 2
        tarea.refresh_from_db()
        
        # Si por flujo individual estándar no pasó por el bloque de lotes, la aseguramos aquí
        if tarea.estado != "Completado":
            tarea.estado = "Completado"
            tarea.mensaje_error = None
            tarea.relacion.fecha_sincronizacion = timezone.now()
            with transaction.atomic():
                tarea.relacion.save(update_fields=["fecha_sincronizacion"])
                tarea.save(update_fields=["estado", "mensaje_error", "posicion"])

        try:
            if tipo == "agregar" and posicion_original_db == -1:
                log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea_consecuencias_skip", 
                           f"Tarea Agregar con posición '-1' (Último). Saltando re-indexación de consecuencias.", source)
            else:
                # 📐 CONTROL DE MAGNITUD DE ELEMENTOS (K)
                # Pasamos la cantidad de elementos viva (ej: 1, 3, 4) siempre en POSITIVO.
                valor_desplazamiento = getattr(tarea, "_ancho_lote", 1)

                # 🎯 PUENTE DE IDENTIDAD MATEMÁTICA PARA ELIMINAR
                if tipo == "eliminar":
                    # Si la Capa 2 inyectó una posición menor detectada del lote, la usamos.
                    # Si no, usamos la posición unitaria que respaldamos antes del refresh.
                    posicion_pivote = getattr(tarea, "_posicion_real_eliminar", None) or posicion_relacion_pre_refresh
                    
                    if posicion_pivote:
                        # Forzamos temporalmente en memoria para que N = tarea_ejecutada.relacion.posicion funcione
                        tarea.relacion.posicion = posicion_pivote
                        log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea_pivote_eliminar", 
                                   f"Inyectando pivote de colapso N={posicion_pivote} para eliminar {valor_desplazamiento} tracks.", source)
                
                # 🎯 PUENTE DE IDENTIDAD MATEMÁTICA PARA AGREGAR
                elif ejecutar_como_lote and tipo == "agregar":
                    log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea_pivote_lote", 
                               f"Disparando consecuencias desde la posición menor del lote: {tarea.posicion}", source)

                pos_anterior_mov = (tarea.posicion_anterior or tarea.relacion.posicion) if tipo == "posicionar" else None
                
                # 🚀 EJECUCIÓN MAESTRA DEL RECALCULADOR (CON PARÁMETROS LIMPIOS Y POSITIVOS)
                recalcular_posiciones_tareas_pendientes(
                    tarea, 
                    posicion_anterior_movimiento=pos_anterior_mov,
                    desplazamiento=valor_desplazamiento  # Tu función se encarga de restar usando este entero positivo
                )
                        
        except Exception as ce:
            log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea_consecuencias", f"Error aislado en consecuencias: {str(ce)}", source)

        playlist_obj.refresh_from_db()
        with transaction.atomic():
            playlist_obj.total_canciones = PlaylistCancion.objects.filter(playlist=playlist_obj, estado="activo").count()
            playlist_obj.save(update_fields=["total_canciones"])

        log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea", f"Tarea {tarea.tipo} completada con éxito.", source)
        return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 500
        if status == 429:
            retry_after = int(e.response.headers.get("Retry-After", 60)) if e.response else 60
            if cred:
                cred.rate_limit_until = timezone.now() + timezone.timedelta(seconds=retry_after)
                cred.save(update_fields=["rate_limit_until"])
            tarea.estado = "Reprogramada"
            tarea.mensaje_error = f"Rate limit, reintentar en {retry_after}s"
        elif status in (400, 403, 404):
            tarea.estado = "Error"
            tarea.mensaje_error = f"Error definitivo {status}: {e.response.text if e.response else str(e)}"
        else:
            tarea.estado = "Error temporal"
            tarea.mensaje_error = f"Error temporal {status}: {str(e)}"

        tarea.save(update_fields=["estado", "mensaje_error"])
        log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
        return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion

    except Exception as e:
        tarea.estado = "Error"
        tarea.mensaje_error = str(e)
        tarea.save(update_fields=["estado", "mensaje_error"])
        log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
        return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion