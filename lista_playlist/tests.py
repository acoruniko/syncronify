from django.test import TestCase
from django.urls import reverse

class ListaPlaylistTests(TestCase):
    def test_hola_mundo_lista_responde(self):
        # Usamos reverse para obtener la URL con nombre 'hola'
        url = reverse('hola')
        response = self.client.get(url)

        # Verificamos que responde con código 200 (OK)
        self.assertEqual(response.status_code, 200)

        # Verificamos que el contenido incluye el texto esperado
        self.assertContains(response, "Hola Syncronify 🚀 desde lista_playlist. El inicio del inicio")