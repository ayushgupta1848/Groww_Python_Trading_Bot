#!/usr/bin/env python3
"""
TRADE_CONTROL_PANEL.py
======================
Standalone EMERGENCY trade-control page — deliberately independent of
LIVE_DASHBOARD and all trading bots, so it keeps working even when a bot
crashes mid-trade (e.g. the 2026-08-04 incident: BUY executed, avg-price
fetch failed, bot abandoned the position and F&O was locked in the Groww UI).

Talks straight to api.groww.in using the shared cached token (groww_token.py
/ .groww_token.json). No SDK required at runtime for API calls.

Features
--------
  • Live open FNO positions (net qty, avg, LTP, P&L) — auto-refresh
  • One-click EXIT (market, opposite side, correct product/exchange)
  • EXIT ALL panic button
  • Today's order list + cancel pending orders
  • Manual BUY / SELL (market or limit)
  • 📋 curl icons everywhere — copies a ready-to-run curl with the LATEST
    token for every operation (positions / orders / ltp / status / trades /
    buy / sell / exit / cancel)

Run:   python3 TRADE_CONTROL_PANEL.py          (http://127.0.0.1:8790)
"""

import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("TCP_PORT", "8790"))
HOST = "127.0.0.1"          # local only — the page embeds live tokens in curls
API = "https://api.groww.in"

# ─────────────────────────────────────────────────────────────
#  Token handling — cached token shared with all bots
# ─────────────────────────────────────────────────────────────
_token_lock = threading.Lock()
_token_cache = {"token": "", "ts": 0.0}


def _load_creds():
    try:
        cfg = json.loads(open(os.path.join(BASE, "ai_config.json")).read())
        return cfg.get("groww_api_key", ""), cfg.get("groww_totp_secret", "")
    except Exception:
        return "", ""


def _read_cache_file():
    try:
        d = json.loads(open(os.path.join(BASE, ".groww_token.json")).read())
        return d.get("token") or d.get("access_token") or ""
    except Exception:
        return ""


def get_token(force=False) -> str:
    """Freshest token available: groww_token module (mints/refreshes via the
    shared cache) → fall back to reading .groww_token.json directly."""
    with _token_lock:
        if not force and _token_cache["token"] and time.time() - _token_cache["ts"] < 300:
            return _token_cache["token"]
        tok = ""
        try:
            sys.path.insert(0, BASE)
            from groww_token import get_access_token
            key, totp = _load_creds()
            if key and totp:
                tok = get_access_token(key, totp)
        except Exception:
            tok = ""
        if not tok:
            tok = _read_cache_file()
        if tok:
            _token_cache["token"] = tok
            _token_cache["ts"] = time.time()
        return _token_cache["token"]


def _jwt_exp(token: str):
    try:
        import base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
#  Groww API helpers
# ─────────────────────────────────────────────────────────────
def _hdr(token=None):
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token or get_token()}",
        "X-API-VERSION": "1.0",
    }


def _api_get(path, params=None, timeout=8):
    r = requests.get(f"{API}{path}", headers=_hdr(), params=params, timeout=timeout)
    if r.status_code == 401:            # stale cache — force refresh once
        r = requests.get(f"{API}{path}", headers=_hdr(get_token(force=True)),
                         params=params, timeout=timeout)
    return r.json()


def _api_post(path, body, timeout=8):
    h = {**_hdr(), "Content-Type": "application/json"}
    r = requests.post(f"{API}{path}", headers=h, json=body, timeout=timeout)
    if r.status_code == 401:
        h = {**_hdr(get_token(force=True)), "Content-Type": "application/json"}
        r = requests.post(f"{API}{path}", headers=h, json=body, timeout=timeout)
    return r.json()


def fetch_positions():
    """Open FNO positions with net qty. Groww semantics (observed):
    credit = bought, debit = sold, `quantity` = signed net (＋long / −short)."""
    d = _api_get("/v1/positions/user", {"segment": "FNO"})
    out = []
    for p in (d.get("payload") or {}).get("positions", []) or []:
        try:
            net = int(p.get("quantity") or 0)
        except Exception:
            net = 0
        if net == 0:
            continue
        out.append({
            "trading_symbol": p.get("trading_symbol") or p.get("symbol_isin"),
            "exchange": p.get("exchange", "NSE"),
            "product": p.get("product", "MIS"),
            "net_qty": net,
            "avg_price": float(p.get("net_price") or 0),
            "realised_pnl": float(p.get("realised_pnl") or 0),
        })
    return out


