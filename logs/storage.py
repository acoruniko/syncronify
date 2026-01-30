# logs/storage.py
from django.contrib.messages.storage.session import SessionStorage
from django.utils.timezone import now
from .models import LogEvento

class LogStorage(SessionStorage):
    def add(self, level, message, extra_tags=''):
        # Guardar en la tabla de logs
        nivel_map = {
            10: 'DEBUG',
            20: 'INFO',
            25: 'INFO',   # SUCCESS → INFO
            30: 'WARNING',
            40: 'ERROR',
        }
        nivel = nivel_map.get(level, 'INFO')

        try:
            usuario = self.request.user.username if self.request and self.request.user.is_authenticated else "Sistema"
            modulo = self.request.resolver_match.app_name if self.request and self.request.resolver_match else "desconocido"

            LogEvento.objects.create(
                fecha=now(),
                nivel=nivel,
                usuario=usuario,
                modulo=modulo,
                mensaje=message
            )
        except Exception:
            # No romper el flujo de mensajes si falla el log
            pass

        # Llamar al add original para que el mensaje siga en sesión
        return super().add(level, message, extra_tags)
