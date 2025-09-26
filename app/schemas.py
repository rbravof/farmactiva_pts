from pydantic import BaseModel, EmailStr

class BeneficioRequest(BaseModel):
    rut: str
    nombre: str
    correo: EmailStr
    tipo_cliente: str  # "natural" o "empresa"

class BeneficioResponse(BaseModel):
    mensaje: str
    usuario: str
    estado: str
