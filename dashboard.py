#!/usr/bin/env python3
"""
Live local dashboard for your Alpaca paper/live account.

Shows account balance, open positions, and recent orders, refreshing
every few seconds in your browser — no external site, nothing leaves
your machine except the normal calls to Alpaca's API.

Setup (uses the same .env as the trading bot):
  pip install -r requirements.txt
  python3 dashboard.py

Then open http://127.0.0.1:5000 in your browser.
"""

import os
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template_string, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopLossRequest
from alpaca.trading.enums import QueryOrderStatus, OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest, StockLatestTradeRequest, CryptoLatestTradeRequest, Sort
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from flask import request

import strategies
import db
from crypto_trading_bot import place_protective_stop, cancel_and_confirm as cancel_crypto_and_confirm
from day_trading_bot import cancel_open_orders as cancel_equity_orders

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER_TRADE", "true").lower() != "false"

if not API_KEY or not SECRET_KEY:
    raise SystemExit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file first.")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
crypto_data_client = CryptoHistoricalDataClient()
app = Flask(__name__)

# ---------- Chart config (mirrors the bots' own settings) ----------
STOCK_TICKERS = ["NVDA", "INTC", "NOK"]
CRYPTO_SYMBOL = "BTC/USD"
SHORT_WINDOW = 9
LONG_WINDOW = 21
STOP_LOSS_PCT = 0.05
POSITION_SIZE_USD = 500
MOVE_KEYWORDS = ("SIGNAL:", "Order submitted", "Protective stop placed", "FLATTENED", "Daily loss limit hit", "MANUAL")


def ema(values, span):
    k = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def clean_message(msg):
    msg = msg.strip()
    msg = re.sub(r"^\[\d{2}:\d{2}:\d{2}\]\s*", "", msg)
    msg = re.sub(r"\s*—\s*id\s+\S+$", "", msg)
    return msg


def get_moves_by_symbol(limit_per_symbol=20):
    """Reads from the shared SQLite activity log (symbol is tagged at
    write time now, not sniffed out of the message text) and keeps
    only the 'move' style entries per ticker."""
    grouped = {}
    for sym in STOCK_TICKERS + [CRYPTO_SYMBOL]:
        moves = []
        for e in db.get_recent_activity(symbol=sym, limit=200):
            msg = e["message"]
            if not any(k in msg for k in MOVE_KEYWORDS):
                continue
            try:
                ts = datetime.fromisoformat(e["timestamp"])
                local_time = ts.astimezone().strftime("%m/%d %H:%M:%S")
            except Exception:
                local_time = "-"
            moves.append({"time_local": local_time, "message": clean_message(msg)})
            if len(moves) >= limit_per_symbol:
                break
        grouped[sym] = moves
    return grouped


def get_symbol_status(positions):
    pos_by_symbol = {p["symbol"].upper().replace("/", ""): p for p in positions}
    status = {}
    for sym in STOCK_TICKERS + [CRYPTO_SYMBOL]:
        key = sym.upper().replace("/", "")
        status[sym] = pos_by_symbol.get(key)
    return status


