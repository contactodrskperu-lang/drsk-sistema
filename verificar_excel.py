import openpyxl
import os

path = os.path.join(os.path.dirname(__file__), "DRSK_INVENTARIO_POR_SKU.xlsx")
wb = openpyxl.load_workbook(path, data_only=True)
ws = wb.active

count = 0
samples = []
for row in ws.iter_rows(values_only=True):
    ean = row[9] if len(row) > 9 else None
    if ean and str(ean).startswith("775"):
        count += 1
        if len(samples) < 8:
            samples.append((str(row[0] or ""), str(ean)))

print(f"Total filas con EAN 775...: {count}")
for nombre, ean in samples:
    print(f"  EAN={ean}  {nombre[:45]}")
