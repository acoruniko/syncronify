from django.db import models
from django.conf import settings
from usuarios.models import Usuario  # importa tu modelo Usuario


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
    cover_url = models.TextField(blank=True, null=True) 

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
        managed = False

class Tarea(models.Model):
    id_tarea = models.AutoField(primary_key=True)
    relacion = models.ForeignKey(
        PlaylistCancion,
        db_column="id_relacion",   # 👈 nombre real en la BD
        on_delete=models.CASCADE
    )

    tipo = models.CharField(max_length=50)  # 'posicionar' | 'eliminar'
    estado = models.CharField(max_length=20, default='pendiente')

    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_ejecucion = models.DateTimeField()

    intentos = models.IntegerField(default=0)
    mensaje_error = models.TextField(null=True, blank=True)

    usuario = models.ForeignKey(Usuario, db_column="id_usuario_creo", on_delete=models.CASCADE)  # 👈 aquí


    # Campos opcionales según tipo
    url_cancion = models.TextField(null=True, blank=True)  # no se usa aquí, pero lo dejamos por consistencia
    posicion = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.tipo} - rel:{self.relacion_id} - {self.estado}"
    class Meta:
        db_table = "tareas"  
        managed = False   
