"""Servicio de PDFs de remision: reimpresion dinamica con seleccion de modo de valores."""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
_RE_NUMERO = re.compile(r"[A-Za-z]*[_\-\s]?(\d+)\b", re.IGNORECASE)
_PREFIJO = "REM_"


class ModoImpresion(str, Enum):
    MONEDA_LOCAL = "moneda_local"
    DOLARES = "dolares"
    AMBAS = "ambas"
    SIN_VALORES = "sin_valores"


MAPA_MODOS_IMPRESION: Dict[str, ModoImpresion] = {
    "1": ModoImpresion.MONEDA_LOCAL, "local": ModoImpresion.MONEDA_LOCAL, "cop": ModoImpresion.MONEDA_LOCAL,
    "2": ModoImpresion.DOLARES, "usd": ModoImpresion.DOLARES, "dolares": ModoImpresion.DOLARES,
    "3": ModoImpresion.AMBAS, "ambas": ModoImpresion.AMBAS, "todo": ModoImpresion.AMBAS,
    "4": ModoImpresion.SIN_VALORES, "sin_valores": ModoImpresion.SIN_VALORES,
    "limpio": ModoImpresion.SIN_VALORES, "ninguno": ModoImpresion.SIN_VALORES,
}

@dataclass(frozen=True)
class RemisionPdf:
    numero_remision: str
    pdf_url: str
    cliente: str
    fecha_creacion: Optional[str] = None

    @property
    def filename(self) -> str:
        return f"{self.numero_remision}.pdf"

    @property
    def caption(self) -> str:
        c = (self.cliente or "").strip() or "Sin cliente"
        return f"Remision {self.numero_remision} - {c}"


class PdfRemisionError(Exception): pass
class PdfRemisionNoEncontrada(PdfRemisionError): pass
class PdfDescargaError(PdfRemisionError): pass

