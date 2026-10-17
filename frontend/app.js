/* ============================================================
   CONFIG
   ============================================================ */
const API_BASE = 'http://localhost:3000'; // trading server (client API)
const WS_BASE = API_BASE.replace(/^http/, 'ws');
const STORAGE_KEY = 'shopState.v2';
const HISTORY_DAYS = 90;

/* ============================================================
   DATA
   ============================================================ */
const PRODUCTS = [
  { id: 'p1', name: 'Wireless Headphones', price: 79.99, emoji: '🎧', desc: 'Noise-cancelling, 30h battery.' },
  { id: 'p2', name: 'Smartwatch',          price: 149.00, emoji: '⌚', desc: 'Fitness tracking and notifications.' },
  { id: 'p3', name: 'Travel Backpack',     price: 54.50,  emoji: '🎒', desc: '25L, water resistant.' },
];

// Display info per coin. The server only supplies symbol, base and data type,
// and has no history endpoint, so the chart and stats below are simulated placeholders.
const COIN_META = {
  BTC: { name: 'Bitcoin', seed: 22, drift: 0.0008, vol: 0.025,
    notes: ['Steadier of the two: smaller swings', 'Tends to lead the rest of the crypto market'] },
  SOL: { name: 'Solana', seed: 11, drift: 0.0010, vol: 0.040,
    notes: ['Bigger swings than Bitcoin', 'Moves with overall crypto sentiment'] },
};

const REASONS = {
  floor: 'Loss limit reached',
  ceiling: 'Goal reached',
  manual: 'Ended by you',
  time: 'Time limit reached',
  lost: 'Connection lost',
};
const REASON_NOTES = {
  floor: 'Loss limit reached. Trading stopped automatically.',
  ceiling: 'Goal reached. Trading stopped automatically.',
  manual: 'Trading stopped.',
  time: 'Your time limit ended, so trading stopped.',
  lost: 'We lost the connection to this trading session, so the last known balance was used.',
};

// Defaults used until (or unless) GET /v1/instruments answers.
let market = {
  loaded: false,
  houseFundCents: 100000,
  depositMin: 100,
  depositMax: 10000,
  fees: { makerBps: 2, takerBps: 5 },
  instruments: [
    { symbol: 'BTC-USDT', base: 'BTC', data: 'replay' },
    { symbol: 'SOL-USD', base: 'SOL', data: 'live' },
  ],
};

/* ============================================================
   FAKE HISTORY GENERATION (seeded so charts stay stable)
   ============================================================ */
function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function gauss(rand) {
  let u = 0, v = 0;
  while (!u) u = rand();
  while (!v) v = rand();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}
const hashSeed = s => [...s].reduce((a, ch) => (a * 31 + ch.charCodeAt(0)) >>> 0, 7);

function buildHistory(c) {
  const rand = mulberry32(c.seed);
  const history = [100];
  const returns = [];
  for (let i = 0; i < HISTORY_DAYS; i++) {
    const r = c.drift + c.vol * gauss(rand);
    returns.push(r);
    history.push(history[history.length - 1] * (1 + r));
  }
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
  const stdev = Math.sqrt(returns.reduce((a, b) => a + (b - mean) ** 2, 0) / returns.length);
  let peak = history[0], maxDD = 0;
  history.forEach(v => { peak = Math.max(peak, v); maxDD = Math.max(maxDD, (peak - v) / peak); });
  c.history = history;
  c.stats = {
    vol: stdev,
    maxDD,
    ret: history[history.length - 1] / history[0] - 1,
    bestDay: Math.max(...returns),
    worstDay: Math.min(...returns),
  };
}

const coinCache = {};
function getCoin(inst) {
  if (!coinCache[inst.symbol]) {
    const meta = COIN_META[inst.base] ||
      { name: inst.base, seed: hashSeed(inst.symbol), drift: 0.0008, vol: 0.05, notes: [] };
    const c = { ...inst, ...meta };
    buildHistory(c);
    coinCache[inst.symbol] = c;
  }
  return coinCache[inst.symbol];
}
const coins = () => market.instruments.map(getCoin);
const selectedCoin = () =>
  getCoin(market.instruments.find(i => i.symbol === state.trade.symbol) || market.instruments[0]);
const maxVol = () => Math.max(...coins().map(c => c.stats.vol));
function volLevel(c) {
  const sorted = coins().map(x => x.stats.vol).sort((a, b) => a - b);
  const frac = sorted.length > 1 ? sorted.indexOf(c.stats.vol) / (sorted.length - 1) : 0;
  return frac < 0.34 ? 'low' : frac < 0.67 ? 'medium' : 'high';
}

/* ============================================================
   STATE (persisted in localStorage)
   ============================================================ */
const defaultState = () => ({
  cart: {},                 // productId -> qty
  trade: { symbol: 'SOL-USD', amount: null, maxLoss: 20, target: 5, hours: 0, minutes: 0 },
  runs: [],                 // finished trading sessions for this order
  active: null,             // the trading session running right now
  ended: false,             // true once END SESSION is confirmed
});
function loadState() {
  const d = defaultState();
  try {
    const s = JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
    return { ...d, ...s, trade: { ...d.trade, ...(s.trade || {}) } };
  } catch { return d; }
}
let state = loadState();
const save = () => { try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch {} };

