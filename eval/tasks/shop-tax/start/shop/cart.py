"""Cart totals."""


def subtotal(items: list[tuple[float, int]]) -> float:
    return sum(price * qty for price, qty in items)


def total(items: list[tuple[float, int]], region: str = "none") -> float:
    return subtotal(items)
