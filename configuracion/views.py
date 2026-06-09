from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.http import require_POST
from usuarios.models import Usuario # Asegúrate de que la ruta sea correcta
from django.contrib import messages
from django.template.loader import render_to_string
from django.db import connection
from django.utils import timezone
from django_celery_beat.models import PeriodicTask, IntervalSchedule
from datetime import timedelta
import json
import math


@login_required
def configuracion_home_view(request):
    if not request.user.es_admin():
        return HttpResponseForbidden("Acceso denegado.")
    
    # 1. Obtener la tarea y el schedule
    schedule_maestro, _ = IntervalSchedule.objects.get_or_create(
        id=1, 
        defaults={'every': 1, 'period': IntervalSchedule.HOURS}
    )
    
    tarea, _ = PeriodicTask.objects.get_or_create(
        name='syncronify_main_task',
        defaults={'task': 'sincronizar_playlist.tasks.process_due_tasks', 'interval': schedule_maestro}
    )

    # 2. PROCESAR CAMBIO DE INTERVALO
    if request.method == "POST":
        nuevo_intervalo = request.POST.get("intervalo_horas")
        if nuevo_intervalo:
            # Actualizamos el valor y nos aseguramos que el periodo sea horas
            schedule_maestro.every = int(nuevo_intervalo)
            schedule_maestro.period = IntervalSchedule.HOURS 
            schedule_maestro.save()
            
            messages.success(request, f"Intervalo de las tareas programadas actualizado a {nuevo_intervalo} horas.")
            return redirect('configuracion_home')

    # 3. LÓGICA DE TIEMPO REAL (Conversión a Horas)
    ahora = timezone.now()
    
    intervalo_segundos = tarea.interval.every * 3600
    
    referencia = tarea.last_run_at if tarea.last_run_at else ahora
    
    # Próxima ejecución teórica
    proxima_ejecucion = referencia + timedelta(seconds=intervalo_segundos)
    
    # Ajuste para evitar números negativos si el worker se retrasa
    if proxima_ejecucion < ahora:
        segundos_desde_vencimiento = (ahora - proxima_ejecucion).total_seconds()
        vueltas_perdidas = math.ceil(segundos_desde_vencimiento / intervalo_segundos)
        proxima_ejecucion = proxima_ejecucion + timedelta(seconds=vueltas_perdidas * intervalo_segundos)

    segundos_restantes = int((proxima_ejecucion - ahora).total_seconds())

    # Formateo para el display
    h, rem = divmod(max(0, segundos_restantes), 3600)
    m, s = divmod(rem, 60)
    tiempo_humanizado = f"{h}h {m}m {s}s"

    return render(request, 'configuracion/configuracion_home.html', {
        'usuarios': Usuario.objects.filter(eliminado=False).order_by('nombre_completo'),
        'intervalo_actual': tarea.interval.every, # Este valor ahora es 1, 2, 3... (horas)
        'segundos_restantes': segundos_restantes,
        'tiempo_restante': tiempo_humanizado
    })


@login_required
@require_POST
def actualizar_usuario_ajax(request):
    if not request.user.es_admin():
        return JsonResponse({"success": False, "error": "No autorizado"}, status=403)

    try:
        data = json.loads(request.body)
        username_target = data.get("username")
        usuario = Usuario.objects.get(username=username_target)
        
        msg_personalizado = ""

        # --- ACTUALIZACIÓN DE ESTADO ---
        if "activo" in data:
            nuevo_estado = data.get("activo")
            if request.user.username == username_target and nuevo_estado is False:
                return JsonResponse({'status': 'error', 'mensaje': 'No puedes desactivar tu propia cuenta.'}, status=400)
            
            usuario.is_active = nuevo_estado
            estado_txt = "Activado" if nuevo_estado else "Desactivado"
            msg_personalizado = f"El usuario {username_target} ha sido {estado_txt.lower()}."
            messages.success(request, msg_personalizado)

        # --- ACTUALIZACIÓN DE ROLES ---
        if "rol" in data:
            # Lógica para detectar qué rol cambió exactamente
            viejos_roles = set(r.strip() for r in usuario.rol.split(',') if r.strip())
            nuevos_roles = set(r.strip() for r in data.get("rol").split(',') if r.strip())
            
            if request.user.username == username_target and "admin" not in nuevos_roles:
                return JsonResponse({'status': 'error', 'mensaje': 'No puedes quitarte el rol admin a ti mismo.'}, status=400)

            # Detectamos la diferencia
            agregados = nuevos_roles - viejos_roles
            quitados = viejos_roles - nuevos_roles

            detalles_rol = []
            # Diccionario para nombres bonitos
            nombres_roles = {'admin': 'Administrador', 'usuario': 'Usuario Estándar'}

            for r in agregados:
                nombre = nombres_roles.get(r, r)
                detalles_rol.append(f"se agregó el rol {nombre}")
            for r in quitados:
                nombre = nombres_roles.get(r, r)
                detalles_rol.append(f"se quitó el rol {nombre}")

            if detalles_rol:
                msg_personalizado = f"{', '.join(detalles_rol).capitalize()} al usuario {username_target}."
                messages.success(request, msg_personalizado)
            
            usuario.rol = data.get("rol")

        usuario.save()

        return JsonResponse({
            "success": True, 
            "status": "ok",
            "mensaje": msg_personalizado,
            "tipo": "success",
            "nuevo_rol": usuario.rol,
            "nuevo_activo": 'true' if usuario.is_active else 'false' # IMPORTANTE para el JS
        })
    
    except Usuario.DoesNotExist:
        return JsonResponse({"success": False, "error": "Usuario no encontrado"}, status=404)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)
    
