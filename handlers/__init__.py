"""Handlers por entidad del bot de inventario.

`MANEJADO` es el sentinel que un handler devuelve cuando ya resolvio (y
notifico) el flujo por su cuenta, de modo que el router no reenvie respuesta."""


class _Manejado:
    """Sentinel: el handler gestiono el mensaje de forma completa."""

    def __repr__(self):
        return "MANEJADO"


MANEJADO = _Manejado()
