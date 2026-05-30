"""
app.py — Backend Flask para DRSK PERU Sistema de Inventario
Iniciar: python app.py
Acceder: http://localhost:5000
"""
import sqlite3, os, json, io, shutil
from datetime import datetime
from functools import wraps
from time import time
from flask import Flask, jsonify, request, render_template, abort, send_file

APP_DIR     = os.path.dirname(__file__)
DB_PATH     = os.path.join(APP_DIR, "inventario.db")
DB_TRAINING = os.path.join(APP_DIR, "inventario_training.db")

_training_mode = False

ESTADOS = [
    "ACTIVO", "BAJO STOCK", "AGOTADO", "EN PEDIDO", "DESCONTINUADO",
    "LIQUIDACION", "EN CONSIGNACION", "RESERVADO", "NUEVO INGRESO",
    "EN REVISION", "DEVOLUCION", "PRÓXIMO INGRESO",
    "DEFECTO", "REVISIÓN PROVEEDOR"
]

ACCIONES_DEFECTO  = ["RECHAZADO", "REHACER", "EN REVISIÓN", "REVISIÓN PROVEEDOR"]
CANALES           = ["WhatsApp", "Ripley", "Juntoz", "Web", "Presencial", "Instagram", "TikTok"]
ESTADOS_DESPACHO  = ["PENDIENTE", "EN RECOLECCIÓN", "LISTO", "ENVIADO", "CANCELADO"]
TALLAS             = ["S", "M", "L", "XL", "XXL"]
MOTIVOS_INCIDENCIA = ["No se encuentra físicamente", "Último en stock con defecto", "Otro motivo"]
TIPOS_PAGO         = ["Efectivo", "Yape", "Plin", "Tarjeta", "Transferencia", "Contra entrega"]
EMPRESAS_ENVIO     = ["Dinsides (Lima)", "Olva Courier (Provincia)", "Ripley", "Falabella", "Recojo en tienda"]
CANALES_VENTA      = ["WhatsApp", "Instagram", "Facebook", "Ripley", "Falabella", "Juntoz"]

app = Flask(__name__)

# ── EAN-13 generator ────────────────────────────────────────────────────
def ean13_gen(seq):
    digits = f"7750000{seq:05d}"
    total  = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    return digits + str((10 - total % 10) % 10)

# ── Rate limiting (in-memory, per-endpoint) ─────────────────────────────
_rl: dict = {}
def rate_limit(calls=60, period=60):
    def dec(f):
        @wraps(f)
        def wrap(*a, **kw):
            key = f.__name__
            now = time()
            hist = [t for t in _rl.get(key, []) if now - t < period]
            if len(hist) >= calls:
                return jsonify({"error": "Demasiadas solicitudes. Espera un momento."}), 429
            hist.append(now)
            _rl[key] = hist
            return f(*a, **kw)
        return wrap
    return dec

# ── Security headers + CORS ─────────────────────────────────────────────
@app.after_request
def security_headers(r):
    r.headers.update({
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options':        'SAMEORIGIN',
        'X-XSS-Protection':       '1; mode=block',
        'Referrer-Policy':        'strict-origin-when-cross-origin',
    })
    origin = request.headers.get('Origin', '')
    allowed = ('http://localhost', 'http://127.0.0.1', 'http://192.168.')
    if any(origin.startswith(h) for h in allowed):
        r.headers['Access-Control-Allow-Origin']  = origin
        r.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return r

@app.route('/<path:p>', methods=['OPTIONS'])
def preflight(p):
    return '', 204