class PdfRemisionService:
    def __init__(self, supabase: Any, http_client: Any = None,
                 cache_dir: Optional[Path] = None) -> None:
        self._supabase = supabase
        self._http_client = http_client
        self._cache_dir = Path(cache_dir) if cache_dir else None

    @staticmethod
    def normalizar_numero(numero: str) -> str:
        if not numero: return ""
        limpio = str(numero).strip().upper()
        if _PREFIJO in limpio or "REM-" in limpio:
            return limpio.replace("REM-", _PREFIJO)
        m = _RE_NUMERO.search(limpio)
        return f"{_PREFIJO}{m.group(1)}" if m else ""

    @staticmethod
    def resolver_modo(modo: str | ModoImpresion) -> ModoImpresion:
        if isinstance(modo, ModoImpresion): return modo
        return MAPA_MODOS_IMPRESION.get(str(modo).strip().lower(), ModoImpresion.SIN_VALORES)

    @staticmethod
    def evaluar_permisos_impresion(rol_usuario: str,
                                   modo_solicitado: str | ModoImpresion) -> Tuple[ModoImpresion, Optional[str]]:
        modo = PdfRemisionService.resolver_modo(modo_solicitado)
        rol = (rol_usuario or "").strip().lower()
        if rol not in ("", "usuario_administrador", "admin", "administrador"):
            if modo != ModoImpresion.SIN_VALORES:
                return ModoImpresion.SIN_VALORES, (
                    f"Tu rol '{rol_usuario}' no permite imprimir valores "
                    f"({modo.value}). Se generara el PDF en modo limpio (sin precios)."
                )
        return modo, None

    def obtener_datos_completos_remision(self, numero: str) -> Dict[str, Any]:
        if not self._supabase: raise PdfRemisionError("Supabase no disponible")
        try:
            from services.inventario_service import InventarioService
            inv = InventarioService(supabase=self._supabase)
            return inv.obtener_datos_pdf_remision(numero)
        except ImportError:
            pass
        num = self.normalizar_numero(numero)
        if not num: raise PdfRemisionError(f"Numero invalido: {numero}")
        res = self._supabase.table("remisiones").select("*").eq("numero", num).limit(1).execute()
        filas = getattr(res, "data", None) or []
        if not filas: raise PdfRemisionNoEncontrada(f"No existe la remision '{num}'")
        rem = filas[0]
        cliente, conductor = {}, {}
        if rem.get("cliente_id"):
            c = self._supabase.table("clientes").select("*").eq("id", rem["cliente_id"]).limit(1).execute()
            if getattr(c, "data", None): cliente = c.data[0] or {}
        if rem.get("conductor_id"):
            d = self._supabase.table("conductores").select("*").eq("id", rem["conductor_id"]).limit(1).execute()
            if getattr(d, "data", None): conductor = d.data[0] or {}
        items = [
            {"material_nombre": (m.get("materiales") or {}).get("nombre", "Material"),
             "cantidad_kg": abs(float(m["cantidad_kg"])),
             "precio_unitario": m.get("precio_unitario")}
            for m in rem.get("movimientos", []) if float(m.get("cantidad_kg", 0)) > 0
        ]
        return {"numero_remision": rem.get("numero", num),
                "fecha_operacion": rem.get("fecha_operacion"),
                "bodega_id": rem.get("bodega_id"),
                "estado": rem.get("estado"),
                "vr_dolar_dia": rem.get("vr_dolar_dia"),
                "cliente": cliente, "conductor": conductor, "items": items}

    def obtener_pdf_remision(self, numero: str) -> Optional[RemisionPdf]:
        if not self._supabase: return None
        num = self.normalizar_numero(numero)
        if not num: return None
        try:
            res = (self._supabase.table("remisiones")
                .select("numero_remision,pdf_url,cliente,fecha_creacion")
                .eq("numero_remision", num).limit(1).execute())
        except Exception as exc:
            logger.error("Error %s: %s", num, exc); return None
        filas = getattr(res, "data", None) or []
        if not filas: return None
        fila = filas[0] or {}
        url = (fila.get("pdf_url") or "").strip()
        if not url: return None
        return RemisionPdf(numero_remision=fila.get("numero_remision") or num,
                           pdf_url=url,
                           cliente=fila.get("cliente") or "",
                           fecha_creacion=fila.get("fecha_creacion"))

    def listar_remisiones_con_pdf(self, limite: int = 10) -> List[RemisionPdf]:
        if not self._supabase or limite <= 0: return []
        try:
            res = (self._supabase.table("remisiones")
                .select("numero_remision,pdf_url,cliente,fecha_creacion")
                .neq("pdf_url", "null")
                .order("fecha_creacion", desc=True)
                .order("numero_remision", desc=True).limit(limite).execute())
        except Exception as exc:
            logger.error("Error listando: %s", exc); return []
        out: List[RemisionPdf] = []
        for fila in (getattr(res, "data", None) or []):
            url = (fila.get("pdf_url") or "").strip()
            if url:
                out.append(RemisionPdf(numero_remision=fila.get("numero_remision") or "",
                                       pdf_url=url,
                                       cliente=fila.get("cliente") or "",
                                       fecha_creacion=fila.get("fecha_creacion")))
        return out

    def descargar_pdf(self, remision: RemisionPdf) -> Path:
        if not remision.pdf_url: raise PdfDescargaError("pdf_url vacio")
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            destino = self._cache_dir / remision.filename
        else:
            tmp = tempfile.NamedTemporaryFile(prefix="remision_", suffix=".pdf", delete=False)
            tmp.close()
            destino = Path(tmp.name)
        try:
            client = self._http_client
            if not client:
                import httpx
                client = httpx.Client(timeout=30.0, follow_redirects=True)
            resp = client.get(remision.pdf_url)
            if resp.status_code != 200: raise PdfDescargaError(f"HTTP {resp.status_code}")
            contenido = resp.content
            if not contenido: raise PdfDescargaError("PDF vacio")
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype and "pdf" not in ctype and not contenido.startswith(b"%PDF"):
                raise PdfDescargaError(f"Content-Type: {ctype}")
            destino.write_bytes(contenido)
            return destino
        except PdfDescargaError:
            try:
                if destino.exists() and destino.stat().st_size == 0: destino.unlink(missing_ok=True)
            except OSError: pass
            raise
        except Exception as exc:
            try:
                if destino.exists() and destino.stat().st_size == 0: destino.unlink(missing_ok=True)
            except OSError: pass
            raise PdfDescargaError(str(exc)) from exc

    async def enviar_pdf_remision(self, telefono: str, numero_remision: str,
                                  *, enviar_documento_fn=None) -> Tuple[bool, str]:
        remision = self.obtener_pdf_remision(numero_remision)
        if not remision:
            return False, (f"No encontre el PDF para '{numero_remision}'. "
                           "Verifica el numero e intenta de nuevo.")
        ruta: Optional[Path] = None
        try:
            ruta = self.descargar_pdf(remision)
        except PdfDescargaError as exc:
            logger.error("Descarga %s: %s", remision.numero_remision, exc)
            return False, f"Encontre {remision.numero_remision} pero no pude bajar el PDF."
        if not enviar_documento_fn:
            from core.whatsapp import enviar_documento_whatsapp
            enviar_documento_fn = enviar_documento_whatsapp
        try:
            await enviar_documento_fn(destino=telefono, ruta_archivo=str(ruta),
                                      nombre_documento=remision.filename)
        except Exception as exc:
            logger.error("Envio %s: %s", remision.filename, exc)
            return False, "PDF descargado pero fallo envio a WhatsApp. Reintenta."
        finally:
            if not self._cache_dir and ruta is not None:
                try: Path(ruta).unlink(missing_ok=True)
                except OSError: pass
        return True, f"Remision {remision.numero_remision} enviada."

    async def reimprimir_pdf_dinamico(self, numero_remision: str,
                                      modo_solicitado: str | ModoImpresion,
                                      rol_usuario: str = "usuario_administrador",
                                      trm: float = 4000.0) -> Tuple[Path, ModoImpresion, Optional[str]]:
        num = self.normalizar_numero(numero_remision)
        if not num: raise PdfRemisionError(f"Numero invalido: '{numero_remision}'")
        modo, advertencia = self.evaluar_permisos_impresion(rol_usuario, modo_solicitado)
        datos = self.obtener_datos_completos_remision(num)
        cache = self._cache_dir or Path(tempfile.gettempdir()) / "pdf_remision_cache"
        cache.mkdir(parents=True, exist_ok=True)
        ruta_salida = str(cache / f"{num}_{modo.value}.pdf")
        cliente = datos.get("cliente") or {}
        cond = datos.get("conductor") or {}
        vr_dia = datos.get("vr_dolar_dia") or trm
        await asyncio.to_thread(_generar_pdf_desde_datos,
                                ruta_salida, datos, cliente, cond, vr_dia, modo.value)
        return Path(ruta_salida), modo, advertencia



