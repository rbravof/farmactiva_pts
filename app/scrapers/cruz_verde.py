import requests
from bs4 import BeautifulSoup

def obtener_precio_cruz_verde(nombre_producto):
    url = f"https://www.cruzverde.cl/search?q={nombre_producto.replace(' ', '+')}"
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, 'html.parser')

    try:
        primer_producto = soup.select_one(".product__price--final").text.strip()
        return primer_producto
    except Exception as e:
        print(f"❌ Error al obtener precio de Cruz Verde: {e}")
        return None
