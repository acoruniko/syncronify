from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from .forms import LoginForm 
from .models import Sesion
from logs.models import LogEvento 
from django.utils.timezone import now

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
                Sesion.objects.create(
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

