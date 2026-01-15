# contenido de usuarios/forms.py
from django import forms

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