/* ============================================================
   HELPERS
   ============================================================ */
const $ = sel => document.querySelector(sel);
const app = $('#app');
const modalRoot = $('#modal-root');
const esc = s => String(s).replace(/[&<>"']/g, ch =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const setText = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };

const money = n => '$' + n.toFixed(2);
const moneyC = c => money(c / 100);
const signedC = c => (c >= 0 ? '+' : '−') + money(Math.abs(c) / 100);
const pct = (n, d = 1) => (n * 100).toFixed(d) + '%';
const currentPath = () => location.hash.slice(1) || '/';

function fmtClock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(sec).padStart(2, '0');
}
function fmtDuration(ms) {
  const total = Math.round(ms / 60000), h = Math.floor(total / 60), m = total % 60;
  return ((h ? h + 'h ' : '') + (m ? m + 'm' : '')).trim() || '0m';
}

const cartItems = () => Object.entries(state.cart)
  .map(([id, qty]) => ({ ...PRODUCTS.find(p => p.id === id), qty }))
  .filter(i => i.qty > 0);
const cartTotalC = () => cartItems().reduce((s, i) => s + Math.round(i.price * 100) * i.qty, 0);
const cartCount = () => cartItems().reduce((s, i) => s + i.qty, 0);
const netC = () => state.runs.reduce((s, r) => s + r.pnlCents, 0);
const dueC = () => Math.max(0, cartTotalC() - netC());

/* ============================================================
   API (client API: health, instruments, session, stop, watch socket)
   ============================================================ */
async function api(path, opts = {}) {
  let res;
  try {
    const headers = opts.body ? { 'Content-Type': 'application/json' } : {};
    res = await fetch(API_BASE + path, { ...opts, headers });
  } catch {
    const e = new Error('offline'); e.code = 'offline'; throw e;
  }
  let body = null;
  try { body = await res.json(); } catch {}
  if (!res.ok) {
    const e = new Error((body && body.error && body.error.message) || 'HTTP ' + res.status);
    e.code = (body && body.error && body.error.code) || 'http_' + res.status;
    e.status = res.status;
    throw e;
  }
  return body;
}

function setOnline(on) {
  const el = $('#server-status');
  if (!el) return;
  el.className = 'server ' + (on ? 'on' : 'off');
  el.lastElementChild.textContent = on ? 'Server connected' : 'Server offline';
}

let loadingMarket = false;
async function loadMarket() {
  if (loadingMarket) return;
  loadingMarket = true;
  try {
    const r = await api('/v1/instruments');
    if (!r.instruments || !r.instruments.length) return;
    market = {
      loaded: true,
      houseFundCents: r.houseFundCents,
      depositMin: r.deposit.minCents,
      depositMax: r.deposit.maxCents,
      fees: r.fees,
      instruments: r.instruments,
    };
    if (!market.instruments.some(i => i.symbol === state.trade.symbol)) {
      state.trade.symbol = market.instruments[0].symbol;
      save();
    }
    if (currentPath() === '/trading' && modal !== 'live') renderTrading();
  } finally { loadingMarket = false; }
}

async function checkHealth() {
  try {
    await api('/v1/health');
    setOnline(true);
    if (!market.loaded) await loadMarket();
  } catch { setOnline(false); }
}

/* ============================================================
   TRADE SETTINGS -> SESSION RAILS
   ============================================================ */
const minInvestC = () => market.depositMin;
const maxInvestC = () => Math.min(cartTotalC(), market.depositMax); // 100% of order, capped by the server

function ensureTradeDefaults() {
  const t = state.trade, min = minInvestC(), max = maxInvestC();
  if (t.amount == null) t.amount = Math.min(Math.max(Math.round(cartTotalC() * 0.10), min), max) / 100;
  else if (Math.round(t.amount * 100) > max) t.amount = max / 100;
  if (!market.instruments.some(i => i.symbol === t.symbol)) t.symbol = market.instruments[0].symbol;
}

function validateTrade() {
  const t = state.trade, errs = {};
  const dC = Math.round(Number(t.amount) * 100);
  if (!Number.isFinite(dC) || dC < minInvestC()) errs.amount = 'Minimum investment is ' + moneyC(minInvestC()) + '.';
  else if (dC > maxInvestC()) errs.amount = 'Maximum investment is ' + moneyC(maxInvestC()) + '.';
  if (!(t.target >= 1 && t.target <= 100)) errs.target = 'Enter a goal between 1% and 100%.';
  const h = t.hours, m = t.minutes;
  if (!Number.isInteger(h) || h < 0 || h > 24 || !Number.isInteger(m) || m < 0 || m > 59)
    errs.duration = 'Use 0–24 hours and 0–59 minutes.';
  return errs;
}

// Max loss -> stake floor. Discount goal -> stake ceiling (the server requires one).
function derived() {
  const t = state.trade;
  const orderC = cartTotalC();
  const depositC = Math.round(t.amount * 100);
  const floorC = Math.min(depositC - 1, Math.max(0, depositC - Math.round(depositC * t.maxLoss / 100)));
  const goalC = Math.max(1, Math.round(orderC * t.target / 100));
  return {
    orderC, depositC, floorC, ceilingC: depositC + goalC,
    lossC: depositC - floorC, goalC,
    durationMs: ((t.hours || 0) * 60 + (t.minutes || 0)) * 60000,
  };
}

/* ============================================================
   PAGES
   ============================================================ */
function renderHome() {
  app.innerHTML = `
    <div class="page">
      <h1>Featured products</h1>
      <div class="grid">
        ${PRODUCTS.map(p => `
          <div class="card product">
            <div class="product-img">${p.emoji}</div>
            <h3>${p.name}</h3>
            <div class="muted small">${p.desc}</div>
            <div class="price">${money(p.price)}</div>
            <button class="btn" data-action="add" data-id="${p.id}">Add to cart</button>
          </div>`).join('')}
      </div>
    </div>`;
}

function renderCart() {
  const items = cartItems();
  if (!items.length) {
    app.innerHTML = `<div class="page"><div class="card"><h2>Your cart is empty</h2>
      <a class="btn" href="#/">Browse products</a></div></div>`;
    return;
  }
  app.innerHTML = `
    <div class="page">
      <h1>Shopping cart</h1>
      <div class="two-col">
        <div class="card">
          ${items.map(i => `
            <div class="row">
              <div class="thumb">${i.emoji}</div>
              <div class="info"><strong>${i.name}</strong><br><span class="muted small">${money(i.price)} each</span><br>
                <button class="link-btn" data-action="remove" data-id="${i.id}">Remove</button></div>
              <div class="qty">
                <button data-action="dec" data-id="${i.id}">−</button>
                <span>${i.qty}</span>
                <button data-action="inc" data-id="${i.id}">+</button>
              </div>
              <strong>${money(i.price * i.qty)}</strong>
            </div>`).join('')}
        </div>
        <div class="card stack">
          <div class="totals"><span>Subtotal (${cartCount()} items)</span><span>${moneyC(cartTotalC())}</span></div>
          <a class="btn big" href="#/payment">Proceed to payment</a>
        </div>
      </div>
    </div>`;
}

function renderPayment() {
  const items = cartItems();
  if (!items.length) { location.hash = '#/'; return; }
  app.innerHTML = `
    <div class="page">
      <h1>Payment</h1>
      <div class="two-col">
        <div class="card">
          <h3>Order summary</h3>
          ${items.map(i => `
            <div class="row">
              <div class="thumb">${i.emoji}</div>
              <div class="info"><strong>${i.name}</strong><br><span class="muted small">Qty ${i.qty}</span></div>
              <strong>${money(i.price * i.qty)}</strong>
            </div>`).join('')}
          <div class="totals"><span>Total</span><span>${moneyC(cartTotalC())}</span></div>
        </div>
        <div class="card stack">
          <button class="btn big secondary" data-action="pay-full">Pay full price · ${moneyC(cartTotalC())}</button>
          <div class="divider">or</div>
          <button class="btn big discount" data-action="activate">✨ Activate discount</button>
          <div class="muted small">Put part of your payment into automated live trading. If it grows, you pay less. You choose the crypto, the amount, your goal, and the loss limit.</div>
        </div>
      </div>
    </div>`;
}

function sessionCardHTML() {
  if (!state.runs.length) return '';
  const net = netC();
  return `
    <div class="card">
      <h3>Your session</h3>
      ${state.runs.map(runRowHTML).join('')}
      <div class="run-net"><span>Net result</span><span class="${net >= 0 ? 'good' : 'bad'}">${signedC(net)}</span></div>
      <button class="btn danger big" data-action="end-session">END SESSION</button>
      <span class="hint">Stops all trading and takes you to your final payment.</span>
    </div>`;
}
function runRowHTML(r) {
  return `<div class="run-row"><span>${esc(r.symbol.split('-')[0])} · ${esc(REASONS[r.reason] || r.reason)}</span>
    <strong class="${r.pnlCents >= 0 ? 'good' : 'bad'}">${signedC(r.pnlCents)}</strong></div>`;
}

function renderTrading() {
  const orderC = cartTotalC();
  if (!orderC) { location.hash = '#/'; return; }
  ensureTradeDefaults();
  const t = state.trade, c = selectedCoin(), list = coins(), mv = maxVol();
  const minC = minInvestC(), maxC = maxInvestC(), f = market.fees;
  const capped = orderC > market.depositMax;
  const num = v => (Number.isFinite(v) ? v : '');
  const notes = [
    ...c.notes,
    c.data === 'replay' ? 'Prices replay recorded market data.' : 'Follows the live market price.',
    `Fees of ${pct(f.makerBps / 10000, 2)} to ${pct(f.takerBps / 10000, 2)} per trade are already included in your results.`,
  ];

  app.innerHTML = `
    <div class="page">
      ${state.runs.length ? '' : '<a href="#/payment" class="muted small">← Back to payment</a>'}
      <div class="trade-layout" style="margin-top:12px">

        <!-- LEFT COLUMN -->
        <div class="stack">
          <div class="card">
            <h3>Select crypto</h3>
            <div class="crypto-list">
              ${list.map(x => `
                <button class="crypto-item ${x.symbol === c.symbol ? 'active' : ''}" data-action="select-crypto" data-id="${esc(x.symbol)}">
                  <span><strong>${esc(x.base)}</strong> <span class="muted small">${esc(x.name)}</span></span>
                  <span class="tag ${volLevel(x)}">${volLevel(x)} vol</span>
                </button>`).join('')}
            </div>
            <label class="field">Initial investment
              <div class="input-prefix"><span>$</span>
                <input type="number" id="f-amount" data-field="amount" min="${minC / 100}" max="${maxC / 100}" step="0.01" value="${num(t.amount)}">
              </div>
            </label>
            <div class="chips"><span>% of order:</span>
              ${[10, 25, 50, 100].map(p => `<button type="button" class="chip" data-action="set-pct" data-id="${p}">${p}%</button>`).join('')}
            </div>
            <span class="hint">${moneyC(minC)} to ${moneyC(maxC)}${capped ? ' (the most one trade can hold)' : ' (up to 100% of your order)'}</span>
            <div class="field-error" id="err-amount"></div>
          </div>
          ${sessionCardHTML()}
        </div>

        <!-- RIGHT COLUMN -->
        <div class="stack">
          <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <h2 style="margin:0">${esc(c.name)} (${esc(c.base)})</h2>
              <span class="tag ${volLevel(c)}">${volLevel(c)} volatility</span>
            </div>
            <div class="muted small" style="margin:4px 0 8px">Model performance on $100 over the last ${HISTORY_DAYS} days (simulated placeholder data)</div>
            <canvas id="chart"></canvas>

            <div class="stats">
              <div class="stat"><div class="label">Avg daily swing</div><div class="value">${pct(c.stats.vol)}</div></div>
              <div class="stat"><div class="label">Max drawdown</div><div class="value">−${pct(c.stats.maxDD, 0)}</div></div>
              <div class="stat"><div class="label">${HISTORY_DAYS}-day model return</div><div class="value">${c.stats.ret >= 0 ? '+' : ''}${pct(c.stats.ret, 0)}</div></div>
              <div class="stat"><div class="label">Best day</div><div class="value">+${pct(c.stats.bestDay)}</div></div>
              <div class="stat"><div class="label">Worst day</div><div class="value">${pct(c.stats.worstDay)}</div></div>
              <div class="stat"><div class="label">Price data</div><div class="value">${c.data === 'replay' ? 'Recorded' : 'Live'}</div></div>
            </div>

            <strong class="small">Volatility vs. other cryptos</strong>
            ${list.map(x => `
              <div class="vol-row ${x.symbol === c.symbol ? 'active' : ''}">
                <span>${esc(x.base)}</span>
                <div class="vol-bar"><div style="width:${(x.stats.vol / mv) * 100}%"></div></div>
                <span>${pct(x.stats.vol)}</span>
              </div>`).join('')}

            <strong class="small" style="display:block;margin-top:14px">Things to know</strong>
            <ul class="factors">${notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>
          </div>

          <div class="card">
            <h3>Trading parameters</h3>
            <div class="params">
              <label class="field">Max loss: <span id="v-maxLoss">${t.maxLoss}%</span> of investment
                <input type="range" min="5" max="100" step="5" value="${t.maxLoss}" data-field="maxLoss">
                <span class="hint">Trading stops automatically if you lose this much.</span>
              </label>
              <div class="field">
                <label for="f-target">Discount goal (% of order)</label>
                <input type="number" id="f-target" min="1" max="100" step="1" value="${num(t.target)}" data-field="target">
                <span class="hint">Trading stops once your profit reaches this.</span>
                <div class="field-error" id="err-target"></div>
              </div>
              <div class="field">Time limit (optional)
                <div class="duration">
                  <label class="dur"><input type="number" id="f-hours" min="0" max="24" step="1" value="${t.hours}" data-field="hours"><span>hrs</span></label>
                  <label class="dur"><input type="number" id="f-minutes" min="0" max="59" step="1" value="${t.minutes}" data-field="minutes"><span>min</span></label>
                </div>
                <span class="hint">Leave at 0 to trade until you stop it, or your goal or loss limit is hit.</span>
                <div class="field-error" id="err-duration"></div>
              </div>
            </div>
            <div class="summary" id="summary"></div>
            <div class="field-error" id="start-error" style="margin-top:10px"></div>
            <button class="btn big" id="start-btn" style="margin-top:14px" data-action="start-trading">Start trading</button>
            <span class="hint">Demo mode: trades are simulated and no real money moves.</span>
          </div>
        </div>
      </div>
    </div>`;

  drawChart($('#chart'), c.history);
  updateSummary();
}

let startBusy = false;
function setStartError(html) { const el = $('#start-error'); if (el) el.innerHTML = html; }

/* Recomputed on every input change; does not re-render the page, so inputs keep focus. */
function updateSummary() {
  const el = $('#summary');
  if (!el) return;
  const errs = validateTrade(), bad = Object.keys(errs).length > 0;
  for (const k of ['amount', 'target', 'duration']) setText('err-' + k, errs[k] || '');
  const btn = $('#start-btn');
  if (btn) { btn.disabled = bad || startBusy; btn.textContent = startBusy ? 'Starting…' : 'Start trading'; }
  if (bad) { el.innerHTML = '<div class="muted">Fix the highlighted fields to see your summary.</div>'; return; }

  const d = derived();
  // Difficulty is judged on the whole trading pool (placeholder thresholds), without showing the pool.
  const need = d.goalC / (market.houseFundCents + d.depositC);
  const [label, cls] = need < 0.0025 ? ['Low', 'good'] : need < 0.01 ? ['Moderate', ''] :
    need < 0.03 ? ['High', 'warn'] : ['Very high', 'bad'];
  el.innerHTML = `
    <div class="line"><span>Your investment</span><strong>${moneyC(d.depositC)}</strong></div>
    <div class="line"><span>Stops if it falls to</span><strong>${moneyC(d.floorC)} <span class="muted small">(−${moneyC(d.lossC)})</span></strong></div>
    <div class="line"><span>Stops if it grows to</span><strong>${moneyC(d.ceilingC)} <span class="muted small">(+${moneyC(d.goalC)})</span></strong></div>
    <div class="line"><span>Time limit</span><strong>${d.durationMs ? fmtDuration(d.durationMs) : 'None'}</strong></div>
    <div class="line"><span>Goal difficulty</span><strong class="${cls}">${label}</strong></div>
    <div class="line"><span>Best case: you pay</span><strong class="good">${moneyC(Math.max(0, d.orderC - d.goalC))}</strong></div>
    <div class="line"><span>Worst case: you pay</span><strong class="bad">${moneyC(d.orderC + d.lossC)}</strong></div>
    ${label === 'Very high' ? '<div class="small bad" style="margin-top:6px">A goal this big is a long shot. Try a smaller goal or a larger investment.</div>' : ''}`;
}

function renderCheckout() {
  const items = cartItems();
  if (!items.length) { location.hash = '#/'; return; }
  const orderC = cartTotalC(), net = netC(), due = dueC();
  app.innerHTML = `
    <div class="page">
      <h1>Final payment</h1>
      <div class="two-col">
        <div class="card">
          <h3>Order summary</h3>
          ${items.map(i => `
            <div class="row">
              <div class="thumb">${i.emoji}</div>
              <div class="info"><strong>${i.name}</strong><br><span class="muted small">Qty ${i.qty}</span></div>
              <strong>${money(i.price * i.qty)}</strong>
            </div>`).join('')}
          <div class="totals"><span>Order total</span><span>${moneyC(orderC)}</span></div>
          <h3 style="margin-top:22px">Trading results</h3>
          ${state.runs.length ? state.runs.map(runRowHTML).join('') : '<div class="muted small">No trades were made.</div>'}
        </div>
        <div class="card stack">
          <div class="summary" style="margin-top:0">
            <div class="line"><span>Order total</span><strong>${moneyC(orderC)}</strong></div>
            <div class="line"><span>${net >= 0 ? 'Trading profit' : 'Trading loss'}</span><strong class="${net >= 0 ? 'good' : 'bad'}">${net >= 0 ? '−' : '+'}${moneyC(Math.abs(net))}</strong></div>
            <div class="line total"><span>You pay</span><strong>${moneyC(due)}</strong></div>
          </div>
          ${net > orderC ? '<div class="small muted">Profit beyond your order total is not paid out.</div>' : ''}
          <button class="btn big" data-action="pay-final">Pay ${moneyC(due)}</button>
          <div class="muted small">Trading has ended for this order.</div>
        </div>
      </div>
    </div>`;
}

function completeOrder(paidC) {
  state = defaultState(); save(); updateCartCount();
  app.innerHTML = `<div class="page"><div class="card"><h2>Order placed ✅</h2>
    <p class="muted">You paid ${moneyC(paidC)}. (Placeholder confirmation.)</p><a class="btn" href="#/">Keep shopping</a></div></div>`;
}

/* ============================================================
   CHART (plain canvas, no libraries)
   ============================================================ */
function drawChart(canvas, data) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const pad = { l: 44, r: 12, t: 10, b: 22 };
  const min = Math.min(...data), max = Math.max(...data);
  const range = (max - min) || 1;
  const x = i => pad.l + (i / (data.length - 1)) * (w - pad.l - pad.r);
  const y = v => pad.t + (1 - (v - min) / range) * (h - pad.t - pad.b);

  ctx.font = '11px system-ui'; ctx.fillStyle = '#6b7280'; ctx.strokeStyle = '#eceef3';
  for (let i = 0; i <= 4; i++) {
    const v = min + (range * i) / 4, yy = y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy); ctx.stroke();
    ctx.fillText('$' + v.toFixed(0), 6, yy + 4);
  }
  ctx.fillText('90d ago', pad.l, h - 6);
  ctx.fillText('today', w - pad.r - 30, h - 6);

  if (min < 100 && max > 100) {
    ctx.setLineDash([4, 4]); ctx.strokeStyle = '#c4c8d2';
    ctx.beginPath(); ctx.moveTo(pad.l, y(100)); ctx.lineTo(w - pad.r, y(100)); ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.beginPath(); ctx.moveTo(x(0), y(data[0]));
  data.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(data.length - 1), h - pad.b); ctx.lineTo(x(0), h - pad.b); ctx.closePath();
  ctx.fillStyle = 'rgba(108,76,241,0.10)'; ctx.fill();

  ctx.beginPath();
  data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = '#6c4cf1'; ctx.lineWidth = 2; ctx.stroke();
}

