import asyncio
import logging
import uuid
from pathlib import Path

import ccxt
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import load_config
from app.exchange import get_perpetual_symbols, make_exchange, ping_exchange
from app.models import TradeRequest
from app.sse import cancel_session, create_session, event_generator
from app.strategy import DeltaNeutralStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="DeltaOpen")

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# ---------------------------------------------------------------------------
# Exchange instances (initialised lazily from config)
# ---------------------------------------------------------------------------

_exchanges: dict[str, ccxt.Exchange] = {}


def _get_exchanges() -> dict[str, ccxt.Exchange]:
    if _exchanges:
        return _exchanges
    cfg = load_config()
    if cfg is None:
        raise HTTPException(status_code=503, detail="config.json not found or invalid")
    _exchanges["binance"] = make_exchange("binance", cfg.binance)
    _exchanges["bybit"] = make_exchange("bybit", cfg.bybit)
    return _exchanges


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/api/config/status")
async def config_status():
    cfg = load_config()
    if cfg is None:
        return {"binance_ok": False, "bybit_ok": False, "error": "config.json missing"}

    try:
        exchanges = _get_exchanges()
    except HTTPException:
        return {"binance_ok": False, "bybit_ok": False, "error": "Could not initialise exchanges"}

    binance_ok, bybit_ok = await asyncio.gather(
        ping_exchange("binance", exchanges["binance"]),
        ping_exchange("bybit", exchanges["bybit"]),
    )
    return {"binance_ok": binance_ok, "bybit_ok": bybit_ok}


@app.get("/api/symbols")
async def get_symbols():
    try:
        exchanges = _get_exchanges()
    except HTTPException:
        return {"symbols": [], "error": "Exchange not configured"}

    symbols = await get_perpetual_symbols(exchanges["binance"], exchanges["bybit"])
    return {"symbols": symbols}


@app.post("/api/trade/start")
async def start_trade(request: TradeRequest):
    try:
        exchanges = _get_exchanges()
    except HTTPException as e:
        raise e

    session_id = str(uuid.uuid4())
    queue, cancel_event = create_session(session_id)

    strategy = DeltaNeutralStrategy(
        session_id=session_id,
        request=request,
        exchanges=exchanges,
        event_queue=queue,
        cancel_event=cancel_event,
    )

    asyncio.create_task(strategy.run())
    logger.info(f"Started trade session {session_id}")
    return {"session_id": session_id}


@app.get("/api/trade/events/{session_id}")
async def trade_events(session_id: str):
    return StreamingResponse(
        event_generator(session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/trade/cancel/{session_id}")
async def cancel_trade(session_id: str):
    cancelled = cancel_session(session_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Session not found or already finished")
    return {"cancelled": True}
