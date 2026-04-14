import asyncio
import logging
import math
from typing import Optional

import ccxt

from app.exchange import (
    cancel_order,
    ccxt_side,
    create_limit_order,
    fetch_order,
    fetch_order_book,
    limit_price_for_side,
)
from app.models import LegStatus, SSEEvent, TradeRequest
from app.sse import cancel_session

logger = logging.getLogger(__name__)


class DeltaNeutralStrategy:
    def __init__(
        self,
        session_id: str,
        request: TradeRequest,
        exchanges: dict[str, ccxt.Exchange],
        event_queue: asyncio.Queue,
        cancel_event: asyncio.Event,
    ):
        self.session_id = session_id
        self.request = request
        self.exchanges = exchanges
        self.event_queue = event_queue
        self.cancel_event = cancel_event

        self._second_exchange = (
            "bybit" if request.first_exchange == "binance" else "binance"
        )
        self._second_side = "short" if request.first_side == "long" else "long"

        # Track leg statuses for SSE updates
        self._leg_statuses: dict[str, LegStatus] = {
            request.first_exchange: LegStatus(
                exchange=request.first_exchange,
                side=request.first_side,
                target_usdt=request.position_size_usdt,
                filled_usdt=0.0,
                status="PENDING",
                chunk_index=0,
                total_chunks=self._total_chunks(),
            ),
            self._second_exchange: LegStatus(
                exchange=self._second_exchange,
                side=self._second_side,
                target_usdt=request.position_size_usdt,
                filled_usdt=0.0,
                status="PENDING",
                chunk_index=0,
                total_chunks=self._total_chunks(),
            ),
        }

    def _total_chunks(self) -> int:
        return math.ceil(1.0 / self.request.chunk_pct)

    async def run(self) -> None:
        try:
            await self._run_leg(
                exchange_name=self.request.first_exchange,
                side=self.request.first_side,
            )
            if self.cancel_event.is_set():
                await self._emit_log("Trade cancelled by user.")
                await self._emit_done()
                return

            await self._run_leg(
                exchange_name=self._second_exchange,
                side=self._second_side,
            )
            if self.cancel_event.is_set():
                await self._emit_log("Trade cancelled by user.")
            await self._emit_done()

        except ccxt.ExchangeError as e:
            await self._emit_error(f"Exchange error: {e}")
        except Exception as e:
            logger.exception(f"[{self.session_id}] Unexpected error")
            await self._emit_error(f"Unexpected error: {e}")

    async def _run_leg(self, exchange_name: str, side: str) -> None:
        total_chunks = self._total_chunks()
        chunk_usdt = self.request.position_size_usdt * self.request.chunk_pct
        leg = self._leg_statuses[exchange_name]
        leg.status = "PLACING"

        for chunk_idx in range(total_chunks):
            if self.cancel_event.is_set():
                leg.status = "CANCELLED"
                await self._emit_leg_update(exchange_name)
                return

            leg.chunk_index = chunk_idx
            await self._execute_chunk(exchange_name, side, chunk_usdt, chunk_idx, total_chunks)

            if self.cancel_event.is_set():
                return

        leg.status = "FILLED"
        await self._emit_leg_update(exchange_name)

    async def _execute_chunk(
        self,
        exchange_name: str,
        side: str,
        chunk_usdt: float,
        chunk_idx: int,
        total_chunks: int,
    ) -> None:
        exchange = self.exchanges[exchange_name]
        leg = self._leg_statuses[exchange_name]
        filled_usdt_this_chunk = 0.0
        reprice_attempts = 0

        while filled_usdt_this_chunk < chunk_usdt and not self.cancel_event.is_set():
            # Fetch current order book to get best price
            ob = await fetch_order_book(exchange_name, exchange, self.request.symbol)
            price = limit_price_for_side(ob, side)
            remaining_usdt = chunk_usdt - filled_usdt_this_chunk
            amount = remaining_usdt / price

            # Place postOnly limit order
            leg.status = "PLACING"
            leg.current_order_price = price
            leg.current_order_id = None
            await self._emit_leg_update(exchange_name)
            await self._emit_log(
                f"{exchange_name.capitalize()}/{side.upper()} "
                f"chunk {chunk_idx + 1}/{total_chunks}: "
                f"placing {amount:.6f} @ {price}"
            )

            try:
                order = await create_limit_order(
                    exchange_name, exchange,
                    self.request.symbol, ccxt_side(side), amount, price
                )
            except ccxt.InvalidOrder:
                # postOnly rejected (price would cross) — reprice immediately
                await self._emit_log(
                    f"{exchange_name.capitalize()}: postOnly rejected, repricing immediately"
                )
                await asyncio.sleep(0.5)
                continue

            order_id: str = order["id"]
            leg.current_order_id = order_id
            leg.status = "OPEN"
            await self._emit_leg_update(exchange_name)

            # Polling loop
            while not self.cancel_event.is_set():
                await asyncio.sleep(self.request.reprice_interval_s)

                fetched = await fetch_order(exchange_name, exchange, order_id, self.request.symbol)
                filled_base = float(fetched.get("filled") or 0)
                avg_price = float(fetched.get("average") or price)
                newly_filled_usdt = filled_base * avg_price

                # Update running totals
                total_filled_usdt = (
                    sum(
                        float(ls.filled_usdt)
                        for ls in self._leg_statuses.values()
                        if ls.exchange == exchange_name
                    )
                )
                leg.filled_usdt = (
                    self.request.position_size_usdt
                    - (chunk_usdt * (total_chunks - chunk_idx - 1))
                    - (chunk_usdt - filled_usdt_this_chunk - newly_filled_usdt)
                )
                leg.filled_usdt = max(0.0, leg.filled_usdt)

                if fetched["status"] == "closed":
                    filled_usdt_this_chunk += newly_filled_usdt
                    leg.filled_usdt = min(
                        self.request.position_size_usdt,
                        chunk_usdt * chunk_idx + filled_usdt_this_chunk + newly_filled_usdt,
                    )
                    leg.status = "PARTIALLY_FILLED" if chunk_idx < total_chunks - 1 else "FILLED"
                    leg.current_order_id = None
                    await self._emit_leg_update(exchange_name)
                    await self._emit_log(
                        f"{exchange_name.capitalize()}/{side.upper()} "
                        f"chunk {chunk_idx + 1}/{total_chunks} filled."
                    )
                    return

                # Auto-reprice check
                if (
                    self.request.auto_reprice
                    and reprice_attempts < self.request.max_reprice_attempts
                ):
                    ob_new = await fetch_order_book(exchange_name, exchange, self.request.symbol)
                    new_best = limit_price_for_side(ob_new, side)
                    if new_best != price:
                        # Cancel and re-place at new best
                        try:
                            await cancel_order(exchange_name, exchange, order_id, self.request.symbol)
                        except ccxt.OrderNotFound:
                            pass  # already filled/cancelled on exchange

                        # Record partial fill from cancelled order
                        filled_usdt_this_chunk += float(fetched.get("filled") or 0) * float(
                            fetched.get("average") or price
                        )
                        reprice_attempts += 1
                        leg.current_order_id = None
                        await self._emit_log(
                            f"{exchange_name.capitalize()}: repricing "
                            f"{price} → {new_best} (attempt {reprice_attempts})"
                        )
                        break  # break inner loop → re-enter outer while to place new order

        # Cancel open order if cancelled externally mid-chunk
        if self.cancel_event.is_set() and leg.current_order_id:
            try:
                await cancel_order(
                    exchange_name, exchange, leg.current_order_id, self.request.symbol
                )
            except Exception:
                pass
            leg.current_order_id = None
            leg.status = "CANCELLED"
            await self._emit_leg_update(exchange_name)

    # ---------------------------------------------------------------------------
    # SSE emitters
    # ---------------------------------------------------------------------------

    async def _emit_leg_update(self, exchange_name: str) -> None:
        leg = self._leg_statuses[exchange_name]
        await self.event_queue.put(
            SSEEvent(event="leg_update", data=leg.model_dump())
        )

    async def _emit_log(self, message: str) -> None:
        await self.event_queue.put(
            SSEEvent(event="log", data={"msg": message})
        )

    async def _emit_done(self) -> None:
        await self.event_queue.put(SSEEvent(event="done", data={}))

    async def _emit_error(self, message: str) -> None:
        await self.event_queue.put(
            SSEEvent(event="error", data={"msg": message})
        )
