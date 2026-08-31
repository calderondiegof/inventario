"""Paso 3: descargar_pdf, enviar_pdf_remision, reimprimir_pdf_dinamico"""
import pathlib
p = pathlib.Path("services/pdf_remision_service.py")
p.write_text(p.read_text(encoding="utf-8") + """
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
""", encoding="utf-8")
print("g3 OK:", p.stat().st_size, "bytes")
