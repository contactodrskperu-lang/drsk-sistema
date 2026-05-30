# DRSK PERU — Resumen de Sesión 28 May 2026

---

## Estado actual del sistema

| Componente | Estado |
|------------|--------|
| **Servidor** | Flask en `http://localhost:5000` (Python 3.12) |
| **Base de datos** | `inventario.db` — tabla `skus` con **649 SKUs** activos |
| **EANs** | Todos válidos — fórmula EAN-13 estándar (pos. impar ×1, par ×3) |
| **Rango EANs** | `7750000000014` → `7750000006498` (secuenciales únicos por SKU) |
| **Excel maestro** | `DRSK_INVENTARIO_POR_SKU.xlsx` — 10 columnas A:J con COSTO S/ |
| **Búsqueda por EAN** | Tolerante: coincide por primeros 12 dígitos (ignora check digit del lector) |

---

## Schema actual — tabla `skus`

```sql
CREATE TABLE skus (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre        TEXT    NOT NULL DEFAULT '',   -- nombre WA completo
    tipo          TEXT    NOT NULL DEFAULT '',   -- categoría (POLERA, PANTALON, etc.)
    precio        REAL    NOT NULL DEFAULT 0,    -- precio de venta
    costo         REAL    NOT NULL DEFAULT 0,    -- costo de compra
    producto_inv  TEXT    NOT NULL DEFAULT '',   -- nombre limpio para etiquetas/tickets
    color         TEXT    NOT NULL DEFAULT '',
    talla         TEXT    NOT NULL DEFAULT '',   -- un SKU por talla (S/M/L/XL/XXL/U)
    stock         INTEGER NOT NULL DEFAULT 0,
    estado        TEXT    NOT NULL DEFAULT 'ACTIVO',
    codigo_barras TEXT    UNIQUE                 -- EAN-13 válido
)
```

---

## Scripts de mantenimiento

| Script | Función |
|--------|---------|
| `python importar_inventario.py` | Limpia y reimporta desde Excel (detecta 9 o 10 columnas) |
| `python corregir_ean.py` | Regenera EAN-13 válidos para todos los SKUs desde cero |
| `python fix_ean_check.py` | Recalcula solo el check digit de EANs existentes (sin cambiar la base) |
| `python verificar_excel.py` | Muestra los primeros 8 EANs del Excel para validar |
| `python app.py` | Inicia el servidor Flask en puerto 5000 |

**Flujo correcto después de actualizar el Excel:**
```
python importar_inventario.py  →  python corregir_ean.py  →  python app.py
```

**Para regenerar el Excel desde la BD (sin reiniciar servidor):**
```
GET http://localhost:5000/api/inventario/exportar
```

---

## Todo lo que se hizo hoy

### Sesión 1 — Migración de schema y bugs base

#### 1. Migración de `productos` a `skus`
- Schema anterior: tabla `productos` con 5 columnas de stock (stock_s, stock_m, stock_l, stock_xl, stock_xxl) — un producto por modelo.
- Schema nuevo: tabla `skus` con 1 columna `stock` — un SKU por talla/color.
- Reset completo: eliminadas todas las tablas antiguas (productos, ventas, ajustes, variantes, config, etc.). Recreada solo `skus`.
- Reimportados 649 SKUs desde Excel.

#### 2. Importación Excel — Nuevo formato 10 columnas
- `importar_inventario.py` solo leía 9 columnas; no tenía columna `costo`.
- Solución: auto-detección de headers en filas 1–5. Soporte para 9 y 10 columnas. Salto automático de filas separadoras. Columna `costo` añadida a la tabla y al script.

#### 3. EAN-13 — Regeneración desde cero
- Los EANs del Excel eran secuenciales simples sin base única, colapsaban al corregir el check digit.
- Solución: `corregir_ean.py` regenera todos los EANs usando `7750000` + 5 dígitos de secuencia + check digit. Cada SKU tiene una base única.

#### 4. Venta Manual — Selector de talla innecesario al escanear
- Al escanear un EAN aparecía un selector S/M/L/XL/XXL (sistema antiguo multi-talla).
- Solución: eliminado `#vmTallaSelect` del HTML. `vmOnScan` llama `vmAddToCart(p.talla)` directo.

