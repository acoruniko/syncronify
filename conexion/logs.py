# conexion/logs.py
from .models import ConexionLog

def log_event(tipo: str, mensaje: str):
    try:
        ConexionLog.objects.create(tipo=tipo, mensaje=mensaje[:1000])
    except Exception:
        # Evitar romper el flujo si el log falla (por migraciones pendientes, etc.)
        pass

def status_ok(label: str, extra: dict = None) -> dict:
    return {"status": "ok", "label": label, "extra": extra or {}}

def status_err(label: str, detalle: str, extra: dict = None) -> dict:
    return {"status": "error", "label": label, "detalle": detalle, "extra": extra or {}}