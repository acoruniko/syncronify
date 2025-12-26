from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required
def lista_playlist_home(request):
    return render(request, "lista_playlist/home.html")