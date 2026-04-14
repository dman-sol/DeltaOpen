'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let sessionId = null;
let eventSource = null;

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const badgeBinance      = document.getElementById('badge-binance');
const badgeBybit        = document.getElementById('badge-bybit');
const symbolSelect      = document.getElementById('symbol');
const firstExchange     = document.getElementById('first-exchange');
const firstSide         = document.getElementById('first-side');
const positionSize      = document.getElementById('position-size');
const chunkPct          = document.getElementById('chunk-pct');
const autoReprice       = document.getElementById('auto-reprice');
const repriceInterval   = document.getElementById('reprice-interval');
const btnStart          = document.getElementById('btn-start');
const btnCancel         = document.getElementById('btn-cancel');
const positionsTbody    = document.getElementById('positions-tbody');
const detailRow         = document.getElementById('detail-row');
const logDiv            = document.getElementById('log');

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
(async function init() {
  await Promise.all([checkConfig(), loadSymbols()]);
})();

// ---------------------------------------------------------------------------
// Exchange status badges
// ---------------------------------------------------------------------------
async function checkConfig() {
  try {
    const res = await fetch('/api/config/status');
    const data = await res.json();
    setBadge(badgeBinance, 'Binance', data.binance_ok);
    setBadge(badgeBybit,   'Bybit',   data.bybit_ok);
  } catch {
    setBadge(badgeBinance, 'Binance', false);
    setBadge(badgeBybit,   'Bybit',   false);
  }
}

function setBadge(el, name, ok) {
  el.textContent = ok ? `${name} OK` : `${name} Error`;
  el.className = 'badge ' + (ok ? 'badge-ok' : 'badge-err');
}

// ---------------------------------------------------------------------------
// Symbol dropdown
// ---------------------------------------------------------------------------
async function loadSymbols() {
  try {
    const res  = await fetch('/api/symbols');
    const data = await res.json();
    const syms = data.symbols || [];
    symbolSelect.innerHTML = syms.length
      ? syms.map(s => `<option value="${s}">${s}</option>`).join('')
      : '<option value="">No symbols available</option>';
    // Default to BTC/USDT:USDT if present
    const btc = syms.find(s => s.startsWith('BTC/USDT'));
    if (btc) symbolSelect.value = btc;
  } catch {
    symbolSelect.innerHTML = '<option value="">Failed to load</option>';
  }
}

// ---------------------------------------------------------------------------
// Start trade
// ---------------------------------------------------------------------------
btnStart.addEventListener('click', async () => {
  const symbol = symbolSelect.value;
  if (!symbol) { logError('Please select a symbol.'); return; }

  const chunkFraction = parseFloat(chunkPct.value) / 100;
  if (isNaN(chunkFraction) || chunkFraction <= 0 || chunkFraction > 1) {
    logError('Chunk % must be between 1 and 100.');
    return;
  }

  const payload = {
    first_exchange:     firstExchange.value,
    first_side:         firstSide.value,
    symbol,
    position_size_usdt: parseFloat(positionSize.value),
    chunk_pct:          chunkFraction,
    auto_reprice:       autoReprice.checked,
    reprice_interval_s: parseFloat(repriceInterval.value),
  };

  try {
    const res  = await fetch('/api/trade/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      logError(`Failed to start trade: ${err.detail || res.statusText}`);
      return;
    }
    const data = await res.json();
    sessionId = data.session_id;
    setRunning(true);
    initPositionRows(payload);
    openSSE(sessionId);
    appendLog('Trade started.', 'ok');
  } catch (e) {
    logError(`Network error: ${e.message}`);
  }
});

