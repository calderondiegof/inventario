"""Paso 2a: clase con metodos estaticos, datos, obtener, listar"""
import pathlib
p = pathlib.Path("services/pdf_remision_service.py")
p.write_text(p.read_text(encoding="utf-8") + """
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
""", encoding="utf-8")
print("g2 OK:", p.stat().st_size, "bytes")
