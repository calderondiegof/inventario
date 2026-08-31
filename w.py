"""Script generador del servicio PDF (parte 1/2)."""
import pathlib

code = r'''"""Servicio de PDFs de remision: busqueda, descarga y envio WhatsApp."""
from __future__ import annotations
import logging, re, tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)
_RE_NUMERO = re.compile(r"[A-Za-z]*[_\-\s]?(\d+)\b", re.IGNORECASE)
_PREFIJO = "REM_"

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
        return RemisionPdf(
            numero_remision=fila.get("numero_remision") or num,
            pdf_url=url,
            cliente=fila.get("cliente") or "",
            fecha_creacion=fila.get("fecha_creacion"))

    def listar_remisiones_con_pdf(self, limite: int = 10) -> List[RemisionPdf]:
        if not self._supabase or limite <= 0: return []
        try:
            res = (self._supabase.table("remisiones")
                .select("numero_remision,pdf_url,cliente,fecha_creacion")
                .not_.is_("pdf_url", "null")
                .order("fecha_creacion", desc=True)
                .order("numero_remision", desc=True).limit(limite).execute())
        except Exception as exc:
            logger.error("Error listando: %s", exc); return []
        out: List[RemisionPdf] = []
        for fila in (getattr(res, "data", None) or []):
            url = (fila.get("pdf_url") or "").strip()
            if url:
                out.append(RemisionPdf(
                    numero_remision=fila.get("numero_remision") or "",
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
            if resp.status_code != 200:
                raise PdfDescargaError(f"HTTP {resp.status_code}")
            contenido = resp.content
            if not contenido: raise PdfDescargaError("PDF vacio")
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype and "pdf" not in ctype and not contenido.startswith(b"%PDF"):
                raise PdfDescargaError(f"Content-Type: {ctype}")
            destino.write_bytes(contenido)
            return destino
        except PdfDescargaError:
            try:
                if destino.exists() and destino.stat().st_size == 0:
                    destino.unlink(missing_ok=True)
            except OSError: pass
            raise
        except Exception as exc:
            try:
    async def enviar_pdf_remision(self, telefono: str, numero_remision: str,
                                  *, enviar_documento_fn=None) -> Tuple[bool, str]:
        remision = self.obtener_pdf_remision(numero_remision)
        if not remision:
            return False, (f"No encontre el PDF para '{numero_remision}'. "
                           "Verifica el numero e intenta de nuevo.")
        try:
            ruta = self.descargar_pdf(remision)
        except PdfDescargaError as exc:
            logger.error("Descarga %s: %s", remision.numero_remision, exc)
            return False, (f"Encontre {remision.numero_remision} pero no pude "
                           f"bajar el PDF. Intenta 'corregir {remision.numero_remision}'.")
        if not enviar_documento_fn:
            from core.whatsapp import enviar_documento_whatsapp
            enviar_documento_fn = enviar_documento_whatsapp
        try:
            await enviar_documento_fn(destino=telefono,
                                      ruta_archivo=str(ruta),
                                      nombre_documento=remision.filename)
        except Exception as exc:
            logger.error("Envio %s: %s", remision.filename, exc)
            return False, "PDF descargado pero fallo envio a WhatsApp. Reintenta."
        finally:
            if not self._cache_dir:
                try: Path(ruta).unlink(missing_ok=True)
                except OSError: pass
        return True, f"Remision {remision.numero_remision} enviada."

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
        titulo = num[:24]
        desc = f"{cliente[:40]}{' - ' + fecha if fecha else ''}"[:72]
        filas.append((num, titulo, desc))
    return filas

                if destino.exists() and destino.stat().st_size == 0:
                    destino.unlink(missing_ok=True)
            except OSError: pass
            raise PdfDescargaError(str(exc)) from exc
'''

dest = pathlib.Path("services/pdf_remision_service.py")
dest.write_text(code, encoding="utf-8")
print("Parte 1 OK")
