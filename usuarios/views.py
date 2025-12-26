from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.contrib import messages

def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)  # crea la sesión
            return redirect("lista_playlist_home")  # redirige a tu página principal
        else:
            messages.error(request, "Usuario o contraseña incorrectos")

    return render(request, "usuarios/login.html")
