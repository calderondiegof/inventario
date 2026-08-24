import os
import io
import matplotlib
# Configurar backend no interactivo antes de importar pyplot (CRÍTICO para servidores)
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
from supabase import Client, create_client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def generar_y_subir_grafico_stock(bodega_id: int) -> str:
    fig = None
    try:
        print(f"--> [GRÁFICO] 1. Consultando vista SQL en Supabase para bodega {bodega_id}...")
        res = (
            supabase.table("vista_balance_inventario")
            .select("material_nombre, stock_actual_kg")
            .eq("bodega_id", bodega_id)
            .neq("stock_actual_kg", 0)
            .execute()
        )

        datos = res.data or []
        if not datos:
            print(f"⚠️ [GRÁFICO] No hay materiales con stock registrado en bodega {bodega_id}.")
            return None

        print(f"--> [GRÁFICO] 2. Procesando {len(datos)} registros...")
        df = pd.DataFrame(datos)
        df.rename(columns={"material_nombre": "material", "stock_actual_kg": "kg"}, inplace=True)
        df = df.sort_values(by="kg", ascending=True)

        colores = ["#d9534f" if val < 0 else "#1f77b4" for val in df["kg"]]

        num_items = len(df)
        alto_grafico = max(6, num_items * 0.45)

        print("--> [GRÁFICO] 3. Generando figura con Matplotlib (Backend Agg)...")
        fig, ax = plt.subplots(figsize=(10, alto_grafico))
        barras = ax.barh(df["material"], df["kg"], color=colores, edgecolor="black", height=0.6)

        for bar in barras:
            ancho = bar.get_width()
            pos_x = ancho + (3 if ancho >= 0 else -3)
            ha_align = "left" if ancho >= 0 else "right"
            texto = f"⚠️ {ancho:,.1f} kg" if ancho < 0 else f"{ancho:,.1f} kg"

            ax.annotate(
                texto,
                xy=(ancho, bar.get_y() + bar.get_height() / 2),
                xytext=(pos_x, 0),
                textcoords="offset points",
                ha=ha_align,
                va="center",
                fontsize=9,
                fontweight="bold",
                color="red" if ancho < 0 else "black"
            )

        ax.axvline(0, color="black", linewidth=1.2, linestyle="--")
        ax.set_title(f"🌐 Balance de Inventario — Bodega #{bodega_id} (kg)", fontsize=14, fontweight="bold", pad=15)
        ax.set_xlabel("Kilogramos (kg)", fontsize=11, labelpad=10)
        ax.set_ylabel("Material", fontsize=11)
        ax.grid(axis="x", linestyle="--", alpha=0.5)

        min_kg = min(0, df["kg"].min())
        max_kg = max(0, df["kg"].max())
        ax.set_xlim(min_kg * 1.25, max_kg * 1.25)

        plt.tight_layout()
        img_buffer = io.BytesIO()
        plt.savefig(img_buffer, format="png", dpi=150)
        img_buffer.seek(0)

        print("--> [GRÁFICO] 4. Subiendo imagen a Supabase Storage...")
        bucket_name = "reportes"
        nombre_archivo_remote = f"grafico_stock_b{bodega_id}_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.png"

        supabase.storage.from_(bucket_name).upload(
            file=img_buffer.getvalue(),
            path=nombre_archivo_remote,
            file_options={"content-type": "image/png", "x-upsert": "true"}
        )

        print("--> [GRÁFICO] 5. Obteniendo URL pública...")
        url_publica = supabase.storage.from_(bucket_name).get_public_url(nombre_archivo_remote)

        print("✅ [GRÁFICO] Proceso completado exitosamente.")
        return url_publica

    except Exception as e:
        print(f"❌ [GRÁFICO ERROR]: {type(e).__name__} - {e}")
        return None
    finally:
        if fig:
            plt.close(fig)
        plt.close("all")

if __name__ == "__main__":
    url = generar_y_subir_grafico_stock(bodega_id=1)
    print("URL generada:", url)