#### 5. Scanner Bluetooth — Doble escaneo
- El lector Bluetooth enviaba el mismo EAN dos veces consecutivas.
- Solución: cooldown de 500ms en `vmOnScan` y `scanProduct` usando `_lastScanEan` + `_lastScanMs`.

#### 6. Talla U no reconocida
- Productos con talla `U` (beanies, gorras, relojes, billeteras) aparecían como AGOTADO aunque tuvieran stock.
- `sku_to_dict` solo iteraba `s/m/l/xl/xxl`, nunca definía `stock_u`.
- Solución: añadido `"u"` al loop en `sku_to_dict`. `_autoAdd` tiene fallback: `prod[col] ?? prod.stock_total ?? prod.stock ?? 0`.

#### 7. Etiqueta — Nombre duplicado
- La etiqueta mostraba "PANTALON Pantalon JOGGER STRECHE GRIS — GRIS" porque `generate_tspl` concatenaba `tipo + nombre`.
- Solución: `generate_tspl` y ZPL usan `producto_inv` (ej: "JOGGER STRECHE") como línea principal.

#### 8. Ticket de Venta Manual — Nombre duplicado
- El ticket mostraba "PANTALON Pantalon JOGGER OVERSIZE BEIGE".
- Solución:
  - Backend (`sku_to_dict`): `nombre_completo = producto_inv if producto_inv else nombre`.
  - Frontend: nueva función `_nombreProd(p)` que lee `producto_inv` directamente. Reemplaza todas las ocurrencias en el template.

#### 9. Búsqueda por nombre en Venta Manual
- El campo de escaneo solo aceptaba EAN-13 numérico.
- Solución: `vmOnScan` detecta input de texto → búsqueda con debounce 280ms via `/api/productos?q=...`, muestra hasta 10 resultados. Click en resultado agrega al carrito directo. Escape limpia el campo.

#### 10. Módulos usando tabla `productos` antigua
- Conteo físico, Etiquetas y Venta Manual tenían lógica de 5 tallas por producto.
- Solución: `mostrarProductoConteo` y `confirmarProductoConteo` actualizados. `autoSelectTalla` simplificado usando `prod.talla` directo.

---

### Sesión 2 — EAN mismatch con lector físico

#### 11. EAN mismatch — Búsqueda tolerante por 12 dígitos
- El lector recalcula el check digit con su propia fórmula, devolviendo un código diferente al impreso.
- Ejemplo: etiqueta con `7750000000607` → lector lee `7750000000601`.
- Solución: 4 endpoints cambiados de `WHERE codigo_barras=?` a `WHERE substr(codigo_barras,1,12)=substr(?,1,12)`:
  - `/api/scan/<ean13>` — búsqueda principal de Venta Manual
  - `/api/etiqueta/<ean13>` — generación de etiqueta por scan
  - `/api/imprimir/masivo` — impresión batch por EAN
  - `/api/imprimir` — impresión directa

#### 12. Verificación de check digits en BD
- Script `fix_ean_check.py` ejecutado para recalcular todos los check digits.
- Resultado: **649/649 EANs ya correctos** (fórmula: pos. impar ×1, par ×3, 10 − suma mod 10).
- La fórmula del lector coincide exactamente con la estándar EAN-13.

#### 13. Excel actualizado
- `DRSK_INVENTARIO_POR_SKU.xlsx` regenerado con los 649 EANs actuales de la BD.
- Verificado: 649 filas con EAN empezando en `775...`.

---

## Bugs resueltos (total: 13)