# ── DB Migration ────────────────────────────────────────────────────────
def migrate_db():
    conn = sqlite3.connect(DB_PATH)
    # Safe column additions (idempotent)
    for sql in [
        "ALTER TABLE ventas ADD COLUMN canal TEXT DEFAULT 'DIRECTO'",
        "ALTER TABLE ventas ADD COLUMN grupo_id INTEGER DEFAULT NULL",
        "ALTER TABLE ventas ADD COLUMN descuento_pct REAL DEFAULT 0",
        "ALTER TABLE ventas ADD COLUMN descuento_motivo TEXT DEFAULT ''",
        "ALTER TABLE ventas ADD COLUMN descuento_monto REAL DEFAULT 0",
        "ALTER TABLE skus ADD COLUMN costo REAL NOT NULL DEFAULT 0",
    ]:
        try: conn.execute(sql); conn.commit()
        except Exception: pass
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS gastos (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        mes     TEXT NOT NULL,
        canal   TEXT NOT NULL,
        monto   REAL NOT NULL DEFAULT 0,
        nota    TEXT DEFAULT '',
        fecha   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS despachos (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre_cliente TEXT NOT NULL DEFAULT '',
        canal          TEXT NOT NULL DEFAULT 'WhatsApp',
        referencia     TEXT DEFAULT '',
        notas          TEXT DEFAULT '',
        estado         TEXT NOT NULL DEFAULT 'PENDIENTE',
        fecha          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS despacho_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        despacho_id INTEGER NOT NULL,
        producto_id INTEGER,
        ean13       TEXT NOT NULL,
        nombre      TEXT NOT NULL,
        color       TEXT NOT NULL,
        talla       TEXT NOT NULL DEFAULT '',
        cantidad    INTEGER NOT NULL DEFAULT 1,
        escaneado   INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (despacho_id) REFERENCES despachos(id)
    );
    CREATE TABLE IF NOT EXISTS defectos (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        producto_id INTEGER,
        ean13       TEXT    NOT NULL,
        nombre      TEXT    NOT NULL,
        color       TEXT    NOT NULL,
        talla       TEXT    NOT NULL DEFAULT '',
        descripcion TEXT    NOT NULL,
        accion      TEXT    NOT NULL DEFAULT 'EN REVISIÓN',
        proveedor   TEXT    DEFAULT '',
        repuesto    INTEGER NOT NULL DEFAULT 0,
        fecha       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (producto_id) REFERENCES productos(id)
    );
    CREATE TABLE IF NOT EXISTS conteos (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        nota              TEXT,
        total_items       INTEGER DEFAULT 0,
        total_diferencias INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS pedidos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        categoria       TEXT    NOT NULL DEFAULT '',
        nombre          TEXT    NOT NULL,
        color           TEXT    NOT NULL DEFAULT 'UNICO',
        talla           TEXT    NOT NULL DEFAULT 'S',
        cantidad_pedida INTEGER NOT NULL DEFAULT 1,
        proveedor       TEXT    DEFAULT '',
        fecha_estimada  TEXT    DEFAULT '',
        notas           TEXT    DEFAULT '',
        estado          TEXT    NOT NULL DEFAULT 'PENDIENTE',
        fecha           TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS conteo_items (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        conteo_id      INTEGER NOT NULL,
        producto_id    INTEGER NOT NULL,
        nombre         TEXT    NOT NULL,
        color          TEXT    NOT NULL,
        talla          TEXT    NOT NULL,
        stock_esperado INTEGER NOT NULL DEFAULT 0,
        stock_contado  INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (conteo_id) REFERENCES conteos(id)
    );
    CREATE TABLE IF NOT EXISTS pedidos_venta (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre_cliente  TEXT NOT NULL,
        telefono        TEXT DEFAULT '',
        direccion       TEXT DEFAULT '',
        ciudad          TEXT DEFAULT '',
        canal           TEXT NOT NULL DEFAULT 'WhatsApp',
        empresa_envio   TEXT DEFAULT '',
        tipo_pago       TEXT DEFAULT '',
        indicaciones    TEXT DEFAULT '',
        estado          TEXT NOT NULL DEFAULT 'PENDIENTE',
        fecha           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS pv_items (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        pedido_id   INTEGER NOT NULL,
        producto_id INTEGER,
        ean13       TEXT NOT NULL,
        nombre      TEXT NOT NULL,
        color       TEXT NOT NULL DEFAULT '',
        talla       TEXT NOT NULL DEFAULT '',
        precio      REAL NOT NULL DEFAULT 0,
        cantidad    INTEGER NOT NULL DEFAULT 1,
        recolectado INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (pedido_id) REFERENCES pedidos_venta(id)
    );
    CREATE TABLE IF NOT EXISTS pv_incidencias (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        pedido_id   INTEGER NOT NULL,
        item_id     INTEGER,
        ean13       TEXT DEFAULT '',
        nombre      TEXT DEFAULT '',
        motivo      TEXT NOT NULL,
        notas       TEXT DEFAULT '',
        estado      TEXT NOT NULL DEFAULT 'PENDIENTE',
        fecha       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (pedido_id) REFERENCES pedidos_venta(id)
    );
    CREATE TABLE IF NOT EXISTS ventas_grupos (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre_cliente   TEXT NOT NULL DEFAULT 'DIRECTO',
        telefono         TEXT DEFAULT '',
        canal            TEXT NOT NULL DEFAULT 'DIRECTO',
        total            REAL NOT NULL DEFAULT 0,
        descuento_pct    REAL NOT NULL DEFAULT 0,
        descuento_motivo TEXT DEFAULT '',
        descuento_monto  REAL NOT NULL DEFAULT 0,
        fecha            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS config (
        clave TEXT PRIMARY KEY,
        valor TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS variantes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        producto_id INTEGER NOT NULL,
        talla       TEXT NOT NULL,
        ean13       TEXT UNIQUE NOT NULL,
        FOREIGN KEY (producto_id) REFERENCES productos(id)
    );
    """)
    # Default commission values (INSERT OR IGNORE = don't overwrite user changes)
    for k, v in [("comision_Ripley","12"),("comision_Falabella","12"),
                  ("comision_Juntoz","10"),("comision_Oechsle","10")]:
        try: conn.execute("INSERT OR IGNORE INTO config (clave,valor) VALUES (?,?)",(k,v)); conn.commit()
        except: pass
    _generar_variantes(conn)
    conn.commit()
    conn.close()

# ── TSPL Generator (Gprinter GP-3120TU, 50×25mm, 203 DPI) ──────────────
# Label: 400×200 dots.  All text centered.  No price.
# Layout (top→bottom):  barcode (90% width) | EAN-13 | NOMBRE (max font) | TALLA-COLOR | DRSK (bottom-right)

TALLA_FULL = {"S":"SMALL","M":"MEDIUM","L":"LARGE","XL":"X-LARGE","XXL":"XX-LARGE"}
_TALLA_IDX = {"S":1,"M":2,"L":3,"XL":4,"XXL":5}

def ean13_variante(producto_id, talla):
    """Unique EAN-13 per (producto_id, talla). Prefix 7752 + talla_idx + pid(7d)."""
    idx    = _TALLA_IDX.get(str(talla).upper(), 0)
    digits = f"7752{idx}{producto_id:07d}"          # 12 digits
    total  = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    return digits + str((10 - total % 10) % 10)

def _generar_variantes(conn):
    """No-op: variants are now embedded in skus.codigo_barras (one EAN per SKU row)."""
    pass
# TSPL built-in font char widths in dots (font "2"=12, "3"=16, "4"=24)
_FW = {"1":8, "2":12, "3":16, "4":24}

def _cx(text, font, lw=400):
    """X position to center text on label."""
    return max(0, (lw - _FW.get(font, 16) * len(text)) // 2)

def generate_tspl_bulk(items):
    return "".join(generate_tspl(p, t, c) for p, t, c in items if c > 0)

def generate_tspl(prod, talla="", copies=1):
    # Line 1: use producto_inv (clean inventory name); fallback to nombre
    inv_name   = str(prod.get("producto_inv") or "").strip().upper()
    nombre_raw = str(prod["nombre"]).upper().strip()
    nombre     = inv_name if inv_name else nombre_raw
    color      = str(prod["color"]).upper()
    # Use talla-specific EAN if available (enables direct talla resolution on scan)
    ean        = prod.get("variante_ean") or prod["ean13"]
    talla_full = TALLA_FULL.get(talla.upper(), talla.upper()) if talla else ""
    meta       = f"{talla_full} - {color}" if talla_full else color

    # Largest font that fits 390 dots wide; truncate only if nothing fits
    if   len(nombre) * _FW["4"] <= 390: nf="4"; ny=92; ty=132
    elif len(nombre) * _FW["3"] <= 390: nf="3"; ny=92; ty=120
    else: nombre=nombre[:24]; nf="3"; ny=92; ty=120

    xdrsk = _cx("DRSK", "1")
    xean  = _cx(ean,    "2")
    xname = _cx(nombre, nf)
    xmeta = _cx(meta,   "2")

    return (
        "SIZE 50 mm,25 mm\r\n"
        "GAP 2 mm,0 mm\r\n"
        "DIRECTION 0\r\n"
        "SPEED 4\r\n"
        "DENSITY 8\r\n"
        "CLS\r\n"
        f'TEXT {xdrsk},2,"1",0,1,1,"DRSK"\r\n'           # DRSK centered at top
        f'BARCODE 10,16,"EAN13",44,0,0,3,3,"{ean}"\r\n'  # barcode below DRSK
        f'TEXT {xean},64,"2",0,1,1,"{ean}"\r\n'          # EAN-13 centered
        f'TEXT {xname},{ny},"{nf}",0,1,1,"{nombre}"\r\n' # categoria + nombre
        f'TEXT {xmeta},{ty},"2",0,1,1,"{meta}"\r\n'      # TALLA - COLOR
        f"PRINT {copies},1\r\n"
    )

def generate_tspl_defecto(prod_or_dict, talla, descripcion, accion, copies=1):
    """Defect label: thick BOX border, DRSK top, full name + defect text."""
    cat        = str(prod_or_dict.get("categoria","")).upper().strip()
    nombre_raw = str(prod_or_dict["nombre"]).upper().strip()
    nombre     = f"{cat} {nombre_raw}".strip()[:20] if cat else nombre_raw[:20]
    color      = str(prod_or_dict["color"]).upper()
    ean        = prod_or_dict["ean13"]
    talla_full = TALLA_FULL.get(talla.upper(), talla.upper()) if talla else ""
    meta       = f"{talla_full} - {color}" if talla_full else color
    desc_str   = str(descripcion)[:40].upper()
    acc_str    = str(accion)[:20].upper()

    xean  = _cx(ean,    "2")
    xname = _cx(nombre, "3")
    xmeta = _cx(meta,   "2")
    xdesc = max(5, _cx(f"!! DEFECTO: {desc_str}", "1"))

    return (
        "SIZE 50 mm,25 mm\r\n"
        "GAP 2 mm,0 mm\r\n"
        "DIRECTION 0\r\n"
        "SPEED 4\r\n"
        "DENSITY 8\r\n"
        "CLS\r\n"
        "BOX 2,2,398,198,4\r\n"           # thick black border = "red" on thermal
        f'BARCODE 10,6,"EAN13",45,0,0,3,3,"{ean}"\r\n'
        f'TEXT {xean},55,"2",0,1,1,"{ean}"\r\n'
        f'TEXT {xname},78,"3",0,1,1,"{nombre}"\r\n'
        f'TEXT {xmeta},106,"2",0,1,1,"{meta}"\r\n'
        f'TEXT 5,130,"1",0,1,1,"!! DEFECTO: {desc_str[:36]}"\r\n'
        f'TEXT 5,150,"1",0,1,1,"Estado: {acc_str}"\r\n'
        f'TEXT 344,174,"1",0,1,1,"DRSK"\r\n'
        f"PRINT {copies},1\r\n"
    )

# ── Helpers ────────────────────────────────────────────────
def get_db():
    path = DB_TRAINING if _training_mode else DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

def stock_status(total):
    if total == 0:    return "agotado"
    if total <= 2:    return "bajo"
    return "ok"

def row_to_dict(row):
    d = dict(row)
    d["stock_total"]     = d["stock_s"] + d["stock_m"] + d["stock_l"] + d["stock_xl"] + d["stock_xxl"]
    d["status"]          = stock_status(d["stock_total"])
    d["nombre_completo"] = f"{d['categoria']} {d['nombre']}".strip()
    return d

def sku_to_dict(row):
    """Convert a skus table row to a JS-compatible dict.
    Adds aliases so existing JS code (ean13, categoria, stock_s/m/l/xl/xxl) keeps working."""
    d = dict(row)
    d["stock_total"]     = d["stock"]
    d["status"]          = stock_status(d["stock"])
    inv = (d.get("producto_inv") or "").strip()
    d["nombre_completo"] = inv if inv else d["nombre"]
    d["ean13"]           = d.get("codigo_barras", "")   # JS compat
    d["categoria"]       = d.get("tipo", "")             # JS compat
    d["talla_escaneada"] = d.get("talla", "")            # scan response flag
    # Populate per-talla stock columns so JS _autoAdd works unchanged
    tl = d.get("talla", "").lower()
    for t in ("s", "m", "l", "xl", "xxl", "u"):
        d[f"stock_{t}"] = d["stock"] if t == tl else 0
    return d

# ── Rutas ─────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", estados=ESTADOS, canales=CANALES)

@app.route("/api/training/status")
def training_status():
    return jsonify({"training": _training_mode})

@app.route("/api/training/toggle", methods=["POST"])
def toggle_training():
    global _training_mode
    data   = request.get_json(silent=True) or {}
    enable = data.get("enable", not _training_mode)
    if enable:
        try:
            shutil.copy2(DB_PATH, DB_TRAINING)
        except Exception as e:
            return jsonify({"error": f"No se pudo crear BD de entrenamiento: {e}"}), 500
        _training_mode = True
    else:
        _training_mode = False
    return jsonify({"training": _training_mode})

@app.route("/api/training/reset", methods=["POST"])
def training_reset():
    conn = get_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pedidos)").fetchall()}
    if "es_training" in cols:
        conn.execute("DELETE FROM pedidos WHERE es_training=1")
        conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Listar todos los productos con filtros
@app.route("/api/productos")
def api_productos():
    q      = request.args.get("q", "").strip()
    cat    = request.args.get("cat", "").strip()
    status = request.args.get("status", "").strip()

    conn   = get_db()
    sql    = "SELECT * FROM skus WHERE 1=1"
    params = []

    if q:
        sql += " AND (nombre LIKE ? OR color LIKE ? OR codigo_barras LIKE ? OR tipo LIKE ? OR producto_inv LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like, like]
    if cat:
        sql += " AND tipo = ?"
        params.append(cat)

    sql += " ORDER BY tipo, nombre, color, talla"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    products = [sku_to_dict(r) for r in rows]
    if status:
        products = [p for p in products if p["status"] == status]

    return jsonify(products)

# Buscar por EAN-13
@app.route("/api/scan/<ean13>")
def api_scan(ean13):
    conn = get_db()
    row  = conn.execute("SELECT * FROM skus WHERE substr(codigo_barras,1,12)=substr(?,1,12)", (ean13,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Producto no encontrado"}), 404
    return jsonify(sku_to_dict(row))

# Registrar venta
@app.route("/api/venta", methods=["POST"])
@rate_limit(calls=120, period=60)
def api_venta():
    data             = request.get_json()
    pid              = data.get("producto_id")
    cant             = int(data.get("cantidad", 1))
    canal            = str(data.get("canal", "DIRECTO")).strip()
    descuento_pct    = float(data.get("descuento_pct", 0))
    descuento_motivo = str(data.get("descuento_motivo", "")).strip()

    if not pid:
        return jsonify({"error": "Datos inválidos"}), 400

    conn = get_db()
    cur  = conn.cursor()
    prod = cur.execute("SELECT * FROM skus WHERE id=?", (pid,)).fetchone()
    if not prod:
        conn.close()
        return jsonify({"error": "Producto no encontrado"}), 404

    if prod["stock"] < cant:
        conn.close()
        return jsonify({"error": f"Stock insuficiente. Disponible: {prod['stock']}"}), 400

    precio_pagado   = prod["precio"] * (1 - descuento_pct / 100)
    descuento_monto = prod["precio"] * cant * descuento_pct / 100
    nuevo_stock     = prod["stock"] - cant
    cur.execute("UPDATE skus SET stock=? WHERE id=?", (nuevo_stock, pid))
    cur.execute("""
        INSERT INTO ventas (producto_id, ean13, nombre, color, talla, precio, cantidad, canal,
                            descuento_pct, descuento_motivo, descuento_monto)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (pid, prod["codigo_barras"], prod["nombre"], prod["color"], prod["talla"],
          precio_pagado, cant, canal, descuento_pct, descuento_motivo, descuento_monto))

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "stock_nuevo": nuevo_stock})

# Ver historial de ventas
@app.route("/api/ventas")
def api_ventas():
    desde = request.args.get("desde", "")
    conn  = get_db()
    sql = """SELECT v.*,
               CASE WHEN p.tipo IS NOT NULL
                    THEN p.tipo || ' ' || v.nombre
                    ELSE v.nombre END as nombre_display
             FROM ventas v LEFT JOIN skus p ON v.producto_id = p.id"""
    params = []
    if desde:
        sql += " WHERE v.fecha >= ?"
        params.append(desde)
    sql += " ORDER BY v.fecha DESC LIMIT 200"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return jsonify(rows)

# Ajuste de stock manual
@app.route("/api/ajuste", methods=["POST"])
@rate_limit(calls=60, period=60)
def api_ajuste():
    data   = request.get_json()
    pid    = data.get("producto_id")
    nuevo  = int(data.get("cantidad", 0))
    motivo = data.get("motivo", "Ajuste manual")

    if not pid:
        return jsonify({"error": "Datos inválidos"}), 400

    conn = get_db()
    cur  = conn.cursor()
    prod = cur.execute("SELECT * FROM skus WHERE id=?", (pid,)).fetchone()
    if not prod:
        conn.close()
        return jsonify({"error": "Producto no encontrado"}), 404

    antes = prod["stock"]
    cur.execute("UPDATE skus SET stock=? WHERE id=?", (nuevo, pid))
    cur.execute("INSERT INTO ajustes (producto_id, talla, antes, despues, motivo) VALUES (?,?,?,?,?)",
                (pid, prod["talla"], antes, nuevo, motivo))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "antes": antes, "despues": nuevo})

