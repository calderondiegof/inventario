"""Enumerados de dominio para el servicio de inventario.

Este módulo centraliza los tipos enumerados usados en toda la aplicación,
evitando duplicación y facilitando el mantenimiento.
"""
from enum import Enum


class TipoMaterial(str, Enum):
    """Categorías de material según su estado de procesamiento."""
    BRUTO = "BRUTO"
    SEMILIMPIO = "SEMILIMPIO"
    LIMPIO = "LIMPIO"
    MERMA = "MERMA"
    # Alias heredado de la versión anterior; al cargar el catálogo se
    # normaliza a MERMA. Se mantiene por compatibilidad con la BD existente.
    DESPERDICIO = "DESPERDICIO"


class TipoTransaccion(str, Enum):
    """Tipos de transacciones de inventario soportadas."""
    COMPRA = "COMPRA"
    ENTRADA_BRUTA = "ENTRADA_BRUTA"
    VENTA = "VENTA"
    TRANSFORMACION = "TRANSFORMACION"
    DESPACHO = "DESPACHO"
    AJUSTE_INVENTARIO = "INVENTARIO_INICIAL"
    ANULACION = "ANULACION"
