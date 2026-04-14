import asyncio
import logging
from typing import Optional

import ccxt

from app.models import ExchangeConfig

logger = logging.getLogger(__name__)

# Per-exchange locks to prevent concurrent ccxt calls on the same object
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(name: str) -> asyncio.Lock:
    if name not in _locks:
        _locks[name] = asyncio.Lock()
    return _locks[name]


def make_exchange(name: str, cfg: ExchangeConfig) -> ccxt.Exchange:
    """Create and configure a ccxt exchange instance for perpetual futures."""
    params = {
        "apiKey": cfg.api_key,
        "secret": cfg.api_secret,
        "enableRateLimit": True,
    }
    if name == "binance":
        exchange = ccxt.binance(params)
        exchange.options["defaultType"] = "future"
    elif name == "bybit":
        exchange = ccxt.bybit(params)
        exchange.options["defaultType"] = "linear"
    else:
        raise ValueError(f"Unknown exchange: {name}")
    return exchange


async def _run(exchange: ccxt.Exchange, fn_name: str, *args, **kwargs):
    """Run a synchronous ccxt method in a thread executor."""
    loop = asyncio.get_event_loop()
    fn = getattr(exchange, fn_name)
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def _run_with_retry(
    exchange_name: str,
    exchange: ccxt.Exchange,
    fn_name: str,
    *args,
    retries: int = 3,
    **kwargs,
):
    """Run a ccxt call with rate-limit handling and network retry."""
    lock = _get_lock(exchange_name)
    for attempt in range(retries):
        async with lock:
            try:
                return await _run(exchange, fn_name, *args, **kwargs)
            except ccxt.RateLimitExceeded:
                wait = exchange.rateLimit / 1000.0
                logger.warning(f"[{exchange_name}] Rate limit hit, sleeping {wait}s")
                await asyncio.sleep(wait)
                # retry
            except ccxt.NetworkError as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"[{exchange_name}] Network error ({e}), retry in {wait}s")
                    await asyncio.sleep(wait)
                else:
                    raise
            except ccxt.InvalidOrder:
                raise  # propagate immediately (post-only rejection)
            except ccxt.ExchangeError:
                raise  # propagate immediately (unrecoverable)
    raise RuntimeError(f"[{exchange_name}] {fn_name} failed after {retries} retries")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

async def fetch_order_book(exchange_name: str, exchange: ccxt.Exchange, symbol: str) -> dict:
    return await _run_with_retry(exchange_name, exchange, "fetch_order_book", symbol, 5)


async def create_limit_order(
    exchange_name: str,
    exchange: ccxt.Exchange,
    symbol: str,
    side: str,  # "buy" or "sell"
    amount: float,
    price: float,
) -> dict:
    params = {"postOnly": True}
    return await _run_with_retry(
        exchange_name, exchange, "create_order",
        symbol, "limit", side, amount, price, params
    )


async def fetch_order(
    exchange_name: str, exchange: ccxt.Exchange, order_id: str, symbol: str
) -> dict:
    return await _run_with_retry(exchange_name, exchange, "fetch_order", order_id, symbol)


async def cancel_order(
    exchange_name: str, exchange: ccxt.Exchange, order_id: str, symbol: str
) -> dict:
    return await _run_with_retry(exchange_name, exchange, "cancel_order", order_id, symbol)


async def fetch_markets(exchange_name: str, exchange: ccxt.Exchange) -> list:
    return await _run_with_retry(exchange_name, exchange, "fetch_markets")


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def best_bid(order_book: dict) -> float:
    return float(order_book["bids"][0][0])


def best_ask(order_book: dict) -> float:
    return float(order_book["asks"][0][0])


def limit_price_for_side(order_book: dict, side: str) -> float:
    """Return the price that gives maker treatment for the given side."""
    if side == "long":
        return best_bid(order_book)
    return best_ask(order_book)


def ccxt_side(side: str) -> str:
    """Convert 'long'/'short' to ccxt 'buy'/'sell'."""
    return "buy" if side == "long" else "sell"


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

async def get_perpetual_symbols(
    binance: ccxt.Exchange, bybit: ccxt.Exchange
) -> list[str]:
    """Return sorted list of USDT-linear perpetual symbols available on both exchanges."""
    try:
        binance_markets = await fetch_markets("binance", binance)
        bybit_markets = await fetch_markets("bybit", bybit)

        def is_linear_perp(m: dict) -> bool:
            return (
                m.get("type") in ("future", "swap")
                and m.get("linear") is True
                and m.get("active") is True
                and m.get("settle") == "USDT"
                and ":" in m.get("symbol", "")
            )

        binance_syms = {m["symbol"] for m in binance_markets if is_linear_perp(m)}
        bybit_syms = {m["symbol"] for m in bybit_markets if is_linear_perp(m)}
        common = sorted(binance_syms & bybit_syms)
        return common
    except Exception as e:
        logger.error(f"Failed to fetch symbols: {e}")
        return []


async def ping_exchange(exchange_name: str, exchange: ccxt.Exchange) -> bool:
    """Return True if the exchange responds (keys not required for this check)."""
    try:
        await _run_with_retry(exchange_name, exchange, "fetch_time")
        return True
    except Exception:
        return False