PAGE = """
<!doctype html>
<html>
<head>
  <meta http-equiv="refresh" content="5">
  <title>Alpaca Live Dashboard</title>
  <style>
    body { font-family: -apple-system, Helvetica, Arial, sans-serif; background: #0e0e0e; color: #eee; margin: 0; padding: 24px; }
    h1 { font-size: 20px; margin-bottom: 4px; }
    .mode { color: {{ '#4ade80' if paper else '#f87171' }}; font-weight: bold; }
    .cards { display: flex; gap: 16px; margin: 20px 0; flex-wrap: wrap; }
    .card { background: #1a1a1a; border-radius: 10px; padding: 16px 20px; min-width: 160px; }
    .card .label { font-size: 12px; color: #999; text-transform: uppercase; }
    .card .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #2a2a2a; font-size: 14px; }
    th { color: #999; font-weight: 500; }
    .pos { color: #4ade80; }
    .neg { color: #f87171; }
    .updated { color: #666; font-size: 12px; margin-top: 24px; }
    section { margin-top: 28px; }
    h2 { font-size: 15px; color: #ccc; }
  </style>
</head>
<body>
  <h1>Alpaca Live Dashboard <span class="mode">({{ 'PAPER' if paper else 'LIVE' }})</span></h1>
  <p><a href="/charts" style="color:#60a5fa;">📈 View live charts (candles, EMA lines, stop-loss)</a></p>

  <div class="cards">
    <div class="card">
      <div class="label">Portfolio Value</div>
      <div class="value">${{ '%.2f'|format(equity) }}</div>
    </div>
    <div class="card">
      <div class="label">Cash</div>
      <div class="value">${{ '%.2f'|format(cash) }}</div>
    </div>
    <div class="card">
      <div class="label">Buying Power</div>
      <div class="value">${{ '%.2f'|format(buying_power) }}</div>
    </div>
    <div class="card">
      <div class="label">Day P/L</div>
      <div class="value {{ 'pos' if day_pl >= 0 else 'neg' }}">{{ '+' if day_pl >= 0 else '' }}${{ '%.2f'|format(day_pl) }}</div>
    </div>
  </div>

  <section>
    <h2>Bot Status</h2>
    <table>
      <tr><th>Ticker</th><th>Status</th><th>Strategy</th></tr>
      {% for sym in symbols_order %}
      {% set p = symbol_status[sym] %}
      <tr>
        <td>{{ sym }}</td>
        <td>
          {% if p %}
          <span class="pos">In position</span> — {{ p.qty }} @ ${{ '%.2f'|format(p.avg_entry) }}
          (<span class="{{ 'pos' if p.pl >= 0 else 'neg' }}">{{ '+' if p.pl >= 0 else '' }}${{ '%.2f'|format(p.pl) }}</span>)
          {% else %}
          <span style="color:#999;">Watching</span>
          {% endif %}
        </td>
        <td>{{ active_crypto_strategy if sym == 'BTC/USD' else 'EMA 9/21 crossover' }}</td>
      </tr>
      {% endfor %}
    </table>

    <h2 style="margin-top:22px;">Recent Moves</h2>
    {% for sym in symbols_order %}
    <details style="margin:8px 0 8px 24px;">
      <summary style="cursor:pointer; color:#ccc; font-size:14px; padding:6px 0;">
        {{ sym }} <span style="color:#666; font-weight:normal;">({{ moves_by_symbol[sym]|length }})</span>
      </summary>
      <div style="margin-left:20px; margin-top:6px;">
        {% if moves_by_symbol[sym] %}
        <table>
          <tr><th>Time</th><th>Event</th></tr>
          {% for m in moves_by_symbol[sym] %}
          <tr>
            <td>{{ m.time_local }}</td>
            <td>{{ m.message }}</td>
          </tr>
          {% endfor %}
        </table>
        {% else %}
        <p style="color:#666; font-size:13px;">No recent activity.</p>
        {% endif %}
      </div>
    </details>
    {% endfor %}
  </section>

  <section>
    <h2>Open Positions</h2>
    {% if positions %}
    <table>
      <tr><th>Symbol</th><th>Qty</th><th>Avg Entry</th><th>Current</th><th>Unrealized P/L</th></tr>
      {% for p in positions %}
      <tr>
        <td>{{ p.symbol }}</td>
        <td>{{ p.qty }}</td>
        <td>${{ '%.2f'|format(p.avg_entry) }}</td>
        <td>${{ '%.2f'|format(p.current) }}</td>
        <td class="{{ 'pos' if p.pl >= 0 else 'neg' }}">{{ '+' if p.pl >= 0 else '' }}${{ '%.2f'|format(p.pl) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p style="color:#666;">No open positions right now.</p>
    {% endif %}
  </section>

  <section>
    <h2>Recent Orders</h2>
    {% if orders %}
    <table>
      <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th></tr>
      {% for o in orders %}
      <tr>
        <td>{{ o.time }}</td>
        <td>{{ o.symbol }}</td>
        <td>{{ o.side }}</td>
        <td>{{ o.qty }}</td>
        <td>{{ o.status }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <p style="color:#666;">No recent orders.</p>
    {% endif %}
  </section>

  <div class="updated">Last updated {{ now }} — refreshes automatically every 5 seconds.</div>
</body>
</html>
"""


@app.route("/")
def home():
    account = trading_client.get_account()
    equity = float(account.equity)
    cash = float(account.cash)
    buying_power = float(account.buying_power)
    day_pl = float(account.equity) - float(account.last_equity)

    positions = []
    for p in trading_client.get_all_positions():
        positions.append({
            "symbol": p.symbol,
            "qty": p.qty,
            "avg_entry": float(p.avg_entry_price),
            "current": float(p.current_price),
            "pl": float(p.unrealized_pl),
        })

    orders_request = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=15)
    orders = []
    for o in trading_client.get_orders(orders_request):
        orders.append({
            "time": o.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if o.submitted_at else "-",
            "symbol": o.symbol,
            "side": o.side.value if o.side else "-",
            "qty": o.qty,
            "status": o.status.value if o.status else "-",
        })

    return render_template_string(
        PAGE,
        paper=PAPER,
        equity=equity,
        cash=cash,
        buying_power=buying_power,
        day_pl=day_pl,
        positions=positions,
        orders=orders,
        symbols_order=STOCK_TICKERS + [CRYPTO_SYMBOL],
        symbol_status=get_symbol_status(positions),
        moves_by_symbol=get_moves_by_symbol(20),
        active_crypto_strategy=db.get_active_strategy(),
        now=datetime.now().strftime("%H:%M:%S"),
    )


