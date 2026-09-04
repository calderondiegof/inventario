"""Fixtures de pytest para la suite offline.

El directorio raíz del proyecto tiene un paquete `supabase/` (migraciones SQL)
que, junto con `core/config.py` (que fuerza la importación del paquete REAL de
Supabase al hacer `from supabase._sync.client import ...`), hace frágil la
orden de recolección de tests: una vez que se carga el paquete real con
`SUPABASE_URL` vacío, `create_client("", "")` lanza
``SupabaseException: supabase_url is required`` y rompen tests que importan
`reporte_grafico` al vuelo.

Aquí definimos credenciales dummy ANTES de que ningún test importe supabase:
el cliente real de supabase es perezoso (no conecta hasta hacer una request),
así que con una URL no vacía la construcción no falla y las funciones puras
que no tocan la DB (p.ej. `_preparar_datos_torta`) funcionan igual.
"""
import os

os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy-supabase-anon-key")