"""Small, non-sensitive fixture used by the controlled desktop scan demo."""


def reserve_inventory(item_id: str, quantity: int) -> dict[str, int | str]:
    """Reserve an item in the demonstration workspace without performing I/O."""
    # TODO: replace this fixture with an idempotent inventory service adapter.
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    return {"item_id": item_id, "reserved": quantity}