CHARTS_PAGE = """
<!doctype html>
<html>
<head>
  <title>Live Charts</title>
  <script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
  <style>
    body { font-family: -apple-system, Helvetica, Arial, sans-serif; background: #0e0e0e; color: #eee; margin: 0; padding: 24px; }
    h1 { font-size: 20px; }
    a { color: #60a5fa; }
    .tabs { display: flex; gap: 8px; margin: 16px 0; flex-wrap: wrap; }
    .tab { background: #1a1a1a; border: 1px solid #2a2a2a; color: #eee; padding: 8px 16px;
           border-radius: 8px; cursor: pointer; font-size: 14px; }
    .tab.active { background: #2563eb; border-color: #2563eb; }
    .legend { color: #999; font-size: 13px; margin-top: 8px; }
    .swatch { display: inline-block; width: 12px; height: 3px; margin-right: 4px; vertical-align: middle; }
    .status { color: #666; font-size: 12px; margin-top: 8px; }
    .zoom-controls { display: flex; align-items: center; gap: 8px; margin: 8px 0; }
    .zoom-btn { background: #1a1a1a; border: 1px solid #2a2a2a; color: #eee; width: 34px; height: 34px;
                border-radius: 8px; cursor: pointer; font-size: 18px; line-height: 1; }
    .zoom-btn:hover { background: #2a2a2a; }
    .zoom-label { color: #999; font-size: 13px; }
    .tf-btn { background: #1a1a1a; border: 1px solid #2a2a2a; color: #eee; padding: 6px 12px;
              border-radius: 8px; cursor: pointer; font-size: 13px; }
    .tf-btn.active { background: #2563eb; border-color: #2563eb; }
    .strat-btn { background: #1a1a1a; border: 1px solid #2a2a2a; color: #eee; padding: 6px 12px;
                 border-radius: 8px; cursor: pointer; font-size: 13px; }
    .strat-btn.active { background: #7c3aed; border-color: #7c3aed; }
    .regime-note { color: #999; font-size: 12px; margin-left: 6px; }
    .live-dot { display: none; width: 9px; height: 9px; border-radius: 50%; background: #4ade80;
                margin-right: 6px; animation: blink 1.2s infinite; }
    @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
  </style>
</head>
<body>
  <h1>Live Charts <a href="/" style="float:right; font-size:14px;">← back to dashboard</a></h1>
  <div class="tabs" id="tabs"></div>
  <div class="tabs" id="tfTabs"></div>
  <div class="tabs" id="strategyTabs" style="display:none; align-items:center;">
    <span class="zoom-label" style="margin-right:4px;">Strategy:</span>
  </div>
  <div class="tabs" id="overrideRow" style="align-items:center;">
    <span class="zoom-label" style="margin-right:4px;">Manual override:</span>
    <button class="strat-btn" id="manualBuyBtn" style="border-color:#4ade80;">Buy</button>
    <button class="strat-btn" id="manualSellBtn" style="border-color:#f87171;">Sell</button>
    <button class="strat-btn" id="disconnectBtn">Bot: ON</button>
  </div>
  <div class="zoom-controls">
    <button class="zoom-btn" id="zoomOut" title="Zoom out">−</button>
    <button class="zoom-btn" id="zoomIn" title="Zoom in">+</button>
    <button class="zoom-btn" id="zoomReset" title="Reset zoom" style="width:auto; padding:0 12px; font-size:13px;">Reset</button>
    <span class="zoom-label" id="zoomLabel"></span>
  </div>
  <div id="chart" style="height:560px;"></div>
  <div class="legend" id="legend"></div>
  <div class="status"><span class="live-dot" id="liveDot"></span><span id="status"></span></div>

  <script>
    const symbols = {{ symbols|tojson }};
    const timeframes = [1, 2, 3, 5, 10];
    let current = symbols[0];
    let currentTf = 5;
    const tabsEl = document.getElementById('tabs');
    const tfTabsEl = document.getElementById('tfTabs');

    symbols.forEach(s => {
      const btn = document.createElement('button');
      btn.className = 'tab' + (s === current ? ' active' : '');
      btn.textContent = s;
      btn.onclick = () => { current = s; render(); };
      btn.dataset.symbol = s;
      tabsEl.appendChild(btn);
    });

    timeframes.forEach(tf => {
      const btn = document.createElement('button');
      btn.className = 'tf-btn' + (tf === currentTf ? ' active' : '');
      btn.textContent = tf + 'm';
      btn.onclick = () => { currentTf = tf; render(); };
      btn.dataset.tf = tf;
      tfTabsEl.appendChild(btn);
    });

    // ---- Strategy toggle (crypto only) ----
    const strategyTabsEl = document.getElementById('strategyTabs');
    const strategyOptions = ['trend', 'reversion', 'breakout', 'auto'];
    const strategyLabels = { trend: 'Trend', reversion: 'Reversion', breakout: 'Breakout', auto: 'Auto' };
    let activeStrategy = 'auto';

    strategyOptions.forEach(name => {
      const btn = document.createElement('button');
      btn.className = 'strat-btn';
      btn.textContent = strategyLabels[name];
      btn.dataset.strategy = name;
      btn.onclick = () => setStrategy(name);
      strategyTabsEl.appendChild(btn);
    });
    const regimeNoteEl = document.createElement('span');
    regimeNoteEl.className = 'regime-note';
    regimeNoteEl.id = 'regimeNote';
    strategyTabsEl.appendChild(regimeNoteEl);

    function setStrategyActiveUI() {
      document.querySelectorAll('.strat-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.strategy === activeStrategy);
      });
    }

    async function fetchStrategy() {
      try {
        const res = await fetch('/api/strategy');
        const d = await res.json();
        activeStrategy = d.active || 'auto';
        setStrategyActiveUI();
        regimeNoteEl.textContent = (activeStrategy === 'auto' && d.regime)
          ? 'currently: ' + strategyLabels[d.regime] : '';
      } catch (e) { /* ignore */ }
    }

    async function setStrategy(name) {
      activeStrategy = name;
      setStrategyActiveUI();
      try {
        const res = await fetch('/api/strategy', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ strategy: name }),
        });
        const d = await res.json();
        if (d.error) { regimeNoteEl.textContent = 'Error: ' + d.error; return; }
      } catch (e) { regimeNoteEl.textContent = 'Error saving strategy.'; }
      fetchStrategy();
    }

    function updateStrategyVisibility() {
      strategyTabsEl.style.display = (current === 'BTC/USD') ? 'flex' : 'none';
    }

    // ---- Manual override (buy / sell / disconnect) ----
    const manualBuyBtn = document.getElementById('manualBuyBtn');
    const manualSellBtn = document.getElementById('manualSellBtn');
    const disconnectBtn = document.getElementById('disconnectBtn');
    let botPaused = false;

    function updateDisconnectUI() {
      disconnectBtn.textContent = botPaused ? 'Bot: OFF (manual)' : 'Bot: ON';
      disconnectBtn.style.background = botPaused ? '#7f1d1d' : '#1a1a1a';
      disconnectBtn.style.borderColor = botPaused ? '#f87171' : '#2a2a2a';
    }

    async function fetchControl() {
      try {
        const res = await fetch('/api/control?symbol=' + encodeURIComponent(current));
        const d = await res.json();
        botPaused = !!d.paused;
        updateDisconnectUI();
      } catch (e) { /* ignore */ }
    }

    disconnectBtn.onclick = async () => {
      const next = !botPaused;
      const verb = next ? 'disconnect the bot from' : 'reconnect the bot to';
      if (!confirm('Sure you want to ' + verb + ' ' + current + '?')) return;
      disconnectBtn.disabled = true;
      try {
        await fetch('/api/control', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: current, paused: next }),
        });
      } catch (e) { /* ignore */ }
      disconnectBtn.disabled = false;
      fetchControl();
    };

    async function manualOrder(side) {
      const label = side === 'buy' ? 'Buy' : 'Sell';
      if (!confirm(label + ' ' + current + ' now at market price?')) return;
      const btn = side === 'buy' ? manualBuyBtn : manualSellBtn;
      const original = btn.textContent;
      btn.textContent = 'Working…';
      manualBuyBtn.disabled = true; manualSellBtn.disabled = true;
      try {
        const res = await fetch('/api/manual_order', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: current, side: side }),
        });
        const d = await res.json();
        if (d.error) {
          alert('Order failed: ' + d.error);
        } else {
          alert(label.toUpperCase() + ' order placed for ' + current + ' at ~$' + d.price.toFixed(2));
          render();
        }
      } catch (e) {
        alert('Order failed.');
      }
      btn.textContent = original;
      manualBuyBtn.disabled = false; manualSellBtn.disabled = false;
    }

    manualBuyBtn.onclick = () => manualOrder('buy');
    manualSellBtn.onclick = () => manualOrder('sell');

    function setActive() {
      document.querySelectorAll('.tab').forEach(b => {
        b.classList.toggle('active', b.dataset.symbol === current);
      });
      document.querySelectorAll('.tf-btn').forEach(b => {
        b.classList.toggle('active', Number(b.dataset.tf) === currentTf);
      });
    }

    let lastData = null;     // raw completed bars from the last full fetch
    let liveCandle = null;   // the currently-forming candle, built from tick polling
    let lastArrs = null;     // completed bars + liveCandle, used for the plot & zoom
    let visibleBars = null;  // null = show everything

    function resetLiveCandle(data) {
      if (!data.times.length) { liveCandle = null; return; }
      const intervalMs = currentTf * 60000;
      const lastBarTime = new Date(data.times[data.times.length - 1]).getTime();
      const lastClose = data.close[data.close.length - 1];
      liveCandle = {
        time: new Date(lastBarTime + intervalMs).toISOString(),
        open: lastClose, high: lastClose, low: lastClose, close: lastClose,
      };
    }

    function buildArrs(data) {
      const times = data.times.slice(), opens = data.open.slice(), highs = data.high.slice(),
            lows = data.low.slice(), closes = data.close.slice();
      if (liveCandle) {
        times.push(liveCandle.time); opens.push(liveCandle.open);
        highs.push(liveCandle.high); lows.push(liveCandle.low); closes.push(liveCandle.close);
      }
      return { times, opens, highs, lows, closes };
    }

    function updateZoomLabel(total) {
      const shown = visibleBars ? Math.min(visibleBars, total) : total;
      document.getElementById('zoomLabel').textContent =
        'Showing last ' + shown + ' of ' + total + ' bars';
    }

    function xRangeFor(times) {
      const total = times.length;
      if (!total) return null;
      const intervalMs = currentTf * 60000;
      const padMs = intervalMs * 3; // leave ~3 bars of empty space on the right
      const endTime = new Date(new Date(times[total - 1]).getTime() + padMs).toISOString();
      const startTime = (!visibleBars || visibleBars >= total) ? times[0] : times[total - visibleBars];
      return [startTime, endTime];
    }

    function applyZoom() {
      if (!lastArrs || !lastArrs.times.length) return;
      const range = xRangeFor(lastArrs.times);
      Plotly.relayout('chart', { 'xaxis.range': range || [lastArrs.times[0], lastArrs.times[lastArrs.times.length - 1]] });
      updateZoomLabel(lastArrs.times.length);
    }

    document.getElementById('zoomIn').onclick = () => {
      if (!lastArrs || !lastArrs.times.length) return;
      const total = lastArrs.times.length;
      const base = visibleBars || total;
      visibleBars = Math.max(10, Math.round(base * 0.8));
      applyZoom();
    };
    document.getElementById('zoomOut').onclick = () => {
      if (!lastArrs || !lastArrs.times.length) return;
      const total = lastArrs.times.length;
      const base = visibleBars || total;
      visibleBars = Math.min(total, Math.round(base / 0.8));
      if (visibleBars >= total) visibleBars = null;
      applyZoom();
    };
    document.getElementById('zoomReset').onclick = () => {
      visibleBars = null;
      applyZoom();
    };

    function setStatus(price) {
      let msg;
      if (price !== undefined) {
        msg = 'LIVE · ' + current + ' $' + price.toFixed(2) + ' · ' + currentTf + 'm bars · ' + new Date().toLocaleTimeString();
      } else {
        msg = 'Updated ' + new Date().toLocaleTimeString() + ' · ' + currentTf + 'm bars';
      }
      if (lastData && lastData.entry_price) msg += ' · entry $' + lastData.entry_price.toFixed(2);
      if (lastData && lastData.stop_price) msg += ' · stop $' + lastData.stop_price.toFixed(2);
      document.getElementById('status').textContent = msg;
    }

    function swatch(color) { return '<span class="swatch" style="background:' + color + ';"></span>'; }

    function buildLegendHTML(mode, showRsiPanel) {
      let html = '';
      if (showRsiPanel) {
        html += swatch('#a78bfa') + 'RSI(14) &nbsp;&nbsp;' +
                swatch('#f87171') + 'Overbought 70 (sell) &nbsp;&nbsp;' +
                swatch('#4ade80') + 'Oversold 30 (buy) &nbsp;&nbsp;';
      } else if (mode === 'breakout') {
        html += swatch('#4ade80') + 'BB Upper (buy trigger) &nbsp;&nbsp;' +
                swatch('#f87171') + 'BB Mid (sell trigger) &nbsp;&nbsp;' +
                swatch('#555') + 'BB Lower &nbsp;&nbsp;';
      } else {
        html += swatch('#f59e0b') + 'EMA 9 &nbsp;&nbsp;' + swatch('#60a5fa') + 'EMA 21 &nbsp;&nbsp;';
      }
      html += swatch('#4ade80') + 'Entry price &nbsp;&nbsp;' + swatch('#f87171') + 'Stop-loss (5%)';
      return html;
    }

    async function render() {
      setActive();
      updateStrategyVisibility();
      if (current === 'BTC/USD') fetchStrategy();
      fetchControl();
      document.getElementById('liveDot').style.display = 'none';
      document.getElementById('status').textContent = 'Loading ' + current + '...';
      let data;
      try {
        const res = await fetch('/api/chart/' + encodeURIComponent(current) + '?tf=' + currentTf);
        data = await res.json();
      } catch (e) {
        document.getElementById('status').textContent = 'Error loading data.';
        return;
      }
      if (data.error) {
        document.getElementById('status').textContent = 'Error: ' + data.error;
        return;
      }
      lastData = data;
      resetLiveCandle(data);
      lastArrs = buildArrs(data);

      const traces = [{
        type: 'candlestick',
        x: lastArrs.times, open: lastArrs.opens, high: lastArrs.highs, low: lastArrs.lows, close: lastArrs.closes,
        name: current, increasing: {line: {color: '#4ade80'}}, decreasing: {line: {color: '#f87171'}},
      }];

      let effectiveMode = 'trend';
      if (current === 'BTC/USD' && data.active_strategy) {
        effectiveMode = (data.active_strategy === 'auto') ? (data.regime || 'trend') : data.active_strategy;
      }
      const showRsiPanel = !!(effectiveMode === 'reversion' && data.rsi && data.rsi.length);

      if (effectiveMode === 'breakout' && data.bb_upper && data.bb_upper.length) {
        traces.push({ type: 'scatter', mode: 'lines', x: data.times.slice(-data.bb_upper.length),
          y: data.bb_upper, line: {color: '#4ade80', width: 1.5, dash: 'dot'}, name: 'BB Upper (buy)' });
        traces.push({ type: 'scatter', mode: 'lines', x: data.times.slice(-data.bb_mid.length),
          y: data.bb_mid, line: {color: '#f87171', width: 1.5, dash: 'dot'}, name: 'BB Mid (sell)' });
        traces.push({ type: 'scatter', mode: 'lines', x: data.times.slice(-data.bb_lower.length),
          y: data.bb_lower, line: {color: '#555', width: 1, dash: 'dot'}, name: 'BB Lower' });
      } else if (showRsiPanel) {
        traces.push({ type: 'scatter', mode: 'lines', x: data.times.slice(-data.rsi.length),
          y: data.rsi, yaxis: 'y2', line: {color: '#a78bfa', width: 1.5}, name: 'RSI(14)' });
      } else {
        if (data.ema_short.length) {
          traces.push({ type: 'scatter', mode: 'lines', x: data.times.slice(-data.ema_short.length),
            y: data.ema_short, line: {color: '#f59e0b', width: 1.5}, name: 'EMA 9' });
        }
        if (data.ema_long.length) {
          traces.push({ type: 'scatter', mode: 'lines', x: data.times.slice(-data.ema_long.length),
            y: data.ema_long, line: {color: '#60a5fa', width: 1.5}, name: 'EMA 21' });
        }
      }

      const shapes = [];
      if (data.entry_price) {
        shapes.push({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: data.entry_price, y1: data.entry_price,
          line: {color: '#4ade80', width: 1, dash: 'dot'} });
      }
      if (data.stop_price) {
        shapes.push({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: data.stop_price, y1: data.stop_price,
          line: {color: '#f87171', width: 1.5, dash: 'dash'} });
      }
      if (showRsiPanel) {
        shapes.push({ type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y2', y0: 70, y1: 70,
          line: {color: '#f87171', width: 1, dash: 'dot'} });
        shapes.push({ type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y2', y0: 30, y1: 30,
          line: {color: '#4ade80', width: 1, dash: 'dot'} });
      }

      const range = xRangeFor(lastArrs.times);
      const layout = {
        paper_bgcolor: '#0e0e0e', plot_bgcolor: '#0e0e0e',
        font: {color: '#ccc'},
        margin: {t: 20, r: 20, l: 50, b: 40},
        xaxis: { rangeslider: {visible: false}, gridcolor: '#222', autorange: !range, range: range || undefined },
        yaxis: { gridcolor: '#222', autorange: true, domain: showRsiPanel ? [0.32, 1] : [0, 1] },
        shapes: shapes,
        showlegend: false,
      };
      if (showRsiPanel) {
        layout.yaxis2 = { gridcolor: '#222', range: [0, 100], domain: [0, 0.22], tickfont: {size: 10} };
      }

      document.getElementById('legend').innerHTML = buildLegendHTML(effectiveMode, showRsiPanel);

      Plotly.react('chart', traces, layout, {responsive: true, displayModeBar: false});
      updateZoomLabel(lastArrs.times.length);
      setStatus();
    }

    async function pollLive() {
      if (!lastData || !liveCandle) return;
      let d;
      try {
        const res = await fetch('/api/latest/' + encodeURIComponent(current));
        d = await res.json();
      } catch (e) { return; }
      if (d.error) return;

      const price = d.price;
      liveCandle.high = Math.max(liveCandle.high, price);
      liveCandle.low = Math.min(liveCandle.low, price);
      liveCandle.close = price;
      lastArrs = buildArrs(lastData);

      Plotly.restyle('chart', {
        x: [lastArrs.times], open: [lastArrs.opens], high: [lastArrs.highs],
        low: [lastArrs.lows], close: [lastArrs.closes],
      }, [0]);

      document.getElementById('liveDot').style.display = 'inline-block';
      setStatus(price);
    }

    render();
    setInterval(render, 30000);
    setInterval(pollLive, 3000);
  </script>
</body>
</html>
"""