/* ============================================================
   MODALS
   ============================================================ */
let modal = null; // 'live' | 'confirm' | null
function openModal(html, cls = '') {
  modalRoot.innerHTML = `<div class="overlay"><div class="modal ${cls}" role="dialog" aria-modal="true">${html}</div></div>`;
  document.body.classList.add('modal-open');
}
function closeModal() {
  modalRoot.innerHTML = '';
  document.body.classList.remove('modal-open');
  modal = null;
}

function openEndConfirm() {
  modal = 'confirm';
  const orderC = cartTotalC(), net = netC(), due = dueC();
  openModal(`
    <h2>End session and pay?</h2>
    <p class="muted">This is your final payment. All trading stops, and you won't be able to trade again after you continue.</p>
    <div class="summary">
      <div class="line"><span>Order total</span><strong>${moneyC(orderC)}</strong></div>
      <div class="line"><span>Trading sessions</span><strong>${state.runs.length}</strong></div>
      <div class="line"><span>${net >= 0 ? 'Profit from trading' : 'Lost while trading'}</span><strong class="${net >= 0 ? 'good' : 'bad'}">${signedC(net)}</strong></div>
      <div class="line total"><span>You pay</span><strong>${moneyC(due)}</strong></div>
    </div>
    ${net < 0 ? `<p class="small bad">Your ${moneyC(-net)} of losses is added to your total.</p>` : ''}
    ${net > 0 ? `<p class="small good">You saved ${moneyC(Math.min(net, orderC))} on this order.</p>` : ''}
    <div class="modal-actions">
      <button class="btn secondary" data-action="cancel-end">Keep trading</button>
      <button class="btn danger" data-action="confirm-end">End session &amp; pay</button>
    </div>`, 'narrow');
}

