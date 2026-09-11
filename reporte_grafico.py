import os
import io
import matplotlib
# Configurar backend no interactivo antes de importar pyplot (CRÍTICO para servidores)
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
try:  # Compatibilidad con supabase v2 (sync) y v1
    from supabase import Client, create_client  # type: ignore
except ImportError:  # pragma: no cover - fallback de compat
    import supabase as _supabase_mod  # type: ignore
    Client = getattr(_supabase_mod, "Client", object)
    create_client = getattr(_supabase_mod, "create_client", None) or (
        lambda *_a, **_k: None
    )
from dotenv import load_dotenv
from typing import Any, Dict, List

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

COLOR_NEGATIVO = "#d9534f"
COLORES_TORTA = [
    "#1f3a5f", "#2c6693", "#4a90c4", "#7fb3d5", "#b0d0e8",
    "#f2a154", "#e07b39", "#c65b28", "#8c9b6e",
]
COLOR_OTROS = "#b0b0b0"

# Máximo de porciones individuales en la torta (barras/rebanadas) ANTES de agrupar
# el resto en "Otros". Requisito: el gráfico muestra a lo sumo 10 rebanadas —
# las 9 categorías principales por peso + una décima "Otros" que consolida todo
# lo que queda. Si hay 10 o menos materiales, se muestran todos sin agrupar.
MAX_PORCIONES_TORTA = 10


def _preparar_datos_torta(df_positivo: pd.DataFrame) -> pd.DataFrame:
    # Si hay <=10 materiales se muestran todos, sin agrupar.
    if len(df_positivo) <= MAX_PORCIONES_TORTA:
        return df_positivo

    # Top (MAX_PORCIONES_TORTA - 1 = 9) principales + "Otros" = 10 porciones.
    principales = df_positivo.iloc[:MAX_PORCIONES_TORTA - 1].copy()
    resto = df_positivo.iloc[MAX_PORCIONES_TORTA - 1:]
    fila_otros = pd.DataFrame([{
        "material": f"Otros ({len(resto)} materiales)",
        "kg": resto["kg"].sum(),
        "porcentaje": resto["porcentaje"].sum(),
    }])
    return pd.concat([principales, fila_otros], ignore_index=True)


