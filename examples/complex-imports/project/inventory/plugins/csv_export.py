import csv
import io

from ..repository import load


def export(path: str) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    for item in load(path):
        writer.writerow([item.sku, item.stock])
    return out.getvalue()
