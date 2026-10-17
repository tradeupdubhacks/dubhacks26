/* ============================================================
   DATA (all placeholder -- swap for real data later)
   ============================================================ */
const PRODUCTS = [
  { id: 'p1', name: 'Wireless Headphones', price: 79.99, emoji: '🎧', desc: 'Noise-cancelling, 30h battery.' },
  { id: 'p2', name: 'Smartwatch',          price: 149.00, emoji: '⌚', desc: 'Fitness tracking and notifications.' },
  { id: 'p3', name: 'Travel Backpack',     price: 54.50,  emoji: '🎒', desc: '25L, water resistant.' },
];

// vol = daily volatility used to fake history; replace with real backtest output.
const CRYPTOS = [
  { id: 'SOL',  name: 'Solana',   seed: 11, drift: 0.0010, vol: 0.040, liquidity: 'Very high',
    notes: ['Large, liquid market: low slippage', 'Moves with overall crypto sentiment', 'Network outages have caused sharp drops'] },
  { id: 'BTC',  name: 'Bitcoin',  seed: 22, drift: 0.0008, vol: 0.025, liquidity: 'Very high',
    notes: ['Most stable of the group', 'Tends to lead the rest of the market', 'Smaller percentage swings, so smaller upside'] },
  { id: 'ETH',  name: 'Ethereum', seed: 33, drift: 0.0009, vol: 0.032, liquidity: 'Very high',
    notes: ['Large and liquid', 'Moves closely with Bitcoin', 'Sensitive to network fee and upgrade news'] },
  { id: 'JUP',  name: 'Jupiter',  seed: 44, drift: 0.0010, vol: 0.060, liquidity: 'High',
    notes: ['Solana ecosystem token: tied to Solana activity', 'Bigger swings than SOL', 'Governed by token unlock schedules'] },
  { id: 'BONK', name: 'Bonk',     seed: 55, drift: 0.0005, vol: 0.090, liquidity: 'Medium',
    notes: ['Meme coin: price driven mostly by hype', 'Highest volatility in this list', 'Thinner liquidity can mean more slippage'] },
];

