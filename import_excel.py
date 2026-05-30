"""
import_excel.py — Importa inventario.xlsx a inventario.db
Ejecutar solo una vez: python import_excel.py
"""
import openpyxl
import sqlite3
import os
import sys

EXCEL_PATH = r"C:\Users\DRSK PERU\Desktop\CLAUDE\inventario.xlsx"
DB_PATH    = os.path.join(os.path.dirname(__file__), "inventario.db")

ESTADOS = [
    "ACTIVO", "BAJO STOCK", "AGOTADO", "EN PEDIDO", "DESCONTINUADO",
    "LIQUIDACION", "EN CONSIGNACION", "RESERVADO", "NUEVO INGRESO",
    "EN REVISION", "DEVOLUCION"
]

def ean13(seq: int) -> str:
    digits = f"7750000{seq:05d}"       # 12 dígitos: 775 (Perú) + 0000 + secuencia
    total  = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    check  = (10 - total % 10) % 10
    return digits + str(check)

def to_int(v):
    try:   return max(0, int(v))
    except: return 0

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS productos (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ean13     TEXT    UNIQUE NOT NULL,
        categoria TEXT    NOT NULL,
        nombre    TEXT    NOT NULL,
        color     TEXT    NOT NULL DEFAULT 'UNICO',
        stock_s   INTEGER NOT NULL DEFAULT 0,
        stock_m   INTEGER NOT NULL DEFAULT 0,
        stock_l   INTEGER NOT NULL DEFAULT 0,
        stock_xl  INTEGER NOT NULL DEFAULT 0,
        stock_xxl INTEGER NOT NULL DEFAULT 0,
        costo     REAL    NOT NULL DEFAULT 0,
        precio    REAL    NOT NULL DEFAULT 0,
        estado    TEXT    NOT NULL DEFAULT 'ACTIVO'
    );

    CREATE TABLE IF NOT EXISTS ventas (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        producto_id INTEGER NOT NULL,
        ean13       TEXT    NOT NULL,
        nombre      TEXT    NOT NULL,
        color       TEXT    NOT NULL,
        talla       TEXT    NOT NULL,
        precio      REAL    NOT NULL,
        cantidad    INTEGER NOT NULL DEFAULT 1,
        fecha       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (producto_id) REFERENCES productos(id)
    );

    CREATE TABLE IF NOT EXISTS ajustes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        producto_id INTEGER NOT NULL,
        talla       TEXT    NOT NULL,
        antes       INTEGER,
        despues     INTEGER,
        motivo      TEXT,
        fecha       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (producto_id) REFERENCES productos(id)
    );
    """)
    conn.commit()

def import_data(conn):
    wb = openpyxl.load_workbook(EXCEL_PATH, data_only=True)
    ws = wb["Hoja1"]

    cur = conn.cursor()
    categoria = "POLERA OVERSIZE"
    seq       = 1
    imported  = 0
    skipped   = 0

    TALLAS_HEADER = {"S", "M", "L", "XL", "XXL"}

    for row in ws.iter_rows(min_row=2, max_row=200, values_only=True):
        a, _b, nombre, color, s, m, l, xl, xxl, total, costo, precio, *_ = row

        if not nombre:
            continue

        nombre_clean = str(nombre).strip().replace("\xa0", "")
        if not nombre_clean:
            continue

        # Fila de encabezado de categoría (color == 'COLOR' o texto de talla)
        if isinstance(color, str) and color.strip().upper() in ("COLOR", "S", "M", "L", ""):
            categoria = nombre_clean
            continue
        # Fila de totales al final
        if nombre_clean.lower().startswith("total") or a is None and precio is None:
            continue

        color_clean = str(color).strip() if isinstance(color, str) else (str(int(color)) if isinstance(color, (int,float)) and color else "UNICO")

        precio_val = float(precio) if isinstance(precio, (int, float)) and precio else 0.0
        costo_val  = float(costo)  if isinstance(costo,  (int, float)) and costo  else 0.0

        if precio_val == 0:
            skipped += 1
            continue

        ean = ean13(seq)
        cur.execute("""
            INSERT OR IGNORE INTO productos
            (ean13, categoria, nombre, color, stock_s, stock_m, stock_l, stock_xl, stock_xxl, costo, precio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ean, categoria, nombre_clean, color_clean,
              to_int(s), to_int(m), to_int(l), to_int(xl), to_int(xxl),
              costo_val, precio_val))

        if cur.rowcount:
            imported += 1
            seq += 1

    conn.commit()
    return imported, skipped

if __name__ == "__main__":
    if not os.path.exists(EXCEL_PATH):
        print(f"ERROR: No se encontró {EXCEL_PATH}")
        sys.exit(1)

    fresh = not os.path.exists(DB_PATH)
    conn  = sqlite3.connect(DB_PATH)
    init_db(conn)

    n, s = import_data(conn)
    conn.close()

    print(f"OK Base de datos: {DB_PATH}")
    print(f"OK Importados:    {n} productos")
    print(f"   Omitidos:      {s} filas sin precio")