def _generar_pdf_desde_datos(ruta_salida: str, datos: Dict, cliente: Dict,
                             conductor: Dict, vr_dolar_dia: float, modo_valores: str) -> None:
    from generador_pdf import generar_remision_pdf_archivo
    generar_remision_pdf_archivo(
        ruta_salida,
        fecha=str(datos.get("fecha_operacion") or ""),
        cliente=cliente.get("nombre", ""),
        documento=cliente.get("identificacion"),
        direccion=cliente.get("direccion"),
        celular=cliente.get("telefono"),
        placa=conductor.get("placa"),
        conductor=conductor.get("nombre"),
        id_conductor=conductor.get("identificacion"),
        celular_conductor=conductor.get("telefono"),
        items=datos.get("items") or [],
        numero_remision=datos.get("numero_remision", "SIN-NUMERO"),
        bodega_id=datos.get("bodega_id"),
        estado=datos.get("estado"),
        vr_dolar_dia=vr_dolar_dia,
        modo_valores=modo_valores,
    )


def construir_filas_listado_pdf(remisiones: List[RemisionPdf]) -> List[Tuple[str, str, str]]:
    filas: List[Tuple[str, str, str]] = []
    for r in remisiones[:10]:
        num = r.numero_remision
        cliente = (r.cliente or "Sin cliente").strip()
        fecha = ""
        if r.fecha_creacion:
            try:
                dt = datetime.fromisoformat(str(r.fecha_creacion).replace("Z", ""))
                fecha = dt.strftime("%Y-%m-%d")
            except ValueError:
                fecha = str(r.fecha_creacion)[:10]
        filas.append((num, num[:24], f"{cliente[:40]}{' - ' + fecha if fecha else ' '}"))
    return filas
