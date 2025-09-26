from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import time

# Lista de productos a buscar
productos = [
    "Paracetamol 500 mg",
    "Ibuprofeno 400 mg",
    "Amoxicilina 500 mg",
    "Omeprazol 20 mg",
    "Salbutamol",
    "Enalapril 10 mg",
    "Loratadina 10 mg",
    "Metformina 850 mg",
    "Losartan 50 mg",
    "Simvastatina 20 mg"
]

# Configurar ChromeDriver
options = Options()
options.add_argument("--headless")  # Ocultar ventana del navegador
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")

# Ruta a tu chromedriver
service = Service("C:/Users/rbrav/chromedriver/chromedriver.exe")
driver = webdriver.Chrome(service=service, options=options)

for producto in productos:
    try:
        print(f"\n🔍 Buscando: {producto}")
        query = producto.replace(" ", "%20")
        url = f"https://www.cruzverde.cl/search?query={query}"
        driver.get(url)
        time.sleep(4)  # Espera para que Angular cargue los elementos

        # Selector CSS ajustado
        precio_elem = driver.find_element(By.CSS_SELECTOR, "ml-new-card-product:nth-child(1) p.text-green-turquoise")
        precio = precio_elem.text.strip()
        print(f"{producto}: 💲 {precio}")
    except Exception as e:
        print(f"{producto}: ❌ Error: {str(e)}")

driver.quit()
