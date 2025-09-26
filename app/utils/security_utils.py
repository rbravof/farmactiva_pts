# app/utils/security_utils.py
from __future__ import annotations
import os
from passlib.context import CryptContext

_BCRYPT_ROUNDS = int(os.getenv("BCRYPT_ROUNDS", "12"))  # permite afinar en prod

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=_BCRYPT_ROUNDS,
)

def crear_hash_contrasena(plain: str) -> str:
    """
    Retorna un hash bcrypt para la contraseña en texto plano.
    Lanza ValueError si la contraseña es muy corta.
    """
    if not plain or len(plain) < 8:  # subí a 8 por seguridad
        raise ValueError("La contraseña es demasiado corta (mínimo 8 caracteres)")
    return pwd_context.hash(plain)

def verificar_contrasena(plain: str, hashed: str) -> bool:
    """
    Verifica una contraseña contra el hash almacenado (bcrypt).
    Retorna False si el hash no es válido o no coincide.
    """
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False

def necesita_rehash(hashed: str) -> bool:
    """
    Indica si el hash existente debería re-generarse (por ejemplo,
    si aumentaste los rounds y quieres “actualizar” gradualmente).
    """
    try:
        return pwd_context.needs_update(hashed)
    except Exception:
        return True