@app.route("/charts")
def charts_page():
    symbols = STOCK_TICKERS + [CRYPTO_SYMBOL]
    return render_template_string(CHARTS_PAGE, symbols=symbols)


ALLOWED_TIMEFRAMES = [1, 2, 3, 5, 10]


@app.route("/api/strategy", methods=["GET"])
def api_get_strategy():
    active = db.get_active_strategy()
    regime = None
    if active == "auto":
        regime = db.get_regime_state().get("current_regime")
        if regime is None:
            try:
                request_obj = CryptoBarsRequest(
                    symbol_or_symbols=CRYPTO_SYMBOL,
                    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                    limit=50,
                )
                bars = crypto_data_client.get_crypto_bars(request_obj)
                closes = [b.close for b in bars[CRYPTO_SYMBOL]]
                regime = strategies.detect_regime(closes)
            except Exception:
                regime = None
    return jsonify({"active": active, "regime": regime})


@app.route("/api/strategy", methods=["POST"])
def api_set_strategy():
    body = request.get_json(silent=True) or {}
    name = body.get("strategy")
    if name not in strategies.VALID_STRATEGIES:
        return jsonify({"error": f"invalid strategy: {name}"}), 400
    db.set_active_strategy(name)
    return jsonify({"ok": True, "active": name})


