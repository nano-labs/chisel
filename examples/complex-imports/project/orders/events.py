from orders.service import notify_warehouse


def order_placed(order) -> None:
    notify_warehouse(order)
