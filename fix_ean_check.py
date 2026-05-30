"""
fix_ean_check.py
Recalcula el digito verificador de todos los EANs en skus.
Formula: posiciones impares (1,3,5...) x1, pares (2,4,6...) x3, suma, 10 - (suma mod 10).
"""
import sqlite3, os

DB = os.path.join(os.path.dirname(__file__), "inventario.db")

def ean13_check(base12):
    # 1-indexed: posicion impar x1, posicion par x3
    # En Python 0-indexed: indice par (0,2,4...) x1, indice impar (1,3,5...) x3
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(base12))
    return str((10 - total % 10) % 10)

# Verificar con ejemplo del usuario antes de tocar la BD
ejemplo = "775000000060"
print(f"Verificacion: base={ejemplo}  ->  check={ean13_check(ejemplo)}  (esperado por lector: 1)")

conn = sqlite3.connect(DB)
cur  = conn.cursor()
rows = cur.execute("SELECT id, codigo_barras FROM skus WHERE codigo_barras IS NOT NULL ORDER BY id").fetchall()

# Fase 1: poner todos NULL para evitar conflicto UNIQUE durante UPDATE
cur.execute("UPDATE skus SET codigo_barras = NULL")

# Fase 2: recalcular check digit de cada EAN y guardar
updated = 0
for sku_id, ean in rows:
    base12  = ean[:12]
    new_ean = base12 + ean13_check(base12)
    cur.execute("UPDATE skus SET codigo_barras=? WHERE id=?", (new_ean, sku_id))
    if new_ean != ean:
        updated += 1

conn.commit()

# Mostrar primeros 5 para confirmar
sample = cur.execute("SELECT id, codigo_barras FROM skus ORDER BY id LIMIT 5").fetchall()
print(f"\nActualizados : {updated} de {len(rows)} EANs")
print("\nPrimeros 5 EANs en la BD:")
for sid, ean in sample:
    base = ean[:12]
    check = ean[-1]
    check_calc = ean13_check(base)
    ok = "OK" if check == check_calc else f"ERROR (esperado {check_calc})"
    print(f"  id={sid:>4}  EAN={ean}  check={check}  [{ok}]")

conn.close()
