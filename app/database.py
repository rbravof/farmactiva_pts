from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

AMBIENTE = os.getenv("AMBIENTE", "Desarollo")

# ✅ Para Por Tu Salud usamos PTS_DB_URL (nueva base FARMACTIVA_PTS)
DATABASE_URL = os.getenv("PTS_DB_URL") or os.getenv("DATABASE_URL")

print(f"📡 Conectando a la base de datos en entorno: {AMBIENTE}")

if not DATABASE_URL:
    # Mensaje claro si falta la URL
    raise RuntimeError(
        "No se encontró PTS_DB_URL (ni DATABASE_URL). "
        "Define PTS_DB_URL en tu .env, ej.: "
        "postgresql+psycopg2://postgres:Admin@localhost:5432/FARMACTIVA_PTS"
    )

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db            # NO hagas commit aquí; los commits van en los endpoints que escriben
    except Exception:
        db.rollback()       # <-- clave: limpiar la tx fallida
        raise
    finally:
        db.close()