@login_required
@require_POST       
def eliminar_usuario_ajax(request):
    if not request.user.es_admin():
        return JsonResponse({'status': 'error', 'mensaje': 'No autorizado'}, status=403)

    try:
        data = json.loads(request.body)
        username_target = data.get("username")
        usuario = Usuario.objects.get(username=username_target)

        if request.user.username == username_target:
            return JsonResponse({'status': 'error', 'mensaje': 'No puedes eliminarte a ti mismo.'})

        # --- FLUJO PARA PERFIL BASURA (BORRADO FÍSICO DIRECTO) ---
        if usuario.last_login is None:
            user_id = usuario.id_usuario
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM sesiones WHERE id_usuario = %s", [user_id])
                cursor.execute("DELETE FROM usuarios WHERE id_usuario = %s", [user_id])

            msg = f'El usuario "{username_target}" ha sido eliminado (datos purgados).'
        
        else:
            # --- FLUJO PARA USUARIO REAL (BORRADO LÓGICO) ---
            usuario.eliminado = True
            usuario.is_active = False
            usuario.save()

            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE sesiones 
                    SET estado = 'cerrada', fecha_cierre = %s 
                    WHERE id_usuario = %s AND estado = 'activo'
                """, [timezone.now(), usuario.id_usuario])
            
            msg = f'El usuario "{username_target}" ha sido eliminado (datos preservados).'

        messages.success(request, msg)
        return JsonResponse({'status': 'ok', 'mensaje': msg})

    except Usuario.DoesNotExist:
        return JsonResponse({'status': 'error', 'mensaje': 'Usuario no encontrado.'})
    except Exception as e:
        # Si esto falla, es que hay otra tabla amarrada. El print te dirá cuál.
        print(f"DEBUG: Error al eliminar: {str(e)}")
        return JsonResponse({'status': 'error', 'mensaje': f"Error de DB: {str(e)}"})
    
@login_required  
def obtener_tiempo_restante_ajax(request):
    tarea = PeriodicTask.objects.get(name='syncronify_main_task')
    
    # Usamos timezone.now() directo. Si viene con zona horaria, 
    # la removemos para comparar manzanas con manzanas en formato plano (naive)
    ahora = timezone.now()
    if timezone.is_aware(ahora):
        ahora = ahora.replace(tzinfo=None)
        
    intervalo_segundos = tarea.interval.every * 3600
    
    # Tratamos last_run_at exactamente igual, removiendo su tzinfo si existe
    if tarea.last_run_at:
        referencia = tarea.last_run_at
        if timezone.is_aware(referencia):
            referencia = referencia.replace(tzinfo=None)
    else:
        referencia = ahora
    
    # Calculamos la próxima ejecución en tiempo plano
    proxima_ejecucion = referencia + timedelta(seconds=intervalo_segundos)
    
    # Si por desfase de inicialización da negativo, calculamos la vuelta real
    if proxima_ejecucion < ahora:
        segundos_desde_vencimiento = (ahora - proxima_ejecucion).total_seconds()
        vueltas_perdidas = math.ceil(segundos_desde_vencimiento / intervalo_segundos)
        proxima_ejecucion = proxima_ejecucion + timedelta(seconds=vueltas_perdidas * intervalo_segundos)

    segundos_restantes = int((proxima_ejecucion - ahora).total_seconds())
    
    # Control de seguridad: si da un número absurdo o negativo por el desfase inicial
    if segundos_restantes < 0 or segundos_restantes > intervalo_segundos:
        segundos_restantes = segundos_restantes % intervalo_segundos

    return JsonResponse({'segundos': segundos_restantes})