@app.route("/api/control", methods=["GET"])
def api_get_control():
    symbol = request.args.get("symbol", "")
    return jsonify({"symbol": symbol, "paused": db.is_paused(symbol)})


@app.route("/api/control", methods=["POST"])
def api_set_control():
    body = request.get_json(silent=True) or {}
    symbol = body.get("symbol")
    if symbol not in STOCK_TICKERS + [CRYPTO_SYMBOL]:
        return jsonify({"error": f"unknown symbol: {symbol}"}), 400
    paused = bool(body.get("paused"))
    db.set_paused(symbol, paused)
    db.log_activity(
        symbol,
        f"MANUAL: bot {'disconnected from' if paused else 'reconnected to'} {symbol} via dashboard override."
    )
    return jsonify({"ok": True, "symbol": symbol, "paused": paused})


@app.route("/api/manual_order", methods=["POST"])
def api_manual_order():
    body = request.get_json(silent=True) or {}
    symbol = body.get("symbol")
    side = body.get("side")
    if side not in ("buy", "sell"):
        return jsonify({"error": "side must be buy or sell"}), 400
    if symbol not in STOCK_TICKERS + [CRYPTO_SYMBOL]:
        return jsonify({"error": f"unknown symbol: {symbol}"}), 400

    is_crypto = symbol == CRYPTO_SYMBOL

    try:
        if is_crypto:
            trade = crypto_data_client.get_crypto_latest_trade(
                CryptoLatestTradeRequest(symbol_or_symbols=CRYPTO_SYMBOL))[CRYPTO_SYMBOL]
        else:
            clock = trading_client.get_clock()
            if not clock.is_open:
                return jsonify({"error": "market is closed — can't place stock orders right now"}), 400
            trade = stock_data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol))[symbol]
        price = float(trade.price)
    except Exception as e:
        return jsonify({"error": f"couldn't get a price: {e}"}), 500

    try:
        if side == "buy":
            if is_crypto:
                order = trading_client.submit_order(MarketOrderRequest(
                    symbol=CRYPTO_SYMBOL, notional=POSITION_SIZE_USD,
                    side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
                time.sleep(2)  # let the market order fill before sizing the stop
                try:
                    filled_qty = float(trading_client.get_open_position(
                        CRYPTO_SYMBOL.replace("/", "")).qty)
                except Exception:
                    filled_qty = 0.0
                stop_id, stop_price = place_protective_stop(filled_qty, price, None) if filled_qty > 0 else (None, None)
                now_iso = datetime.now(timezone.utc).isoformat()
                db.set_position_state(CRYPTO_SYMBOL, entry_price=price, stop_order_id=stop_id, stop_price=stop_price, entry_time=now_iso, peak_price=price)
            else:
                qty = round(POSITION_SIZE_USD / price, 4)
                stop_price = round(price * (1 - STOP_LOSS_PCT), 2)
                order = trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET, stop_loss=StopLossRequest(stop_price=stop_price)))
        else:
            lookup_symbol = symbol.replace("/", "") if is_crypto else symbol
            try:
                position = trading_client.get_open_position(lookup_symbol)
                qty = float(position.qty)
            except Exception:
                return jsonify({"error": f"no open {symbol} position to sell"}), 400
            # Cancel whatever's resting first — the native crypto stop,
            # or the equity bracket's stop leg — so it can't fire later
            # against a position we're about to close out here.
            if is_crypto:
                pos_state = db.get_position_state(CRYPTO_SYMBOL)
                cancel_crypto_and_confirm(pos_state.get("stop_order_id"))
            else:
                cancel_equity_orders(symbol)
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC if is_crypto else TimeInForce.DAY))
            if is_crypto:
                db.clear_position_state(CRYPTO_SYMBOL)
    except Exception as e:
        db.log_activity(symbol, f"MANUAL {side.upper()} {symbol} FAILED: {e}")
        return jsonify({"error": f"order failed: {e}"}), 500

    db.log_activity(
        symbol,
        f"MANUAL: {side.upper()} {symbol} @ ~${price:.2f} — order id {order.id}"
    )
    return jsonify({"ok": True, "order_id": str(order.id), "side": side, "symbol": symbol, "price": price})


