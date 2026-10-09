from inventory.plugins import load_exporter
from orders.service import *


def build(order_id: int, stock_file: str) -> str:
    place_order(order_id, stock_file)
    return load_exporter().export(stock_file)