def fetch_ltp(exchange, trading_symbol):
    key = f"{exchange}_{trading_symbol}"
    try:
        d = _api_get("/v1/live-data/ltp", {"segment": "FNO", "exchange_symbols": key}, timeout=5)
        v = (d.get("payload") or {}).get(key)
        return float(v) if v else None
    except Exception:
        return None


def fetch_orders():
    d = _api_get("/v1/order/list", {"segment": "FNO", "page_size": 50})
    lst = (d.get("payload") or {}).get("order_list") or d.get("order_list") or []
    out = []
    for o in lst:
        out.append({
            "created_at": o.get("created_at", ""),
            "trading_symbol": o.get("trading_symbol", ""),
            "transaction_type": o.get("transaction_type", ""),
            "quantity": o.get("quantity", 0),
            "order_status": o.get("order_status", ""),
            "order_type": o.get("order_type", ""),
            "product": o.get("product", ""),
            "avg_price": o.get("average_price") or o.get("avg_price") or 0,
            "groww_order_id": o.get("groww_order_id", ""),
        })
    return out


def executed_avg(order_id, tries=(0, 0.3, 0.7, 1.2)):
    """Avg fill price + qty with retry (trades endpoint lags status) and
    order-status fallback — same hardening as the PROD10FEB fix."""
    for delay in tries:
        if delay:
            time.sleep(delay)
        try:
            d = _api_get(f"/v1/order/trades/{order_id}",
                         {"segment": "FNO", "page": 0, "page_size": 50}, timeout=5)
            trades = (d.get("payload") or {}).get("trade_list", [])
            if trades:
                q = sum(t["quantity"] for t in trades)
                v = sum(t["price"] * t["quantity"] for t in trades)
                return round(v / q, 2), q
        except Exception:
            pass
    try:
        p = (_api_get(f"/v1/order/status/{order_id}", {"segment": "FNO"}, timeout=5)
             .get("payload") or {})
        ap = p.get("average_price") or p.get("avg_price")
        q = p.get("filled_quantity") or p.get("quantity")
        if ap and q:
            return round(float(ap), 2), int(q)
    except Exception:
        pass
    return None, None


def place_order(body):
    """Place an order and wait briefly for terminal status. Returns dict."""
    body.setdefault("validity", "DAY")
    body.setdefault("segment", "FNO")
    body.setdefault("order_reference_id", "CTL" + datetime.now().strftime("%H%M%S%f")[:12])
    resp = _api_post("/v1/order/create", body)
    oid = (resp.get("payload") or {}).get("groww_order_id") or resp.get("groww_order_id")
    if not oid:
        return {"ok": False, "error": f"no order id in response: {json.dumps(resp)[:300]}"}
    status = "UNKNOWN"
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            p = (_api_get(f"/v1/order/status/{oid}", {"segment": "FNO"}, timeout=5)
                 .get("payload") or {})
            status = p.get("order_status", "UNKNOWN")
        except Exception:
            pass
        if status in ("EXECUTED", "COMPLETED", "DELIVERY_AWAITED",
                      "FAILED", "REJECTED", "CANCELLED"):
            break
        time.sleep(0.3)
    result = {"ok": status in ("EXECUTED", "COMPLETED", "DELIVERY_AWAITED"),
              "order_id": oid, "status": status}
    if result["ok"]:
        ap, q = executed_avg(oid)
        result["avg_price"], result["filled_qty"] = ap, q
    return result


def exit_position(trading_symbol, exchange, product, net_qty):
    side = "SELL" if net_qty > 0 else "BUY"
    return place_order({
        "trading_symbol": trading_symbol,
        "quantity": abs(int(net_qty)),
        "exchange": exchange,
        "product": product,
        "order_type": "MARKET",
        "transaction_type": side,
    })


