from shop.cart import Cart
from shop.pricing import total


def checkout() -> float:
    cart = Cart()
    cart.add("apple", 3)
    cart.add("coffee")
    return total(cart.amounts())


if __name__ == "__main__":
    print(checkout())
