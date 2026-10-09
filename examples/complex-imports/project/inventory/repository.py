import json

from .models import Item


def load(path: str) -> list:
    with open(path) as fh:
        return [Item(**row) for row in json.load(fh)]


def load_from_db(dsn: str) -> list:
    import sqlite3

    with sqlite3.connect(dsn) as conn:
        return [
            Item(sku, stock)
            for sku, stock in conn.execute("SELECT sku, stock FROM items")
        ]
