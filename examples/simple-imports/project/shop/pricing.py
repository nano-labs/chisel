from .catalog import PRODUCTS

VAT = 0.23


def price_of(product: str) -> float:
    return PRODUCTS[product]


def total(amounts: list) -> float:
    return round(sum(amounts) * (1 + VAT), 2)
