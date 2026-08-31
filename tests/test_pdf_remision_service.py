"""Tests del servicio PDF de remision (sin interaccion con la suite legacy)."""
import pytest


def test_pdf_normalizar():
    from services.pdf_remision_service import PdfRemisionService
    assert PdfRemisionService.normalizar_numero("117") == "REM_117"
    assert PdfRemisionService.normalizar_numero("rem_117") == "REM_117"
    assert PdfRemisionService.normalizar_numero("REM-117") == "REM_117"
    assert PdfRemisionService.normalizar_numero("Remision 117") == "REM_117"
    assert PdfRemisionService.normalizar_numero("") == ""


def test_pdf_dto():
    from services.pdf_remision_service import RemisionPdf
    r = RemisionPdf("REM_117", "https://x/a.pdf", "Acme SA", "2026-08-31")
    assert r.filename == "REM_117.pdf"
    assert "Acme" in r.caption


def test_pdf_supabase_none():
    from services.pdf_remision_service import PdfRemisionService
    svc = PdfRemisionService(supabase=None)
    assert svc.obtener_pdf_remision("117") is None
    assert svc.listar_remisiones_con_pdf(10) == []


def test_pdf_construir_filas():
    from services.pdf_remision_service import RemisionPdf, construir_filas_listado_pdf
    filas = construir_filas_listado_pdf([
        RemisionPdf("REM_117", "https://x/p", "Acme SA", "2026-08-31"),
        RemisionPdf("REM_116", "https://x/p", "Bronce SRL", None),
    ])
    assert len(filas) == 2
    assert filas[0][0] == "REM_117"
    assert filas[0][1] == "REM_117"
    assert "Acme" in filas[0][2]


def test_pdf_descargar_vacia():
    from services.pdf_remision_service import PdfRemisionService, RemisionPdf, PdfDescargaError
    with pytest.raises(PdfDescargaError):
        PdfRemisionService(supabase=None).descargar_pdf(RemisionPdf("X", "", "C"))
