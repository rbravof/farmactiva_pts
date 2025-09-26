from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import urllib.parse

CHROMEDRIVER_PATH = "C:/Users/rbrav/chromedriver/chromedriver.exe"

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

def obtener_precio_selenium(producto):
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--log-level=3")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-dev-shm-usage")

    service = Service(CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=options)

    query = urllib.parse.quote(producto)
    url = f"https://www.drsimi.cl/{query}?_q={query}&map=ft"
    print(f"🔍 Buscando: {producto}")

    try:
        driver.get(url)
        wait = WebDriverWait(driver, 10)

        # Esperar a que aparezca el producto (hasta 10s)
        price_integer = wait.until(
            EC.presence_of_element_located((By.CLASS_NAME, "vtex-product-price-1-x-currencyInteger"))
        )
        price_decimal = driver.find_elements(By.CLASS_NAME, "vtex-product-price-1-x-currencyFraction")

        precio = price_integer.text
        if price_decimal:
            precio += "," + price_decimal[0].text

        return f"💲 ${precio}"

    except TimeoutException:
        return "❌ Producto no encontrado o sin precio visible"
    except Exception as e:
        return f"❌ Error: {str(e)}"
    finally:
        driver.quit()

if __name__ == "__main__":
    for producto in productos:
        resultado = obtener_precio_selenium(producto)
        print(f"{producto}: {resultado}")
        time.sleep(2)
