from dataclasses import dataclass, field

from inventory import Item


@dataclass
class Order:
    id: int
    items: list = field(default_factory=list)

    def add(self, item: Item) -> None:
        item.reserve_for(self)
        self.items.append(item)