/* ============================================================
   LIVE TRADING SESSION (popup) -- placeholder shell, real API wiring
   ============================================================ */
const live = {
  ws: null, session: null, finished: null, connected: false,
  stopping: false, stopWhy: null,
  timer: null, pingTimer: null, reconnectTimer: null, lastMsg: 0, actionsKey: null,
};

async function startTrading() {
  if (state.active || startBusy) return;
  if (Object.keys(validateTrade()).length) return;
  const d = derived(), t = state.trade;
  startBusy = true; setStartError(''); updateSummary();
  try {
    const s = await api('/v1/session', {
      method: 'POST',
      body: JSON.stringify({ depositCents: d.depositC, instrument: t.symbol, floorCents: d.floorC, ceilingCents: d.ceilingC }),
    });
    state.active = {
      sessionId: s.sessionId, symbol: s.instrument, depositCents: s.depositCents,
      floorCents: s.floorCents, ceilingCents: s.ceilingCents,
      startedAt: Date.now(), durationMs: d.durationMs,
    };
    save();
    live.session = s; live.finished = null; live.stopping = false; live.stopWhy = null;
    openLiveModal(); connectWatch(); scheduleDuration();
  } catch (e) {
    if (e.code === 'offline') setStartError(`Can't reach the trading server at ${esc(API_BASE)}. Make sure it's running, then try again.`);
    else if (e.code === 'session_running') setStartError('A trading session is already running on the server. <button class="link-btn" data-action="force-stop">Stop it and try again</button>');
    else setStartError(esc(e.message));
  } finally {
    startBusy = false; updateSummary();
  }
}

