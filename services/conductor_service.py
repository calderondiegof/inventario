"""Servicio especializado para la gestión de conductores."""
from typing import Any, Dict, Optional

from utils.text_normalizer import normalizar, normalizar_placa  # noqa: F401  (re-exports)

class ConductorService:
    """Manejo de operaciones y persistencia de conductores en Supabase."""
    def __init__(self, supabase_client):
        self.supabase = supabase_client

    def buscar_conductor_existente(self, *, identificacion: Optional[str] = None,
                                    placa: Optional[str] = None,
                                    nombre: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Solo búsqueda (sin crear): devuelve el conductor que coincida por
        identificacion, placa o nombre normalizado (insensible a tildes y mayúsculas)."""
        if identificacion:
            filas = self.supabase.table("conductores").select("*").eq("identificacion", identificacion).execute().data
            if filas:
                return filas[0]
        if placa:
            placa_limpia = normalizar_placa(placa)
            todos = self.supabase.table("conductores").select("*").execute().data or []
            for cond in todos:
                if normalizar_placa(cond.get("placa") or "") == placa_limpia or normalizar_placa(cond.get("placa_trailer") or "") == placa_limpia:
                    return cond
        if nombre:
            todos = self.supabase.table("conductores").select("*").execute().data or []
            nombre_norm = normalizar(nombre)
            for cond in todos:
                if normalizar(cond.get("nombre") or "") == nombre_norm:
                    return cond
        return None

    def obtener_conductor_por_nombre(self, nombre: str) -> Optional[Dict[str, Any]]:
        """Busca un conductor por nombre en la tabla 'conductores' (insensible a tildes/mayúsculas)."""
        return self.buscar_conductor_existente(nombre=nombre)

    def obtener_conductor_por_id(self, conductor_id: Any) -> Optional[Dict[str, Any]]:
        """Busca un conductor por su ID primario."""
        filas = self.supabase.table("conductores").select("*").eq("id", conductor_id).execute().data
        return filas[0] if filas else None

    def obtener_o_crear_conductor(self, *, nombre: str, identificacion: Optional[str] = None,
                                  placa: Optional[str] = None, telefono: Optional[str] = None) -> Dict[str, Any]:
        """Busca por identificación (si se dio) o por nombre; si el conductor ya existe,
        completa en su registro los campos que le falten; si no existe, lo crea.
        """
        nombre = (nombre or "").strip()
        if not nombre:
            raise ValueError("Toda venta debe indicar el nombre del conductor.")
        existente = self.buscar_conductor_existente(identificacion=identificacion, nombre=nombre)
        if existente:
            cambios = {}
            if not existente.get("identificacion") and identificacion:
                cambios["identificacion"] = identificacion
            if not existente.get("placa") and placa:
                cambios["placa"] = placa
            if not existente.get("telefono") and telefono:
                cambios["telefono"] = telefono
            if cambios:
                actualizado = self.supabase.table("conductores").update(cambios).eq("id", existente["id"]).execute().data
                existente = actualizado[0] if actualizado else existente
            return existente
        nuevo = self.supabase.table("conductores").insert({
            "nombre": nombre, "identificacion": identificacion, "placa": placa, "telefono": telefono,
        }).execute().data
        if not nuevo:
            raise ValueError("No se pudo registrar el conductor.")
        return nuevo[0]

    def registrar_conductor(self, *, nombre_conductor: str,
                            id_conductor: Optional[str] = None,
                            telefono_conductor: Optional[str] = None,
                            direccion_conductor: Optional[str] = None,
                            placa_conductor: Optional[str] = None,
                            placa_trailer_conductor: Optional[str] = None) -> Dict[str, Any]:
        """Crea un conductor (tabla 'conductores') recibiendo ÚNICAMENTE el
        diccionario sanitizado del conductor. Dirección y remolque/tráiler son opcionales."""
        nombre_conductor = (nombre_conductor or "").strip()
        if not nombre_conductor:
            raise ValueError("El conductor debe tener un nombre.")
        if id_conductor:
            existente = self.buscar_conductor_existente(identificacion=id_conductor)
            if existente:
                raise ValueError(f"Ya existe un registro con la identificación {id_conductor}.")
        fila = {
            "nombre": nombre_conductor,
            "identificacion": id_conductor,
            "placa": placa_conductor,
            "telefono": telefono_conductor,
            "direccion": direccion_conductor,
        }
        if placa_trailer_conductor:
            fila["placa_trailer"] = placa_trailer_conductor
        nuevo = self.supabase.table("conductores").insert(fila).execute().data
        if not nuevo:
            raise ValueError("No se pudo registrar el conductor.")
        return nuevo[0]