# Cambiar estado de un producto
@app.route("/api/estado/<int:pid>", methods=["PUT"])
def api_estado(pid):
    data   = request.get_json()
    estado = data.get("estado", "")
    if estado not in ESTADOS:
        return jsonify({"error": "Estado inválido"}), 400
    conn = get_db()
    conn.execute("UPDATE skus SET estado=? WHERE id=?", (estado, pid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Categorías disponibles
@app.route("/api/categorias")
def api_categorias():
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT tipo FROM skus WHERE tipo!='' ORDER BY tipo").fetchall()
    conn.close()
    return jsonify([r["tipo"] for r in rows])

# Stats para el dashboard
@app.route("/api/dashboard")
def api_dashboard():
    conn = get_db()
    cur  = conn.cursor()

    total_skus  = cur.execute("SELECT COUNT(*) FROM skus").fetchone()[0]
    agotados    = cur.execute("SELECT COUNT(*) FROM skus WHERE stock=0").fetchone()[0]
    bajo_stock  = cur.execute("SELECT COUNT(*) FROM skus WHERE stock BETWEEN 1 AND 2").fetchone()[0]

    hoy = datetime.now().strftime("%Y-%m-%d")
    ventas_hoy  = cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(precio*cantidad),0) FROM ventas WHERE fecha LIKE ?", (f"{hoy}%",)
    ).fetchone()
    ventas_mes  = cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(precio*cantidad),0) FROM ventas WHERE fecha LIKE ?",
        (f"{hoy[:7]}%",)
    ).fetchone()

    valor_inv   = cur.execute(
        "SELECT COALESCE(SUM(stock*precio),0) FROM skus"
    ).fetchone()[0]

    conn.close()
    return jsonify({
        "total_skus":   total_skus,
        "agotados":     agotados,
        "bajo_stock":   bajo_stock,
        "ventas_hoy_n": ventas_hoy[0],
        "ventas_hoy_s": round(ventas_hoy[1], 2),
        "ventas_mes_n": ventas_mes[0],
        "ventas_mes_s": round(ventas_mes[1], 2),
        "valor_inv":    round(valor_inv, 2),
    })

