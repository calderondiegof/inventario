"""Tipos de dominio (DTOs) para el servicio de inventario.

Data Transfer Objects inmutables (frozen dataclasses) que representan
las entidades del dominio sin lógica de negocio.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MaterialDTO:
    """Representa un material del catálogo con sus propiedades."""
    id: int
    nombre: str
    tipo_material: str
    es_comercializable: bool = True
    precio_base: Optional[float] = None
    fuente_predeterminada: Optional[str] = None

    @property
    def es_merma(self) -> bool:
        """True si el material es categoria Merma/Basura (no vendible)."""
        return self.tipo_material in {"MERMA", "DESPERDICIO"}


@dataclass(frozen=True)
class FuenteDTO:
    """Representa una fuente de materiales (proveedor/tipo de entrada)."""
    id: int
    nombre: str
    tipo_fuente: str