def generar_y_subir_grafico_stock(bodega_id: int) -> str:
    fig = None
    try:
        print(f"--> [DASHBOARD] 1. Consultando vista SQL en Supabase para bodega {bodega_id}...")
        res = (
            supabase.table("vista_balance_inventario")
            .select("material_nombre, stock_actual_kg")
            .eq("bodega_id", bodega_id)
            .neq("stock_actual_kg", 0)
            .execute()
        )
        datos = res.data or []
        if not datos:
            print(f"⚠️ [DASHBOARD] No hay materiales con stock registrado en bodega {bodega_id}.")
            return None

        print(f"--> [DASHBOARD] 2. Procesando {len(datos)} registros...")
        df = pd.DataFrame(datos)
        df.rename(columns={"material_nombre": "material", "stock_actual_kg": "kg"}, inplace=True)
        df = df.sort_values(by="kg", ascending=False).reset_index(drop=True)

        total_kg = df["kg"].sum()
        df["porcentaje"] = (df["kg"] / total_kg * 100) if total_kg else 0

        print("--> [DASHBOARD] 3. Generando figura con Matplotlib (Backend Agg)...")
        num_items = len(df)
        # Filas más compactas cuando hay muchos materiales, para que la imagen
        # no crezca sin control con inventarios de 20-25+ materiales.
        alto_fila = 0.42 if num_items <= 15 else 0.32
        tam_fuente_tabla = 10 if num_items <= 15 else 8.5
        alto_fig = max(6, num_items * alto_fila + 1.5)

        fig, (ax_tabla, ax_torta) = plt.subplots(
            1, 2, figsize=(13, alto_fig), gridspec_kw={"width_ratios": [1.3, 1]}
        )

        # ---------- Tabla (izquierda) — lista TODOS los materiales, sin recortar ----------
        ax_tabla.axis("off")
        filas = [
            [row["material"], f"{row['kg']:,.1f} kg", f"{row['porcentaje']:.1f}%"]
            for _, row in df.iterrows()
        ]
        filas.append(["TOTAL", f"{total_kg:,.1f} kg", "100.0%"])

        tabla = ax_tabla.table(
            cellText=filas,
            colLabels=["Material", "Stock", "%"],
            cellLoc="left",
            colLoc="left",
            loc="center",
        )
        tabla.auto_set_font_size(False)
        tabla.set_fontsize(tam_fuente_tabla)
        tabla.scale(1, 1.5 if num_items <= 15 else 1.25)
        for (fila, col), celda in tabla.get_celld().items():
            celda.set_edgecolor("#dddddd")
            if fila == 0:
                celda.set_facecolor("#1f3a5f")
                celda.set_text_props(color="white", fontweight="bold")
            elif fila == len(filas):
                celda.set_facecolor("#eef2f7")
                celda.set_text_props(fontweight="bold")
            else:
                kg_val = df.iloc[fila - 1]["kg"]
                if kg_val < 0 and col == 1:
                    celda.set_text_props(color=COLOR_NEGATIVO, fontweight="bold")

        # ---------- Torta (derecha) — agrupa en "Otros" si hay demasiados materiales ----------
        df_positivo = df[df["kg"] > 0].reset_index(drop=True)
        if not df_positivo.empty:
            df_torta = _preparar_datos_torta(df_positivo)
            colores = []
            for i, nombre in enumerate(df_torta["material"]):
                if nombre.startswith("Otros ("):
                    colores.append(COLOR_OTROS)
                else:
                    colores.append(COLORES_TORTA[i % len(COLORES_TORTA)])

            wedges, _, autotextos = ax_torta.pie(
                df_torta["kg"],
                labels=None,
                autopct=lambda pct: f"{pct:.1f}%" if pct >= 3 else "",
                startangle=90,
                colors=colores,
                pctdistance=0.8,
                wedgeprops={"edgecolor": "white", "linewidth": 1.2},
            )
            for t in autotextos:
                t.set_fontsize(8)
                t.set_color("white")
                t.set_fontweight("bold")
            ax_torta.legend(
                wedges, df_torta["material"],
                loc="center left", bbox_to_anchor=(1.02, 0.5),
                fontsize=8, frameon=False,
            )
            ax_torta.set_title("Distribución del stock", fontsize=11, fontweight="bold", pad=10)
        else:
            ax_torta.axis("off")
            ax_torta.text(0.5, 0.5, "Sin stock positivo\npara graficar", ha="center", va="center", fontsize=10)

        fig.suptitle(f"📊 Dashboard de Inventario — Bodega #{bodega_id}", fontsize=15, fontweight="bold")
        plt.tight_layout(rect=[0, 0, 1, 0.95])

        img_buffer = io.BytesIO()
        plt.savefig(img_buffer, format="png", dpi=150, bbox_inches="tight")
        img_buffer.seek(0)

        print("--> [DASHBOARD] 4. Subiendo imagen a Supabase Storage...")
        bucket_name = "reportes"
        nombre_archivo_remote = f"dashboard_stock_b{bodega_id}_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.png"
        supabase.storage.from_(bucket_name).upload(
            file=img_buffer.getvalue(),
            path=nombre_archivo_remote,
            file_options={"content-type": "image/png", "x-upsert": "true"}
        )

        print("--> [DASHBOARD] 5. Obteniendo URL pública...")
        url_publica = supabase.storage.from_(bucket_name).get_public_url(nombre_archivo_remote)
        print("✅ [DASHBOARD] Proceso completado exitosamente.")
        return url_publica

    except Exception as e:
        print(f"❌ [DASHBOARD ERROR]: {type(e).__name__} - {e}")
        return None
    finally:
        if fig:
            plt.close(fig)
        plt.close("all")


