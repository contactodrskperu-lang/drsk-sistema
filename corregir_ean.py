"""
corregir_ean.py
Regenera EAN-13 válidos para todos los SKUs.

Los EANs actuales son números secuenciales simples (7750000000001, 002, 003…)
que NO son EAN-13 válidos porque comparten la misma base de 12 dígitos.

Solución: asignar a cada SKU una base única usando:
    7750000 (7 dígitos) + secuencia de 5 dígitos + dígito verificador
Ejemplo: SKU #1 → base 775000000001 → EAN 7750000000014

Ejecutar: python corregir_ean.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "inventario.db")

def ean13_calcular(seq):
    """Genera EAN-13 válido: prefijo 7750000 + secuencia 5 dígitos + check digit."""
    base  = f"7750000{seq:05d}"           # 12 dígitos
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(base))
    check = (10 - total % 10) % 10
    return base + str(check)

def regenerar(conn):
    cur = conn.cursor()

    # Traer todos los SKUs ordenados por id para asignación consistente
    rows = cur.execute("SELECT id FROM skus ORDER BY id").fetchall()

    # Fase 1: poner todos a NULL para evitar conflictos UNIQUE durante UPDATE
    cur.execute("UPDATE skus SET codigo_barras = NULL")

    # Fase 2: asignar nuevo EAN a cada SKU
    for seq, (sku_id,) in enumerate(rows, start=1):
        ean = ean13_calcular(seq)
        cur.execute("UPDATE skus SET codigo_barras=? WHERE id=?", (ean, sku_id))

    conn.commit()
    return len(rows)

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    total = regenerar(conn)
    conn.close()

    print(f"OK  SKUs actualizados: {total}")
    print(f"    Primer EAN: {ean13_calcular(1)}")
    print(f"    Ultimo EAN: {ean13_calcular(total)}")
