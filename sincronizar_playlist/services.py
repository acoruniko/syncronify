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

    try:
        # 🔑 2. CENTRALIZACIÓN DEL TOKEN
        token = get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}

        # 📡 3. CONSULTA LIGERA DEL SNAPSHOT REAL EN SPOTIFY
        url_playlist_master = f"https://api.spotify.com/v1/playlists/{playlist_obj.id_spotify}"
        resp_snapshot = requests.get(url_playlist_master, headers=headers, timeout=12)
        resp_snapshot.raise_for_status()
        snapshot_spotify_real = resp_snapshot.json().get("snapshot_id")

        # 🔄 4. CONDICIONAL DE CONCILIACIÓN
        tarea._telemetria_sincronizacion = {
            "desfase_detectado": False,
            "snapshot_local": playlist_obj.snapshot_id,
            "snapshot_spotify": snapshot_spotify_real,
            "conciliacion_ejecutada": False,
            "mensaje": f"Sincronización limpia. Los snapshots coinciden ({snapshot_spotify_real})."
        }

        # Si difieren, disparamos la conciliación
        if playlist_obj.snapshot_id != snapshot_spotify_real:
            tarea._telemetria_sincronizacion["desfase_detectado"] = True
            
            log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea",
                       f"La Playlist '{playlist_obj.nombre}' se encuentra desactualizada. Forzando actualización...", source)
            
            resultado_conciliacion = conciliar_playlist_con_spotify(
                id_playlist_local=playlist_obj.id_playlist,
                forzar_actualizacion=True,
                spotify_token=token,
                request_user=tarea.usuario
            )

            if not resultado_conciliacion.get("ok"):
                raise Exception(f"Fallo crítico en servicio de conciliación: {resultado_conciliacion.get('error')}")

            # 🛠️ CAPTURAMOS EL REPORTE DINÁMICO DE CONSECUENCIAS GENERADO POR EL SERVICIO
            msg_servicio_completo = resultado_conciliacion.get("mensaje")

            tarea._telemetria_sincronizacion["conciliacion_ejecutada"] = True
            tarea._telemetria_sincronizacion["mensaje"] = msg_servicio_completo

            # 🛡️ 5. REEVALUACIÓN Y BLINDAJE DE ESTADO (Dentro de la conciliación)
            tarea.refresh_from_db()
            
            if tarea.estado != "Pendiente":
                log_evento("WARNING", getattr(tarea.usuario, "username", None), "execute_tarea", 
                           f"Ejecución detenida tras conciliación. Estado actual: {tarea.estado}", source)
                # 🟢 CORREGIDO: Si la tarea que íbamos a ejecutar se anuló o mutó en la conciliación,
                # retornamos el reporte real con sus consecuencias directo a la interfaz.
                return tarea.estado, True, msg_servicio_completo

            playlist_obj.refresh_from_db()
            hubo_conciliacion_previa = True
            # Guardamos el mensaje para usarlo en el retorno de éxito final si pasa el blindaje
            msg_final_conciliacion = msg_servicio_completo
        else:
            hubo_conciliacion_previa = False
            msg_final_conciliacion = None

        # 🛑 6. SEGUNDA BARRERA DE SEGURIDAD INTERNA (Data local validada)
        if tarea.relacion.estado == "eliminado":
            tarea.estado = "Error"
            tarea.mensaje_error = "No se puede ejecutar la tarea: la canción ya está marcada como eliminada localmente."
            tarea.save(update_fields=["estado", "mensaje_error"])
            log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
            return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion

        # 🚀 Si pasamos estas alcabalas, la data es 100% idéntica a la de Spotify. 
        tarea.intentos += 1
        tarea.estado = "En progreso"
        tarea.save(update_fields=["estado", "intentos"])

        log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea",
                   f"Iniciando bloque físico para tarea {tarea.tipo} de '{tarea.relacion.cancion.nombre}'", source)

        # =====================================================================
        # BLOQUE DE EJECUCIÓN FÍSICA (DATA 100% COHERENTE Y VALIDADA)
        # =====================================================================
        track_spotify_id = tarea.relacion.cancion.id_spotify
        playlist_spotify_id = playlist_obj.id_spotify
        tipo = tarea.tipo.strip().lower()
        snapshot_id_nuevo = None

        # 👉 POSICIONAR
        if tipo == "posicionar":
            old_pos = tarea.posicion_anterior or tarea.relacion.posicion
            new_pos = tarea.posicion
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
                
                if snapshot_id_nuevo:
                    playlist_obj.snapshot_id = snapshot_id_nuevo
                    playlist_obj.save(update_fields=["snapshot_id"])

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
                if snapshot_id_nuevo:
                    playlist_obj.snapshot_id = snapshot_id_nuevo
                playlist_obj.save(update_fields=["total_canciones", "snapshot_id"])

        # 👉 AGREGAR
        elif tipo == "agregar":
            total_activas = PlaylistCancion.objects.filter(playlist=playlist_obj, estado="activo").count()
            
            if tarea.posicion == -1:
                new_pos = total_activas + 1
                tarea.posicion = new_pos  
            else:
                new_pos = tarea.posicion

            payload_add = {"uris": [f"spotify:track:{track_spotify_id}"]}
            resp_add = requests.post(
                f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                headers=headers,
                json=payload_add,
                timeout=12
            )
            resp_add.raise_for_status()
            snapshot_id_nuevo = resp_add.json().get("snapshot_id")

            if new_pos <= total_activas:
                range_start = total_activas  
                insert_before = new_pos - 1

                payload_move = {
                    "range_start": range_start,
                    "insert_before": insert_before,
                    "range_length": 1
                }
                resp_move = requests.put(
                    f"https://api.spotify.com/v1/playlists/{playlist_spotify_id}/tracks",
                    headers=headers,
                    json=payload_move,
                    timeout=12
                )
                resp_move.raise_for_status()
                snapshot_id_nuevo = resp_move.json().get("snapshot_id")

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
                if snapshot_id_nuevo:
                    playlist_obj.snapshot_id = snapshot_id_nuevo
                playlist_obj.save(update_fields=["total_canciones", "snapshot_id"])

        else:
            tarea.estado = "Error"
            tarea.mensaje_error = f"Tipo de tarea inválido: {tarea.tipo}"
            tarea.save(update_fields=["estado", "mensaje_error"])
            log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea", tarea.mensaje_error, source)
            return tarea.estado, hubo_conciliacion_previa, msg_final_conciliacion

        # =====================================================================
        # BLOQUE FINAL DE ÉXITO CONSOLIDADO (Al final de execute_tarea)
        # =====================================================================
        tarea.estado = "Completado"
        tarea.mensaje_error = None
        tarea.relacion.fecha_sincronizacion = timezone.now()
        
        with transaction.atomic():
            tarea.relacion.save(update_fields=["fecha_sincronizacion"])
            tarea.save(update_fields=["estado", "mensaje_error", "posicion"])

        consecuencias_reportadas = []
        try:
            pos_anterior = old_pos if tipo == "posicionar" else None
            consecuencias_reportadas = recalcular_posiciones_tareas_pendientes(tarea, posicion_anterior_movimiento=pos_anterior)
        except Exception as ce:
            log_evento("ERROR", getattr(tarea.usuario, "username", None), "execute_tarea_consecuencias", f"Error crítico aislado al recalcular consecuencias: {str(ce)}", source)

        log_evento("INFO", getattr(tarea.usuario, "username", None), "execute_tarea",
                   f"La tarea {tarea.tipo} de '{tarea.relacion.cancion.nombre}' en la playlist '{playlist_obj.nombre}' fue ejecutada correctamente.", source)

        tarea._consecuencias = consecuencias_reportadas 
        
        # 🟢 CORREGIDO: Si se ejecutó con éxito pero antes hubo una conciliación por desfase,
        # devolvemos el reporte real completo que generó el servicio en lugar del texto fijo.
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