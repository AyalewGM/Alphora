import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { APPROVED_POLICY, loadLocalReadOnlyStatus } from "./api.js";

const WATCHLIST = [
  { symbol: "SPY", name: "S&P 500 ETF", base: 582.40, change: 0.82 },
  { symbol: "QQQ", name: "Nasdaq 100 ETF", base: 501.15, change: 1.27 },
  { symbol: "IWM", name: "Russell 2000 ETF", base: 218.76, change: -0.45 },
  { symbol: "AAPL", name: "Apple", base: 225.65, change: 0.38 },
  { symbol: "MSFT", name: "Microsoft", base: 416.20, change: -0.21 },
  { symbol: "NVDA", name: "Nvidia", base: 142.35, change: 1.91 }
];
const TIMEFRAMES = ["1D", "5D", "1M", "3M", "1Y"];
const DEMO_POLICY = APPROVED_POLICY;

function syntheticCandles(symbol, timeframe) {
  // Fixed illustrative bars: deterministic, offline and NEVER sourced from a market feed.
  const seed = symbol.split("").reduce((a, c) => a + c.charCodeAt(0), 0);
  const count = timeframe === "1D" ? 44 : 60;
  return Array.from({ length: count }, (_, i) => {
    const trend = i * (0.09 + (seed % 6) * 0.018);
    const wave = Math.sin(i * 0.39 + seed) * 3.2 + Math.cos(i * 0.13 + seed) * 2.5;
    const center = 85 + trend + wave;
    const open = center + Math.sin(i * 1.6 + seed) * 1.1;
    const close = center + Math.cos(i * 1.37 + seed) * 1.2;
    return {
      open, close,
      high: Math.max(open, close) + 0.5 + ((i + seed) % 5) / 3,
      low: Math.min(open, close) - 0.5 - ((i + seed) % 4) / 3,
      volume: 12 + ((i * 13 + seed) % 28)
    };
  });
}

function Candles({ symbol, timeframe }) {
  const candles = useMemo(() => syntheticCandles(symbol, timeframe), [symbol, timeframe]);
  const low = Math.min(...candles.map(c => c.low)) - 2;
  const high = Math.max(...candles.map(c => c.high)) + 2;
  const w = 960, h = 390, top = 23, bottom = 45, plotH = h - top - bottom, step = (w - 75) / candles.length;
  const y = value => top + ((high - value) / (high - low)) * plotH;
  const levels = Array.from({ length: 5 }, (_, i) => low + (high - low) * i / 4);
  return (
    <svg className="chart" viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`Illustrative ${symbol} candlestick chart, not actual market prices`}>
      {levels.map(level => <g key={level}><line x1="0" x2="890" y1={y(level)} y2={y(level)} stroke="#263342" strokeDasharray="4 6" /><text x="898" y={y(level)+4} fill="#8092a9" fontSize="12">{level.toFixed(1)}</text></g>)}
      {candles.map((c, i) => {
        const x = i * step + 9, rising = c.close >= c.open;
        return <g key={i} stroke={rising ? "#28c9a3" : "#ef657a"} fill={rising ? "#28c9a3" : "#ef657a"}>
          <line x1={x + step/2} x2={x + step/2} y1={y(c.high)} y2={y(c.low)} strokeWidth="1.5" />
          <rect x={x + step/2 - Math.max(2,step*.25)} y={Math.min(y(c.open),y(c.close))} width={Math.max(4,step*.5)} height={Math.max(1.5,Math.abs(y(c.open)-y(c.close)))} rx="1"/>
          <rect x={x + step/2 - Math.max(2,step*.25)} y={h-17-c.volume*.55} width={Math.max(4,step*.5)} height={c.volume*.55} opacity=".24" />
        </g>;
      })}
      {["09:30","10:30","11:30","12:30","13:30","14:30","15:30"].map((t,i)=><text key={t} x={20 + i*130} y={h-3} fill="#8092a9" fontSize="12">{t}</text>)}
    </svg>
  );
}

