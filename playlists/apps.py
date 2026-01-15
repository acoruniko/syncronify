from django.apps import AppConfig

class PlaylistsConfig(AppConfig):
    default_auto_field = 'django.db.models.AutoField'  # o BigAutoField si prefieres
    name = 'playlists'