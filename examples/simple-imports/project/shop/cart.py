from .pricing import price_of


class Cart:
    def __init__(self):
        self.items = []

    def add(self, product: str, quantity: int = 1) -> None:
        self.items.extend([product] * quantity)

    def amounts(self) -> list:
        return [price_of(p) for p in self.items]
