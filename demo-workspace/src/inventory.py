"""Small, non-sensitive fixture used by the controlled desktop scan demo."""


def reserve_inventory(item_id: str, quantity: int) -> dict[str, int | str]:
    """Return a deterministic reservation-shaped result for the scan demo.

    This directory is mounted as a read-only, non-production scan fixture.  It
    intentionally performs no inventory mutation or network I/O; a real
    business integration belongs in a separately governed Tool Gateway
    adapter with an idempotency key and durable response.  Keeping that
    boundary explicit prevents the demo helper from being mistaken for a
    production side-effecting implementation.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    return {"item_id": item_id, "reserved": quantity}
