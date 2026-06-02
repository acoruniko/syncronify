import requests
from django.utils import timezone
from django.db.models import F
from django.db import transaction
from conexion.models import CredencialesSpotify
from conexion.services import get_spotify_token
from playlists.models import Tarea, PlaylistCancion
from logs.models import LogEvento
from playlists.services import recalcular_posiciones_tareas_pendientes, conciliar_playlist_con_spotify

def log_evento(nivel, usuario, modulo, mensaje, source="manual"):
    if source == "celery":
        LogEvento.objects.create(
            fecha=timezone.now(),
            nivel=nivel,
            usuario=usuario or "celery",
            modulo=modulo,
            mensaje=mensaje
        )

def execute_tarea(tarea_id, source="manual"):
    # 1. CARGA INICIAL COMPLETA
    try:
        tarea = Tarea.objects.select_related(
            "usuario", 
            "relacion", 
            "relacion__playlist", 
            "relacion__cancion"
        ).get(id_tarea=tarea_id)
    except Tarea.DoesNotExist:
        log_evento("ERROR", None, "execute_tarea", f"La tarea con ID {tarea_id} no existe.", source)
        return "Error", False, None

    playlist_obj = tarea.relacion.playlist
    cred = CredencialesSpotify.objects.first()

    # ⚠️ VERIFICACIÓN PREVIA DE RATE LIMIT EXTERNO
    if cred and cred.rate_limit_until and cred.rate_limit_until > timezone.now():
        tarea.estado = "Reprogramada"
        tarea.mensaje_error = f"Rate limit activo hasta {cred.rate_limit_until}"
        tarea.save(update_fields=["estado", "mensaje_error"])
        log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
        return tarea.estado, False, None

    # 🛠️ FUNCIÓN INTERNA DE SOPORTE: El Pasamanos Matemático Universal
    def _save_snapshot_sucesion(obj_playlist, nuevo_snap):
        """Aplica el desplazamiento temporal: el presente pasa al pasado, el nuevo al presente."""
        if nuevo_snap:
            obj_playlist.snapshot_anterior = obj_playlist.snapshot_ahorita
            obj_playlist.snapshot_ahorita = nuevo_snap
            obj_playlist.save(update_fields=["snapshot_anterior", "snapshot_ahorita"])

    try:
        # 🔑 2. CENTRALIZACIÓN DEL TOKEN
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}

        # 📡 3. CONSULTA LIGERA DEL SNAPSHOT REAL EN SPOTIFY
        url_playlist_master = f"https://api.spotify.com/v1/playlists/{playlist_obj.id_spotify}"
        resp_snapshot = requests.get(url_playlist_master, headers=headers, timeout=12)
        resp_snapshot.raise_for_status()
        snapshot_spotify_real = resp_snapshot.json().get("snapshot_id")

        # 🔄 4. CONDICIONAL DE CONCILIACIÓN CON ESCUDO DOBLE
        tarea._telemetria_sincronizacion = {
            "desfase_detectado": False,
            "snapshot_anterior_local": playlist_obj.snapshot_anterior,
            "snapshot_ahorita_local": playlist_obj.snapshot_ahorita,
            "snapshot_spotify": snapshot_spotify_real,
            "conciliacion_ejecutada": False,
            "mensaje": f"Sincronización limpia. Spotify coincide con el estado actual ({snapshot_spotify_real})."
        }

        # 🛡️ VALIDACIÓN EN CADENA: ¿Coincide con el presente O con el pasado inmediato de la caché?
        if snapshot_spotify_real == playlist_obj.snapshot_ahorita:
            hubo_conciliacion_previa = False
            msg_final_conciliacion = None
        elif snapshot_spotify_real == playlist_obj.snapshot_anterior:
            # 🟢 SEGUNDA OPORTUNIDAD (Amortiguador de Caché activo)
            hubo_conciliacion_previa = False
            msg_final_conciliacion = None
            tarea._telemetria_sincronizacion["mensaje"] = (
                f"Sincronización validada por amortiguador de caché (coincide con snapshot_anterior: {snapshot_spotify_real}). "
                "Evitando actualización innecesaria."
            )
            log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea_cache_guard",
                       f"Playlist '{playlist_obj.nombre}' en ráfaga. Spotify devolvió snapshot_anterior. Avanzando de forma segura.", source)
        else:
            # 🚨 DISCREPANCIA REAL EXTERNA: No coincide con ninguno de nuestros dos registros históricos
            tarea._telemetria_sincronizacion["desfase_detectado"] = True
            
            log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea",
                       f"La Playlist '{playlist_obj.nombre}' se encuentra desactualizada externamente. Forzando actualización...", source)
            
            resultado_conciliacion = conciliar_playlist_con_spotify(
                id_playlist_local=playlist_obj.id_playlist,
                spotify_token=token,
                request_user=tarea.usuario
            )

            if not resultado_conciliacion.get("ok"):
                raise Exception(f"Fallo crítico en servicio de conciliación: {resultado_conciliacion.get('error')}")

            msg_servicio_completo = resultado_conciliacion.get("mensaje")
            tarea._telemetria_sincronizacion["conciliacion_ejecutada"] = True
            tarea._telemetria_sincronizacion["mensaje"] = msg_servicio_completo

            # 🛡️ 5. REEVALUACIÓN Y BLINDAJE DE ESTADO (Tras conciliación externa)
            tarea.refresh_from_db()
            tarea.relacion.refresh_from_db()
            
            if tarea.estado != "Pendiente":
                log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea", 
                           f"Ejecución detenida tras conciliación. Estado actual: {tarea.estado}", source)
                return tarea.estado, True, msg_servicio_completo

            # 🔄 SUCESIÓN TRAS EL CONCILIO: Forzamos el pasamanos con el snapshot fresco que estabilizó el servicio
            playlist_obj.refresh_from_db()
            _save_snapshot_sucesion(playlist_obj, snapshot_spotify_real)
            
            hubo_conciliacion_previa = True
            msg_final_conciliacion = msg_servicio_completo

        # 🛑 6. SEGUNDA BARRERA DE SEGURIDAD INTERNA (Data local validada)
        if tarea.relacion.estado == "eliminado":
            tarea.estado = "Error"
            tarea.mensaje_error = "No se puede ejecutar la tarea: la canción ya está marcada como eliminada localmente."
            tarea.save(update_fields=["estado", "mensaje_error"])
            log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
            return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion

        # 🚀 Si pasamos estas alcabalas, la data es 100% idéntica o segura para operar
        tarea.intentos += 1
        tarea.estado = "En progreso"
        tarea.save(update_fields=["estado", "intentos"])

        # 🎛️ INICIALIZACIÓN PREVENTIVA DE VARIABLES
        autocuracion_intentada = False
        tipo = tarea.tipo.strip().lower()
        old_pos = None

        # =====================================================================
        # BUCLE AISLADO DE EJECUCIÓN FÍSICA Y AUTOCURACIÓN
        # =====================================================================
        while True:
            try:
                intento_str = "[INTENTO 2 - POST-CURA]" if autocuracion_intentada else "[INTENTO 1]"
                log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea",
                           f"{intento_str} Iniciando bloque físico para tarea {tarea.tipo} de '{tarea.relacion.cancion.nombre}'", source)

                track_spotify_id = tarea.relacion.cancion.id_spotify
                playlist_spotify_id = playlist_obj.id_spotify
                snapshot_id_nuevo = None

                # 👉 POSICIONAR
                if tipo == "posicionar":
                    old_pos = tarea.posicion_anterior or tarea.relacion.posicion

                    # 🛡️ ESCUDO ANTI-NONE TYPE
                    if old_pos is None:
                        tarea.estado = "Error"
                        tarea.mensaje_error = "No se puede posicionar: la canción no cuenta con una posición de origen válida en la BD local."
                        tarea.save(update_fields=["estado", "mensaje_error"])
                        log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea_posicion_none", tarea.mensaje_error, source)
                        return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion
                    
                    new_pos = tarea.posicion
                    
                    # 🛡️ ESCUDO DE IDENTIDAD
                    indice_verificacion = old_pos - 1
                    url_verificar = f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks?offset={indice_verificacion}&limit=1"
                    
                    try:
                        resp_verif = requests.get(url_verificar, headers=headers, timeout=10)
                        resp_verif.raise_for_status()
                        items_verif = resp_verif.json().get("items", [])
                        
                        if not items_verif:
                            raise ValueError("Índice vacío o fuera de rango en Spotify.")
                        
                        track_real_id = items_verif[0].get("track", {}).get("id")
                        
                        # 🚨 ¡CONFLICTO DETECTADO! Desfase silencioso de índices
                        if track_real_id != track_spotify_id:
                            log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea_desfase_fantasma",
                                       f"Desfase fantasma detectado. Se esperaba '{track_spotify_id}' en índice {indice_verificacion} pero se encontró '{track_real_id}'. Curando e intentando de inmediato...", source)
                            
                            resultado_emergencia = conciliar_playlist_con_spotify(
                                id_playlist_local=playlist_obj.id_playlist,
                                spotify_token=token,
                                request_user=tarea.usuario
                            )
                            
                            tarea.refresh_from_db()
                            playlist_obj.refresh_from_db()
                            
                            # 🔄 SUCESIÓN TRAS CONCILIO DE EMERGENCIA: Extraemos el snapshot que resultó de la reparación
                            url_snap_em = f"https://api.spotify.com/v1/playlists/{playlist_obj.id_spotify}"
                            try:
                                r_em = requests.get(url_snap_em, headers=headers, timeout=8)
                                s_em = r_em.json().get("snapshot_id")
                                _save_snapshot_sucesion(playlist_obj, s_em)
                            except Exception:
                                pass

                            hubo_conciliacion_previa = True
                            msg_final_conciliacion = resultado_emergencia.get("mensaje")
                            
                            if tarea.estado != "En progreso" and tarea.estado != "Pendiente":
                                return tarea.estado, True, msg_final_conciliacion
                            
                            tarea.estado = "En progreso"
                            continue
                            
                    except Exception as e_verif:
                        log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea_error_verificacion",
                                   f"Error al verificar identidad del track ({str(e_verif)}). Forzando actualización y re-intento.", source)
                        
                        resultado_emergencia = conciliar_playlist_con_spotify(
                            id_playlist_local=playlist_obj.id_playlist,
                            spotify_token=token,
                            request_user=tarea.usuario
                        )
                        tarea.refresh_from_db()
                        playlist_obj.refresh_from_db()
                        
                        # 🔄 SUCESIÓN TRAS CONCILIO EN EXCEPCIÓN DE VERIFICACIÓN
                        try:
                            r_em = requests.get(f"https://api.spotify.com/v1/playlists/{playlist_obj.id_spotify}", headers=headers, timeout=8)
                            _save_snapshot_sucesion(playlist_obj, r_em.json().get("snapshot_id"))
                        except Exception:
                            pass

                        hubo_conciliacion_previa = True
                        msg_final_conciliacion = resultado_emergencia.get("mensaje")
                        
                        if tarea.estado != "En progreso" and tarea.estado != "Pendiente":
                            return tarea.estado, True, msg_final_conciliacion
                        
                        tarea.estado = "En progreso"
                        continue

                    # 🟢 SI PASA EL ESCUDO: Procedemos con el movimiento físico.
                    total_items = PlaylistCancion.objects.filter(
                        playlist=playlist_obj,
                        estado="activo"
                    ).count()

                    range_start = old_pos - 1
                    if new_pos == total_items:
                        insert_before = total_items
                    elif new_pos > old_pos:
                        insert_before = new_pos
                    else:
                        insert_before = new_pos - 1

                    payload = {
                        "range_start": range_start,
                        "insert_before": insert_before,
                        "range_length": 1
                    }
                    resp = requests.put(
                        f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                        headers=headers,
                        json=payload,
                        timeout=12
                    )
                    resp.raise_for_status()
                    snapshot_id_nuevo = resp.json().get("snapshot_id")

                    with transaction.atomic():
                        if new_pos > old_pos:
                            PlaylistCancion.objects.filter(
                                playlist=playlist_obj,
                                estado="activo",
                                posicion__gt=old_pos,
                                posicion__lte=new_pos
                            ).update(posicion=F("posicion") - 1)
                        elif new_pos < old_pos:
                            PlaylistCancion.objects.filter(
                                playlist=playlist_obj,
                                estado="activo",
                                posicion__gte=new_pos,
                                posicion__lt=old_pos
                            ).update(posicion=F("posicion") + 1)

                        tarea.relacion.posicion = new_pos
                        tarea.posicion_anterior = old_pos
                        tarea.save(update_fields=["posicion_anterior"])
                        tarea.relacion.save(update_fields=["posicion"])
                        
                        # 🔄 SUCESIÓN FÍSICA INMEDIATA: Desplazamos los snapshots en DB de forma atómica
                        if snapshot_id_nuevo:
                            _save_snapshot_sucesion(playlist_obj, snapshot_id_nuevo)

                # 👉 ELIMINAR
                elif tipo == "eliminar":
                    payload = {"tracks": [{"uri": f"spotify:track:{track_spotify_id}"}]}
                    resp = requests.delete(
                        f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                        headers=headers,
                        json=payload,
                        timeout=12
                    )
                    resp.raise_for_status()
                    snapshot_id_nuevo = resp.json().get("snapshot_id")

                    with transaction.atomic():
                        PlaylistCancion.objects.filter(
                            playlist=playlist_obj,
                            cancion__id_spotify=track_spotify_id,
                            estado="activo"
                        ).update(estado="eliminado", posicion=None)

                        activas = PlaylistCancion.objects.filter(playlist=playlist_obj, estado="activo").order_by("posicion")
                        for i, rel in enumerate(activas, start=1):
                            rel.posicion = i
                            rel.save(update_fields=["posicion"])

                        playlist_obj.total_canciones = activas.count()
                        # 🔄 SUCESIÓN FÍSICA INMEDIATA
                        if snapshot_id_nuevo:
                            _save_snapshot_sucesion(playlist_obj, snapshot_id_nuevo)
                        else:
                            playlist_obj.save(update_fields=["total_canciones"])

                # 👉 AGREGAR
                elif tipo == "agregar":
                    total_activas = PlaylistCancion.objects.filter(playlist=playlist_obj, estado="activo").count()
                    
                    if tarea.posicion == -1:
                        new_pos = total_activas + 1
                        tarea.posicion = new_pos  
                    else:
                        new_pos = tarea.posicion

                    # 📡 Inserción física en Spotify
                    payload_add = {"uris": [f"spotify:track:{track_spotify_id}"]}
                    resp_add = requests.post(
                        f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                        headers=headers,
                        json=payload_add,
                        timeout=12
                    )
                    resp_add.raise_for_status()
                    snapshot_id_nuevo = resp_add.json().get("snapshot_id")

                    # Sub-bloque de posicionamiento con Rollback Externo
                    if new_pos <= total_activas:
                        range_start = total_activas  
                        insert_before = new_pos - 1

                        payload_move = {
                            "range_start": range_start,
                            "insert_before": insert_before,
                            "range_length": 1
                        }
                        
                        try:
                            resp_move = requests.put(
                                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                                headers=headers,
                                json=payload_move,
                                timeout=12
                            )
                            resp_move.raise_for_status()
                            snapshot_id_nuevo = resp_move.json().get("snapshot_id")
                        
                        except Exception as e:
                            log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea_rollback_add",
                                       f"Fallo al posicionar track agregado. Ejecutando borrado de emergencia en Spotify para evitar duplicados.", source)
                            
                            payload_rollback = {"tracks": [{"uri": f"spotify:track:{track_spotify_id}"}]}
                            try:
                                requests.delete(
                                    f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                                    headers=headers,
                                    json=payload_rollback,
                                    timeout=10
                                )
                            except Exception as delete_error:
                                log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea_rollback_critical",
                                           f"No se pudo ejecutar el rollback de emergencia en Spotify: {str(delete_error)}", source)
                            raise e

                    with transaction.atomic():
                        PlaylistCancion.objects.filter(
                            playlist=playlist_obj,
                            estado="activo",
                            posicion__gte=new_pos
                        ).update(posicion=F("posicion") + 1)

                        tarea.relacion.estado = "activo"
                        tarea.relacion.posicion = new_pos
                        tarea.relacion.save(update_fields=["estado", "posicion"])

                        playlist_obj.total_canciones = PlaylistCancion.objects.filter(playlist=playlist_obj, estado="activo").count()
                        # 🔄 SUCESIÓN FÍSICA INMEDIATA
                        if snapshot_id_nuevo:
                            _save_snapshot_sucesion(playlist_obj, snapshot_id_nuevo)
                        else:
                            playlist_obj.save(update_fields=["total_canciones"])

                else:
                    tarea.estado = "Error"
                    tarea.mensaje_error = f"Tipo de tarea inválido: {tarea.tipo}"
                    tarea.save(update_fields=["estado", "mensaje_error"])
                    log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
                    return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion
                
                # 🟢 EL BLOQUE FÍSICO FUE UN ÉXITO
                break

            # 🛡️ CAPTURA DE ERRORES EXCLUSIVA DEL BLOQUE FÍSICO (API REST)
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 500
                
                if status in (400, 403, 404):
                    if not autocuracion_intentada:
                        log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea_autocuracion", 
                                   f"Fallo a ciegas {status}. Snapshot engañoso. Forzando actualización real...", source)
                        
                        resultado_emergencia = conciliar_playlist_con_spotify(
                            id_playlist_local=playlist_obj.id_playlist,
                            spotify_token=token,
                            request_user=tarea.usuario
                        )
                        
                        tarea.refresh_from_db()
                        playlist_obj.refresh_from_db()
                        
                        # 🔄 SUCESIÓN EN AUTOCURACIÓN FALLIDA
                        try:
                            r_em = requests.get(f"https://api.spotify.com/v1/playlists/{playlist_obj.id_spotify}", headers=headers, timeout=8)
                            _save_snapshot_sucesion(playlist_obj, r_em.json().get("snapshot_id"))
                        except Exception:
                            pass

                        if tarea.estado != "En progreso" and tarea.estado != "Pendiente":
                            return tarea.estado, True, resultado_emergencia.get("mensaje")
                        
                        tarea.estado = "En progreso"
                        autocuracion_intentada = True
                        hubo_conciliacion_previa = True
                        msg_final_conciliacion = resultado_emergencia.get("mensaje")
                        continue 
                    else:
                        tarea.estado = "Error"
                        tarea.mensaje_error = f"Error definitivo {status}: {e.response.text if e.response else str(e)}"
                        tarea.save(update_fields=["estado", "mensaje_error"])
                        log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
                        return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion
                
                elif status == 429:
                    retry_after = int(e.response.headers.get("Retry-After", 60)) if e.response else 60
                    if cred:
                        cred.rate_limit_until = timezone.now() + timezone.timedelta(seconds=retry_after)
                        cred.save(update_fields=["rate_limit_until"])
                    tarea.estado = "Reprogramada"
                    tarea.mensaje_error = f"Rate limit, reintentar en {retry_after}s"
                    tarea.save(update_fields=["estado", "mensaje_error"])
                    log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
                    return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion
                else:
                    tarea.estado = "Error temporal"
                    tarea.mensaje_error = f"Error temporal {status}: {str(e)}"
                    tarea.save(update_fields=["estado", "mensaje_error"])
                    log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
                    return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion

            except Exception as e:
                tarea.estado = "Error"
                tarea.mensaje_error = f"Fallo interno en bucle de ejecución: {str(e)}"
                tarea.save(update_fields=["estado", "mensaje_error"])
                log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea_bucle_interno", tarea.mensaje_error, source)
                return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion

        # =====================================================================
        # BLOQUE FINAL DE ÉXITO CONSOLIDADO
        # =====================================================================
        # 1. Guardamos el estado de la tarea actual
        tarea.estado = "Completado"
        tarea.mensaje_error = None
        tarea.relacion.fecha_sincronizacion = timezone.now()
        
        with transaction.atomic():
            tarea.relacion.save(update_fields=["fecha_sincronizacion"])
            tarea.save(update_fields=["estado", "mensaje_error", "posicion"])

        # 2. Ejecutamos el servicio de consecuencias
        consecuencias_reportadas = []
        try:
            pos_anterior = old_pos if tipo == "posicionar" else None
            consecuencias_reportadas = recalcular_posiciones_tareas_pendientes(tarea, posicion_anterior_movimiento=pos_anterior)
        except Exception as ce:
            log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea_consecuencias", f"Error crítico aislado al recalcular consecuencias: {str(ce)}", source)

        # 3. Sincronización del total de canciones del disco
        playlist_obj.refresh_from_db() 
        playlist_obj.total_canciones = PlaylistCancion.objects.filter(playlist=playlist_obj, estado="activo").count()
        
        # 🛡️ ASENTAMIENTO ATÓMICO FINAL: Fijamos el total de canciones.
        # Quitamos la asignación simple directa de 'playlist_obj.snapshot_id = snapshot_id_nuevo' 
        # porque la función interna '_save_snapshot_sucesion' ya desplazó y guardó los campos 
        # 'snapshot_ahorita' y 'snapshot_anterior' de forma segura e inmediata dentro del bucle.
        with transaction.atomic():
            playlist_obj.save(update_fields=["total_canciones"])

        log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea",
                   f"La tarea {tarea.tipo} de '{tarea.relacion.cancion.nombre}' en la playlist '{playlist_obj.nombre}' fue ejecutada correctamente.", source)

        tarea._consecuencias = consecuencias_reportadas 
        
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