function resumeLive() {
  if (!state.active) return;
  live.session = null; live.finished = null; live.stopping = false; live.stopWhy = null;
  openLiveModal(); connectWatch(); scheduleDuration();
}

function openLiveModal() {
  const a = state.active;
  if (!a) return;
  modal = 'live'; live.actionsKey = null;
  const base = a.symbol.split('-')[0];
  openModal(`
    <div class="modal-head">
      <div><h2 style="margin:0">Live trading · ${esc(base)}</h2><div class="muted small">Demo: trades are simulated</div></div>
      <span id="lv-status" class="pill">Connecting…</span>
    </div>
    <div class="live-stats">
      <div class="stat"><div class="label">Your investment now</div><div class="value" id="lv-stake"></div></div>
      <div class="stat"><div class="label">Result so far</div><div class="value" id="lv-pnl"></div></div>
    </div>
    <div class="summary">
      <div class="line"><span>Started with</span><strong>${moneyC(a.depositCents)}</strong></div>
      <div class="line"><span>Stops if it falls to</span><strong>${moneyC(a.floorCents)}</strong></div>
      <div class="line"><span>Stops if it grows to</span><strong>${moneyC(a.ceilingCents)}</strong></div>
      <div class="line"><span>Time</span><strong id="lv-time"></strong></div>
    </div>
    <div class="live-graph-placeholder">Live graph goes here (next step)</div>
    <div class="small muted" id="lv-note"></div>
    <div class="field-error" id="lv-error"></div>
    <div class="modal-actions" id="lv-actions"></div>`);
  updateLive();
}

