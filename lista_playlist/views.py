from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.utils import timezone
from playlists.models import Playlist, Genero
from conexion.models import CredencialesSpotify
from conexion.services import check_rate_limit
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Q, Count  # 🚀 Agregamos Count para la agregación masiva

def aplicar_anotaciones_alertas(queryset, ahora):
    """
    Función auxiliar para inyectar los contadores de alertas al vuelo
    directamente en el motor SQL de la base de datos.
    """
    limite_amarillo = ahora - timezone.timedelta(days=15)
    limite_rojo = ahora - timezone.timedelta(days=30)

    return queryset.annotate(
        # Alertas Amarillas: Entre 16 y 30 días (inclusive) con la relación activa
        total_amarillas=Count(
            'playlistcancion',
            filter=Q(
                playlistcancion__estado="activo",
                playlistcancion__fecha_sincronizacion__lt=limite_amarillo,
                playlistcancion__fecha_sincronizacion__gte=limite_rojo
            )
        ),
        # Alertas Rojas: Estrictamente más de 30 días con la relación activa
        total_rojas=Count(
            'playlistcancion',
            filter=Q(
                playlistcancion__estado="activo",
                playlistcancion__fecha_sincronizacion__lt=limite_rojo
            )
        )
    )

@login_required
def lista_playlist_home(request):
    ahora = timezone.now()
    
    # 1. Obtener playlists anotadas con sus alertas y el catálogo de géneros
    playlists_base = Playlist.objects.all()
    playlists = aplicar_anotaciones_alertas(playlists_base, ahora)
    
    generos = Genero.objects.all().order_by('nombre')

    # 2. Verificar rate limit usando servicio de conexion
    cred = CredencialesSpotify.objects.first()
    seconds_remaining = None
    rate_limited = False

    if cred:
        seconds_remaining = check_rate_limit(request, cred, show_message=False)
        rate_limited = seconds_remaining is not None

    return render(request, "lista_playlist/home.html", {
        "playlists": playlists,
        "generos": generos,
        "mostrar_alertas": True,
        "rate_limited": rate_limited,
        "seconds_remaining": seconds_remaining or 0,
    })

@login_required
def filtrar_playlists(request):
    """
    Endpoint asíncrono blindado contra duplicación de alertas.
    Filtra primero los IDs y luego calcula la telemetría sobre el set limpio.
    """
    generos_str = request.GET.get("generos", "")
    buscar_texto = request.GET.get("q", "").strip()
    ahora = timezone.now()

    # 1. PASO A: Filtrado básico para obtener los IDs únicos de las playlists que aplican
    queryset_base = Playlist.objects.all()

    # Filtro por Géneros
    if generos_str:
        try:
            generos_ids = [int(x) for x in generos_str.split(",") if x.isdigit()]
            if generos_ids:
                queryset_base = queryset_base.filter(generos__id_genero__in=generos_ids)
        except ValueError:
            pass

    # Filtro por Texto
    if buscar_texto:
        queryset_base = queryset_base.filter(
            Q(nombre__icontains=buscar_texto) |
            Q(playlistcancion__cancion__nombre__icontains=buscar_texto) |
            Q(playlistcancion__cancion__artistas__icontains=buscar_texto)
        )

    # Extraemos solo la lista de IDs únicos que pasaron los filtros
    # Esto destruye cualquier posibilidad de duplicación por JOINs relacionales
    playlist_ids_filtrados = queryset_base.values_list('id_playlist', flat=True).distinct()

    # 2. PASO B: Ahora que tenemos los IDs limpios, pedimos las playlists reales
    # y calculamos las alertas en un entorno 100% controlado y libre de duplicados
    playlists_limpias = Playlist.objects.filter(id_playlist__in=playlist_ids_filtrados)
    playlists_finales = aplicar_anotaciones_alertas(playlists_limpias, ahora)

    return render(request, "lista_playlist/partials/playlist_rows.html", {
        "playlists": playlists_finales,
        "mostrar_alertas": True
    })

@require_POST
def eliminar_playlist(request, playlist_id):
    playlist = get_object_or_404(Playlist, id_playlist=playlist_id)
    nombre = playlist.nombre
    playlist.delete()
    messages.success(request, f'Playlist "{nombre}" eliminada correctamente.')
    return JsonResponse({'status': 'ok'})