@app.route("/api/chart/<path:symbol>")
def api_chart(symbol):
    is_crypto = symbol.upper().replace("USD", "/USD").replace("//", "/") == CRYPTO_SYMBOL or symbol.upper() == "BTC/USD"

    try:
        tf_minutes = int(request.args.get("tf", 5))
    except (TypeError, ValueError):
        tf_minutes = 5
    if tf_minutes not in ALLOWED_TIMEFRAMES:
        tf_minutes = 5

    # Wide lower bound only; sort=DESC + limit=100 below is what actually
    # guarantees we get the newest bars, not the oldest ones in this window.
    lookback_start = datetime.now(timezone.utc) - timedelta(days=10)

    try:
        if is_crypto:
            bars_request = CryptoBarsRequest(
                symbol_or_symbols=CRYPTO_SYMBOL,
                timeframe=TimeFrame(tf_minutes, TimeFrameUnit.Minute),
                start=lookback_start,
                limit=100,
                sort=Sort.DESC,
            )
            bars = list(crypto_data_client.get_crypto_bars(bars_request)[CRYPTO_SYMBOL])
        else:
            bars_request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(tf_minutes, TimeFrameUnit.Minute),
                start=lookback_start,
                limit=100,
                sort=Sort.DESC,
            )
            bars = list(stock_data_client.get_stock_bars(bars_request)[symbol])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    bars = list(reversed(bars))  # DESC fetch -> put back in chronological order

    times = [b.timestamp.isoformat() for b in bars]
    opens = [float(b.open) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    closes = [float(b.close) for b in bars]

    ema_short = ema(closes, SHORT_WINDOW) if len(closes) >= SHORT_WINDOW else []
    ema_long = ema(closes, LONG_WINDOW) if len(closes) >= LONG_WINDOW else []

    active_strategy = None
    regime = None
    bb_upper, bb_mid, bb_lower = [], [], []
    rsi_vals = []

    if is_crypto:
        active_strategy = db.get_active_strategy()
        if len(closes) >= strategies.BB_PERIOD:
            for i in range(strategies.BB_PERIOD - 1, len(closes)):
                window = closes[i - strategies.BB_PERIOD + 1:i + 1]
                bb = strategies.bollinger(window)
                bb_mid.append(bb["mean"])
                bb_upper.append(bb["upper"])
                bb_lower.append(bb["lower"])
        rsi_vals = strategies.rsi_series(closes)
        if active_strategy == "auto":
            regime = db.get_regime_state().get("current_regime") or strategies.detect_regime(closes)

    entry_price = None
    stop_price = None

    if is_crypto:
        pos_state = db.get_position_state(CRYPTO_SYMBOL)
        entry_price = pos_state.get("entry_price")
        stop_price = pos_state.get("stop_price")
        if entry_price and not stop_price:
            # Older state saved before this column existed, or a stop
            # that failed to place — fall back to the fixed % as a
            # display estimate only (not what's actually resting).
            stop_price = round(entry_price * (1 - STOP_LOSS_PCT), 2)
    else:
        try:
            position = trading_client.get_open_position(symbol)
            entry_price = float(position.avg_entry_price)
            # Read the ACTUAL resting stop's price from Alpaca rather
            # than recomputing from a fixed % — Tier 2 stops are
            # ATR-based and vary per trade.
            try:
                orders = trading_client.get_orders(GetOrdersRequest(
                    status=QueryOrderStatus.OPEN, symbols=[symbol]
                ))
                stop_orders = [o for o in orders if o.stop_price]
                stop_price = float(stop_orders[0].stop_price) if stop_orders else round(entry_price * (1 - STOP_LOSS_PCT), 2)
            except Exception:
                stop_price = round(entry_price * (1 - STOP_LOSS_PCT), 2)
        except Exception:
            pass

    return jsonify({
        "times": times, "open": opens, "high": highs, "low": lows, "close": closes,
        "ema_short": ema_short, "ema_long": ema_long,
        "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower,
        "rsi": rsi_vals,
        "active_strategy": active_strategy, "regime": regime,
        "entry_price": entry_price, "stop_price": stop_price,
        "tf_minutes": tf_minutes,
    })


@app.route("/api/latest/<path:symbol>")
def api_latest(symbol):
    is_crypto = symbol.upper().replace("USD", "/USD").replace("//", "/") == CRYPTO_SYMBOL or symbol.upper() == "BTC/USD"
    try:
        if is_crypto:
            trade_request = CryptoLatestTradeRequest(symbol_or_symbols=CRYPTO_SYMBOL)
            trade = crypto_data_client.get_crypto_latest_trade(trade_request)[CRYPTO_SYMBOL]
        else:
            trade_request = StockLatestTradeRequest(symbol_or_symbols=symbol)
            trade = stock_data_client.get_stock_latest_trade(trade_request)[symbol]
        return jsonify({"price": float(trade.price), "time": trade.timestamp.isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("Dashboard starting.")
    print("On this Mac, open:      http://127.0.0.1:5050")
    print("On your phone (same Wi-Fi), open your Mac's local IP")
    print("address followed by :5050 — e.g. http://192.168.1.23:5050")
    print(f"Mode: {'PAPER' if PAPER else 'LIVE'}")
    app.run(host="0.0.0.0", port=5050, debug=False)
