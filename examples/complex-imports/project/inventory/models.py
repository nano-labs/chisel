from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orders.models import Order


@dataclass
class Item:
    sku: str
    stock: int

    def reserve_for(self, order: Order) -> None:
        self.stock -= 1
