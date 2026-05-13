# contenido de usuarios/forms.py
import re
from django import forms
from django.core.validators import RegexValidator
from .models import Usuario

class LoginForm(forms.Form):
    username = forms.CharField(
        max_length=50,
        required=True,
        label="Usuario",
        error_messages={"required": "Debes ingresar tu usuario"}
    )
    password = forms.CharField(
        widget=forms.PasswordInput,
        required=True,
        label="Contraseña",
        error_messages={"required": "Debes ingresar tu contraseña"}
    )

class RegistroForm(forms.ModelForm):
    # Validador para forzar minúsculas y sin caracteres especiales
    username_validator = RegexValidator(
        regex=r'^[a-z0-9]+$',
        message="El nombre de usuario debe contener solo letras minúsculas y números, sin espacios ni símbolos."
    )

    username = forms.CharField(
        validators=[username_validator],
        widget=forms.TextInput(attrs={'placeholder': 'Nombre de usuario (ej: larry123)'})
    )
    
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Contraseña'}),
        required=True
    )
    
    confirmar_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Confirmar contraseña'}),
        required=True
    )

    class Meta:
        model = Usuario
        fields = ['nombre_completo', 'username']
        widgets = {
            'nombre_completo': forms.TextInput(attrs={'placeholder': 'Nombre Completo'}),
        }

    def clean_username(self):
        username = self.cleaned_data.get('username').lower()
        if Usuario.objects.filter(username=username).exists():
            raise forms.ValidationError("Este nombre de usuario ya existe.")
        return username

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get("password")
        p2 = cleaned_data.get("confirmar_password")

        if p1 != p2:
            raise forms.ValidationError("Las contraseñas no coinciden.")
        return cleaned_data

class EditarPerfilForm(forms.ModelForm):
    # Definimos los campos de contraseña fuera del Meta
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Nueva contraseña (opcional)'}),
        required=False
    )
    confirmar_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Confirmar nueva contraseña'}),
        required=False
    )

    class Meta:
        model = Usuario
        fields = ['nombre_completo', 'username']
        widgets = {
            'username': forms.TextInput(attrs={'readonly': 'readonly'}),
            'nombre_completo': forms.TextInput(attrs={'placeholder': 'Nombre Completo'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].required = False
        # Si el campo viene vacío en el POST (por el readonly), 
        # le asignamos el valor de la instancia para que no se vea "None" en el HTML
        if self.data and not self.data.get('username'):
            self.fields['username'].initial = self.instance.username

    def clean_username(self):
        # Siempre devolvemos el valor original de la instancia, ignorando el POST
        return self.instance.username

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get("password")
        p2 = cleaned_data.get("confirmar_password")

        if (p1 or p2) and p1 != p2:
            raise forms.ValidationError("Las contraseñas no coinciden.")
        return cleaned_data