def _agrupar_entradas_salidas(datos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Agrupa movimientos de un día por material: entradas (kg>=0) y salidas
    (|kg| para kg<0). Devuelve lista ordenada (de mayor a menor movimiento)
    solo con materiales que tengan algún movimiento neto distinto de cero."""
    totales: Dict[str, Dict[str, float]] = {}
    for fila in datos:
        material = (fila.get("materiales") or {}).get("nombre", "Desconocido")
        cantidad = float(fila.get("cantidad_kg") or 0.0)
        bucket = totales.setdefault(material, {"entradas": 0.0, "salidas": 0.0})
        if cantidad >= 0:
            bucket["entradas"] += cantidad
        else:
            bucket["salidas"] += abs(cantidad)

    # Solo materiales con algún movimiento neto distinto de cero, ordenados del
    # de mayor movimiento (entrada o salida) al menor.
    materiales = sorted(
        (m for m, b in totales.items()
         if round(b["entradas"], 2) or round(b["salidas"], 2)),
        key=lambda m: -max(totales[m]["entradas"], totales[m]["salidas"]),
    )
    return [
        {"material": m, "entradas": totales[m]["entradas"],
         "salidas": totales[m]["salidas"]}
        for m in materiales
    ]


def generar_y_subir_grafico_movimientos_dia(bodega_id: int, fecha: str) -> str:
    """Genera y sube el gráfico de ENTRADAS vs SALIDAS de una bodega en un día.

    Regla usada (libro mayor): cantidad_kg > 0 = entrada, cantidad_kg < 0 =
    salida. Se agrupa por material y se dibujan barras horizontales
    apareadas: verde = entradas, rojo = salidas (valores absolutos).
    Devuelve la URL pública, o None si no hay movimientos o hay error.
    """
    fig = None
    try:
        print(f"--> [ENTR/SAL] 1. Consultando movimientos del día {fecha} para bodega {bodega_id}...")
        res = (
            supabase.table("movimientos_inventario")
            .select("tipo_movimiento,cantidad_kg,materiales(nombre)")
            .eq("bodega_id", bodega_id)
            .eq("fecha_operacion", fecha)
            .execute()
        )
        datos = res.data or []
        if not datos:
            print(f"⚠️ [ENTR/SAL] No hay movimientos el {fecha} en bodega {bodega_id}.")
            return None

        print(f"--> [ENTR/SAL] 2. Procesando {len(datos)} registros...")
        agrupados = _agrupar_entradas_salidas(datos)
        if not agrupados:
            print(f"⚠️ [ENTR/SAL] Movimientos encontrados pero todos en cero.")
            return None

        materiales = [g["material"] for g in agrupados]
        entradas = [g["entradas"] for g in agrupados]
        salidas = [g["salidas"] for g in agrupados]
        y = list(range(len(materiales)))
        alto_fig = max(5.5, len(materiales) * 0.5 + 1.5)

        fig, ax = plt.subplots(figsize=(10, alto_fig))
        ax.barh(y, entradas, height=0.4, color="#2ca02c", label="Entradas (kg)")
        ax.barh([pos + 0.4 for pos in y], salidas, height=0.4, color=COLOR_NEGATIVO,
                label="Salidas (kg)")
        ax.set_yticks([pos + 0.2 for pos in y])
        ax.set_yticklabels(materiales, fontsize=max(8, 11 - max(0, len(materiales) - 12)))
        ax.invert_yaxis()
        ax.set_xlabel("Kilogramos (kg)", fontsize=10)
        ax.set_title(f"📊 Movimientos del día {fecha} — Bodega #{bodega_id}",
                     fontsize=13, fontweight="bold")
        ax.legend(loc="lower right", fontsize=9, frameon=False)
        ax.grid(axis="x", linestyle="--", alpha=0.4)
        ax.set_axisbelow(True)
        for pos, ent, sal in zip(y, entradas, salidas):
            maxv = max(ent, sal) * 0.02
            if ent:
                ax.text(ent + maxv, pos, f"{ent:,.0f}", va="center", fontsize=8, color="#1e7a1e")
            if sal:
                ax.text(sal + maxv, pos + 0.4, f"{sal:,.0f}", va="center", fontsize=8, color=COLOR_NEGATIVO)

        plt.tight_layout()
        img_buffer = io.BytesIO()
        plt.savefig(img_buffer, format="png", dpi=150, bbox_inches="tight")
        img_buffer.seek(0)

        print("--> [ENTR/SAL] 3. Subiendo imagen a Supabase Storage...")
        bucket_name = "reportes"
        nombre_archivo_remote = f"entradas_salidas_b{bodega_id}_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.png"
        supabase.storage.from_(bucket_name).upload(
            file=img_buffer.getvalue(),
            path=nombre_archivo_remote,
            file_options={"content-type": "image/png", "x-upsert": "true"}
        )
        url_publica = supabase.storage.from_(bucket_name).get_public_url(nombre_archivo_remote)
        print("✅ [ENTR/SAL] Proceso completado exitosamente.")
        return url_publica

    except Exception as e:
        print(f"❌ [ENTR/SAL ERROR]: {type(e).__name__} - {e}")
        return None
    finally:
        if fig:
            plt.close(fig)
        plt.close("all")


if __name__ == "__main__":
    url = generar_y_subir_grafico_stock(bodega_id=1)
    print("URL generada:", url)