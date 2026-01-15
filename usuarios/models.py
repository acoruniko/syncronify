from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone

class UsuarioManager(BaseUserManager):
    def create_user(self, username, password=None, **extra_fields):
        if not username:
            raise ValueError("El usuario debe tener un username")
        user = self.model(username=username, **extra_fields)
        user.set_password(password)  # encripta la contraseña
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        return self.create_user(username, password, **extra_fields)


class Sesion(models.Model):
    id_sesion = models.AutoField(primary_key=True)
    id_usuario = models.IntegerField()  # FK hacia usuarios.id_usuario
    token_sesion = models.CharField(max_length=255, unique=True)
    fecha_inicio = models.DateTimeField(default=timezone.now)
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    estado = models.CharField(max_length=20, default="activo")
    ip_origen = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = "sesiones"
        managed = False   

    def cerrar(self):
        self.fecha_cierre = timezone.now()
        self.estado = "cerrada"
        self.save()

    def __str__(self):
        return f"Sesión {self.token_sesion} (Usuario {self.id_usuario})"
    
class Usuario(AbstractBaseUser, PermissionsMixin):
    id_usuario = models.AutoField(primary_key=True)
    nombre_completo = models.CharField(max_length=100)
    username = models.CharField(max_length=50, unique=True)
    password = models.CharField(max_length=255)  
    rol = models.CharField(max_length=20, default="usuario")
    fecha_creacion = models.DateTimeField()
    activo = models.BooleanField(default=True)

    # Campos que Django espera
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    last_login = models.DateTimeField(null=True)

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["nombre_completo"]
    
    objects = UsuarioManager()
    
    class Meta:
        db_table = "usuarios"
        managed = False