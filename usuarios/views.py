from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from .forms import LoginForm 
from .models import Sesion

def login_view(request):
    if request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():  # valida que no estén vacíos
            username = form.cleaned_data["username"]
            password = form.cleaned_data["password"]

            user = authenticate(request, username=username, password=password)

            if user is not None:
                login(request, user)  # crea la sesión
                return redirect("lista_playlist_home")  # redirige a tu página principal
            else:
                messages.error(request, "Usuario o contraseña incorrectos")
        else:
            messages.error(request, "Debes completar todos los campos")
    else:
        form = LoginForm()

    return render(request, "usuarios/login.html", {"form": form})



def validar_sesion(request):
    if not request.user.is_authenticated:
        messages.error(request, "Debes iniciar sesión")
        return False

    sesion_valida = Sesion.objects.filter(
        usuario=request.user,
        token_sesion=request.session.session_key,
        estado="activo"
    ).exists()

    if not sesion_valida:
        messages.error(request, "Sesión inválida o expirada")
        return False

    return True


def logout_view(request):
    logout(request)  # Django borra la sesión y dispara la señal user_logged_out
    return redirect("login")
