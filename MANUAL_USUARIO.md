# Manual de Usuario - Agente de Inventario por WhatsApp

Bot que permite **gestionar el inventario de materiales de reciclaje/chatarra**
por medio de WhatsApp. Se opera escribiendo mensajes de texto o tocando
botones. El bot, asistido por un modelo IA, entiende frases en lenguaje
natural (ej. *"Cooperativa 500"*, *"Quema 1000 de Cable"*).

---

## 1. Requisitos para usar el bot

- El **número de WhatsApp debe estar registrado** en el sistema (tabla
  `usuarios`) y tener **asignada una bodega**. Si no, el bot responde
  *"Acceso denegado: numero no registrado"*.
- Solo se procesan mensajes de un usuario autorizado. Cada usuario opera sobre
  **su bodega asignada**.

> Si necesitas registrar un número o cambiar la bodega, pídeselo al
> administrador del sistema.

---

## 2. Cancelar cualquier operación

Siempre puedes detener lo que estés haciendo con cualquiera de estas palabras:

```
cancelar
limpiar
reiniciar
```

El bot responderá *"Operación cancelada."* y abandonará el borrador.

---

## 3. Menú principal

El bot responde a las opciones clásicas (por texto o por botones):

| Opción | Texto que puedes escribir | Qué hace |
|--------|---------------------------|----------|
| **1 - Ingresar Inventario** | `1` o `Ingresar Inventario` | Submenú para registrar movimientos |
| **2 - Ver Inventario** | `2`, `inventario`, `saldos`, `ver saldos` | Menú de consultas |
| **3 - Anular/Corregir Rem** | `3`, `Anular Inventario`, `anular`, `corregir`, `anular/corregir rem` | Asistente para anular o corregir remisiones |

---

## 4. Registrar un movimiento (opción 1)

Al elegir `Ingresar Inventario` el bot muestra:

```
Selecciona el tipo de movimiento a registrar:
  * Entrada
  * Selección o Arreglo
  * Salida o venta
```

O puedes escribirlo directamente: `entrada`, `arreglo` (o `seleccion`,
`transformacion`), `salida` (o `venta`, `despacho`), o los números `1`, `2`, `3`.

### 4.1 Entrada de inventario (directa / inicial / ajuste)
Elige **Entrada** y describe el material y su cantidad, ej.:
- *"Entrada inicial: Carter 500, Cobre 300"*
- *"Corregir stock: Bronce 200"*

### 4.2 Entrada de Revuelto desde fuentes
Si es material **Revuelto** que llega desde una fuente:
> *"Cooperativa 500, Pesca 300, Planta 200, Corrientes 150, Compra 100"*

El bot registra cada fuente como **entrada de Revuelto**.

### 4.3 Selección / Arreglo (transformación de material)
Para transformar materiales, escribes el proceso y sus resultados. El sistema
valida que **la suma de salidas = la cantidad de entrada** (conservación de
masa/peso).

- **Selección de Revuelto** (seleccionar material bruto):
  > *"Seleccionar 1000 kg de Revuelto -> Carter 500, Cable 200, Basura 300"*
- **Re-transformación** (procesar un semilimpio, ej. quemar cable):
  > *"Quemar 1000 kg de Cable -> 600 kg de Cable Quemado + 400 kg de Basura"*
- **Selección técnica / desmonte** (ej. arreglar Carter):
  > *"Arreglar 1000 kg de Arreglo Carter -> Carter 500, Lata 200, Cable 50, Arreglo Difícil 50, Basura 200"*

> Importante: los kilos de entrada **deben ser iguales** a los kilos de salida
> (materiales **+ merma/basura**). Si no cuadran, el bot lo rechaza. Los
> desperdicios ("Basura", "Tierra") quedan como stock que **también se puede
> vender**.

### 4.4 Compra directa
> *"Compra de 250 kg de Cobre"*

El material entra directo al inventario (no pasa por Revuelto).

### 4.5 Salida / Venta
Describes la venta y los datos del cliente/conductor. Ver sección 6.

---

## 5. Consultar inventario (opción 2)

El bot muestra:
- **Inventario total** - todos los saldos de tu bodega (mayor a menor).
- **Ver movimientos** - historial de un material (todo o por rango de fechas).
- **Reporte de hoy** - resumen del día (entradas, compras, selecciones, ventas, merma).

### Comandos directos de consulta

| Escribes | Qué obtienes |
|----------|--------------|
| `inventario`, `saldos`, `ver inventario`, `2` | Submenú de consultas |
| `reporte de hoy`, `reporte hoy` | Reporte del día |
| `reporte de ayer`, `reporte ayer` | Reporte del día anterior |
| `ver grafico`, `ver gráfico`, `reporte visual` | Imagen con gráfica de inventario |
| `movimientos`, `ver movimientos`, `historial` | Historial de un material |