function updateLive() {
  if (modal !== 'live') return;
  const a = state.active, fin = live.finished, s = live.session;
  const dep = fin ? fin.depositCents : a ? a.depositCents : 0;
  const stakeC = fin ? fin.stakeCents : s ? s.stakeCents : dep;
  const pnlC = stakeC - dep;
  setText('lv-stake', moneyC(stakeC));
  const pnlEl = document.getElementById('lv-pnl');
  if (pnlEl) { pnlEl.textContent = signedC(pnlC); pnlEl.className = 'value ' + (pnlC > 0 ? 'good' : pnlC < 0 ? 'bad' : ''); }

  let label, cls = '';
  if (fin) {
    label = REASONS[fin.reason] || 'Ended';
    cls = fin.reason === 'ceiling' ? 'good' : (fin.reason === 'floor' || fin.reason === 'lost') ? 'bad' : 'warn';
  } else if (live.stopping) label = 'Stopping…';
  else if (live.connected) { label = 'Live'; cls = 'good'; }
  else label = 'Connecting…';
  setText('lv-status', label);
  const st = document.getElementById('lv-status');
  if (st) st.className = 'pill ' + cls;

  let timeTxt;
  if (fin) timeTxt = fmtClock(fin.endedAt - fin.startedAt) + ' elapsed';
  else if (a.durationMs) timeTxt = fmtClock(a.startedAt + a.durationMs - Date.now()) + ' left';
  else timeTxt = fmtClock(Date.now() - a.startedAt) + ' · no time limit';
  setText('lv-time', timeTxt);
  setText('lv-note', fin ? (REASON_NOTES[fin.reason] || '') : '');

  // Only rebuild the buttons when their state changes, so clicks are never lost to a refresh.
  const key = fin ? 'fin' : live.stopping ? 'stopping' : 'run';
  if (live.actionsKey !== key) {
    live.actionsKey = key;
    const box = document.getElementById('lv-actions');
    if (box) box.innerHTML = key === 'fin'
      ? '<button class="btn" data-action="close-live">Close</button>'
      : key === 'stopping'
        ? '<button class="btn danger" disabled>Stopping…</button>'
        : '<button class="btn danger" data-action="end-trading">End trading session</button>';
  }
}
function setLiveError(msg) { setText('lv-error', msg || ''); }

