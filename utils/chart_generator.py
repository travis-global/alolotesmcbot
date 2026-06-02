"""
utils/chart_generator.py
==========================
Generates a standalone Plotly HTML chart from the current state.
Called by the H1 workflow after each Retina run.
Output is committed to output/chart.html and served via GitHub Pages.
"""

import json
import os
from datetime import datetime

OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "output", "chart.html"
)


def generate_chart(state: dict):
    """
    Generates a standalone HTML chart from the current state.
    One chart per symbol — uses the most recently updated symbol.
    If multiple symbols exist, generates a multi-symbol dashboard.
    """
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    per_symbol = state.get("per_symbol", {})
    open_trades = state.get("open_trades", [])
    closed_trades = state.get("closed_trades", [])

    if not per_symbol:
        _write_empty_chart()
        return

    # Build chart data for each symbol
    symbol_charts = []
    for symbol, data in per_symbol.items():
        ohlc = data.get("ohlc", [])
        if not ohlc:
            continue
        symbol_charts.append({
            "symbol":      symbol,
            "ohlc":        ohlc,
            "obs":         [ob for ob in data.get("order_blocks", []) if not ob.get("mitigated")],
            "fvgs":        [fvg for fvg in data.get("fvgs", []) if not fvg.get("mitigated")],
            "breakers":    data.get("breakers", []),
            "bos":         data.get("bos_events", []),
            "choch":       data.get("choch_events", []),
            "sweeps":      data.get("sweeps", []),
            "pois":        data.get("pois", []),
            "trendlines":  data.get("trendlines", []),
            "double_tops": data.get("double_tops", []),
            "double_bots": data.get("double_bottoms", []),
            "pd":          data.get("pd_arrays", {}),
            "updated_at":  data.get("updated_at", ""),
        })

    html = _build_html(symbol_charts, open_trades, closed_trades, state)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[Chart] Generated {OUTPUT_PATH} — {len(symbol_charts)} symbol(s)")


def _write_empty_chart():
    html = """<!DOCTYPE html><html><body style="background:#0b0b0f;color:#666;
    font-family:monospace;display:flex;align-items:center;justify-content:center;
    height:100vh;font-size:14px">
    <div>No H1 scan data yet. Waiting for first run.</div></body></html>"""
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)