| # | Bug | Archivo | Solución |
|---|-----|---------|----------|
| 1 | Importación Excel — formato 10 columnas no detectado | `importar_inventario.py` | `find_header_row()` escanea filas 1-5 |
| 2 | EAN-13 con bases duplicadas → conflicto UNIQUE | `corregir_ean.py` | Regenera desde cero con secuencias únicas |
| 3 | Selector de talla innecesario en Venta Manual | `index.html` | Eliminado `vmTallaSelect`; add directo |
| 4 | Doble escaneo con lector Bluetooth | `index.html` | Cooldown 500ms con `_lastScanMs` |
| 5 | Talla U reportada como AGOTADO | `app.py` + `index.html` | `"u"` en loop `sku_to_dict`; fallback en `_autoAdd` |
| 6 | Etiqueta muestra "PANTALON Pantalon JOGGER..." | `app.py` | `generate_tspl` usa `producto_inv` |
| 7 | Ticket muestra tipo+nombre duplicado | `app.py` + `index.html` | `nombre_completo = producto_inv`; `_nombreProd(p)` |
| 8 | Sin búsqueda por nombre en scanner VM | `index.html` | Text search con debounce 280ms |
| 9 | Módulos con schema de 5 tallas antiguo | `index.html` | `mostrarProductoConteo` y `autoSelectTalla` refactorizados |
| 10 | Reset completo de BD | `inventario.db` | Eliminadas 17 tablas; recreada solo `skus` |
| 11 | EAN mismatch: lector recalcula check digit | `app.py` | `substr(codigo_barras,1,12)=substr(?,1,12)` |
| 12 | Excel desactualizado con EANs viejos | `DRSK_INVENTARIO_POR_SKU.xlsx` | Regenerado desde BD via `/api/inventario/exportar` |
| 13 | Separadores Excel importados como SKUs falsos | `importar_inventario.py` | Skip si `cols[1:]` todas vacías |

---

## Bugs pendientes / por verificar

1. **`row_to_dict` legacy** (app.py línea 349): función que construye `nombre_completo = categoria + nombre` con el formato incorrecto. No es llamada por ningún endpoint activo, pero puede confundir. Candidata a eliminar.

2. **110 EANs duplicados en Excel maestro**: el Excel original tiene 110 SKUs duplicados. El importador conserva la primera ocurrencia y descarta la segunda. Limpiar el Excel maestro.

3. **Historial de ventas — nombre antiguo**: ventas registradas antes del fix de `nombre_completo` muestran el nombre antiguo. Es comportamiento esperado (registro histórico inmutable), pero puede confundir en reportes.

4. **Etiquetas físicas impresas**: las etiquetas actualmente pegadas en productos tienen EANs del Excel viejo. El sistema las encuentra igual (búsqueda por 12 dígitos), pero para eliminar la discrepancia permanentemente hay que reimprimir todas las etiquetas.

5. **`defectos` FK apunta a `productos`** (app.py línea 136): `FOREIGN KEY (producto_id) REFERENCES productos(id)` — tabla `productos` ya no existe. No causa error porque SQLite no enforcea FKs por defecto, pero es técnicamente incorrecto.

---

## Próximos pasos recomendados

1. **Reimprimir todas las etiquetas físicas**: los EANs en la BD son correctos; reimprimir desde el módulo Etiquetas para que las etiquetas pegadas en los productos coincidan exactamente.

2. **Limpiar Excel maestro**: eliminar los 110 SKUs duplicados y rellenar la columna COSTO S/ con los valores reales de costo por producto.

3. **Actualizar CSV de EANs** (`ean_actualizados.csv`): exportar mapeo nombre→EAN actual desde la BD.

4. **Eliminar `row_to_dict`** (app.py línea 349): función sin uso que puede causar confusión futura.

5. **Corregir FK en `defectos`**: cambiar `REFERENCES productos(id)` a `REFERENCES skus(id)`.

6. **Tabla `ventas`**: fue eliminada en el reset pero se recrea automáticamente al iniciar `app.py` via `migrate_db`. El historial previo al reset no existe. Si se necesita historial, las ventas nuevas se registran desde cero.

---

## Arquitectura del sistema

### Stack técnico

| Componente | Tecnología |
|------------|------------|
| Backend | Python 3.12 + Flask |
| Base de datos | SQLite (`inventario.db`) |
| Frontend | HTML/CSS/JS vanilla (1 archivo: `templates/index.html`) |
| Impresión etiquetas | TSPL (Gprinter GP-3120TU, 50×25mm, 203 DPI) + ZPL |
| Impresión en Windows | `win32print` (pywin32) — envío RAW directo a impresora |
| Excel | `openpyxl` (import/export) |

### Tablas de la base de datos