function connectWatch() {
  closeWatch();
  let ws;
  try { ws = new WebSocket(WS_BASE + '/v1/watch'); }
  catch { live.reconnectTimer = setTimeout(connectWatch, 1500); return; }
  live.ws = ws; live.lastMsg = Date.now();
  ws.onopen = () => { live.connected = true; live.lastMsg = Date.now(); updateLive(); };
  ws.onmessage = ev => {
    live.lastMsg = Date.now();
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if ((m.type === 'snapshot' || m.type === 'halt') && m.session) onSession(m.session);
  };
  ws.onclose = () => {
    if (live.ws !== ws) return;
    live.connected = false; updateLive();
    if (state.active) live.reconnectTimer = setTimeout(connectWatch, 1500);
  };
  ws.onerror = () => {};
  live.pingTimer = setInterval(() => {
    const w = live.ws;
    if (!w || w.readyState !== 1) return;
    if (Date.now() - live.lastMsg > 6000) { w.close(); return; } // silent socket: reconnect
    try { w.send(JSON.stringify({ op: 'ping' })); } catch {}
  }, 3000);
}
function closeWatch() {
  clearInterval(live.pingTimer); clearTimeout(live.reconnectTimer);
  const ws = live.ws; live.ws = null; live.connected = false;
  if (ws) { ws.onclose = null; ws.onmessage = null; try { ws.close(); } catch {} }
}

function onSession(s) {
  const a = state.active;
  if (!a) return;
  if (s.status === 'idle' || s.sessionId !== a.sessionId) { lostSession(); return; }
  live.session = s;
  if (s.status === 'halted') finalizeRun(s); else updateLive();
}

function recordRun(run) {
  state.runs.push(run); state.active = null; save();
  clearTimeout(live.timer); closeWatch();
  live.finished = run;
}

// Called when the server reports a halted session (floor, ceiling, or our own stop).
function finalizeRun(s) {
  const a = state.active;
  if (!a || s.sessionId !== a.sessionId) return false;
  const halt = s.halt || {};
  let reason = halt.reason || 'manual';
  if (reason === 'manual' && live.stopWhy === 'time') reason = 'time';
  const userStopped = reason === 'manual' && live.stopping;
  recordRun({
    sessionId: s.sessionId, symbol: s.instrument, depositCents: s.depositCents,
    stakeCents: halt.stakeCents ?? s.stakeCents, pnlCents: halt.poolPnlCents ?? s.poolPnlCents,
    reason, startedAt: a.startedAt, endedAt: Date.now(),
  });
  live.session = s;
  if (userStopped) closeLive(); else { live.stopping = false; updateLive(); }
  return true;
}