### Ver movimientos paso a paso
1. Escribe el material, ej. `Carter`.
2. Elige **Todo** (historial completo) o **Rango** (te pide *desde* y *hasta*
   en formato `dd-mm-aaaa` o `dd-mm`; no soporta fechas futuras).
3. Recibes cada movimiento con fecha, tipo, kilos, fuente y saldo acumulado.

> Puedes salir del modo con `cancelar`, `salir` o `menu`.

---

## 6. Venta / despacho (con remisión PDF)

El bot pide los datos del **cliente** y del **conductor** **paso a paso**. Si el
cliente/conductor ya existe, solo pide los datos que le falten.

Para iniciar una venta, describe lo que vendes, por ejemplo:

> *"Vender 3500 kg de Carter a Compañía X"*
> *"Salida: Cobre 800, Bronce 600 para cliente Y"*

El bot hará preguntas como:

1. **Nombre del cliente**
2. **Documento / cédula**
3. **Dirección**
4. **Celular**
5. **Nombre del conductor**
6. **ID / cédula del conductor**
7. **Placa del vehículo**
8. **Celular del conductor**

Puedes escribir los valores con la etiqueta: `telefono 3001234567`,
`direccion Calle 10 #5-20`, `placa ABC123`, `id 1098...`.

Al terminar:
- se registra la venta (descontando stock),
- se genera la **Remisión en PDF** y se te envía por WhatsApp,
- se descuenta automáticamente el inventario.

> Todos los materiales (bruto, semilimpio, limpio y **merma**) pueden venderse.

---

## 7. Anular / corregir remisiones (opción 3)

Escribe `3`, `anular`, `corregir` o `anular/corregir rem`.

1. El bot pregunta si deseas **ANULAR** o **CORREGIR** la remisión.
   - **Anular** -> te pide el número de remisión y confirma si quieres anular
     **toda** la remisión (`sí`/`no`).
     - **No** -> pasas al modo de corrección.
   - **Corregir** -> te pide el número de remisión y te muestra las opciones:
     **1. Material, 2. Cliente, 3. Finalizar**.

### Corregir un material
Escribe `Material cantidad`, ej. `Carter 3500`.
- Si el dato no existe, te confirma si **actualizarlo** (`sí`/`no`).
- Si anulas una línea, **el stock se devuelve** al inventario.

### Corregir datos del cliente
Escribe los datos a corregir, ej.: *"telefono 3001234567, direccion Calle 10 #5-20"*

### Finalizar
Cuando termines las correcciones escribe **`finalizar`** (o `3` en el submenú).
El sistema **regenera el PDF de la remisión conservando el MISMO número
correlativo** y te lo envía por WhatsApp con los datos ya corregidos.

---

## 8. Ejemplos rápidos (resumen)

| Intención | Ejemplo de mensaje |
|-----------|--------------------|
| Entrada de inventario inicial | `Inventario inicial: Carter 500, Cobre 300` |
| Entrada de Revuelto | `Cooperativa 500, Pesca 300` |
| Compra directa | `Comprar 250 kg de Cobre` |
| Procesamiento (quema) | `Quemar 1000 kg de Cable -> 600 Cable Quemado, 400 Basura` |
| Transformación técnica | `Arreglar 1000 kg de Arreglo Carter -> Carter 500, Lata 200, Cable 50, Arreglo Difícil 50, Basura 200` |
| Venta | `Vender 3500 kg de Carter a Compañía` |
| Consultar inventario | `inventario` / `saldos` |
| Ver movimientos | `movimientos` |
| Reporte de hoy | `reporte de hoy` |
| Anular remisión | `anular` -> `REM_112` -> `sí` |
| Corregir datos de cliente | `corregir cliente` -> nombre -> nuevos datos |

---

## 9. Errores frecuentes y cómo evitarlos

| Situación | Qué hacer |
|-----------|-----------|
| "Acceso denegado" | Tu número no está registrado o no tiene bodega asignada. Contacta al administrador. |
| "Conservación de masa" / "debe ser igual a resultados + merma" | La cantidad de entrada debe ser **exactamente** la suma de los kilos de salida (incluida la merma). Revisa y corrige los números. |
| "Stock insuficiente" | Intentas registrar más kilos de los que existen. Consulta el saldo con `inventario`. |
| "No entendí el tipo de movimiento" | Responde con `entrada`, `seleccion arreglo` (o `arreglo`) o `salida`. |
| Fecha inválida | Escribe `hoy`, `ayer`, un día (`lunes`, `martes`...), o `dd-mm-aaaa` (no futuras). |
| La venta no cuadra | La conservación de masa aplica a transformaciones; en ventas verifica que el stock alcance. |

---

*Documento generado a partir del comportamiento real de la aplicación. El bot
usa Supabase (inventario, clientes, conductores) y emite PDF de remisión.*