from typing import Literal, Optional
from pydantic import BaseModel, field_validator


class TradeRequest(BaseModel):
    first_exchange: Literal["binance", "bybit"]
    first_side: Literal["long", "short"]
    symbol: str
    position_size_usdt: float
    chunk_pct: float  # e.g. 0.10 for 10%
    auto_reprice: bool = True
    reprice_interval_s: float = 2.0
    max_reprice_attempts: int = 20

    @field_validator("chunk_pct")
    @classmethod
    def validate_chunk_pct(cls, v: float) -> float:
        if not (0 < v <= 1):
            raise ValueError("chunk_pct must be between 0 (exclusive) and 1 (inclusive)")
        return v

    @field_validator("position_size_usdt")
    @classmethod
    def validate_position_size(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("position_size_usdt must be positive")
        return v


class LegStatus(BaseModel):
    exchange: str
    side: str
    target_usdt: float
    filled_usdt: float
    current_order_price: Optional[float] = None
    current_order_id: Optional[str] = None
    status: str  # PENDING|PLACING|OPEN|PARTIALLY_FILLED|FILLED|CANCELLED|ERROR
    chunk_index: int
    total_chunks: int


class SSEEvent(BaseModel):
    event: str  # "leg_update" | "log" | "done" | "error"
    data: dict


class ExchangeConfig(BaseModel):
    api_key: str
    api_secret: str


class AppConfig(BaseModel):
    binance: ExchangeConfig
    bybit: ExchangeConfig