| Tabla | Descripción |
|-------|-------------|
| `skus` | **Principal** — 1 fila por SKU (producto+color+talla) |
| `ventas` | Registro de cada venta unitaria |
| `ventas_grupos` | Agrupa ventas de una misma transacción (cliente, descuento) |
| `ajustes` | Log de ajustes manuales de stock |
| `conteos` | Cabecera de cada conteo físico |
| `conteo_items` | Detalle por SKU de cada conteo físico |
| `defectos` | Productos con defecto pendientes de acción |
| `pedidos` | Pedidos de compra a proveedor |
| `despachos` | Despachos de envío a cliente |
| `despacho_items` | Ítems de cada despacho |
| `pedidos_venta` | Pedidos de venta con datos de cliente/envío |
| `pv_items` | Ítems de cada pedido de venta |
| `pv_incidencias` | Incidencias durante recolección de pedido |
| `gastos` | Gastos por canal para cálculo de rentabilidad |
| `config` | Comisiones por marketplace y config general |
| `variantes` | Legacy (no-op; EAN está en `skus.codigo_barras`) |

---

## Los 11 módulos del sistema

### 1. Dashboard
- **Ruta UI**: botón principal en barra lateral
- **API**: `GET /api/dashboard`
- **Función**: estadísticas en tiempo real — total SKUs, agotados, bajo stock, ventas del día, ventas del mes, valor del inventario.

---

### 2. Venta Manual
- **Ruta UI**: `showPage('vender')`
- **API**: `POST /api/venta_manual`, `GET /api/scan/<ean13>`, `GET /api/productos?q=...`
- **Función**: punto de venta presencial. Escaneo de EAN o búsqueda por nombre. Carrito de compra. Descuentos con motivo obligatorio. Soporte multicanal (WhatsApp, Instagram, Presencial, etc.).
- **Flujo**: escanear/buscar → agregar al carrito → aplicar descuento (opcional) → confirmar venta → descuenta stock, registra en `ventas` + `ventas_grupos`.
- **Tolerancia EAN**: `substr(codigo_barras,1,12)=substr(?,1,12)` — ignora diferencias en el check digit del lector.

---

### 3. Historial de Ventas
- **Ruta UI**: `showPage('ventas')`
- **API**: `GET /api/ventas_grupos`, `GET /api/ventas/por_mes`
- **Función**: historial agrupado por transacción (cliente + ticket completo). Filtro por fecha. Totales por mes y canal.

---

### 4. Pedidos de Venta (PV) — 4 fases
- **Ruta UI**: `showPage('pv')`
- **API**: `/api/pv` (CRUD), `/api/pv/recoleccion`, `/api/pv/recoleccion/scan`, `/api/pv/armado`, `/api/pv/<id>/scan_armado`, `/api/pv/<id>/sellar`, `/api/pv/exportar/dinsides`, `/api/pv/exportar/olva`, `/api/pv/exportar/marketplace`
- **Función**: gestión completa del ciclo de vida de un pedido de venta online.
- **Fase 1 — Recolección**: escanear cada prenda del pedido para confirmar que está en stock. Registra incidencias si un producto no se encuentra.
- **Fase 2 — Clasificación**: agrupar pedidos por courier (Dinsides / Olva) para optimizar la preparación.
- **Fase 3 — Armado**: verificar cada paquete antes de sellar.
- **Fase 4 — Despacho**: marcar como enviado, exportar manifiestos para Dinsides/Olva/marketplaces.

---

### 5. Despachos
- **Ruta UI**: `showPage('despachos')`
- **API**: `/api/despachos` (CRUD), `/api/despacho/<id>/scan`, `/api/despacho/<id>/estado`
- **Función**: despachos manuales/directos (fuera del flujo PV). Crear despacho con ítems, escanear para confirmar, cambiar estado (PENDIENTE → EN RECOLECCIÓN → LISTO → ENVIADO).

---

### 6. Inventario
- **Ruta UI**: `showPage('inventario')`
- **API**: `GET /api/productos`, `GET /api/categorias`, `GET /api/inventario/exportar`, `POST /api/inventario/importar`, `PUT /api/estado/<id>`
- **Función**: tabla completa de SKUs con filtros por categoría, estado y búsqueda libre. Exportar Excel actualizado. Importar Excel para actualizar masivamente.
- **Formato Excel** (10 columnas):
  ```
  A: NOMBRE PRODUCTO (WA)
  B: TIPO
  C: PRECIO LISTA S/
  D: COSTO S/
  E: PRODUCTO INV
  F: COLOR
  G: TALLA
  H: STOCK
  I: ESTADO
  J: CODIGO EAN-13
  ```

---

