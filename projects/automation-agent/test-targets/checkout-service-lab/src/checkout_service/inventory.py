def can_reserve(available: int, requested: int) -> bool:
    if available < 0:
        raise ValueError("available stock cannot be negative")
    if requested <= 0:
        raise ValueError("requested quantity must be positive")
    return requested < available
