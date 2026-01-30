# logs/models.py
from django.db import models

class LogEvento(models.Model):
    id = models.BigAutoField(primary_key=True)
    fecha = models.DateTimeField()
    nivel = models.CharField(max_length=16)
    usuario = models.CharField(max_length=128)
    modulo = models.CharField(max_length=128)
    mensaje = models.TextField()

    class Meta:
        db_table = "logs_eventos"   # tu tabla ya creada
        managed = False             # Django no intentará recrearla

    def __str__(self):
        return f"[{self.fecha}] {self.nivel} | {self.usuario} | {self.modulo} | {self.mensaje}"