### 7. Etiquetas
- **Ruta UI**: `showPage('etiquetas')`
- **API**: `GET /api/etiqueta/<ean13>`, `POST /api/imprimir`, `POST /api/imprimir/masivo`, `POST /api/imprimir/raw`, `GET /api/productos/lista_masivo`, `GET /api/productos/para_imprimir`, `GET /api/impresoras`
- **Función**: generación e impresión de etiquetas TSPL para Gprinter GP-3120TU (50×25mm).
- **Contenido etiqueta** (top→bottom): DRSK | código de barras EAN-13 | número EAN | nombre producto | talla - color.
- **Impresión individual**: escanear EAN → preview → imprimir.
- **Impresión masiva**: imprimir etiquetas de todo el inventario, nuevo ingreso, o por categoría. Cantidad = stock actual.
- **Etiqueta de defecto**: `generate_tspl_defecto` — borde grueso, texto de defecto y estado de acción.
- **Envío RAW**: TSPL enviado directo a la impresora via `win32print` (sin diálogo de impresión).

---

### 8. Defectos
- **Ruta UI**: `showPage('defectos')`
- **API**: `POST /api/defecto`, `GET /api/defectos`, `PUT /api/defecto/<id>/accion`, `GET /api/defectos/reporte_proveedor`, `GET /api/defecto/etiqueta/<id>`
- **Función**: registrar productos con defecto detectados en inventario o al preparar pedidos. Asignar acción (RECHAZADO, REHACER, EN REVISIÓN, REVISIÓN PROVEEDOR). Imprimir etiqueta de defecto. Exportar reporte para proveedor.

---

### 9. Ajuste de Stock
- **Ruta UI**: `showPage('ajuste')`
- **API**: `POST /api/ajuste`
- **Función**: corrección manual del stock de cualquier SKU. Escanear EAN → ingresar nuevo stock → confirmar con motivo. Registra en tabla `ajustes` para auditoría.

---

### 10. Conteo Físico
- **Ruta UI**: `showPage('conteo')`
- **API**: `POST /api/conteo`, `GET /api/conteos`, `GET /api/conteo/<id>`
- **Función**: inventario físico contra la BD. Escanear cada prenda físicamente → registrar unidades contadas → comparar con stock esperado → guardar diferencias. Histórico de conteos anteriores con diferencias detectadas.

---

### 11. Pedidos a Proveedor
- **Ruta UI**: `showPage('pedidos')`
- **API**: `GET /api/pedidos`, `POST /api/pedido`, `DELETE /api/pedido/<id>`, `POST /api/pedido/<id>/confirmar`
- **Función**: registrar pedidos de reposición al proveedor. Al confirmar un pedido, agrega el stock recibido al SKU correspondiente y cambia el estado del producto a ACTIVO si estaba agotado.

---

### Dashboard Financiero (integrado)
- **Ruta UI**: sección dentro del dashboard
- **API**: `GET /api/gastos`, `POST /api/gasto`, `DELETE /api/gasto/<id>`, `GET /api/ventas/por_mes`, `GET /api/config`, `POST /api/config`
- **Función**: registrar gastos por canal (publicidad, comisiones, envíos). Configurar comisiones por marketplace (Ripley, Falabella, Juntoz, Oechsle). Ver rentabilidad por mes y canal comparando ventas vs gastos vs comisiones.

---

### Modo Training
- **API**: `GET /api/training/status`, `POST /api/training/toggle`
- **Función**: crea una copia de `inventario.db` como `inventario_training.db`. Todas las operaciones en modo training (ventas, ajustes, etc.) usan la copia sin afectar el inventario real. Ideal para entrenar nuevos empleados.

---

## Fórmula EAN-13 usada en el sistema

```python
def ean13_check(base12):
    # posiciones impares (1,3,5...) x1 — posiciones pares (2,4,6...) x3
    # en Python 0-indexed: índice par x1, índice impar x3
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(base12))
    return str((10 - total % 10) % 10)
```

**Ejemplo verificado**: `775000000060` → check=`1` → EAN=`7750000000601`

**Estructura del EAN**: `7750000` (prefijo DRSK) + `XXXXX` (secuencia 5 dígitos, único por SKU) + `C` (check digit)

---

*Sesión: 28 Mayo 2026 | Sistema: DRSK PERU Inventario v2 (schema skus) | SKUs: 649 | Python 3.12 + Flask + SQLite*
