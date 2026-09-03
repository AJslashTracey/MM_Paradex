from __future__ import annotations


class BasisEstimator:
    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.value: float | None = None
        self.last_sample_key: tuple[int | None, int | None] | None = None

    def update(self, io_mid: float, binance_mid: float, sample_key: tuple[int | None, int | None]) -> tuple[float, float]:
        raw_basis = io_mid / binance_mid - 1.0
        if sample_key == self.last_sample_key and self.value is not None:
            return raw_basis, self.value
        if self.value is None:
            self.value = raw_basis
        else:
            self.value = (self.alpha * raw_basis) + ((1.0 - self.alpha) * self.value)
        self.last_sample_key = sample_key
        return raw_basis, self.value