# ─────────────────────────────────────────────────────────────
#  HTML page
# ─────────────────────────────────────────────────────────────
PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>🛡 Trade Control</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--dim:#8b949e;
--grn:#3fb950;--red:#f85149;--yel:#d29922;--blu:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;padding:16px;max-width:1100px;margin:0 auto}
h1{font-size:18px;margin-bottom:4px} h2{font-size:14px;color:var(--blu);margin:0 0 8px}
.sub{color:var(--dim);font-size:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:14px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--dim);text-align:left;font-weight:600;padding:4px 8px;border-bottom:1px solid var(--bd)}
td{padding:6px 8px;border-bottom:1px solid #21262d;white-space:nowrap}
tr:last-child td{border-bottom:none}
.pos{color:var(--grn)} .neg{color:var(--red)} .dim{color:var(--dim)}
button{background:#21262d;color:var(--fg);border:1px solid var(--bd);border-radius:6px;
padding:5px 12px;font:inherit;cursor:pointer} button:hover{border-color:var(--blu)}
button:disabled{opacity:.4;cursor:wait}
.btn-exit{background:#3d1214;border-color:#6e2226;color:#ff7b72;font-weight:700}
.btn-exit:hover{background:#5a1a1e;border-color:var(--red)}
.btn-buy{background:#12321a;border-color:#1f6f34;color:#56d364}
.btn-sell{background:#3d1214;border-color:#6e2226;color:#ff7b72}
.btn-panic{background:var(--red);color:#fff;border:none;font-weight:800;padding:8px 18px;font-size:14px}
.curl{cursor:pointer;border:none;background:none;font-size:15px;padding:2px 4px}
.curl:hover{transform:scale(1.25)}
.badge{display:inline-block;border-radius:10px;padding:0 8px;font-size:11px;font-weight:700}
.b-ok{background:#12321a;color:#56d364} .b-bad{background:#3d1214;color:#ff7b72}
.b-mid{background:#3a2d12;color:#e3b341}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input,select{background:#0d1117;color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:5px 8px;font:inherit}
input{width:170px} input.num{width:90px}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#1f2937;
border:1px solid var(--bd);border-radius:8px;padding:10px 18px;display:none;max-width:90vw;z-index:9;font-size:13px}
.tblwrap{overflow-x:auto}
.hd{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.curlgrid{display:flex;gap:8px;flex-wrap:wrap}
</style></head><body>

<div class="hd"><div>
<h1>🛡 TRADE CONTROL PANEL</h1>
<div class="sub">emergency manual control — direct Groww API, independent of all bots</div>
</div>
<div class="row">
  <span id="tok" class="badge b-mid">token…</span>
  <button class="curl" title="copy raw access token" onclick="copyToken()">🔑</button>
  <button onclick="refresh(true)">⟳ refresh</button>
</div></div>

<div class="card">
<div class="hd"><h2>📌 OPEN POSITIONS <span id="posage" class="dim"></span></h2>
<button class="btn-panic" onclick="exitAll()">🚨 EXIT ALL</button></div>
<div class="tblwrap"><table id="postbl">
<thead><tr><th>symbol</th><th>prod</th><th>net qty</th><th>avg</th><th>LTP</th><th>open P&L</th><th></th><th></th></tr></thead>
<tbody></tbody></table></div>
<div id="posempty" class="dim" style="display:none;padding:6px">no open positions ✓</div>
</div>

<div class="card">
<h2>⚡ MANUAL ORDER</h2>
<div class="row">
<input id="m_sym" placeholder="NIFTY2680424700PE">
<input id="m_qty" class="num" placeholder="qty" value="75">
<select id="m_exch"><option>NSE</option><option>BSE</option></select>
<select id="m_prod"><option>MIS</option><option>NRML</option></select>
<select id="m_type" onchange="document.getElementById('m_px').style.display=this.value==='LIMIT'?'':'none'">
<option>MARKET</option><option>LIMIT</option></select>
<input id="m_px" class="num" placeholder="price" style="display:none">
<button class="btn-buy" onclick="manual('BUY')">BUY</button>
<button class="btn-sell" onclick="manual('SELL')">SELL</button>
<button class="curl" title="copy BUY curl" onclick="curlOrder('BUY')">📋B</button>
<button class="curl" title="copy SELL curl" onclick="curlOrder('SELL')">📋S</button>
</div></div>

<div class="card">
<h2>🧾 TODAY'S ORDERS</h2>
<div class="tblwrap"><table id="ordtbl">
<thead><tr><th>time</th><th>symbol</th><th>side</th><th>qty</th><th>type</th><th>status</th><th>avg</th><th></th><th></th></tr></thead>
<tbody></tbody></table></div>
</div>

<div class="card">
<h2>📋 CURL LIBRARY <span class="dim">(every copy embeds the latest token)</span></h2>
<div class="curlgrid">
<button onclick="curlSimple('positions')">📌 positions</button>
<button onclick="curlSimple('orders')">🧾 order list</button>
<button onclick="curlLtp()">💰 LTP</button>
<button onclick="curlStatus()">🕒 order status</button>
<button onclick="curlTrades()">📦 order trades</button>
<button onclick="curlCancel()">✖ cancel order</button>
<button onclick="curlCancelAll()">✖✖ cancel ALL pending</button>
<button onclick="curlOrder('BUY')">🟢 buy</button>
<button onclick="curlOrder('SELL')">🔴 sell</button>
<button onclick="curlExitPick()">🚪 exit position…</button>
<button onclick="curlExitAll()">🚨 exit ALL positions</button>
</div>
<div class="sub" style="margin-top:8px">LTP / status / trades / cancel prompt for symbol or order-id; buy/sell/exit use the manual-order form or live position data. "ALL" buttons copy one curl per position/order — paste the whole block into a terminal.</div>
</div>

<div id="toast"></div>
<script>
let STATE={positions:[],orders:[]};
const $=q=>document.querySelector(q);
function toast(m,ms){const t=$('#toast');t.textContent=m;t.style.display='block';
clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',ms||3500)}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function copy(text,label){try{await navigator.clipboard.writeText(text)}catch(e){
const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);
ta.select();document.execCommand('copy');ta.remove()}toast('📋 copied: '+label)}

async function freshToken(){const r=await fetch('/api/token');const d=await r.json();
if(!d.token){toast('❌ no token available');throw new Error('no token')}return d.token}
async function copyToken(){copy(await freshToken(),'access token')}

const H=t=>`-H "Accept: application/json" \\\n  -H "Authorization: Bearer ${t}" \\\n  -H "X-API-VERSION: 1.0"`;
async function curlSimple(kind){const t=await freshToken();
const u=kind==='positions'?'https://api.groww.in/v1/positions/user?segment=FNO'
:'https://api.groww.in/v1/order/list?segment=FNO&page_size=50';
copy(`curl -X GET "${u}" \\\n  ${H(t)}`,kind+' curl')}
async function curlLtp(){const sym=prompt('symbol (e.g. NIFTY2680424700PE)',STATE.positions[0]?.trading_symbol||$('#m_sym').value||'');if(!sym)return;
const ex=(STATE.positions.find(p=>p.trading_symbol===sym)||{}).exchange||$('#m_exch').value;const t=await freshToken();
copy(`curl -X GET "https://api.groww.in/v1/live-data/ltp?segment=FNO&exchange_symbols=${ex}_${sym}" \\\n  ${H(t)}`,'LTP curl')}
async function curlStatus(id){id=id||prompt('groww order id',STATE.orders[0]?.groww_order_id||'');if(!id)return;
const t=await freshToken();copy(`curl -X GET "https://api.groww.in/v1/order/status/${id}?segment=FNO" \\\n  ${H(t)}`,'status curl')}
async function curlTrades(){const id=prompt('groww order id',STATE.orders[0]?.groww_order_id||'');if(!id)return;
const t=await freshToken();copy(`curl -X GET "https://api.groww.in/v1/order/trades/${id}?segment=FNO&page=0&page_size=50" \\\n  ${H(t)}`,'trades curl')}
async function curlCancel(id){id=id||prompt('groww order id to cancel','');if(!id)return;
copy(cancelCurl(id,await freshToken()),'cancel curl')}

function orderBody(side,sym,qty,exch,prod,otype,px,refSuffix){
const b={trading_symbol:sym,quantity:+qty,validity:"DAY",exchange:exch,segment:"FNO",
product:prod,order_type:otype,transaction_type:side,
order_reference_id:"CTL"+Date.now().toString().slice(-8)+(refSuffix||'')};
if(otype==='LIMIT')b.price=+px;return b}
function createCurl(body,t){return `curl -X POST "https://api.groww.in/v1/order/create" \\\n  ${H(t)} \\\n  -H "Content-Type: application/json" \\\n  -d '${JSON.stringify(body,null,2)}'`}
function cancelCurl(id,t){return `curl -X POST "https://api.groww.in/v1/order/cancel" \\\n  ${H(t)} \\\n  -H "Content-Type: application/json" \\\n  -d '{"segment":"FNO","groww_order_id":"${id}"}'`}
function exitCurlStr(p,t,i){return createCurl(orderBody(p.net_qty>0?'SELL':'BUY',p.trading_symbol,
Math.abs(p.net_qty),p.exchange,p.product,'MARKET',null,String(i||0)),t)}
async function curlBody(body,label){copy(createCurl(body,await freshToken()),label)}
function readForm(side){const sym=$('#m_sym').value.trim(),qty=$('#m_qty').value.trim();
if(!sym||!qty){toast('fill symbol + qty in MANUAL ORDER first');return null}
return orderBody(side,sym,qty,$('#m_exch').value,$('#m_prod').value,$('#m_type').value,$('#m_px').value)}
async function curlOrder(side){const b=readForm(side);if(b)await curlBody(b,side+' curl')}
async function curlExit(p){copy(exitCurlStr(p,await freshToken()),'exit curl '+p.trading_symbol)}
async function curlExitPick(){
if(!STATE.positions.length){toast('no open positions');return}
let p=STATE.positions[0];
if(STATE.positions.length>1){
const c=prompt('exit which position?\n'+STATE.positions.map((x,i)=>`${i+1}. ${x.trading_symbol}  (net ${x.net_qty}, ${x.product})`).join('\n'),'1');
if(!c)return;p=STATE.positions[(+c)-1];if(!p){toast('invalid choice');return}}
await curlExit(p)}
async function curlExitAll(){
if(!STATE.positions.length){toast('no open positions');return}
const t=await freshToken();
copy('# EXIT ALL '+STATE.positions.length+' open position(s) — market, opposite side\n\n'
+STATE.positions.map((p,i)=>exitCurlStr(p,t,i)).join('\n\n'),
`exit ALL curls (${STATE.positions.length} positions)`)}
const CANCELLABLE=/NEW|PENDING|OPEN|ACKED|APPROVED|TRIGGER/;
async function curlCancelAll(){
const ids=STATE.orders.filter(o=>CANCELLABLE.test(o.order_status)).map(o=>o.groww_order_id);
if(!ids.length){toast('no pending (cancellable) orders');return}
const t=await freshToken();
copy('# CANCEL ALL '+ids.length+' pending order(s)\n\n'+ids.map(id=>cancelCurl(id,t)).join('\n\n'),
`cancel ALL curls (${ids.length} orders)`)}

async function post(url,body){const r=await fetch(url,{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json()}
function report(r){if(r.ok)toast(`✅ ${r.status} ${r.order_id||''} ${r.avg_price?'@ ₹'+r.avg_price:''}`,6000);
else toast('❌ '+(r.error||r.status||JSON.stringify(r)).slice(0,200),8000)}

async function doExit(i){const p=STATE.positions[i];
if(!confirm(`EXIT ${p.trading_symbol}\n${p.net_qty>0?'SELL':'BUY'} ${Math.abs(p.net_qty)} @ MARKET (${p.product})?`))return;
toast('⏳ exiting '+p.trading_symbol+'…',15000);report(await post('/api/exit',p));refresh(true)}
async function exitAll(){if(!STATE.positions.length){toast('no open positions');return}
if(!confirm('🚨 EXIT ALL '+STATE.positions.length+' open position(s) at MARKET?'))return;
if(!confirm('Really sure? This squares off EVERYTHING.'))return;
toast('⏳ exiting all…',20000);const r=await post('/api/exit_all');
toast((r.results||[]).map(x=>(x.ok?'✅':'❌')+' '+x.symbol+' '+(x.status||x.error||'')).join(' | ')||'done',9000);refresh(true)}
async function manual(side){const b=readForm(side);if(!b)return;
if(!confirm(`${side} ${b.quantity} × ${b.trading_symbol} ${b.order_type}${b.price?' @ '+b.price:''} (${b.product})?`))return;
toast('⏳ placing '+side+'…',15000);report(await post('/api/order',b));refresh(true)}
async function cancelOrder(id){if(!confirm('Cancel order '+id+'?'))return;
const r=await post('/api/cancel',{groww_order_id:id});toast(JSON.stringify(r).slice(0,200));refresh(true)}

function render(){
const tb=$('#postbl tbody');tb.innerHTML='';
STATE.positions.forEach((p,i)=>{
const pnl=p.ltp!=null?((p.ltp-p.avg_price)*p.net_qty):null;
tb.insertAdjacentHTML('beforeend',`<tr>
<td>${esc(p.trading_symbol)}</td><td class="dim">${esc(p.product)}</td>
<td class="${p.net_qty>0?'pos':'neg'}">${p.net_qty>0?'+':''}${p.net_qty}</td>
<td>₹${p.avg_price.toFixed(2)}</td><td>${p.ltp!=null?'₹'+p.ltp.toFixed(2):'—'}</td>
<td class="${pnl>=0?'pos':'neg'}">${pnl!=null?(pnl>=0?'+':'')+'₹'+pnl.toFixed(0):'—'}</td>
<td><button class="btn-exit" onclick="doExit(${i})">EXIT</button></td>
<td><button class="curl" title="copy exit curl" onclick="curlExit(STATE.positions[${i}])">📋</button></td></tr>`)});
$('#posempty').style.display=STATE.positions.length?'none':'block';
const ob=$('#ordtbl tbody');ob.innerHTML='';
STATE.orders.slice(0,15).forEach(o=>{
const st=o.order_status,cls=/EXECUTED|COMPLETED/.test(st)?'b-ok':/FAILED|REJECT|CANCEL/.test(st)?'b-bad':'b-mid';
const cancable=/NEW|PENDING|OPEN|ACKED|APPROVED|TRIGGER/.test(st);
ob.insertAdjacentHTML('beforeend',`<tr>
<td class="dim">${esc((o.created_at||'').replace('T',' ').slice(11,19))}</td>
<td>${esc(o.trading_symbol)}</td>
<td class="${o.transaction_type==='BUY'?'pos':'neg'}">${esc(o.transaction_type)}</td>
<td>${o.quantity}</td><td class="dim">${esc(o.order_type)}</td>
<td><span class="badge ${cls}">${esc(st)}</span></td>
<td>${o.avg_price?'₹'+(+o.avg_price).toFixed(2):'—'}</td>
<td>${cancable?`<button onclick="cancelOrder('${esc(o.groww_order_id)}')">✖</button>`:''}</td>
<td><button class="curl" title="copy status curl" onclick="curlStatus('${esc(o.groww_order_id)}')">📋</button></td></tr>`)});
}
async function refresh(manual){try{
const r=await fetch('/api/state');const d=await r.json();
STATE=d;render();
$('#posage').textContent='· '+new Date().toLocaleTimeString();
const tk=$('#tok');
if(d.token_ok){tk.className='badge b-ok';tk.textContent='token OK'+(d.token_exp?' · exp '+new Date(d.token_exp*1000).toLocaleTimeString():'')}
else{tk.className='badge b-bad';tk.textContent='NO TOKEN'}
if(manual)toast('refreshed');
}catch(e){$('#tok').className='badge b-bad';$('#tok').textContent='server error'}}
refresh();setInterval(refresh,5000);
</script></body></html>
"""


# ─────────────────────────────────────────────────────────────
#  HTTP server
# ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        try:
            if p.path in ("/", "/index.html"):
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif p.path == "/api/state":
                token = get_token()
                positions = fetch_positions() if token else []
                for pos in positions:
                    pos["ltp"] = fetch_ltp(pos["exchange"], pos["trading_symbol"])
                orders = fetch_orders() if token else []
                self._json({"token_ok": bool(token), "token_exp": _jwt_exp(token),
                            "positions": positions, "orders": orders})
            elif p.path == "/api/token":
                self._json({"token": get_token()})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        p = urlparse(self.path)
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
            if p.path == "/api/exit":
                self._json(exit_position(body["trading_symbol"], body.get("exchange", "NSE"),
                                         body.get("product", "MIS"), int(body["net_qty"])))
            elif p.path == "/api/exit_all":
                results = []
                for pos in fetch_positions():
                    r = exit_position(pos["trading_symbol"], pos["exchange"],
                                      pos["product"], pos["net_qty"])
                    r["symbol"] = pos["trading_symbol"]
                    results.append(r)
                self._json({"results": results})
            elif p.path == "/api/order":
                allowed = {"trading_symbol", "quantity", "validity", "exchange", "segment",
                           "product", "order_type", "transaction_type", "price",
                           "order_reference_id"}
                self._json(place_order({k: v for k, v in body.items() if k in allowed}))
            elif p.path == "/api/cancel":
                self._json(_api_post("/v1/order/cancel",
                                     {"segment": "FNO",
                                      "groww_order_id": body["groww_order_id"]}))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    tok = get_token()
    print("=" * 56)
    print("🛡  TRADE CONTROL PANEL")
    print(f"    http://{HOST}:{PORT}")
    print(f"    token: {'OK ✓' if tok else '❌ NOT AVAILABLE — run a bot once or check creds'}")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye 👋")


if __name__ == "__main__":
    main()