# Generar ZPL/TSPL — lookea variante EAN para que el código de barras resuelva talla
@app.route("/api/etiqueta/<ean13>")
def api_etiqueta(ean13):
    conn = get_db()
    row  = conn.execute("SELECT * FROM skus WHERE substr(codigo_barras,1,12)=substr(?,1,12)", (ean13,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Producto no encontrado"}), 404

    p      = sku_to_dict(row)
    talla  = p["talla"]

    inv        = (p.get("producto_inv") or "").strip()
    nombre_zpl = (inv if inv else p["nombre"])[:20].upper()
    color_zpl  = p["color"][:16].upper()
    precio_zpl = f"S/ {int(p['precio'])}"

    zpl = (f"^XA^CI28^PW400^LL240"
           f"^FO20,15^ADN,28,14^FD{nombre_zpl}^FS"
           f"^FO20,55^ADN,20,10^FDCOLOR: {color_zpl}^FS"
           f"^FO20,85^ADN,20,10^FDTALLA: {talla}  {precio_zpl}^FS"
           f"^FO20,115^BY2^BEN,80,Y,N^FD{ean13}^FS"
           f"^FO20,210^ADN,14,7^FDDRSK PERU - Stay Positive^FS^XZ")

    tspl = generate_tspl(p, talla, 1)
    return jsonify({"zpl": zpl, "tspl": tspl, "ean13": ean13, "nombre": p["nombre"]})

# Lista completa de ítems para impresión masiva (con detalle por talla)
@app.route("/api/productos/lista_masivo")
def api_lista_masivo():
    tipo = request.args.get("tipo", "all")
    cat  = request.args.get("cat", "")
    conn = get_db()
    if tipo == "nuevo_ingreso":
        rows = conn.execute("SELECT * FROM skus WHERE estado='NUEVO INGRESO' ORDER BY tipo,nombre,color,talla").fetchall()
    elif tipo == "categoria" and cat:
        rows = conn.execute("SELECT * FROM skus WHERE tipo=? ORDER BY nombre,color,talla", (cat,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM skus WHERE stock>0 ORDER BY tipo,nombre,color,talla").fetchall()
    conn.close()
    out = []
    for row in rows:
        p = dict(row)
        if p["stock"] > 0:
            out.append({"key": str(p["id"]), "ean13": p["codigo_barras"],
                        "nombre": p["nombre"], "color": p["color"],
                        "categoria": p.get("tipo",""), "talla": p["talla"],
                        "cantidad": p["stock"], "precio": p["precio"]})
    return jsonify(out)

# Info de cuántas etiquetas generaría una impresión masiva
@app.route("/api/productos/para_imprimir")
def api_para_imprimir():
    tipo = request.args.get("tipo", "all")
    cat  = request.args.get("cat", "")
    conn = get_db()
    if tipo == "nuevo_ingreso":
        rows = conn.execute("SELECT stock FROM skus WHERE estado='NUEVO INGRESO'").fetchall()
    elif tipo == "categoria" and cat:
        rows = conn.execute("SELECT stock FROM skus WHERE tipo=?", (cat,)).fetchall()
    else:
        rows = conn.execute("SELECT stock FROM skus WHERE stock>0").fetchall()
    conn.close()
    total_etiquetas = sum(r["stock"] for r in rows)
    total_skus      = len(rows)
    return jsonify({"total_etiquetas": total_etiquetas, "total_skus": total_skus})

# Imprimir masivo — acepta items[] explícito o filtro tipo/categoria
@app.route("/api/imprimir/masivo", methods=["POST"])
@rate_limit(calls=10, period=60)
def api_imprimir_masivo():
    data    = request.get_json()
    printer = data.get("printer", "")
    conn    = get_db()

    def _var_ean(pid, talla):
        v = conn.execute("SELECT ean13 FROM variantes WHERE producto_id=? AND talla=?",
                         (pid, talla)).fetchone()
        return v["ean13"] if v else None

    if "items" in data:
        tspl_parts, total = [], 0
        for it in data["items"]:
            ean_ = str(it.get("ean13",""))
            qty  = max(1, int(it.get("cantidad", 1)))
            row  = conn.execute("SELECT * FROM skus WHERE substr(codigo_barras,1,12)=substr(?,1,12)", (ean_,)).fetchone()
            if row:
                pd = sku_to_dict(row)
                tspl_parts.append(generate_tspl(pd, pd["talla"], qty))
                total += qty
        conn.close()
        if not tspl_parts:
            return jsonify({"error": "Sin ítems válidos"}), 400
        tspl = "".join(tspl_parts)
    else:
        tipo = data.get("tipo", "all")
        cat  = data.get("categoria", "")
        if tipo == "nuevo_ingreso":
            rows = conn.execute("SELECT * FROM skus WHERE estado='NUEVO INGRESO' AND stock>0").fetchall()
        elif tipo == "categoria" and cat:
            rows = conn.execute("SELECT * FROM skus WHERE tipo=? AND stock>0", (cat,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM skus WHERE stock>0").fetchall()
        items_list = [(sku_to_dict(r), r["talla"], r["stock"]) for r in rows]
        conn.close()
        if not items_list:
            return jsonify({"error": "No hay productos con stock"}), 400
        total = sum(c for _,_,c in items_list)
        tspl  = generate_tspl_bulk(items_list)

    try:
        import win32print
        if not printer:
            printer = win32print.GetDefaultPrinter()
        hPrinter = win32print.OpenPrinter(printer)
        try:
            win32print.StartDocPrinter(hPrinter, 1, ("DRSK Masivo", None, "RAW"))
            win32print.StartPagePrinter(hPrinter)
            win32print.WritePrinter(hPrinter, tspl.encode("ascii", errors="replace"))
            win32print.EndPagePrinter(hPrinter)
            win32print.EndDocPrinter(hPrinter)
        finally:
            win32print.ClosePrinter(hPrinter)
        return jsonify({"ok": True, "total_etiquetas": total, "printer": printer})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/imprimir/raw", methods=["POST"])
@rate_limit(calls=30, period=60)
def api_imprimir_raw():
    data    = request.get_json()
    tspl    = data.get("tspl", "")
    printer = data.get("printer", "")
    if not tspl:
        return jsonify({"error": "Sin datos TSPL"}), 400
    try:
        import win32print
        if not printer:
            printer = win32print.GetDefaultPrinter()
        hP = win32print.OpenPrinter(printer)
        try:
            win32print.StartDocPrinter(hP, 1, ("DRSK Label", None, "RAW"))
            win32print.StartPagePrinter(hP)
            win32print.WritePrinter(hP, tspl.encode("ascii", errors="replace"))
            win32print.EndPagePrinter(hP)
            win32print.EndDocPrinter(hP)
        finally:
            win32print.ClosePrinter(hP)
        return jsonify({"ok": True, "printer": printer})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Guardar conteo físico
@app.route("/api/conteo", methods=["POST"])
def api_conteo_guardar():
    data  = request.get_json()
    items = data.get("items", [])
    nota  = data.get("nota", "")
    diffs = sum(1 for i in items if i["stock_contado"] != i["stock_esperado"])
    conn  = get_db()
    cur   = conn.cursor()
    cur.execute("INSERT INTO conteos (nota, total_items, total_diferencias) VALUES (?,?,?)",
                (nota, len(items), diffs))
    cid = cur.lastrowid
    for item in items:
        cur.execute("""INSERT INTO conteo_items
            (conteo_id,producto_id,nombre,color,talla,stock_esperado,stock_contado)
            VALUES (?,?,?,?,?,?,?)""",
            (cid, item["producto_id"], item["nombre"], item["color"],
             item["talla"], item["stock_esperado"], item["stock_contado"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "conteo_id": cid, "diferencias": diffs})

# Historial de conteos
@app.route("/api/conteos")
def api_conteos():
    conn = get_db()
    rows = conn.execute("SELECT * FROM conteos ORDER BY fecha DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# Detalle de un conteo
@app.route("/api/conteo/<int:cid>")
def api_conteo_detalle(cid):
    conn  = get_db()
    items = conn.execute("SELECT * FROM conteo_items WHERE conteo_id=?", (cid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in items])

# ── DEFECTOS ───────────────────────────────────────────────────────────
@app.route("/api/defecto", methods=["POST"])
@rate_limit(calls=60, period=60)
def api_defecto_registrar():
    data       = request.get_json()
    pid        = data.get("producto_id")
    ean13_     = str(data.get("ean13","")).strip()
    nombre     = str(data.get("nombre","")).strip()
    color      = str(data.get("color","")).strip()
    categoria  = str(data.get("categoria","")).strip()
    talla      = str(data.get("talla","")).upper()
    desc       = str(data.get("descripcion","")).strip()
    accion     = str(data.get("accion","EN REVISIÓN"))
    proveedor  = str(data.get("proveedor","")).strip()
    imprimir   = data.get("imprimir", False)
    printer    = str(data.get("printer",""))
    cant       = max(1, int(data.get("cantidad", 1)))

    if not ean13_ or not desc or accion not in ACCIONES_DEFECTO:
        return jsonify({"error": "Datos inválidos"}), 400

    conn = get_db()
    cur  = conn.cursor()

    # Deduct from stock only if RECHAZADO
    if accion == "RECHAZADO" and pid:
        prod = cur.execute("SELECT * FROM skus WHERE id=?", (pid,)).fetchone()
        if prod:
            actual = prod["stock"]
            nuevo  = max(0, actual - cant)
            cur.execute("UPDATE skus SET stock=? WHERE id=?", (nuevo, pid))
            cur.execute("INSERT INTO ajustes (producto_id,talla,antes,despues,motivo) VALUES (?,?,?,?,?)",
                        (pid, prod["talla"], actual, nuevo, f"Defecto: {desc[:50]}"))

    cur.execute("""INSERT INTO defectos (producto_id,ean13,nombre,color,talla,descripcion,accion,proveedor)
        VALUES (?,?,?,?,?,?,?,?)""",
        (pid, ean13_, nombre, color, talla, desc, accion, proveedor))
    defecto_id = cur.lastrowid
    conn.commit()

    # Generate and optionally print defect label
    prod_d = {"nombre": nombre, "color": color, "ean13": ean13_, "categoria": categoria}
    tspl   = generate_tspl_defecto(prod_d, talla, desc, accion, cant)

    if imprimir and printer:
        try:
            import win32print
            hP = win32print.OpenPrinter(printer)
            try:
                win32print.StartDocPrinter(hP, 1, ("DRSK Defecto", None, "RAW"))
                win32print.StartPagePrinter(hP)
                win32print.WritePrinter(hP, tspl.encode("ascii", errors="replace"))
                win32print.EndPagePrinter(hP)
                win32print.EndDocPrinter(hP)
            finally:
                win32print.ClosePrinter(hP)
        except Exception:
            pass

    conn.close()
    return jsonify({"ok": True, "id": defecto_id, "tspl": tspl})

@app.route("/api/defectos")
def api_defectos():
    accion    = request.args.get("accion", "")
    proveedor = request.args.get("proveedor", "")
    conn      = get_db()
    sql, params = "SELECT * FROM defectos WHERE 1=1", []
    if accion:
        sql += " AND accion=?"; params.append(accion)
    if proveedor:
        sql += " AND proveedor LIKE ?"; params.append(f"%{proveedor}%")
    sql += " ORDER BY fecha DESC LIMIT 500"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/defecto/<int:did>/accion", methods=["PUT"])
def api_defecto_accion(did):
    data   = request.get_json()
    accion = data.get("accion","")
    if accion not in ACCIONES_DEFECTO + ["REPUESTO"]:
        return jsonify({"error": "Acción inválida"}), 400

    conn = get_db()
    cur  = conn.cursor()
    d    = cur.execute("SELECT * FROM defectos WHERE id=?", (did,)).fetchone()
    if not d:
        conn.close()
        return jsonify({"error": "Defecto no encontrado"}), 404

    d = dict(d)
    # If reponing to stock and was previously RECHAZADO → add 1 back
    if accion == "REPUESTO" and d["accion"] == "RECHAZADO" and d["producto_id"] and not d["repuesto"]:
        prod = cur.execute("SELECT * FROM skus WHERE id=?", (d["producto_id"],)).fetchone()
        if prod:
            actual = prod["stock"]
            cur.execute("UPDATE skus SET stock=? WHERE id=?", (actual + 1, d["producto_id"]))
            cur.execute("INSERT INTO ajustes (producto_id,talla,antes,despues,motivo) VALUES (?,?,?,?,?)",
                        (d["producto_id"], prod["talla"], actual, actual+1, f"Reposición defecto #{did}"))

    cur.execute("UPDATE defectos SET accion=?, repuesto=? WHERE id=?",
                (accion if accion != "REPUESTO" else "REVISIÓN PROVEEDOR",
                 1 if accion == "REPUESTO" else d["repuesto"], did))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/defectos/reporte_proveedor")
def api_reporte_proveedor():
    proveedor = request.args.get("proveedor","")
    conn = get_db()
    sql    = "SELECT * FROM defectos WHERE accion='REVISIÓN PROVEEDOR' AND repuesto=0"
    params = []
    if proveedor:
        sql += " AND proveedor LIKE ?"; params.append(f"%{proveedor}%")
    sql += " ORDER BY proveedor, fecha"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()

    if not rows:
        return jsonify({"reporte": "", "total": 0, "proveedores": []})

    # Group by proveedor
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        groups[r["proveedor"] or "Proveedor sin nombre"].append(r)

    proveedores = list(groups.keys())
    bloques = []
    for prov, items in groups.items():
        lineas = [f"  - {i['nombre']} / {i['talla'] or 'UNICO'} / {i['color']}: {i['descripcion']} ({i['fecha'][:10]})"
                  for i in items]
        bloques.append(
            f"Estimado {prov},\n\n"
            f"Se encontraron los siguientes defectos en el último pedido:\n\n"
            + "\n".join(lineas) +
            f"\n\nTotal: {len(items)} prenda(s) con defecto.\n\n"
            f"Por favor confirmar reposición o solución.\n— DRSK PERU"
        )

    return jsonify({"reporte": "\n\n---\n\n".join(bloques), "total": len(rows), "proveedores": proveedores})

@app.route("/api/defecto/etiqueta/<int:did>")
def api_defecto_etiqueta(did):
    conn = get_db()
    d    = conn.execute("SELECT * FROM defectos WHERE id=?", (did,)).fetchone()
    conn.close()
    if not d:
        return jsonify({"error": "No encontrado"}), 404
    d = dict(d)
    conn2 = get_db()
    sku   = conn2.execute("SELECT tipo FROM skus WHERE id=?", (d["producto_id"],)).fetchone()
    conn2.close()
    cat   = dict(sku)["tipo"] if sku else ""
    prod_d = {"nombre": d["nombre"], "color": d["color"], "ean13": d["ean13"], "categoria": cat}
    tspl   = generate_tspl_defecto(prod_d, d["talla"], d["descripcion"], d["accion"])
    return jsonify({"tspl": tspl})

# ── PEDIDOS (PRÓXIMO INGRESO) ──────────────────────────────────────────
@app.route("/api/pedidos")
def api_pedidos():
    estado   = request.args.get("estado", "PENDIENTE")
    training = request.args.get("training", "0") == "1"
    tf       = "es_training=1" if training else "(es_training=0 OR es_training IS NULL)"
    conn     = get_db()
    if estado == "all":
        rows = conn.execute(f"SELECT * FROM pedidos WHERE {tf} ORDER BY fecha DESC").fetchall()
    else:
        rows = conn.execute(f"SELECT * FROM pedidos WHERE estado=? AND {tf} ORDER BY fecha DESC", (estado,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/pedido", methods=["POST"])
@rate_limit(calls=30, period=60)
def api_pedido_crear():
    data   = request.get_json()
    nombre = str(data.get("nombre","")).strip()
    talla  = str(data.get("talla","S")).upper()
    if not nombre or talla not in TALLAS:
        return jsonify({"error": "Datos inválidos"}), 400
    is_training = 1 if data.get("training") else 0
    conn = get_db()
    cur  = conn.cursor()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pedidos)").fetchall()}
    if "es_training" not in cols:
        conn.execute("ALTER TABLE pedidos ADD COLUMN es_training INTEGER DEFAULT 0")
        conn.commit()
    cur.execute("""INSERT INTO pedidos (categoria,nombre,color,talla,cantidad_pedida,proveedor,fecha_estimada,notas,es_training)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(data.get("categoria","")), nombre,
         str(data.get("color","UNICO")), talla,
         max(1, int(data.get("cantidad_pedida",1))),
         str(data.get("proveedor","")),
         str(data.get("fecha_estimada","")),
         str(data.get("notas","")),
         is_training))
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": pid})

@app.route("/api/pedido/<int:pid>", methods=["DELETE"])
def api_pedido_cancelar(pid):
    conn = get_db()
    conn.execute("UPDATE pedidos SET estado='CANCELADO' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/pedido/<int:pid>/confirmar", methods=["POST"])
@rate_limit(calls=20, period=60)
def api_pedido_confirmar(pid):
    data     = request.get_json()
    llegados = max(0, int(data.get("llegados", 0)))
    errores  = max(0, int(data.get("errores", 0)))
    buenos   = max(0, llegados - errores)
    imprimir = data.get("imprimir", False)
    printer  = data.get("printer", "")

    conn   = get_db()
    pedido = conn.execute("SELECT * FROM pedidos WHERE id=?", (pid,)).fetchone()
    if not pedido:
        conn.close()
        return jsonify({"error": "Pedido no encontrado"}), 404

    p     = dict(pedido)
    talla = p["talla"]
    col   = f"stock_{talla.lower()}"

    # Find existing SKU by nombre+color+talla
    prod = conn.execute("SELECT * FROM skus WHERE nombre=? AND color=? AND talla=?",
                        (p["nombre"], p["color"], talla)).fetchone()
    cur  = conn.cursor()

    if prod:
        prod_id  = prod["id"]
        ean13_   = prod["codigo_barras"]
        anterior = prod["stock"]
        cur.execute("UPDATE skus SET stock=?, estado='ACTIVO' WHERE id=?",
                    (anterior + buenos, prod_id))
        cur.execute("INSERT INTO ajustes (producto_id,talla,antes,despues,motivo) VALUES (?,?,?,?,?)",
                    (prod_id, talla, anterior, anterior + buenos, f"Llegada pedido #{pid}"))
    else:
        # Generate new EAN and insert new SKU
        seq    = (conn.execute("SELECT COALESCE(MAX(id),0) FROM skus").fetchone()[0] or 0) + 1
        ean13_ = ean13_gen(seq)
        while conn.execute("SELECT id FROM skus WHERE codigo_barras=?", (ean13_,)).fetchone():
            seq += 1; ean13_ = ean13_gen(seq)
        cur.execute("""INSERT INTO skus (nombre,tipo,precio,producto_inv,color,talla,stock,estado,codigo_barras)
            VALUES (?,?,0,?,?,?,?,'ACTIVO',?)""",
            (p["nombre"], p["categoria"], p["nombre"], p["color"], talla, buenos, ean13_))
        prod_id = cur.lastrowid

    cur.execute("UPDATE pedidos SET estado='LLEGADO' WHERE id=?", (pid,))
    conn.commit()

    prod_final = conn.execute("SELECT * FROM skus WHERE id=?", (prod_id,)).fetchone()
    conn.close()

    tspl = generate_tspl(sku_to_dict(prod_final), talla, buenos) if buenos > 0 else ""

    if imprimir and buenos > 0 and printer:
        try:
            import win32print
            hP = win32print.OpenPrinter(printer)
            try:
                win32print.StartDocPrinter(hP, 1, ("DRSK Pedido", None, "RAW"))
                win32print.StartPagePrinter(hP)
                win32print.WritePrinter(hP, tspl.encode("ascii", errors="replace"))
                win32print.EndPagePrinter(hP)
                win32print.EndDocPrinter(hP)
            finally:
                win32print.ClosePrinter(hP)
        except Exception:
            pass

    return jsonify({"ok": True, "producto_id": prod_id, "ean13": ean13_,
                    "buenos": buenos, "errores": errores, "tspl": tspl})

# Listar impresoras Windows
@app.route("/api/impresoras")
def api_impresoras():
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        names = [p[2] for p in win32print.EnumPrinters(flags)]
        return jsonify(names)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Imprimir TSPL directo a impresora USB
@app.route("/api/imprimir", methods=["POST"])
def api_imprimir():
    data    = request.get_json()
    ean13_  = data.get("ean13", "")
    talla   = data.get("talla", "")
    copies  = max(1, int(data.get("copies", 1)))
    printer = data.get("printer", "")

    conn = get_db()
    prod = conn.execute("SELECT * FROM skus WHERE substr(codigo_barras,1,12)=substr(?,1,12)", (ean13_,)).fetchone()
    conn.close()
    if not prod:
        return jsonify({"error": "Producto no encontrado"}), 404

    pd   = sku_to_dict(prod)
    tspl = generate_tspl(pd, pd["talla"], copies)

    try:
        import win32print
        if not printer:
            printer = win32print.GetDefaultPrinter()
        hPrinter = win32print.OpenPrinter(printer)
        try:
            win32print.StartDocPrinter(hPrinter, 1, ("DRSK Label", None, "RAW"))
            win32print.StartPagePrinter(hPrinter)
            win32print.WritePrinter(hPrinter, tspl.encode("ascii", errors="replace"))
            win32print.EndPagePrinter(hPrinter)
            win32print.EndDocPrinter(hPrinter)
        finally:
            win32print.ClosePrinter(hPrinter)
        return jsonify({"ok": True, "printer": printer, "copies": copies})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── DASHBOARD VENTAS ────────────────────────────────────────────────────
@app.route("/api/ventas/por_mes")
def api_ventas_por_mes():
    meses = int(request.args.get("meses", 12))
    conn  = get_db()
    rows  = conn.execute("""
        SELECT strftime('%Y-%m', fecha) as mes,
               COALESCE(NULLIF(canal,''),'DIRECTO') as canal,
               COUNT(*) as n_ventas,
               ROUND(SUM(precio * cantidad), 2) as total
        FROM ventas
        GROUP BY mes, canal
        ORDER BY mes DESC
        LIMIT ?
    """, (meses * 10,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/gastos")
def api_gastos():
    mes  = request.args.get("mes", "")
    conn = get_db()
    if mes:
        rows = conn.execute("SELECT * FROM gastos WHERE mes=? ORDER BY canal", (mes,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM gastos ORDER BY mes DESC, canal LIMIT 200").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/gasto", methods=["POST"])
@rate_limit(calls=30, period=60)
def api_gasto_crear():
    data  = request.get_json()
    mes   = str(data.get("mes","")).strip()
    canal = str(data.get("canal","")).strip()
    monto = float(data.get("monto", 0) or 0)
    nota  = str(data.get("nota","")).strip()
    if not mes or not canal or monto <= 0:
        return jsonify({"error": "Datos inválidos"}), 400
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("INSERT INTO gastos (mes,canal,monto,nota) VALUES (?,?,?,?)", (mes,canal,monto,nota))
    conn.commit()
    gid = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": gid})

@app.route("/api/gasto/<int:gid>", methods=["DELETE"])
def api_gasto_eliminar(gid):
    conn = get_db()
    conn.execute("DELETE FROM gastos WHERE id=?", (gid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── DESPACHOS / RECOLECCIÓN ──────────────────────────────────────────────
@app.route("/api/despachos")
def api_despachos():
    estado = request.args.get("estado", "PENDIENTE")
    conn   = get_db()
    if estado == "all":
        rows = conn.execute("SELECT * FROM despachos ORDER BY fecha DESC LIMIT 100").fetchall()
    else:
        rows = conn.execute("SELECT * FROM despachos WHERE estado=? ORDER BY fecha DESC", (estado,)).fetchall()
    # Attach item counts
    out = []
    for r in rows:
        d = dict(r)
        counts = conn.execute(
            "SELECT SUM(cantidad) as tot, SUM(escaneado) as esc FROM despacho_items WHERE despacho_id=?",
            (d["id"],)).fetchone()
        d["total_items"]    = counts["tot"] or 0
        d["total_escaneado"] = counts["esc"] or 0
        out.append(d)
    conn.close()
    return jsonify(out)

@app.route("/api/despacho/<int:did>")
def api_despacho_detalle(did):
    conn  = get_db()
    d     = conn.execute("SELECT * FROM despachos WHERE id=?", (did,)).fetchone()
    items = conn.execute(
        "SELECT * FROM despacho_items WHERE despacho_id=? ORDER BY nombre, talla",
        (did,)).fetchall()
    conn.close()
    if not d:
        return jsonify({"error": "No encontrado"}), 404
    return jsonify({"despacho": dict(d), "items": [dict(i) for i in items]})

@app.route("/api/despacho", methods=["POST"])
@rate_limit(calls=30, period=60)
def api_despacho_crear():
    data   = request.get_json()
    nombre = str(data.get("nombre_cliente","")).strip()
    canal  = str(data.get("canal","WhatsApp")).strip()
    ref    = str(data.get("referencia","")).strip()
    notas  = str(data.get("notas","")).strip()
    items  = data.get("items", [])
    if not nombre:
        return jsonify({"error": "Ingresa el nombre del cliente"}), 400
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("INSERT INTO despachos (nombre_cliente,canal,referencia,notas) VALUES (?,?,?,?)",
                (nombre, canal, ref, notas))
    did = cur.lastrowid
    for it in items:
        ean  = str(it.get("ean13","")).strip()
        prod = conn.execute("SELECT * FROM skus WHERE codigo_barras=?", (ean,)).fetchone()
        if not prod:
            continue
        p    = sku_to_dict(prod)
        cant = max(1, int(it.get("cantidad",1)))
        cur.execute("""INSERT INTO despacho_items (despacho_id,producto_id,ean13,nombre,color,talla,cantidad)
                       VALUES (?,?,?,?,?,?,?)""", (did, p["id"], ean, p["nombre_completo"], p["color"], p["talla"], cant))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": did})

@app.route("/api/despacho/<int:did>/item", methods=["POST"])
def api_despacho_add_item(did):
    data  = request.get_json()
    ean   = str(data.get("ean13","")).strip()
    talla = str(data.get("talla","")).upper()
    cant  = max(1, int(data.get("cantidad",1)))
    conn  = get_db()
    d     = conn.execute("SELECT estado FROM despachos WHERE id=?", (did,)).fetchone()
    if not d or dict(d)["estado"] not in ("PENDIENTE","EN RECOLECCIÓN"):
        conn.close()
        return jsonify({"error": "Despacho no editable"}), 400
    prod = conn.execute("SELECT * FROM skus WHERE codigo_barras=?", (ean,)).fetchone()
    if not prod:
        conn.close()
        return jsonify({"error": "Producto no encontrado"}), 404
    p   = sku_to_dict(prod)
    cur = conn.cursor()
    cur.execute("""INSERT INTO despacho_items (despacho_id,producto_id,ean13,nombre,color,talla,cantidad)
                   VALUES (?,?,?,?,?,?,?)""", (did, p["id"], ean, p["nombre_completo"], p["color"], p["talla"], cant))
    conn.commit()
    iid = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": iid})

@app.route("/api/despacho/<int:did>/item/<int:iid>", methods=["DELETE"])
def api_despacho_del_item(did, iid):
    conn = get_db()
    conn.execute("DELETE FROM despacho_items WHERE id=? AND despacho_id=?", (iid, did))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/despacho/<int:did>/scan", methods=["POST"])
@rate_limit(calls=200, period=60)
def api_despacho_scan(did):
    data  = request.get_json()
    ean   = str(data.get("ean13","")).strip()
    talla = str(data.get("talla","")).upper()
    conn  = get_db()
    d     = conn.execute("SELECT * FROM despachos WHERE id=?", (did,)).fetchone()
    if not d:
        conn.close()
        return jsonify({"error": "Despacho no encontrado"}), 404
    if dict(d)["estado"] == "PENDIENTE":
        conn.execute("UPDATE despachos SET estado='EN RECOLECCIÓN' WHERE id=?", (did,))
        conn.commit()
    # Find unfinished matching item
    items = conn.execute(
        "SELECT * FROM despacho_items WHERE despacho_id=? AND ean13=? AND escaneado < cantidad",
        (did, ean)).fetchall()
    if not items:
        scanned_all = conn.execute(
            "SELECT COUNT(*) FROM despacho_items WHERE despacho_id=? AND ean13=?", (did, ean)).fetchone()[0]
        conn.close()
        msg = "Ya escaneaste todas las unidades de este producto" if scanned_all else "Producto no está en este despacho"
        return jsonify({"ok": False, "msg": msg})
    # Prefer talla match
    item = next((dict(i) for i in items if dict(i)["talla"].upper() == talla), dict(items[0]))
    new_esc = item["escaneado"] + 1
    conn.execute("UPDATE despacho_items SET escaneado=? WHERE id=?", (new_esc, item["id"]))
    conn.commit()
    tots = conn.execute(
        "SELECT SUM(cantidad) as t, SUM(escaneado) as e FROM despacho_items WHERE despacho_id=?",
        (did,)).fetchone()
    total_items, total_esc = tots["t"] or 0, tots["e"] or 0
    all_done = total_esc >= total_items
    if all_done:
        conn.execute("UPDATE despachos SET estado='LISTO' WHERE id=?", (did,))
        conn.commit()
    conn.close()
    return jsonify({"ok": True, "item_id": item["id"], "nombre": item["nombre"],
                    "talla": item["talla"], "escaneado": new_esc, "cantidad": item["cantidad"],
                    "total_items": total_items, "total_escaneado": total_esc, "completo": all_done})

@app.route("/api/despacho/<int:did>/estado", methods=["PUT"])
def api_despacho_estado(did):
    data   = request.get_json()
    estado = str(data.get("estado",""))
    if estado not in ESTADOS_DESPACHO:
        return jsonify({"error": "Estado inválido"}), 400
    conn = get_db()
    conn.execute("UPDATE despachos SET estado=? WHERE id=?", (estado, did))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/despacho/<int:did>", methods=["DELETE"])
def api_despacho_cancelar(did):
    conn = get_db()
    conn.execute("UPDATE despachos SET estado='CANCELADO' WHERE id=?", (did,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ── PEDIDOS VENTA — CRUD ────────────────────────────────────────────────

@app.route("/api/pv")
def api_pv_list():
    estado = request.args.get("estado", "")
    fecha  = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    conn   = get_db()
    sql    = "SELECT * FROM pedidos_venta WHERE date(fecha)=?"
    params = [fecha]
    if estado:
        sql += " AND estado=?"
        params.append(estado)
    sql += " ORDER BY fecha ASC"
    rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        it = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(cantidad),0) tot FROM pv_items WHERE pedido_id=?", (r["id"],)).fetchone()
        inc = conn.execute("SELECT COUNT(*) n FROM pv_incidencias WHERE pedido_id=? AND estado='PENDIENTE'", (r["id"],)).fetchone()
        d["total_items"] = it["tot"] or 0
        d["incidencias"]  = inc["n"] or 0
        result.append(d)
    conn.close()
    return jsonify(result)

@app.route("/api/pv", methods=["POST"])
def api_pv_crear():
    data = request.json or {}
    if not data.get("nombre_cliente","").strip():
        return jsonify({"error": "Nombre del cliente requerido"}), 400
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""INSERT INTO pedidos_venta
        (nombre_cliente,telefono,direccion,ciudad,canal,empresa_envio,tipo_pago,indicaciones)
        VALUES (?,?,?,?,?,?,?,?)""",
        (data["nombre_cliente"].strip(), data.get("telefono","").strip(),
         data.get("direccion","").strip(), data.get("ciudad","").strip(),
         data.get("canal","WhatsApp"), data.get("empresa_envio",""),
         data.get("tipo_pago",""), data.get("indicaciones","").strip()))
    pid = cur.lastrowid
    for item in data.get("items", []):
        ean  = item.get("ean13","").strip()
        prod = conn.execute("SELECT * FROM skus WHERE codigo_barras=?", (ean,)).fetchone() if ean else None
        p    = sku_to_dict(prod) if prod else None
        nombre_b = p["nombre_completo"] if p else item.get("nombre", ean)
        cur.execute("""INSERT INTO pv_items (pedido_id,producto_id,ean13,nombre,color,talla,precio,cantidad)
            VALUES (?,?,?,?,?,?,?,?)""",
            (pid, p["id"] if p else None, ean, nombre_b,
             p["color"] if p else item.get("color",""),
             p["talla"] if p else item.get("talla",""),
             p["precio"] if p else item.get("precio",0),
             max(1, int(item.get("cantidad",1)))))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "id": pid})

@app.route("/api/pv/<int:pid>")
def api_pv_detail(pid):
    conn = get_db()
    p = conn.execute("SELECT * FROM pedidos_venta WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close(); return jsonify({"error": "No encontrado"}), 404
    d = dict(p)
    d["items"]      = [dict(i) for i in conn.execute("SELECT * FROM pv_items WHERE pedido_id=? ORDER BY id", (pid,)).fetchall()]
    d["incidencias"] = [dict(i) for i in conn.execute("SELECT * FROM pv_incidencias WHERE pedido_id=? ORDER BY fecha DESC", (pid,)).fetchall()]
    conn.close()
    return jsonify(d)

@app.route("/api/pv/<int:pid>", methods=["PUT"])
def api_pv_update(pid):
    data   = request.json or {}
    campos = ["nombre_cliente","telefono","direccion","ciudad","canal","empresa_envio","tipo_pago","indicaciones","estado"]
    sets   = [f"{k}=?" for k in campos if k in data]
    vals   = [data[k] for k in campos if k in data]
    if sets:
        conn = get_db()
        conn.execute(f"UPDATE pedidos_venta SET {','.join(sets)} WHERE id=?", vals + [pid])
        conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/pv/<int:pid>", methods=["DELETE"])
def api_pv_cancelar(pid):
    conn = get_db()
    conn.execute("UPDATE pedidos_venta SET estado='CANCELADO' WHERE id=?", (pid,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/pv/<int:pid>/item", methods=["POST"])
def api_pv_add_item(pid):
    data = request.json or {}
    ean  = data.get("ean13","").strip()
    conn = get_db()
    prod = conn.execute("SELECT * FROM skus WHERE codigo_barras=?", (ean,)).fetchone() if ean else None
    p    = sku_to_dict(prod) if prod else None
    nombre_b = p["nombre_completo"] if p else data.get("nombre", ean)
    conn.execute("""INSERT INTO pv_items (pedido_id,producto_id,ean13,nombre,color,talla,precio,cantidad)
        VALUES (?,?,?,?,?,?,?,?)""",
        (pid, p["id"] if p else None, ean, nombre_b,
         p["color"] if p else data.get("color",""),
         p["talla"] if p else data.get("talla",""),
         p["precio"] if p else data.get("precio",0),
         max(1, int(data.get("cantidad",1)))))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/pv/<int:pid>/item/<int:iid>", methods=["DELETE"])
def api_pv_del_item(pid, iid):
    conn = get_db()
    conn.execute("DELETE FROM pv_items WHERE id=? AND pedido_id=?", (iid, pid))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# ── FASE 1: RECOLECCIÓN ──────────────────────────────────────────────────

@app.route("/api/pv/recoleccion")
def api_pv_recoleccion():
    fecha = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    conn  = get_db()
    rows  = conn.execute("""
        SELECT i.*, p.nombre_cliente, p.canal, p.empresa_envio
        FROM pv_items i JOIN pedidos_venta p ON i.pedido_id=p.id
        WHERE date(p.fecha)=? AND p.estado IN ('PENDIENTE','EN RECOLECCIÓN')
        AND i.recolectado < i.cantidad
        ORDER BY p.fecha ASC, i.id ASC
    """, (fecha,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/pv/recoleccion/scan", methods=["POST"])
def api_pv_recoleccion_scan():
    ean   = (request.json or {}).get("ean13","").strip()
    fecha = datetime.now().strftime("%Y-%m-%d")
    conn  = get_db()
    item  = conn.execute("""
        SELECT i.*, p.nombre_cliente, p.canal
        FROM pv_items i JOIN pedidos_venta p ON i.pedido_id=p.id
        WHERE i.ean13=? AND i.recolectado < i.cantidad
          AND p.estado IN ('PENDIENTE','EN RECOLECCIÓN') AND date(p.fecha)=?
        ORDER BY p.fecha ASC, i.id ASC LIMIT 1
    """, (ean, fecha)).fetchone()
    if not item:
        conn.close()
        return jsonify({"error": "No encontrado en pedidos pendientes del día"}), 404
    conn.execute("UPDATE pedidos_venta SET estado='EN RECOLECCIÓN' WHERE id=? AND estado='PENDIENTE'", (item["pedido_id"],))
    conn.execute("UPDATE pv_items SET recolectado=recolectado+1 WHERE id=?", (item["id"],))
    conn.commit()
    pendientes = conn.execute("SELECT COUNT(*) n FROM pv_items WHERE pedido_id=? AND recolectado<cantidad", (item["pedido_id"],)).fetchone()
    conn.close()
    return jsonify({
        "ok": True, "item_id": item["id"], "pedido_id": item["pedido_id"],
        "nombre": item["nombre"], "color": item["color"], "talla": item["talla"],
        "cliente": item["nombre_cliente"], "canal": item["canal"],
        "recolectado": item["recolectado"]+1, "cantidad": item["cantidad"],
        "pedido_completo": pendientes["n"] == 0
    })

@app.route("/api/pv/item/<int:iid>/incidencia", methods=["POST"])
def api_pv_item_incidencia(iid):
    data  = request.json or {}
    conn  = get_db()
    item  = conn.execute("SELECT * FROM pv_items WHERE id=?", (iid,)).fetchone()
    if not item:
        conn.close(); return jsonify({"error": "Item no encontrado"}), 404
    conn.execute("""INSERT INTO pv_incidencias (pedido_id,item_id,ean13,nombre,motivo,notas)
        VALUES (?,?,?,?,?,?)""",
        (item["pedido_id"], iid, item["ean13"], item["nombre"],
         data.get("motivo",""), data.get("notas","")))
    conn.execute("UPDATE pv_items SET recolectado=cantidad WHERE id=?", (iid,))
    conn.execute("""UPDATE pedidos_venta SET estado='INCIDENCIA'
        WHERE id=? AND estado NOT IN ('INCIDENCIA','CANCELADO')""", (item["pedido_id"],))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/pv/recoleccion/completar", methods=["POST"])
def api_pv_recoleccion_completar():
    fecha = datetime.now().strftime("%Y-%m-%d")
    conn  = get_db()
    pend  = conn.execute("""
        SELECT COUNT(*) n FROM pv_items i JOIN pedidos_venta p ON i.pedido_id=p.id
        WHERE date(p.fecha)=? AND p.estado='EN RECOLECCIÓN' AND i.recolectado<i.cantidad
    """, (fecha,)).fetchone()
    if pend["n"] > 0:
        conn.close()
        return jsonify({"error": f"Faltan {pend['n']} productos por recolectar", "pendientes": pend["n"]}), 400
    conn.execute("UPDATE pedidos_venta SET estado='RECOLECTADO' WHERE estado='EN RECOLECCIÓN' AND date(fecha)=?", (fecha,))
    conn.commit()
    resumen = conn.execute("""
        SELECT empresa_envio, COUNT(*) n_pedidos,
               SUM(CASE WHEN estado='INCIDENCIA' THEN 1 ELSE 0 END) incidencias
        FROM pedidos_venta
        WHERE date(fecha)=? AND estado IN ('RECOLECTADO','INCIDENCIA')
        GROUP BY empresa_envio ORDER BY empresa_envio
    """, (fecha,)).fetchall()
    conn.close()
    return jsonify({"ok": True, "resumen": [dict(r) for r in resumen]})

# ── FASE 3: ARMADO DE BOLSAS ─────────────────────────────────────────────

@app.route("/api/pv/armado")
def api_pv_armado_list():
    empresa = request.args.get("empresa","")
    fecha   = request.args.get("fecha", datetime.now().strftime("%Y-%m-%d"))
    conn    = get_db()
    rows    = conn.execute("""
        SELECT * FROM pedidos_venta
        WHERE date(fecha)=? AND empresa_envio=? AND estado IN ('RECOLECTADO','EN ARMADO')
        ORDER BY id ASC
    """, (fecha, empresa)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["items"] = [dict(i) for i in conn.execute("SELECT * FROM pv_items WHERE pedido_id=? ORDER BY id", (r["id"],)).fetchall()]
        result.append(d)
    conn.close()
    return jsonify(result)

@app.route("/api/pv/<int:pid>/scan_armado", methods=["POST"])
def api_pv_scan_armado(pid):
    ean  = (request.json or {}).get("ean13","").strip()
    conn = get_db()
    item = conn.execute("SELECT * FROM pv_items WHERE pedido_id=? AND ean13=? LIMIT 1", (pid, ean)).fetchone()
    if not item:
        other = conn.execute("""
            SELECT p.nombre_cliente, p.id FROM pv_items i
            JOIN pedidos_venta p ON i.pedido_id=p.id
            WHERE i.ean13=? AND date(p.fecha)=date('now') LIMIT 1
        """, (ean,)).fetchone()
        conn.close()
        return jsonify({
            "correcto": False,
            "error": (f"Pertenece al pedido #{other['id']} ({other['nombre_cliente']})" if other
                      else "No encontrado en pedidos del día")
        })
    conn.execute("UPDATE pedidos_venta SET estado='EN ARMADO' WHERE id=? AND estado='RECOLECTADO'", (pid,))
    conn.commit(); conn.close()
    return jsonify({
        "correcto": True, "item_id": item["id"],
        "nombre": item["nombre"], "color": item["color"],
        "talla": item["talla"], "cantidad": item["recolectado"]
    })

@app.route("/api/pv/<int:pid>/sellar", methods=["POST"])
def api_pv_sellar(pid):
    conn   = get_db()
    pedido = conn.execute("SELECT * FROM pedidos_venta WHERE id=?", (pid,)).fetchone()
    if not pedido:
        conn.close(); return jsonify({"error": "Pedido no encontrado"}), 404
    items  = conn.execute("SELECT * FROM pv_items WHERE pedido_id=?", (pid,)).fetchall()
    for it in items:
        qty = it["recolectado"]
        if qty <= 0 or not it["producto_id"]: continue
        conn.execute("UPDATE skus SET stock=MAX(0,stock-?) WHERE id=?", (qty, it["producto_id"]))
        conn.execute("""INSERT INTO ventas (producto_id,ean13,nombre,color,talla,precio,cantidad,canal)
            VALUES (?,?,?,?,?,?,?,?)""",
            (it["producto_id"], it["ean13"], it["nombre"], it["color"],
             it["talla"], it["precio"], qty, pedido["canal"]))
    conn.execute("UPDATE pedidos_venta SET estado='SELLADO' WHERE id=?", (pid,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# ── FASE 4: EXPORTAR ─────────────────────────────────────────────────────

def _pv_pedidos_dia(empresa, conn):
    fecha = datetime.now().strftime("%Y-%m-%d")
    rows  = conn.execute("""
        SELECT * FROM pedidos_venta
        WHERE date(fecha)=? AND empresa_envio=? AND estado='SELLADO' ORDER BY id
    """, (fecha, empresa)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["items"] = [dict(i) for i in conn.execute("SELECT * FROM pv_items WHERE pedido_id=?", (r["id"],)).fetchall()]
        result.append(d)
    return result

def _pv_excel(pedidos, headers, row_fn, sheet_name):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return None
    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = sheet_name
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="DC143C")
    for p in pedidos:
        ws.append(row_fn(p))
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 22
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf

@app.route("/api/pv/exportar/dinsides")
def api_pv_exportar_dinsides():
    conn    = get_db()
    pedidos = _pv_pedidos_dia("Dinsides (Lima)", conn); conn.close()
    headers = ["N°","Cliente","Teléfono","Dirección","Distrito","Canal","Pago","Productos","Notas"]
    def row(p):
        prods = "; ".join(f"{i['nombre']} {i['talla']} x{i['recolectado']}" for i in p["items"] if i["recolectado"]>0)
        return [p["id"],p["nombre_cliente"],p["telefono"],p["direccion"],p["ciudad"],p["canal"],p["tipo_pago"],prods,p["indicaciones"]]
    buf = _pv_excel(pedidos, headers, row, "Dinsides")
    if not buf: return jsonify({"error":"openpyxl no instalado"}), 500
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"dinsides_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")

@app.route("/api/pv/exportar/olva")
def api_pv_exportar_olva():
    conn    = get_db()
    pedidos = _pv_pedidos_dia("Olva Courier (Provincia)", conn); conn.close()
    headers = ["Remitente","Destinatario","Teléfono","Dirección","Ciudad","Peso(kg)","Descripción","Valor S/","Pago","Obs."]
    def row(p):
        prods = "; ".join(f"{i['nombre']} {i['talla']}" for i in p["items"] if i["recolectado"]>0)
        valor = sum(i["precio"]*i["recolectado"] for i in p["items"])
        return ["DRSK PERU",p["nombre_cliente"],p["telefono"],p["direccion"],p["ciudad"],"0.5",prods,f"{valor:.2f}",p["tipo_pago"],p["indicaciones"]]
    buf = _pv_excel(pedidos, headers, row, "Olva")
    if not buf: return jsonify({"error":"openpyxl no instalado"}), 500
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"olva_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")

@app.route("/api/pv/exportar/marketplace")
def api_pv_exportar_marketplace():
    canal   = request.args.get("canal","Ripley")
    conn    = get_db()
    pedidos = _pv_pedidos_dia(canal, conn); conn.close()
    return jsonify({"pedidos": pedidos, "canal": canal, "fecha": datetime.now().strftime("%d/%m/%Y")})

@app.route("/api/config")
def api_config_get():
    conn = get_db()
    rows = conn.execute("SELECT clave, valor FROM config").fetchall()
    conn.close()
    result = {}
    for r in rows:
        try: result[r["clave"]] = float(r["valor"])
        except: result[r["clave"]] = r["valor"]
    return jsonify(result)

@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.get_json()
    conn = get_db()
    for k, v in data.items():
        conn.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES (?, ?)", (str(k), str(v)))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/inventario/exportar")
def api_inventario_exportar():
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify({"error": "openpyxl no instalado"}), 500

    TALLA_ORD = {"S":1,"M":2,"L":3,"XL":4,"XXL":5}
    conn  = get_db()
    rows  = conn.execute("""
        SELECT * FROM skus
        ORDER BY tipo, COALESCE(producto_inv,nombre), color,
        CASE talla WHEN 'S' THEN 1 WHEN 'M' THEN 2 WHEN 'L' THEN 3
                   WHEN 'XL' THEN 4 WHEN 'XXL' THEN 5 ELSE 6 END
    """).fetchall()
    conn.close()

    total      = len(rows)
    con_stock  = sum(1 for r in rows if r["stock"] > 0)
    agotados   = total - con_stock

    # ── Styles ──────────────────────────────────────────────────────────
    def fill(hex_color):
        return PatternFill("solid", fgColor=hex_color)

    def font(size=9, bold=False, color="000000", name="Arial"):
        return Font(name=name, size=size, bold=bold, color=color)

    CENTER = Alignment(horizontal="center", vertical="center", wrap_text=False)
    LEFT   = Alignment(horizontal="left",   vertical="center", wrap_text=False)

    FILL_RED   = fill("CC0000")
    FILL_BLACK = fill("1A1A1A")
    FILL_WHITE = fill("FFFFFF")
    FILL_GRAY  = fill("F7F7F7")

    HEADERS     = ["NOMBRE PRODUCTO (WA)", "TIPO", "PRECIO LISTA S/", "COSTO S/",
                   "PRODUCTO INV", "COLOR", "TALLA", "STOCK", "ESTADO", "CODIGO EAN-13"]
    COL_WIDTHS  = [38, 10, 12, 10, 20, 15, 7, 7, 12, 16]

    # ── Workbook ─────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Inventario"

    # Row 1 — Title
    ws.row_dimensions[1].height = 32
    ws.merge_cells("A1:J1")
    c = ws["A1"]
    c.value     = "DRSK PERU — INVENTARIO POR SKU"
    c.fill      = FILL_RED
    c.font      = font(size=13, bold=True, color="FFFFFF")
    c.alignment = CENTER

    # Row 2 — Subtitle with live counts
    ws.row_dimensions[2].height = 18
    ws.merge_cells("A2:J2")
    c = ws["A2"]
    c.value     = f"Total SKUs: {total}   |   Con stock: {con_stock}   |   Agotados: {agotados}"
    c.fill      = FILL_BLACK
    c.font      = font(size=9, bold=False, color="FFFFFF")
    c.alignment = CENTER

    # Row 3 — Headers
    ws.row_dimensions[3].height = 26
    for ci, h in enumerate(HEADERS, 1):
        c = ws.cell(row=3, column=ci)
        c.value     = h
        c.fill      = FILL_BLACK
        c.font      = font(size=10, bold=True, color="FFFFFF")
        c.alignment = CENTER

    # Column widths
    for ci, w in enumerate(COL_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    # Freeze pane below headers
    ws.freeze_panes = "A4"

    # ── Data rows ────────────────────────────────────────────────────────
    data_row    = 4
    current_tipo = None
    alt          = 0

    for r in rows:
        tipo = (r["tipo"] or "").upper()

        # Separator row on tipo change
        if tipo != current_tipo:
            ws.row_dimensions[data_row].height = 16
            ws.merge_cells(f"A{data_row}:J{data_row}")
            c = ws.cell(row=data_row, column=1)
            c.value     = f"── {tipo} ──"
            c.fill      = FILL_RED
            c.font      = font(size=9, bold=True, color="FFFFFF")
            c.alignment = CENTER
            current_tipo = tipo
            data_row += 1
            alt = 0

        row_fill = FILL_WHITE if alt % 2 == 0 else FILL_GRAY
        alt += 1
        stk = r["stock"]
        costo = r["costo"] if "costo" in r.keys() else 0

        values = [
            r["nombre"], r["tipo"], r["precio"], costo,
            r["producto_inv"] or "", r["color"], r["talla"],
            stk, r["estado"] or "", r["codigo_barras"] or ""
        ]

        for ci, val in enumerate(values, 1):
            c = ws.cell(row=data_row, column=ci)
            c.value     = val
            c.fill      = row_fill
            c.alignment = LEFT

            if ci == 8:   # STOCK — bold when > 0
                c.font = font(size=9, bold=(stk > 0), color="000000")
            elif ci == 9: # ESTADO — color by stock
                c.font = font(size=9, color=("00AA44" if stk > 0 else "888888"))
            else:
                c.font = font(size=9, color="000000")

        data_row += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"inventario_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")

ESTADO_IMPORT_MAP = {
    "✅ stock":   "ACTIVO",
    "❌ agotado": "AGOTADO",
}

@app.route("/api/inventario/importar", methods=["POST"])
def api_inventario_importar():
    mode = request.form.get("mode", "agregar")
    if "file" not in request.files:
        return jsonify({"error": "No se recibió archivo"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".xlsx"):
        return jsonify({"error": "Solo archivos .xlsx"}), 400
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
        ws = wb.active
    except Exception as e:
        return jsonify({"error": f"Error leyendo Excel: {e}"}), 400

    def ti(v):
        try: return max(0, int(round(float(v)))) if v is not None else 0
        except: return 0
    def tf(v):
        try: return float(v) if v is not None else 0.0
        except: return 0.0
    def ts(v):
        return str(v).strip() if v is not None else ""
    def clean_estado(v):
        s = ts(v).lower()
        return ESTADO_IMPORT_MAP.get(s, ts(v).upper() or "ACTIVO")

    conn = get_db(); cur = conn.cursor()

    # Detect format: new per-SKU (col 0=nombre,1=tipo…,8=ean) vs old multi-talla (col 1=ean…)
    # Check row 1 (headers) to decide
    header = [str(c.value).strip().lower() if c.value else "" for c in list(ws.iter_rows(min_row=1, max_row=1))[0]]
    is_new_format = any("nombre producto" in h or "tipo" in h for h in header[:3])

    if mode == "reemplazar":
        cur.execute("DELETE FROM skus")

    imported = updated = skipped = 0

    if is_new_format:
        # New per-SKU format: skiprow 1 (title), row 2 = headers, data from row 3
        # Detect if 10-col (with COSTO) or 9-col (without COSTO)
        header2 = [str(c.value).strip().lower() if c.value else "" for c in list(ws.iter_rows(min_row=2, max_row=2))[0]]
        has_costo_col = any("costo" in h for h in header2)
        ncols = 10 if has_costo_col else 9

        start = 3 if ws.max_row > 2 else 2
        for row in ws.iter_rows(min_row=start, values_only=True):
            if not row or not any(row): continue
            cols = row[:ncols]
            try:
                if has_costo_col:
                    nombre, tipo, precio_raw, costo_raw, producto_inv, color, talla, stock_raw, estado_raw, ean = cols
                else:
                    nombre, tipo, precio_raw, producto_inv, color, talla, stock_raw, estado_raw, ean = cols
                    costo_raw = 0
            except: skipped += 1; continue
            nombre = ts(nombre)
            if not nombre: skipped += 1; continue
            ean_clean = ts(ean) or None

            if mode == "agregar" and ean_clean:
                ex = cur.execute("SELECT id,stock FROM skus WHERE codigo_barras=?", (ean_clean,)).fetchone()
                if ex:
                    cur.execute("UPDATE skus SET stock=stock+? WHERE id=?", (ti(stock_raw), ex["id"]))
                    updated += 1; continue

            try:
                cur.execute("""INSERT OR REPLACE INTO skus
                    (nombre,tipo,precio,costo,producto_inv,color,talla,stock,estado,codigo_barras)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (nombre, ts(tipo), tf(precio_raw), tf(costo_raw), ts(producto_inv), ts(color),
                     ts(talla).upper(), ti(stock_raw), clean_estado(estado_raw), ean_clean))
                imported += 1
            except: skipped += 1
    else:
        # Legacy multi-talla format (old inventario.xlsx export)
        TALLA_MAP = [("S",5),("M",6),("L",7),("XL",8),("XXL",9)]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not any(row): continue
            try:
                ean = ts(row[1]); tipo = ts(row[2]); nombre = ts(row[3]); color = ts(row[4]) or "UNICO"
                precio = tf(row[12]); estado = ts(row[13]) if len(row)>13 else "ACTIVO"
            except: skipped += 1; continue
            if not nombre: skipped += 1; continue
            for talla, col_i in TALLA_MAP:
                stk = ti(row[col_i]) if len(row) > col_i else 0
                if stk == 0 and mode == "agregar": continue
                ex = cur.execute("SELECT id FROM skus WHERE nombre=? AND color=? AND talla=?",
                                 (nombre, color, talla)).fetchone()
                if ex:
                    cur.execute("UPDATE skus SET stock=stock+? WHERE id=?", (stk, ex["id"]))
                    updated += 1
                else:
                    try:
                        cur.execute("""INSERT INTO skus (nombre,tipo,precio,producto_inv,color,talla,stock,estado,codigo_barras)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                            (nombre, tipo, precio, nombre, color, talla, stk, estado, None))
                        imported += 1
                    except: skipped += 1

    conn.commit(); conn.close()
    return jsonify({"ok": True, "importados": imported, "actualizados": updated, "omitidos": skipped})

@app.route("/api/ventas_grupos")
def api_ventas_grupos():
    desde = request.args.get("desde", "")
    conn  = get_db()
    sql   = """SELECT v.*,
                 CASE WHEN p.tipo IS NOT NULL
                      THEN p.tipo || ' ' || v.nombre
                      ELSE v.nombre END AS nombre_display,
                 vg.nombre_cliente, vg.telefono AS vg_telefono,
                 vg.descuento_pct AS vg_descuento_pct,
                 vg.descuento_motivo AS vg_descuento_motivo,
                 vg.descuento_monto AS vg_descuento_monto
               FROM ventas v
               LEFT JOIN skus p ON v.producto_id = p.id
               LEFT JOIN ventas_grupos vg ON v.grupo_id = vg.id"""
    params = []
    if desde:
        sql += " WHERE v.fecha >= ?"
        params.append(desde)
    sql += " ORDER BY v.fecha DESC LIMIT 500"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()

    grupos, orden = {}, []
    for v in rows:
        gid = v.get("grupo_id")
        key = f"g{gid}" if gid else f"s{v['id']}"
        if key not in grupos:
            pct   = (v.get("vg_descuento_pct") if gid else v.get("descuento_pct")) or 0
            motiv = (v.get("vg_descuento_motivo") if gid else v.get("descuento_motivo")) or ""
            grupos[key] = {
                "key": key, "grupo_id": gid,
                "nombre_cliente": v.get("nombre_cliente") or "DIRECTO",
                "telefono": v.get("vg_telefono") or "",
                "canal": v.get("canal") or "DIRECTO",
                "fecha": v["fecha"],
                "descuento_pct": pct, "descuento_motivo": motiv,
                "descuento_monto": 0, "items": [], "total": 0,
            }
            orden.append(key)
        grupos[key]["items"].append(v)
        grupos[key]["total"] += v["precio"] * v["cantidad"]
        grupos[key]["descuento_monto"] += v.get("descuento_monto") or 0
    return jsonify([grupos[k] for k in orden])


@app.route("/api/venta_manual", methods=["POST"])
@rate_limit(calls=60, period=60)
def api_venta_manual():
    data             = request.get_json()
    nombre_cliente   = (data.get("nombre_cliente") or "DIRECTO").strip()
    telefono         = str(data.get("telefono") or "").strip()
    canal            = str(data.get("canal") or "DIRECTO").strip()
    descuento_pct    = float(data.get("descuento_pct") or 0)
    descuento_motivo = str(data.get("descuento_motivo") or "").strip()
    items            = data.get("items", [])

    if not items:
        return jsonify({"error": "Sin productos en el carrito"}), 400
    if descuento_pct > 0 and not descuento_motivo:
        return jsonify({"error": "El motivo del descuento es obligatorio"}), 400

    conn = get_db()
    cur  = conn.cursor()

    subtotal_orig   = sum(float(i.get("precio_original", i.get("precio", 0))) * int(i.get("cantidad", 1)) for i in items)
    descuento_monto = subtotal_orig * descuento_pct / 100
    total_pagado    = subtotal_orig - descuento_monto

    cur.execute("""INSERT INTO ventas_grupos
                   (nombre_cliente,telefono,canal,total,descuento_pct,descuento_motivo,descuento_monto)
                   VALUES (?,?,?,?,?,?,?)""",
                (nombre_cliente, telefono, canal, total_pagado,
                 descuento_pct, descuento_motivo, descuento_monto))
    grupo_id = cur.lastrowid

    for item in items:
        pid         = item.get("producto_id")
        cant        = int(item.get("cantidad", 1))
        precio_orig = float(item.get("precio_original", item.get("precio", 0)))

        if not pid:
            conn.close()
            return jsonify({"error": "producto_id requerido"}), 400

        prod = cur.execute("SELECT * FROM skus WHERE id=?", (pid,)).fetchone()
        if not prod:
            conn.close()
            return jsonify({"error": "Producto no encontrado"}), 404

        if prod["stock"] < cant:
            conn.close()
            return jsonify({"error": f"Stock insuficiente — {prod['nombre']} {prod['talla']}. Disponible: {prod['stock']}"}), 400

        precio_pagado   = precio_orig * (1 - descuento_pct / 100)
        desc_monto_item = precio_orig * cant * descuento_pct / 100
        cur.execute("UPDATE skus SET stock=MAX(0,stock-?) WHERE id=?", (cant, pid))
        cur.execute("""INSERT INTO ventas
                       (producto_id,ean13,nombre,color,talla,precio,cantidad,canal,
                        grupo_id,descuento_pct,descuento_motivo,descuento_monto)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, prod["codigo_barras"], prod["nombre"], prod["color"], prod["talla"],
                     precio_pagado, cant, canal,
                     grupo_id, descuento_pct, descuento_motivo, desc_monto_item))

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "grupo_id": grupo_id})


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print("ERROR: inventario.db no existe. Ejecuta primero: python import_excel.py")
    else:
        migrate_db()
        print("=" * 50)
        print("  DRSK PERU — Sistema de Inventario")
        print("  http://localhost:5000")
        print("=" * 50)
        app.run(debug=False, host="0.0.0.0", port=5000)
