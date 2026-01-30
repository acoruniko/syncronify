# conexion/logs.py
from django.utils.timezone import now
from .models import ConexionLog
from logs.models import LogEvento  # 👈 tu modelo central de logs

NIVELES = {
    "token_error": "ERROR",
    "auth_error": "ERROR",
    "service_error": "ERROR",
    "token_ok": "INFO",
    "auth_ok": "INFO",
    "service_ok": "INFO",
}

def log_event(tipo: str, mensaje: str, usuario: str = "Sistema", modulo: str = "conexion"):
    try:
        # Guardar en tabla especializada de conexiones
        ConexionLog.objects.create(tipo=tipo, mensaje=mensaje[:1000])

        # Resolver nivel siempre con fallback
        nivel = NIVELES.get(tipo)
        if not nivel:
            # fallback: si no está en el mapa, decide por convención
            nivel = "ERROR" if "error" in tipo.lower() else "INFO"

        # Guardar también en tabla central
        LogEvento.objects.create(
            fecha=now(),
            nivel=nivel,
            usuario=usuario,
            modulo=modulo,
            mensaje=mensaje[:1000]
        )
    except Exception as e:
        pass


def status_ok(label: str, extra: dict = None) -> dict:
    return {"status": "ok", "label": label, "extra": extra or {}}


def status_err(label: str, detalle: str, extra: dict = None) -> dict:
    return {"status": "error", "label": label, "detalle": detalle, "extra": extra or {}}