// ---------------------------------------------------------------------------
// Cancel trade
// ---------------------------------------------------------------------------
btnCancel.addEventListener('click', async () => {
  if (!sessionId) return;
  try {
    await fetch(`/api/trade/cancel/${sessionId}`, { method: 'POST' });
    appendLog('Cancel signal sent.', '');
  } catch (e) {
    logError(`Cancel failed: ${e.message}`);
  }
});

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------
function openSSE(sid) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/trade/events/${sid}`);

  eventSource.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch { return; }

    switch (evt.event) {
      case 'leg_update': handleLegUpdate(evt.data);  break;
      case 'log':        appendLog(evt.data.msg, ''); break;
      case 'done':       handleDone();                break;
      case 'error':      handleSSEError(evt.data.msg); break;
    }
  };

  eventSource.onerror = () => {
    if (eventSource.readyState === EventSource.CLOSED) {
      appendLog('SSE connection closed.', '');
      setRunning(false);
    }
  };
}

// ---------------------------------------------------------------------------
// Position rows
// ---------------------------------------------------------------------------
const rowData = {}; // exchange -> LegStatus

function initPositionRows(payload) {
  const second_exchange = payload.first_exchange === 'binance' ? 'bybit' : 'binance';
  const second_side     = payload.first_side === 'long' ? 'short' : 'long';
  const totalChunks     = Math.ceil(1 / payload.chunk_pct);

  rowData[payload.first_exchange] = {
    exchange: payload.first_exchange,
    side: payload.first_side,
    target_usdt: payload.position_size_usdt,
    filled_usdt: 0,
    status: 'PENDING',
    chunk_index: 0,
    total_chunks: totalChunks,
    current_order_price: null,
    current_order_id: null,
  };
  rowData[second_exchange] = {
    exchange: second_exchange,
    side: second_side,
    target_usdt: payload.position_size_usdt,
    filled_usdt: 0,
    status: 'PENDING',
    chunk_index: 0,
    total_chunks: totalChunks,
    current_order_price: null,
    current_order_id: null,
  };

  renderPositionRows();
}

function handleLegUpdate(data) {
  rowData[data.exchange] = data;
  renderPositionRows();
  updateDetailRow();
}

function renderPositionRows() {
  const placeholder = document.getElementById('row-placeholder');
  if (placeholder) placeholder.remove();

  ['binance', 'bybit'].forEach(exName => {
    const d = rowData[exName];
    if (!d) return;

    const rowId = `row-${exName}`;
    let tr = document.getElementById(rowId);
    if (!tr) {
      tr = document.createElement('tr');
      tr.id = rowId;
      positionsTbody.appendChild(tr);
    }

    const pct = d.target_usdt > 0
      ? ((d.filled_usdt / d.target_usdt) * 100).toFixed(1)
      : '0.0';

    tr.innerHTML = `
      <td>${capitalize(d.exchange)}</td>
      <td>${capitalize(d.side)}</td>
      <td>$${fmt(d.target_usdt)}</td>
      <td>$${fmt(d.filled_usdt)} <span style="color:#475569">(${pct}%)</span></td>
      <td><span class="status-pill s-${d.status.toLowerCase()}">${d.status}</span></td>
    `;
  });
}

function updateDetailRow() {
  // Show info for the currently active leg
  const active = Object.values(rowData).find(d =>
    ['PLACING', 'OPEN', 'PARTIALLY_FILLED'].includes(d.status)
  );
  if (active && active.current_order_price) {
    detailRow.textContent =
      `${capitalize(active.exchange)} · Order price: $${fmt(active.current_order_price)}` +
      ` · Chunk: ${active.chunk_index + 1}/${active.total_chunks}`;
  } else {
    detailRow.textContent = '';
  }
}

// ---------------------------------------------------------------------------
// Done / Error
// ---------------------------------------------------------------------------
function handleDone() {
  appendLog('Both legs complete. Delta-neutral position opened.', 'ok');
  setRunning(false);
  if (eventSource) eventSource.close();
}

function handleSSEError(msg) {
  logError(msg);
  setRunning(false);
  if (eventSource) eventSource.close();
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------
function setRunning(running) {
  btnStart.disabled  = running;
  btnCancel.disabled = !running;
}

function appendLog(msg, type) {
  const entry = document.createElement('div');
  entry.className = 'log-entry' + (type === 'err' ? ' log-err' : type === 'ok' ? ' log-ok' : '');
  const ts = new Date().toTimeString().slice(0, 8);
  entry.innerHTML = `<span class="log-ts">[${ts}]</span>${escapeHtml(msg)}`;
  logDiv.prepend(entry);
}

function logError(msg) {
  appendLog(msg, 'err');
}

function fmt(n) {
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
