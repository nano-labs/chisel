from models import Order


def migrate(raw: dict) -> Order:
    return Order(raw["id"])
