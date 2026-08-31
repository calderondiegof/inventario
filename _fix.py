import pathlib
p = pathlib.Path("services/pdf_remision_service.py")
s = p.read_text(encoding="utf-8")

old = "    def obtener_datos_completos_remision(self, numero: str) -> Dict[str, Any]:"
idx = s.find(old)
if idx < 0:
    print("NOT FOUND start")
    raise SystemExit(1)

# Find the next method after this one (starts with "    def ")
next_def = s.find("\n    def ", idx + len(old))
if next_def < 0:
    print("NOT FOUND end")
    raise SystemExit(1)

# The block to replace is from idx to next_def
old_block = s[idx:next_def]

new_block = '''    def obtener_datos_completos_remision(self, numero: str) -> Dict[str, Any]:
        if not self._supabase: raise PdfRemisionError("Supabase no disponible")
        # Intentar primero via el servicio refactorizado (si expone el metodo).
        try:
            from services.inventario_service import InventarioServiceConValidacion
            svc = InventarioServiceConValidacion(self._supabase)
            return svc.obtener_datos_pdf_remision(numero)
        except (ImportError, AttributeError):
            pass
        # Fallback: consultas directas con JOIN embebido materiales(nombre).
        num = self.normalizar_numero(numero)
        if not num: raise PdfRemisionError(f"Numero invalido: {numero}")
        res = self._supabase.table("remisiones").select("*").eq("numero", num).limit(1).execute()
        filas = getattr(res, "data", None) or []
        if not filas: raise PdfRemisionNoEncontrada(f"No existe la remision '{num}'")
        rem = filas[0]

        # Cliente y conductor
        cliente, conductor = {}, {}
        if rem.get("cliente_id"):
            c = self._supabase.table("clientes").select("*").eq("id", rem["cliente_id"]).limit(1).execute()
            if getattr(c, "data", None): cliente = c.data[0] or {}
        if rem.get("conductor_id"):
            d = self._supabase.table("conductores").select("*").eq("id", rem["conductor_id"]).limit(1).execute()
            if getattr(d, "data", None): conductor = d.data[0] or {}

        # Movimientos: usar relacion embebida si viene; si no, consultar por lote
        # con JOIN materiales(nombre) (mismo patron que el servicio antiguo).
        raw_movs = list(rem.get("movimientos") or [])
        if not raw_movs:
            lote_id = rem.get("lote_operacion_id")
            if lote_id:
                raw_movs = (self._supabase.table("movimientos_inventario")
                    .select("id,material_id,cantidad_kg,precio_unitario,anulado,tipo_movimiento,observaciones,materiales(nombre)")
                    .eq("lote_operacion_id", lote_id)
                    .eq("anulado", False)
                    .execute().data) or []

        # Fallback final: si el JOIN no devolvio nombres, resolver por id.
        sin_nombre = [m for m in raw_movs if not (m.get("materiales") or {}).get("nombre") and not m.get("productos")]
        material_ids = list({m["material_id"] for m in sin_nombre if m.get("material_id")})
        materiales_map = {}
        if material_ids:
            mats = (self._supabase.table("materiales")
                .select("id,nombre").in_("id", material_ids).execute().data) or []
            materiales_map = {row["id"]: row["nombre"] for row in mats}

        # Mapear cada movimiento al formato que espera generar_remision_pdf_archivo.
        items = []
        for m in raw_movs:
            try:
                cantidad = abs(float(m.get("cantidad_kg") or m.get("cantidad") or m.get("peso_kg") or 0))
            except (TypeError, ValueError):
                cantidad = 0.0
            if cantidad <= 0:
                continue
            mat_id = m.get("material_id")
            mat_nombre = (
                (m.get("materiales") or {}).get("nombre")
                or (m.get("productos") or {}).get("nombre")
                or materiales_map.get(mat_id)
                or m.get("material")
                or m.get("descripcion")
                or f"Material #{mat_id}"
            )
            items.append({
                "material_nombre": mat_nombre,
                "cantidad_kg": cantidad,
                "precio_unitario": m.get("precio_unitario") or 0.0,
                "observaciones": m.get("observaciones"),
                "tipo_movimiento": m.get("tipo_movimiento"),
            })

        return {"numero_remision": rem.get("numero", num),
                "fecha_operacion": rem.get("fecha_operacion"),
                "bodega_id": rem.get("bodega_id"),
                "estado": rem.get("estado"),
                "vr_dolar_dia": rem.get("vr_dolar_dia"),
                "cliente": cliente, "conductor": conductor, "items": items}
'''

s2 = s[:idx] + new_block + s[next_def:]
p.write_text(s2, encoding="utf-8")
print("OK", len(s2) - len(s), "delta")
