# conexion/models.py
from django.db import models
from django.utils import timezone

class ConexionLog(models.Model):
    id_log = models.AutoField(primary_key=True)
    tipo = models.CharField(max_length=50)
    mensaje = models.TextField()
    fecha = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "conexion_logs"


class CredencialesSpotify(models.Model):
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_type = models.CharField(max_length=20, default="Bearer")
    scope = models.TextField(blank=True)
    expires_at = models.DateTimeField()  # fecha/hora de expiración del access token
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    rate_limit_until = models.DateTimeField(null=True, blank=True)


    class Meta:
        db_table = "spotify_token"

    def token_valido(self):
        return self.expires_at > timezone.now()

    def __str__(self):
        return f"Spotify Token (expira {self.expires_at})"