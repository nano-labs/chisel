import requests

try:
    import ujson as json
except ImportError:
    import json

from inventory.repository import load
from orders.events import order_placed
from orders.models import Order

__all__ = ["place_order", "Order"]


def place_order(order_id: int, stock_file: str) -> Order:
    order = Order(order_id)
    for item in load(stock_file):
        order.add(item)
    order_placed(order)
    return order


def notify_warehouse(order: Order) -> None:
    requests.post("https://warehouse.example/orders", data=json.dumps({"id": order.id}))