def _build_html(symbol_charts: list, open_trades: list,
                closed_trades: list, state: dict) -> str:
    """Builds the full standalone HTML with embedded Plotly charts."""

    # Serialize data for injection into JS
    charts_json     = json.dumps(symbol_charts,  default=str)
    trades_json     = json.dumps(open_trades,    default=str)
    closed_json     = json.dumps(closed_trades,  default=str)
    last_h1         = state.get("last_h1_run", "—")
    last_m5         = state.get("last_m5_run", "—")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>SMC Bot — Live Chart</title>
  <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
  <style>
    *,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
    :root{{
      --bg:#0b0b0f;--bg2:#12121a;--bg3:#1a1a26;--border:#1e1e2e;
      --text:#c8c8d8;--muted:#444455;--bull:#26a69a;--bear:#ef5350;
      --warn:#ff9800;--font:"SF Mono","Consolas",monospace;
    }}
    html,body{{height:100%;background:var(--bg);color:var(--text);font-family:var(--font);font-size:12px}}
    #app{{display:flex;flex-direction:column;height:100vh;max-width:520px;margin:0 auto}}
    #header{{background:var(--bg2);border-bottom:1px solid var(--border);padding:8px 12px;flex-shrink:0}}
    .hdr-row{{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}}
    #logo{{font-size:12px;font-weight:700;letter-spacing:3px;color:#fff}}
    .meta{{font-size:9px;color:var(--muted)}}
    #sym-tabs{{display:flex;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;overflow-x:auto}}
    .sym-tab{{padding:7px 12px;font-size:10px;color:var(--muted);cursor:pointer;
              border-bottom:2px solid transparent;white-space:nowrap;letter-spacing:1px}}
    .sym-tab.active{{color:#fff;border-bottom-color:var(--bull)}}
    #chart-wrap{{position:relative;flex:1;min-height:200px;background:var(--bg)}}
    #main-chart{{width:100%;height:100%}}
    #panels{{height:220px;overflow-y:auto;flex-shrink:0}}
    .panel{{display:none;padding:8px}}
    .panel.active{{display:block}}
    #bottom-tabs{{display:flex;background:var(--bg2);border-top:1px solid var(--border);border-bottom:1px solid var(--border);flex-shrink:0}}
    .btab{{flex:1;padding:7px 0;text-align:center;font-size:10px;color:var(--muted);
           cursor:pointer;border-bottom:2px solid transparent;letter-spacing:1px}}
    .btab.active{{color:#fff;border-bottom-color:var(--bull)}}
    .trade-card{{background:var(--bg3);border:1px solid var(--border);border-radius:6px;
                 padding:8px;margin-bottom:6px}}
    .trade-card.profit{{border-color:rgba(38,166,154,.4)}}
    .trade-card.loss{{border-color:rgba(239,83,80,.4)}}
    .tc-row{{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}}
    .dir-pill{{padding:2px 6px;border-radius:3px;font-size:8px;font-weight:700}}
    .long{{background:rgba(38,166,154,.2);color:var(--bull)}}
    .short{{background:rgba(239,83,80,.2);color:var(--bear)}}
    .tc-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:3px;margin-top:4px}}
    .tf{{display:flex;flex-direction:column}}
    .tf .fl{{font-size:7px;color:var(--muted);letter-spacing:1px}}
    .tf .fv{{font-size:9px;font-weight:700}}
    .sl-v{{color:#ff1744}}.tp-v{{color:#00e676}}.en-v{{color:#448aff}}
    .stat-row{{display:flex;justify-content:space-between;padding:3px 0;
               border-bottom:1px solid var(--border);font-size:10px}}
    .stat-row:last-child{{border-bottom:none}}
    .stat-v{{font-weight:700;color:#fff}}
    .empty{{text-align:center;padding:30px;color:var(--muted);font-size:10px}}
    #footer{{padding:3px 12px;background:var(--bg2);border-top:1px solid var(--border);
             display:flex;justify-content:space-between;font-size:9px;color:var(--muted);flex-shrink:0}}
  </style>
</head>
<body>
<div id="app">

  <div id="header">
    <div class="hdr-row">
      <span id="logo">SMC · BOT</span>
      <span class="meta">GitHub Actions · Static</span>
    </div>
    <div style="display:flex;gap:16px;font-size:9px;color:var(--muted)">
      <span>H1 scan: <b id="h1-ts" style="color:var(--text)">{last_h1[:16] if last_h1 else '—'}</b></span>
      <span>M5 exec: <b id="m5-ts" style="color:var(--text)">{last_m5[:16] if last_m5 else '—'}</b></span>
    </div>
  </div>

  <div id="sym-tabs"></div>

  <div id="chart-wrap">
    <div id="main-chart"></div>
  </div>

  <div id="bottom-tabs">
    <div class="btab active" data-panel="trades">TRADES</div>
    <div class="btab" data-panel="closed">CLOSED</div>
    <div class="btab" data-panel="signals">SIGNALS</div>
    <div class="btab" data-panel="stats">STATS</div>
  </div>

  <div id="panels">
    <div class="panel active" id="panel-trades"></div>
    <div class="panel" id="panel-closed"></div>
    <div class="panel" id="panel-signals"></div>
    <div class="panel" id="panel-stats"></div>
  </div>

  <div id="footer">
    <span>SMC Bot v1.0 — Demo Account</span>
    <span id="chart-updated">Updated: {datetime.utcnow().strftime('%H:%M UTC')}</span>
  </div>

</div>

<script>
// ── Injected data ─────────────────────────────────────────
const CHARTS      = {charts_json};
const OPEN_TRADES = {trades_json};
const CLOSED      = {closed_json};

// ── State ─────────────────────────────────────────────────
let activeSymbol  = CHARTS.length ? CHARTS[0].symbol : null;
let chartInited   = false;
let activePanel   = "trades";

// ── Symbol tabs ───────────────────────────────────────────
const symTabs = document.getElementById("sym-tabs");
CHARTS.forEach(c => {{
  const tab = document.createElement("div");
  tab.className = "sym-tab" + (c.symbol === activeSymbol ? " active" : "");
  tab.textContent = c.symbol;
  tab.onclick = () => {{
    activeSymbol = c.symbol;
    document.querySelectorAll(".sym-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    renderChart();
  }};
  symTabs.appendChild(tab);
}});

// ── Bottom tabs ───────────────────────────────────────────
document.querySelectorAll(".btab").forEach(tab => {{
  tab.onclick = () => {{
    document.querySelectorAll(".btab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    activePanel = tab.dataset.panel;
    document.getElementById("panel-" + activePanel).classList.add("active");
  }};
}});

// ── Chart ─────────────────────────────────────────────────
function renderChart() {{
  const cd = CHARTS.find(c => c.symbol === activeSymbol);
  if (!cd || !cd.ohlc.length) return;

  const d    = cd.ohlc;
  const time = d.map(x => x.time);
  const n    = time.length;
  const allH = d.map(x => x.H);
  const allL = d.map(x => x.L);
  const yPad = (Math.max(...allH) - Math.min(...allL)) * 0.05;
  const yMin = Math.min(...allL) - yPad;
  const yMax = Math.max(...allH) + yPad;
  const lastT= time[n-1];

  const traces = [{{
    x: time, open: d.map(x=>x.O), high: d.map(x=>x.H),
    low: d.map(x=>x.L), close: d.map(x=>x.C),
    type: "candlestick",
    increasing: {{line:{{color:"#26a69a",width:1}}, fillcolor:"#26a69a"}},
    decreasing: {{line:{{color:"#ef5350",width:1}}, fillcolor:"#ef5350"}},
    whiskerwidth: 0.3, hoverinfo:"x+y"
  }}];

  const shapes = [];
  const annotations = [];
  const priceRange = yMax - yMin;

  // OBs
  cd.obs.forEach(ob => {{
    const bull = ob.type.includes("Bullish");
    const color = bull ? "#26a69a" : "#ef5350";
    shapes.push({{
      type:"rect", xref:"x", yref:"y",
      x0:ob.time, x1:lastT, y0:ob.bottom, y1:ob.top,
      fillcolor: bull?"rgba(38,166,154,.10)":"rgba(239,83,80,.10)",
      line:{{color,width:1,dash:"longdashdot"}}
    }});
    annotations.push({{
      x:ob.time, y:bull?ob.bottom:ob.top, xref:"x", yref:"y",
      text:bull?"OB+":"OB−", showarrow:false,
      font:{{color,size:8,family:"monospace"}},
      xanchor:"left", yshift:bull?-9:9
    }});
  }});

  // FVGs
  cd.fvgs.forEach(fvg => {{
    const bull = fvg.type.includes("BULLISH");
    const color = bull?"#00bcd4":"#ce93d8";
    shapes.push({{
      type:"rect", xref:"x", yref:"y",
      x0:fvg.time, x1:lastT, y0:fvg.bottom, y1:fvg.top,
      fillcolor: bull?"rgba(0,188,212,.08)":"rgba(206,147,216,.08)",
      line:{{color,width:0.8,dash:"dash"}}
    }});
  }});

  // BOS lines
  const timeByIdx = {{}};
  d.forEach((x,i) => timeByIdx[i]=x.time);
  cd.bos.forEach(b => {{
    const up = b.type.includes("UP");
    shapes.push({{
      type:"line", xref:"x", yref:"y",
      x0:timeByIdx[b.swing_index]||b.time,
      x1:timeByIdx[b.break_index]||b.time,
      y0:b.level, y1:b.level,
      line:{{color:up?"#26a69a":"#ef5350",width:1.5}}
    }});
    const mid = Math.floor(((b.swing_index||0)+(b.break_index||0))/2);
    annotations.push({{
      x:timeByIdx[mid]||b.time, y:b.level, xref:"x", yref:"y",
      text:up?"BOS▲":"BOS▼", showarrow:false,
      font:{{color:up?"#26a69a":"#ef5350",size:8,family:"monospace"}},
      yshift:up?8:-8
    }});
  }});

  // CHoCH lines
  cd.choch.forEach(c => {{
    const up = c.type.includes("UP");
    shapes.push({{
      type:"line", xref:"x", yref:"y",
      x0:timeByIdx[c.swing_index]||c.time,
      x1:timeByIdx[c.break_index]||c.time,
      y0:c.level, y1:c.level,
      line:{{color:"#ff9800",width:1.5,dash:"dot"}}
    }});
  }});

  // Open trade zones
  OPEN_TRADES.filter(t=>t.symbol===activeSymbol).forEach(t => {{
    (t.chart_zones||[]).forEach(z => {{
      if(z.type==="line") {{
        const color = z.color==="sl"?"rgba(255,23,68,.8)":
                      z.color==="tp"?"rgba(0,230,118,.8)":
                      z.color==="entry"?"rgba(68,138,255,.7)":"rgba(200,200,200,.4)";
        shapes.push({{
          type:"line", xref:"x", yref:"y",
          x0:time[0], x1:lastT, y0:z.price, y1:z.price,
          line:{{color,width:z.color==="entry"?1.5:1,dash:"dash"}}
        }});
      }} else if(z.top&&z.bottom) {{
        const bull = z.color==="bull";
        shapes.push({{
          type:"rect", xref:"x", yref:"y",
          x0:time[0], x1:lastT, y0:z.bottom, y1:z.top,
          fillcolor:bull?"rgba(38,166,154,.1)":"rgba(239,83,80,.1)",
          line:{{color:bull?"rgba(38,166,154,.5)":"rgba(239,83,80,.5)",width:1}}
        }});
      }}
    }});
  }});

  const layout = {{
    paper_bgcolor:"#0b0b0f", plot_bgcolor:"#0b0b0f",
    xaxis:{{
      type:"category", showgrid:true, gridcolor:"#13131d",
      tickfont:{{color:"#333344",size:7}}, tickangle:-45,
      rangeslider:{{visible:false}}, nticks:8,
      range:[-0.5,n-0.5]
    }},
    yaxis:{{
      showgrid:true, gridcolor:"#13131d",
      tickfont:{{color:"#333344",size:8}},
      tickformat:".5f", side:"right", range:[yMin,yMax]
    }},
    shapes, annotations,
    showlegend:false,
    margin:{{t:4,l:4,r:72,b:28}},
    uirevision:"stable"
  }};

  const config = {{
    responsive:true, displayModeBar:false, scrollZoom:true
  }};

  if(!chartInited) {{
    Plotly.newPlot("main-chart", traces, layout, config);
    chartInited = true;
  }} else {{
    Plotly.react("main-chart", traces, layout, config);
  }}
}}

// ── Trade panels ──────────────────────────────────────────
function renderTrades() {{
  const el = document.getElementById("panel-trades");
  const trades = OPEN_TRADES.filter(t=>t.symbol===activeSymbol||!activeSymbol);
  if(!trades.length) {{
    el.innerHTML = '<div class="empty">No active trades</div>';
    return;
  }}
  el.innerHTML = trades.map(t => {{
    const pnl = t.live_pnl||t.pnl_pips||0;
    const cls = pnl>0?"profit":pnl<0?"loss":"";
    const pnlStr = (pnl>=0?"+":"")+pnl.toFixed(1)+" pip";
    return `<div class="trade-card ${{cls}}">
      <div class="tc-row">
        <div style="display:flex;gap:6px;align-items:center">
          <span class="dir-pill ${{t.direction}}">${{t.direction.toUpperCase()}}</span>
          <b>${{t.pattern}}</b>
          <span style="color:var(--muted);font-size:9px">${{t.symbol}}</span>
        </div>
        <span style="font-weight:700;color:${{pnl>=0?'var(--bull)':'var(--bear)'}}">${{pnlStr}}</span>
      </div>
      <div class="tc-grid">
        <div class="tf"><span class="fl">ENTRY</span><span class="fv en-v">${{t.entry?.toFixed(5)||'—'}}</span></div>
        <div class="tf"><span class="fl">SL</span><span class="fv sl-v">${{t.sl?.toFixed(5)||'—'}}</span></div>
        <div class="tf"><span class="fl">TP</span><span class="fv tp-v">${{t.tp?.toFixed(5)||'—'}}</span></div>
        <div class="tf"><span class="fl">R:R</span><span class="fv">${{t.rr||'—'}}</span></div>
        <div class="tf"><span class="fl">ZONE HI</span><span class="fv">${{t.zone_top?.toFixed(5)||'—'}}</span></div>
        <div class="tf"><span class="fl">ZONE LO</span><span class="fv">${{t.zone_bottom?.toFixed(5)||'—'}}</span></div>
      </div>
    </div>`;
  }}).join("");
}}

function renderClosed() {{
  const el = document.getElementById("panel-closed");
  if(!CLOSED.length) {{
    el.innerHTML = '<div class="empty">No closed trades yet</div>';
    return;
  }}
  el.innerHTML = CLOSED.slice(0,30).map(t => {{
    const pnl = t.pnl_pips||0;
    const cls = pnl>0?"var(--bull)":"var(--bear)";
    return `<div class="trade-card">
      <div class="tc-row">
        <div style="display:flex;gap:6px;align-items:center">
          <span class="dir-pill ${{t.direction}}">${{t.direction?.toUpperCase()}}</span>
          <b>${{t.pattern}}</b>
          <span style="color:var(--muted);font-size:9px">${{t.symbol}}</span>
        </div>
        <span style="font-weight:700;color:${{cls}}">${{(pnl>=0?"+":"")+pnl.toFixed(1)+" pip"}}</span>
      </div>
      <div style="font-size:9px;color:var(--muted);margin-top:3px">
        ${{t.close_reason||t.exit_type||'—'}}
      </div>
    </div>`;
  }}).join("");
}}

function renderSignals() {{
  const el = document.getElementById("panel-signals");
  // Build from per-symbol data if available
  el.innerHTML = '<div class="empty">Active signals stored in market_state.json</div>';
}}

function renderStats() {{
  const el = document.getElementById("panel-stats");
  const total   = OPEN_TRADES.length + CLOSED.length;
  const wins    = CLOSED.filter(t=>(t.pnl_pips||0)>0).length;
  const losses  = CLOSED.filter(t=>(t.pnl_pips||0)<0).length;
  const totPnl  = CLOSED.reduce((s,t)=>s+(t.pnl_pips||0),0);
  const wr      = CLOSED.length ? ((wins/CLOSED.length)*100).toFixed(1) : "—";

  el.innerHTML = `
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:8px">
      <div class="stat-row"><span>Total trades</span><span class="stat-v">${{total}}</span></div>
      <div class="stat-row"><span>Open</span><span class="stat-v">${{OPEN_TRADES.length}}</span></div>
      <div class="stat-row"><span>Closed</span><span class="stat-v">${{CLOSED.length}}</span></div>
      <div class="stat-row"><span>Wins</span><span class="stat-v" style="color:var(--bull)">${{wins}}</span></div>
      <div class="stat-row"><span>Losses</span><span class="stat-v" style="color:var(--bear)">${{losses}}</span></div>
      <div class="stat-row"><span>Win rate</span><span class="stat-v">${{wr}}%</span></div>
      <div class="stat-row"><span>Total PnL</span>
        <span class="stat-v" style="color:${{totPnl>=0?'var(--bull)':'var(--bear)'}}">${{(totPnl>=0?"+":"")+totPnl.toFixed(1)}} pip</span>
      </div>
    </div>`;
}}

// ── Init ─────────────────────────────────────────────────
renderChart();
renderTrades();
renderClosed();
renderSignals();
renderStats();
</script>
</body>
</html>"""
