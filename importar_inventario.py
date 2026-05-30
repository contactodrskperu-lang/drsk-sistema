"""
importar_inventario.py
Importa DRSK_INVENTARIO_POR_SKU.xlsx a la tabla `skus` en inventario.db.
Ejecutar: python importar_inventario.py

Formato esperado (fila 1 = título, fila 2 = headers, datos desde fila 3):
  Col A: NOMBRE PRODUCTO (WA)
  Col B: TIPO
  Col C: PRECIO LISTA S/
  Col D: COSTO S/
  Col E: PRODUCTO INV
  Col F: COLOR
  Col G: TALLA
  Col H: STOCK
  Col I: ESTADO
  Col J: CODIGO EAN-13
"""
import openpyxl
import sqlite3
import os
import sys

EXCEL_PATH = os.path.join(os.path.dirname(__file__), "DRSK_INVENTARIO_POR_SKU.xlsx")
DB_PATH    = os.path.join(os.path.dirname(__file__), "inventario.db")

ESTADO_MAP = {
    "✅ stock":   "ACTIVO",
    "❌ agotado": "AGOTADO",
}

def limpiar_estado(val):
    if not val:
        return "ACTIVO"
    return ESTADO_MAP.get(str(val).strip().lower(), str(val).strip().upper())

def to_int(v):
    try:   return max(0, int(round(float(v))))
    except: return 0

def to_float(v):
    try:   return float(v)
    except: return 0.0

def to_str(v):
    if v is None:
        return ""
    return str(v).strip()

def init_table(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS skus (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre        TEXT    NOT NULL DEFAULT '',
        tipo          TEXT    NOT NULL DEFAULT '',
        precio        REAL    NOT NULL DEFAULT 0,
        costo         REAL    NOT NULL DEFAULT 0,
        producto_inv  TEXT    NOT NULL DEFAULT '',
        color         TEXT    NOT NULL DEFAULT '',
        talla         TEXT    NOT NULL DEFAULT '',
        stock         INTEGER NOT NULL DEFAULT 0,
        estado        TEXT    NOT NULL DEFAULT 'ACTIVO',
        codigo_barras TEXT    UNIQUE
    );
    """)
    # Idempotent: add costo column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE skus ADD COLUMN costo REAL NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    conn.commit()

def find_header_row(ws):
    """
    Returns (header_row_num, has_costo).
    Scans rows 1-5 to find the row with 'nombre' or 'tipo' in col 1.
    """
    for rn in range(1, 6):
        vals = [str(c.value).strip().lower() if c.value else "" for c in ws[rn]]
        if any("nombre" in v or v == "tipo" for v in vals[:3]):
            has_costo = any("costo" in v for v in vals)
            return rn, has_costo
    return 2, False  # fallback

def importar(conn):
    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)
    ws = wb.active

    header_row, has_costo = find_header_row(ws)
    data_start = header_row + 1
    ncols = 10 if has_costo else 9

    cur = conn.cursor()
    cur.execute("DELETE FROM skus")
    conn.commit()

    insertados = 0
    omitidos   = 0

    for row in ws.iter_rows(min_row=data_start, values_only=True):
        if not row or not any(row):
            continue

        cols = row[:ncols]
        nombre_raw = cols[0] if cols else None

        # Skip separator rows: cols 1+ are all empty (merged-cell dividers in export)
        if all((c is None or str(c).strip() == "") for c in cols[1:]):
            continue

        if has_costo:
            if len(cols) < 10:
                omitidos += 1
                continue
            nombre, tipo, precio_raw, costo_raw, producto_inv, color, talla, stock_raw, estado_raw, ean = cols
        else:
            if len(cols) < 9:
                omitidos += 1
                continue
            nombre, tipo, precio_raw, producto_inv, color, talla, stock_raw, estado_raw, ean = cols
            costo_raw = 0

        nombre = to_str(nombre)
        if not nombre:
            omitidos += 1
            continue

        try:
            cur.execute("""
                INSERT INTO skus
                    (nombre, tipo, precio, costo, producto_inv, color, talla, stock, estado, codigo_barras)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                nombre,
                to_str(tipo),
                to_float(precio_raw),
                to_float(costo_raw),
                to_str(producto_inv),
                to_str(color),
                to_str(talla).upper(),
                to_int(stock_raw),
                limpiar_estado(estado_raw),
                to_str(ean) or None,
            ))
            insertados += 1
        except Exception:
            omitidos += 1

    conn.commit()
    return insertados, omitidos, has_costo, data_start

if __name__ == "__main__":
    if not os.path.exists(EXCEL_PATH):
        print(f"ERROR: No se encontró {EXCEL_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    init_table(conn)
    n, s, new_fmt, data_start = importar(conn)
    conn.close()

    fmt_label = "10 columnas (con COSTO)" if new_fmt else "9 columnas (sin COSTO)"
    print(f"OK  Base de datos : {DB_PATH}")
    print(f"OK  Formato Excel : {fmt_label} (datos desde fila {data_start})")
    print(f"OK  SKUs insertados: {n}")
    if s:
        print(f"    Filas omitidas : {s} (sin nombre, separadores o EAN duplicado)")
