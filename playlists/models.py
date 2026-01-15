from django.db import models
from django.conf import settings


class Playlist(models.Model):
    id_playlist = models.AutoField(primary_key=True)
    id_spotify = models.CharField(max_length=50, unique=True)
    nombre = models.CharField(max_length=500)
    descripcion = models.TextField(blank=True, null=True)
    propietario = models.CharField(max_length=100, blank=True, null=True)
    total_canciones = models.IntegerField(blank=True, null=True)
    cover_url = models.TextField(blank=True, null=True)
    fecha_importacion = models.DateTimeField(auto_now_add=True)

    usuario_importo = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        db_column="id_usuario_importo",  # nombre real en la BD
        on_delete=models.CASCADE
    )

    class Meta:
        db_table = 'playlists'
        managed = False


    def __str__(self):
        return self.nombre

class Cancion(models.Model):
    id_cancion = models.AutoField(primary_key=True)
    id_spotify = models.CharField(max_length=50, unique=True)
    nombre = models.CharField(max_length=500)
    artistas = models.CharField(max_length=500)
    album = models.CharField(max_length=500, blank=True, null=True)
    duracion_ms = models.IntegerField()
    popularidad = models.IntegerField(blank=True, null=True)
    fecha_importacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'canciones'
        managed = False   # Django no intentará crear ni borrar esta tabla


    def __str__(self):
        return self.nombre

class PlaylistCancion(models.Model):
    id_relacion = models.AutoField(primary_key=True)

    playlist = models.ForeignKey(
        Playlist,
        db_column="id_playlist",   # nombre real en la BD
        on_delete=models.CASCADE
    )
    cancion = models.ForeignKey(
        Cancion,
        db_column="id_cancion",    # nombre real en la BD
        on_delete=models.CASCADE
    )

    posicion = models.IntegerField(blank=True, null=True)
    fecha_agregado = models.DateTimeField(blank=True, null=True)
    agregado_por = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        db_table = "playlist_canciones"
        unique_together = ("playlist", "cancion")
        managed = False