const HISTORY_DAYS = 90;
const MAX_INVEST_PCT = 0.30; // cap on how much of the order can be invested

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
function buildCrypto(c) {
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
CRYPTOS.forEach(buildCrypto);
const MAX_VOL = Math.max(...CRYPTOS.map(c => c.stats.vol));
const volLevel = c => {
  const ratio = c.stats.vol / MAX_VOL;
  return ratio < 0.45 ? 'low' : ratio < 0.8 ? 'medium' : 'high';
};

/* ============================================================
   STATE (persisted in localStorage)
   ============================================================ */
const defaultState = () => ({
  cart: {}, // productId -> qty
  trade: { crypto: 'SOL', amount: null, maxLoss: 20, target: 5, days: 14, risk: 'medium' },
});
let state = (() => {
  try { return Object.assign(defaultState(), JSON.parse(localStorage.getItem('state'))); }
  catch { return defaultState(); }
})();
const save = () => localStorage.setItem('state', JSON.stringify(state));

const money = n => '$' + n.toFixed(2);
const pct = (n, d = 1) => (n * 100).toFixed(d) + '%';
const cartItems = () => Object.entries(state.cart)
  .map(([id, qty]) => ({ ...PRODUCTS.find(p => p.id === id), qty }))
  .filter(i => i.qty > 0);
const cartTotal = () => cartItems().reduce((s, i) => s + i.price * i.qty, 0);
const cartCount = () => cartItems().reduce((s, i) => s + i.qty, 0);
const $ = sel => document.querySelector(sel);
const app = $('#app');

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
          <div class="totals"><span>Subtotal (${cartCount()} items)</span><span>${money(cartTotal())}</span></div>
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
          <div class="totals"><span>Total</span><span>${money(cartTotal())}</span></div>
        </div>
        <div class="card stack">
          <button class="btn big secondary" data-action="pay-full">Pay full price · ${money(cartTotal())}</button>
          <div class="divider">or</div>
          <button class="btn big discount" data-action="activate">✨ Activate discount</button>
          <div class="muted small">Put a small part of your payment into automated trading. If it grows, you pay less. You choose the crypto, the amount, and the loss limit.</div>
        </div>
      </div>
    </div>`;
}

function ensureTradeDefaults() {
  const total = cartTotal();
  const t = state.trade;
  const max = Math.floor(total * MAX_INVEST_PCT);
  if (t.amount == null) t.amount = Math.max(1, Math.round(total * 0.10));
  t.amount = Math.min(Math.max(1, t.amount), Math.max(1, max));
}

function renderTrading() {
  const total = cartTotal();
  if (!total) { location.hash = '#/'; return; }
  ensureTradeDefaults();
  const t = state.trade;
  const c = CRYPTOS.find(x => x.id === t.crypto);
  const maxInvest = Math.floor(total * MAX_INVEST_PCT);

  app.innerHTML = `
    <div class="page">
      <a href="#/payment" class="muted small">← Back to payment</a>
      <div class="trade-layout" style="margin-top:12px">

        <!-- LEFT COLUMN -->
        <div class="card">
          <h3>Select crypto</h3>
          <div class="crypto-list">
            ${CRYPTOS.map(x => `
              <button class="crypto-item ${x.id === t.crypto ? 'active' : ''}" data-action="select-crypto" data-id="${x.id}">
                <span><strong>${x.id}</strong> <span class="muted small">${x.name}</span></span>
                <span class="tag ${volLevel(x)}">${volLevel(x)} vol</span>
              </button>`).join('')}
          </div>
          <label class="field">Initial investment
            <div class="input-prefix"><span>$</span>
              <input type="number" id="f-amount" data-field="amount" min="1" max="${maxInvest}" step="1" value="${t.amount}">
            </div>
            <span class="hint">Max ${money(maxInvest)} (${pct(MAX_INVEST_PCT, 0)} of your ${money(total)} order)</span>
          </label>
        </div>

        <!-- RIGHT COLUMN -->
        <div class="stack">
          <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <h2 style="margin:0">${c.name} (${c.id})</h2>
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
              <div class="stat"><div class="label">Liquidity</div><div class="value">${c.liquidity}</div></div>
            </div>

            <strong class="small">Volatility vs. other cryptos</strong>
            ${CRYPTOS.map(x => `
              <div class="vol-row ${x.id === c.id ? 'active' : ''}">
                <span>${x.id}</span>
                <div class="vol-bar"><div style="width:${(x.stats.vol / MAX_VOL) * 100}%"></div></div>
                <span>${pct(x.stats.vol)}</span>
              </div>`).join('')}

            <strong class="small" style="display:block;margin-top:14px">Things to know</strong>
            <ul class="factors">${c.notes.map(n => `<li>${n}</li>`).join('')}</ul>
          </div>

          <div class="card">
            <h3>Trading parameters</h3>
            <div class="params">
              <label class="field">Max loss: <span id="v-maxLoss">${t.maxLoss}%</span> of investment
                <input type="range" min="5" max="50" step="5" value="${t.maxLoss}" data-field="maxLoss">
                <span class="hint">Trading stops automatically if you lose this much.</span>
              </label>
              <label class="field">Discount goal (% of order)
                <input type="number" min="1" max="30" step="1" value="${t.target}" data-field="target">
                <span class="hint">Trading stops once profit reaches this.</span>
              </label>
              <label class="field">Duration
                <select data-field="days">
                  ${[7, 14, 30].map(d => `<option value="${d}" ${t.days === d ? 'selected' : ''}>${d} days</option>`).join('')}
                </select>
                <span class="hint">Trading stops after this long.</span>
              </label>
              <div class="field">Risk level
                <div class="segmented">
                  ${['low', 'medium', 'high'].map(r => `
                    <button class="${t.risk === r ? 'active' : ''}" data-action="set-risk" data-id="${r}">${r[0].toUpperCase() + r.slice(1)}</button>`).join('')}
                </div>
                <span class="hint">Higher risk: bigger possible gains and losses.</span>
              </div>
            </div>
            <div class="summary" id="summary"></div>
            <button class="btn big" style="margin-top:14px" data-action="start-trading">Start trading</button>
          </div>
        </div>
      </div>
    </div>`;

  drawChart($('#chart'), c.history);
  updateSummary();
}

/* Recomputed on every input change; does not re-render the page, so inputs keep focus. */
function updateSummary() {
  const el = $('#summary');
  if (!el) return;
  const total = cartTotal();
  const t = state.trade;
  const maxLossAmt = t.amount * t.maxLoss / 100;
  const goalAmt = total * t.target / 100;
  const requiredReturn = goalAmt / t.amount;
  const unrealistic = requiredReturn > 0.25;
  el.innerHTML = `
    <div class="line"><span>Invested</span><strong>${money(t.amount)}</strong></div>
    <div class="line"><span>Stops if loss reaches</span><strong>${money(maxLossAmt)}</strong></div>
    <div class="line"><span>Discount goal</span><strong>${money(goalAmt)}</strong></div>
    <div class="line"><span>Return needed to hit goal</span>
      <strong class="${unrealistic ? 'warn' : ''}">${pct(requiredReturn, 0)}</strong></div>
    <div class="line"><span>Best case: you pay</span><strong class="good">${money(total - goalAmt)}</strong></div>
    <div class="line"><span>Worst case: you pay</span><strong class="bad">${money(total + maxLossAmt)}</strong></div>
    ${unrealistic ? '<div class="small warn" style="margin-top:6px">That goal needs a very high return for this investment. Try a larger investment or a smaller goal.</div>' : ''}`;
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

  // gridlines + y labels
  ctx.font = '11px system-ui'; ctx.fillStyle = '#6b7280'; ctx.strokeStyle = '#eceef3';
  for (let i = 0; i <= 4; i++) {
    const v = min + (range * i) / 4, yy = y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy); ctx.stroke();
    ctx.fillText('$' + v.toFixed(0), 6, yy + 4);
  }
  ctx.fillText('90d ago', pad.l, h - 6);
  ctx.fillText('today', w - pad.r - 30, h - 6);

  // baseline at $100
  if (min < 100 && max > 100) {
    ctx.setLineDash([4, 4]); ctx.strokeStyle = '#c4c8d2';
    ctx.beginPath(); ctx.moveTo(pad.l, y(100)); ctx.lineTo(w - pad.r, y(100)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // area fill
  ctx.beginPath(); ctx.moveTo(x(0), y(data[0]));
  data.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(data.length - 1), h - pad.b); ctx.lineTo(x(0), h - pad.b); ctx.closePath();
  ctx.fillStyle = 'rgba(108,76,241,0.10)'; ctx.fill();

  // line
  ctx.beginPath();
  data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = '#6c4cf1'; ctx.lineWidth = 2; ctx.stroke();
}

/* ============================================================
   EVENTS
   ============================================================ */
const actions = {
  add: id => { state.cart[id] = (state.cart[id] || 0) + 1; save(); updateCartCount(); },
  inc: id => { state.cart[id]++; save(); route(); },
  dec: id => { state.cart[id] = Math.max(0, state.cart[id] - 1); save(); route(); },
  remove: id => { delete state.cart[id]; save(); route(); },
  'pay-full': () => {
    state.cart = {}; save(); updateCartCount();
    app.innerHTML = `<div class="page"><div class="card"><h2>Order placed ✅</h2>
      <p class="muted">Paid in full. (Placeholder confirmation.)</p><a class="btn" href="#/">Keep shopping</a></div></div>`;
  },
  activate: () => { location.hash = '#/trading'; },
  'select-crypto': id => { state.trade.crypto = id; save(); renderTrading(); },
  'set-risk': id => { state.trade.risk = id; save(); renderTrading(); },
  'start-trading': () => {
    save();
    console.log('Trade config:', state.trade, 'Order total:', cartTotal());
    alert('Trading screen is next. Config logged to the console.');
  },
};

document.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (el && actions[el.dataset.action]) actions[el.dataset.action](el.dataset.id);
});

document.addEventListener('input', e => {
  const field = e.target.dataset.field;
  if (!field) return;
  const val = e.target.type === 'number' || e.target.type === 'range' || field === 'days'
    ? Number(e.target.value) : e.target.value;
  state.trade[field] = val;
  if (field === 'maxLoss') $('#v-maxLoss').textContent = val + '%';
  // clamp the investment amount to the allowed range
  if (field === 'amount') {
    const max = Math.floor(cartTotal() * MAX_INVEST_PCT);
    if (val > max) { state.trade.amount = max; e.target.value = max; }
  }
  save();
  updateSummary();
});

/* ============================================================
   ROUTER
   ============================================================ */
function updateCartCount() { $('#cart-count').textContent = cartCount(); }

const routes = { '/': renderHome, '/cart': renderCart, '/payment': renderPayment, '/trading': renderTrading };
function route() {
  const path = location.hash.slice(1) || '/';
  (routes[path] || renderHome)();
  updateCartCount();
}
window.addEventListener('hashchange', () => { route(); window.scrollTo(0, 0); });
window.addEventListener('resize', () => {
  const canvas = $('#chart');
  if (canvas) drawChart(canvas, CRYPTOS.find(x => x.id === state.trade.crypto).history);
});
route();
