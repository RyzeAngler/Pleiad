from datetime import UTC, datetime


def tighter[T: (int, float, datetime)](first: T | None, second: T | None) -> T | None:
    """Intersect optional ceilings, with None denoting an unbounded dimension."""
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def remaining(limit: int | None, used: int) -> int | None:
    """Return nonnegative remaining finite allowance or an unbounded allowance."""
    return None if limit is None else max(0, limit - used)


def exhausted(value: int | None) -> bool:
    """Return whether a finite allowance has been consumed."""
    return value is not None and value <= 0


def expired(deadline: datetime | None) -> bool:
    """Return whether a finite deadline has elapsed."""
    return deadline is not None and deadline <= datetime.now(UTC)


def seconds_remaining(deadline: datetime | None) -> float | None:
    """Return a timeout suitable for asyncio.timeout, including an unbounded lifetime."""
    return None if deadline is None else max(0.0, (deadline - datetime.now(UTC)).total_seconds())
