from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.utils import timezone
from .forms import LoginForm 
from .models import Sesion, Usuario
from logs.models import LogEvento 
from django.utils.timezone import now
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from .forms import EditarPerfilForm, RegistroForm
from datetime import timedelta


def login_view(request):
    if request.user.is_authenticated: 
        return redirect("lista_playlist_home")
    
    if 'next' in request.GET: 
        messages.error(request, "Debes iniciar sesión primero")
    
    if request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data["username"]
            password = form.cleaned_data["password"]

            user = authenticate(request, username=username, password=password)

            if user is not None:
                login(request, user)

                # 👉 invalidar otras sesiones del mismo usuario
                Sesion.objects.filter(id_usuario=user.id_usuario, estado="activo").update(estado="cerrado")

                # 👉 registrar la nueva sesión
                Sesion.objects.update_or_create(
                    id_usuario=user.id_usuario,
                    token_sesion=request.session.session_key,
                    estado="activo"
                )
                # 👉 registrar en log interno (no mostrar al usuario) 
                LogEvento.objects.create( 
                    fecha=now(), 
                    nivel="INFO", 
                    usuario=user.username, 
                    modulo="usuarios", 
                    mensaje=f"Usuario {user.username} inició sesión correctamente" 
                )

                return redirect("lista_playlist_home")
            else:
                messages.error(request, "Usuario o contraseña incorrectos")
        else:
            messages.error(request, "Debes completar todos los campos")
    else:
        form = LoginForm()

    return render(request, "usuarios/login.html", {"form": form})


def logout_view(request):
    usuario = request.user.username if request.user.is_authenticated else "Anon"
    logout(request)

    LogEvento.objects.create(
        fecha=now(),
        nivel="INFO",
        usuario=usuario,
        modulo="usuarios",
        mensaje=f"Usuario {usuario} cerró sesión"
    )

    return redirect("login")


def registro_view(request):
    if request.user.is_authenticated: 
        return redirect("lista_playlist_home")
    
    if request.method == "POST":
        # --- BLOQUE DE SEGURIDAD ANTISPAM ---
        LIMITE_USUARIOS = 3  # Tu variable ajustable para pruebas
        hace_una_hora = timezone.now() - timedelta(hours=1)

        hace_una_hora = timezone.now() - timedelta(hours=1)

        # Contamos solo la "basura" potencial o registros pendientes
        usuarios_spam_potencial = Usuario.objects.filter(
            fecha_creacion__gte=hace_una_hora,
            is_active=False,       # Si ya lo activaste, sale del conteo
            last_login__isnull=True # Si ya entró una vez, sale del conteo
        ).count()
        
        conteo = usuarios_spam_potencial

        if conteo >= LIMITE_USUARIOS:
            messages.error(request, "Se ha sobrepasado el límite de creación de usuarios. Contacta con el administrador.")
            # Devolvemos el form vacío o con los datos para que no se pierdan, 
            # pero bloqueamos el guardado.
            return render(request, "usuarios/registro.html", {"form": RegistroForm(request.POST)})
        # ------------------------------------

        form = RegistroForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.set_password(form.cleaned_data['password'])
            user.fecha_creacion = timezone.now()
            user.is_active = False  
            user.save()

            # Tu LogEvento y lógica de éxito...
            LogEvento.objects.create(
                fecha=timezone.now(),
                nivel="INFO",
                usuario=user.username,
                modulo="usuarios",
                mensaje=f"Nuevo registro solicitado: {user.username} (Pendiente de activación)"
            )
            messages.success(request, f"Usuario {user.username} creado. Solicite activación al administrador.")
            return redirect('login')
        else:
            # Si el form no es válido porque faltan campos o errores específicos
            if any(error.get('code') == 'required' for field_errors in form.errors.values() for error in field_errors.get_json_data()):
                messages.error(request, "Debes completar todos los campos")
            else:
                # Otros errores (como contraseñas que no coinciden)
                for field, errors in form.errors.items():
                    for error in errors:
                        if field == '__all__':
                            messages.error(request, error)
                        else:
                            messages.error(request, f"{field}: {error}")
            pass
    else:
        form = RegistroForm()
    
    return render(request, "usuarios/registro.html", {"form": form})



@login_required
def editar_perfil_view(request):
    if request.method == 'POST':
        form = EditarPerfilForm(request.POST, instance=request.user)
        
        if form.is_valid():
            user = form.save(commit=False)
            nueva_pass = form.cleaned_data.get('password')
            if nueva_pass:
                user.set_password(nueva_pass)
            
            user.save()
            
            if nueva_pass:
                update_session_auth_hash(request, user)

            # Mensaje con contexto dinámico
            messages.success(request, f"Perfil del usuario {user.username} fue modificado con éxito.")
            return redirect('lista_playlist_home')
        else:
            # LIMPIEZA DE ERRORES: Quitamos el __all__
            for field, errors in form.errors.items():
                for error in errors:
                    if field == '__all__':
                        messages.error(request, error) # Solo el mensaje: "Las contraseñas no coinciden"
                    else:
                        messages.error(request, f"{field}: {error}")
    else:
        form = EditarPerfilForm(instance=request.user)

    return render(request, 'usuarios/editar_perfil.html', {'form': form})