function App() {
  const [selected, setSelected] = useState("SPY");
  const [timeframe, setTimeframe] = useState("1D");
  const [tab, setTab] = useState("Overview");
  const [policy, setPolicy] = useState(DEMO_POLICY);
  const [apiState, setApiState] = useState("Offline preview — API unverified");
  const [search, setSearch] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    // Verify both local read-only contracts before declaring the API connected.
    loadLocalReadOnlyStatus(fetch, controller.signal)
      .then(({ policy: acceptedPolicy }) => {
        if (controller.signal.aborted) return;
        setPolicy(acceptedPolicy);
        setApiState("Read-only local API verified");
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setPolicy(DEMO_POLICY);
        setApiState("Offline preview — API unavailable or unverified");
      });
    return () => controller.abort();
  }, []);
  const item = WATCHLIST.find(x => x.symbol === selected);
  const filtered = WATCHLIST.filter(x => (x.symbol + x.name).toLowerCase().includes(search.toLowerCase()));
  const pct = n => (Number(n) * 100).toFixed(1) + "%";
  return <div className="app">
    <header className="topbar">
      <div className="brand"><span className="logo">Α</span><div>ALPHORA <small>RESEARCH TERMINAL</small></div></div>
      <div className="top-center"><span className="chip blue">PAPER ONLY</span><span className="chip amber">SYNTHETIC DATA</span></div>
      <div className="top-right"><span className="status-dot"/><span role="status" aria-live="polite">{apiState}</span><span className="avatar">A</span></div>
    </header>
    <div className="workspace">
      <aside className="rail" aria-label="Workspace sections"><span className="rail-active">▥</span><span>⌁</span><span>◫</span><span>≡</span></aside>
      <main className="main">
        <div className="pagehead"><div><h1>Market workspace</h1><p>Research, review and monitor simulated decisions</p></div><div className="page-actions"><span>US Stocks & ETFs</span><span className="clock">● Market data disconnected</span></div></div>
        <div className="metrics">
          <div className="metric"><span>Initial virtual equity</span><strong>${Number(policy.starting_virtual_equity).toLocaleString("en-US")}</strong><small>Policy reference · not account balance</small></div>
          <div className="metric"><span>Planned risk per trade</span><strong>{pct(policy.max_planned_trade_risk_fraction)}</strong><small>Hard policy maximum</small></div>
          <div className="metric"><span>Instrument allocation cap</span><strong>{pct(policy.max_instrument_allocation_fraction)}</strong><small>Per ticker including reservations</small></div>
          <div className="metric"><span>Daily loss shutdown</span><strong>{pct(policy.daily_loss_shutdown_fraction)}</strong><small>Not continuously monitored here</small></div>
        </div>
        <section className="panel chart-panel">
          <div className="chart-header"><div className="symbol-title"><div className="symbol-badge">{selected.slice(0,1)}</div><div><h2>{selected} <span>{item.name}</span></h2><p>Illustrative candles · NOT real-time prices</p></div></div><div className="price"><strong>${item.base.toFixed(2)}</strong><span className={item.change >= 0 ? "positive":"negative"}>{item.change>=0?"+":""}{item.change.toFixed(2)}% demo</span></div></div>
          <div className="toolbar"><div className="timeframes">{TIMEFRAMES.map(t=><button key={t} type="button" className={timeframe===t?"active":""} onClick={()=>setTimeframe(t)} aria-pressed={timeframe===t}>{t}</button>)}</div><div className="toolnote">Illustrative candles and prices only</div></div>
          <Candles symbol={selected} timeframe={timeframe} />
          <div className="chart-footer"><span>SIMULATED VISUALIZATION · No live feed or historical vendor data</span><span>Read-only</span></div>
        </section>
        <section className="panel bottom-panel"><div className="tabbar">{["Overview","Signals","Paper decisions","Research"].map(t=><button key={t} onClick={()=>setTab(t)} className={tab===t?"active":""}>{t}</button>)}</div><div className="empty"><div className="empty-icon">⌁</div><strong>{tab === "Overview" ? "Research workspace ready" : `${tab} integration pending`}</strong><p>{tab === "Overview" ? "Connect validated, point-in-time market datasets to replace all illustrative prices and chart bars." : "No verified dataset or authenticated read-only endpoint is connected. Nothing here represents actual positions, recommendations, or orders."}</p></div></section>
      </main>
      <aside className="sidebar">
        <section className="panel watchlist"><div className="sidehead"><h3>Watchlist</h3><span>DEMO</span></div><input aria-label="Filter watchlist" placeholder="Search symbols…" value={search} onChange={e=>setSearch(e.target.value)} /><div className="watchheader"><span>Symbol</span><span>Demo price</span></div>{filtered.map(x=><button type="button" className={selected===x.symbol?"watchrow chosen":"watchrow"} key={x.symbol} onClick={()=>setSelected(x.symbol)}><span className="watchname"><strong>{x.symbol}</strong><small>{x.name}</small></span><span><strong>${x.base.toFixed(2)}</strong><small className={x.change>=0?"positive":"negative"}>{x.change>=0?"+":""}{x.change.toFixed(2)}%</small></span></button>)}{filtered.length===0&&<p className="noresults">No demo symbols match</p>}</section>
        <section className="panel riskpanel"><div className="sidehead"><h3>Execution boundary</h3><span className="safe-tag">LOCKED</span></div><div className="boundary"><span className="lock">◈</span><strong>Broker disconnected</strong><p>No credentials, order routing, execution endpoints or automated trading.</p></div><div className="rule"><span>Execution mode</span><strong>Research only</strong></div><div className="rule"><span>Local API status</span><strong>{apiState.startsWith("Read-only") ? "Verified" : "Unverified"}</strong></div><div className="rule"><span>Rebalancing policy</span><strong>{policy.rebalance_frequency}</strong></div><div className="rule"><span>Live trading</span><strong className="negative">Disabled</strong></div></section>
      </aside>
    </div>
  </div>;
}
createRoot(document.getElementById("root")).render(<React.StrictMode><App /></React.StrictMode>);