// The server no longer knows our session (restart, replaced): use the last known balance.
function lostSession() {
  const a = state.active;
  if (!a) return;
  const s = live.session;
  recordRun({
    sessionId: a.sessionId, symbol: a.symbol, depositCents: a.depositCents,
    stakeCents: s ? s.stakeCents : a.depositCents, pnlCents: s ? s.poolPnlCents : 0,
    reason: 'lost', startedAt: a.startedAt, endedAt: Date.now(),
  });
  live.stopping = false; updateLive();
}

async function endTrading(why = 'manual') {
  if (!state.active || live.stopping) return;
  live.stopping = true; live.stopWhy = why; setLiveError(''); updateLive();
  try {
    finalizeRun(await api('/v1/stop', { method: 'POST' }));
  } catch (e) {
    live.stopping = false; live.stopWhy = null;
    if (e.code === 'no_session') { lostSession(); return; }
    setLiveError(e.code === 'offline' ? "Can't reach the trading server. Check that it's running and try again." : e.message);
    updateLive();
    if (why === 'time') live.timer = setTimeout(() => endTrading('time'), 3000);
  }
}

// The server has no duration option, so the page stops the session when time is up.
function scheduleDuration() {
  clearTimeout(live.timer);
  const a = state.active;
  if (!a || !a.durationMs) return;
  live.timer = setTimeout(() => endTrading('time'), Math.max(0, a.startedAt + a.durationMs - Date.now()));
}

function closeLive() {
  closeWatch(); clearTimeout(live.timer);
  live.finished = null; live.session = null; live.stopping = false; live.stopWhy = null; live.actionsKey = null;
  closeModal();
  if (currentPath() === '/trading') renderTrading();
}

/* ============================================================
   EVENTS
   ============================================================ */
const actions = {
  add: id => { state.cart[id] = (state.cart[id] || 0) + 1; save(); updateCartCount(); },
  inc: id => { state.cart[id]++; save(); route(); },
  dec: id => { state.cart[id] = Math.max(0, state.cart[id] - 1); save(); route(); },
  remove: id => { delete state.cart[id]; save(); route(); },
  'pay-full': () => completeOrder(cartTotalC()),
  activate: () => { location.hash = '#/trading'; },
  'select-crypto': id => { state.trade.symbol = id; save(); renderTrading(); },
  'set-pct': p => {
    const c = Math.min(Math.max(Math.round(cartTotalC() * Number(p) / 100), minInvestC()), maxInvestC());
    state.trade.amount = c / 100; save();
    const inp = $('#f-amount'); if (inp) inp.value = state.trade.amount;
    updateSummary();
  },
  'start-trading': () => startTrading(),
  'force-stop': async () => {
    try { await api('/v1/stop', { method: 'POST' }); setStartError('Stopped the old session. Press Start trading again.'); }
    catch (e) { setStartError(esc(e.message)); }
  },
  'end-trading': () => endTrading('manual'),
  'close-live': () => closeLive(),
  'end-session': () => { if (!state.active) openEndConfirm(); },
  'cancel-end': () => closeModal(),
  'confirm-end': () => {
    if (state.active) return;
    state.ended = true; save(); closeModal(); location.hash = '#/checkout';
    route();
  },
  'pay-final': () => completeOrder(dueC()),
};

document.addEventListener('click', e => {
  if (modal === 'confirm' && e.target.classList && e.target.classList.contains('overlay')) { closeModal(); return; }
  const el = e.target.closest('[data-action]');
  if (el && actions[el.dataset.action]) actions[el.dataset.action](el.dataset.id);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && modal === 'confirm') closeModal(); });

document.addEventListener('input', e => {
  const field = e.target.dataset.field;
  if (!field) return;
  const raw = e.target.value, t = state.trade;
  if (field === 'amount') t.amount = raw === '' ? NaN : Number(raw);
  else if (field === 'target') t.target = raw === '' ? NaN : Number(raw);
  else if (field === 'maxLoss') { t.maxLoss = Number(raw); setText('v-maxLoss', t.maxLoss + '%'); }
  else if (field === 'hours') t.hours = Number(raw || 0);
  else if (field === 'minutes') t.minutes = Number(raw || 0);
  save();
  updateSummary();
});

/* ============================================================
   ROUTER
   ============================================================ */
function updateCartCount() { $('#cart-count').textContent = cartCount(); }

const routes = { '/': renderHome, '/cart': renderCart, '/payment': renderPayment, '/trading': renderTrading, '/checkout': renderCheckout };

// Once trading starts the order is locked to the trading page; once ended, to final payment.
function guard(path) {
  if (state.ended) return '/checkout';
  if (path === '/checkout') return '/payment';
  if ((state.runs.length || state.active) && path !== '/trading') return '/trading';
  return path;
}
function route() {
  const raw = currentPath(), path = guard(raw);
  if (path !== raw) { location.replace('#' + path); return; }
  (routes[path] || renderHome)();
  updateCartCount();
}
window.addEventListener('hashchange', () => { route(); window.scrollTo(0, 0); });
window.addEventListener('resize', () => {
  const canvas = $('#chart');
  if (canvas) drawChart(canvas, selectedCoin().history);
});

route();
checkHealth();
setInterval(checkHealth, 10000);
if (state.active) resumeLive();
