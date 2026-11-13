import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { APPROVED_POLICY, loadLocalReadOnlyStatus } from "./api.js";
import { parseLocalDailySnapshot } from "./local-bars.js";
import { normalizedVolumeHeights } from "./chart-scale.js";
import { DEFAULT_RISK_DRAFT, describeRiskDraftError } from "./risk-draft.js";
import { buildRiskReview } from "./risk-review.js";

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

function Candles({ symbol, timeframe, imported }) {
  const candles = useMemo(() => imported ? imported.bars.slice(-60) : syntheticCandles(symbol, timeframe), [symbol, timeframe, imported]);
  const low = Math.min(...candles.map(c => c.low)) - 2;
  const high = Math.max(...candles.map(c => c.high)) + 2;
  // Keep a separate 40px relative-volume strip below the price pane.
  // Raw daily volume can exceed millions; its magnitude is NOT an SVG pixel height.
  const volumeHeights = useMemo(() => normalizedVolumeHeights(candles, 40), [candles]);
  const w = 960, h = 390, top = 23, bottom = 76, plotH = h - top - bottom, step = (w - 75) / candles.length;
  const y = value => top + ((high - value) / (high - low)) * plotH;
  const levels = Array.from({ length: 5 }, (_, i) => low + (high - low) * i / 4);
  return (
    <svg className="chart" viewBox={`0 0 ${w} ${h}`} role="img" aria-label={imported ? `Locally imported, unauthenticated ${symbol} daily candlestick chart` : `Illustrative ${symbol} candlestick chart, not actual market prices`}>
      {levels.map(level => <g key={level}><line x1="0" x2="890" y1={y(level)} y2={y(level)} stroke="#263342" strokeDasharray="4 6" /><text x="898" y={y(level)+4} fill="#8092a9" fontSize="12">{level.toFixed(1)}</text></g>)}
      {candles.map((c, i) => {
        const x = i * step + 9, rising = c.close >= c.open;
        return <g key={i} stroke={rising ? "#28c9a3" : "#ef657a"} fill={rising ? "#28c9a3" : "#ef657a"}>
          <line x1={x + step/2} x2={x + step/2} y1={y(c.high)} y2={y(c.low)} strokeWidth="1.5" />
          <rect x={x + step/2 - Math.max(2,step*.25)} y={Math.min(y(c.open),y(c.close))} width={Math.max(4,step*.5)} height={Math.max(1.5,Math.abs(y(c.open)-y(c.close)))} rx="1"/>
          <rect x={x + step/2 - Math.max(2,step*.25)} y={h-19-volumeHeights[i]} width={Math.max(4,step*.5)} height={volumeHeights[i]} opacity=".24" />
        </g>;
      })}
      {(imported ? candles.filter((_,i)=>i%Math.max(1,Math.floor(candles.length/7))===0).map(x=>x.day) : ["09:30","10:30","11:30","12:30","13:30","14:30","15:30"]).map((t,i)=><text key={t} x={20 + i*130} y={h-3} fill="#8092a9" fontSize="12">{t}</text>)}
    </svg>
  );
}

function App() {
  const [selected, setSelected] = useState("SPY");
  const [timeframe, setTimeframe] = useState("1D");
  const [tab, setTab] = useState("Overview");
  const [policy, setPolicy] = useState(DEMO_POLICY);
  const [apiState, setApiState] = useState("Offline preview — reference GET endpoints unverified");
  const [search, setSearch] = useState("");
  const [imported, setImported] = useState(null);
  const [importError, setImportError] = useState("");
  // Draft-only operator input; deliberately no storage or backend write.
  const [riskDraft, setRiskDraft] = useState({ ...DEFAULT_RISK_DRAFT });
  const [riskPreview, setRiskPreview] = useState(null);
  const [riskDraftError, setRiskDraftError] = useState("");
  const [riskDraftInvalidField, setRiskDraftInvalidField] = useState(null);
  function previewRiskDraft(event) {
    event.preventDefault();
    try {
      setRiskPreview(buildRiskReview(riskDraft));
      setRiskDraftError("");
      setRiskDraftInvalidField(null);
    } catch (error) {
      setRiskPreview(null);
      setRiskDraftInvalidField(error.field ?? null);
      setRiskDraftError(describeRiskDraftError(error));
      // Move keyboard focus to the invalid control, without discarding input.
      const control = error.field && event.currentTarget.elements.namedItem(error.field);
      if (control && typeof control.focus === "function") control.focus();
    }
  }
  function resetRiskDraft() {
    setRiskDraft({ ...DEFAULT_RISK_DRAFT });
    setRiskPreview(null);
    setRiskDraftError("");
    setRiskDraftInvalidField(null);
  }
  function changeRiskDraft(key, value) {
    setRiskDraft(current => ({ ...current, [key]: value }));
    setRiskPreview(null);
    setRiskDraftError("");
    setRiskDraftInvalidField(null);
  }
  async function onSnapshotFile(event) {
    setImportError("");
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      if (file.size > 512 * 1024) throw new Error("SNAPSHOT_TOO_LARGE");
      const snapshot = parseLocalDailySnapshot(await file.text());
      setImported(snapshot);
      setSelected(snapshot.symbol);
    } catch (error) {
      setImported(null);
      setImportError(`Local import rejected: ${error.message}`);
    } finally {
      event.target.value = "";
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    // Verify both local read-only contracts before declaring the API connected.
    loadLocalReadOnlyStatus(fetch, controller.signal)
      .then(({ policy: acceptedPolicy }) => {
        if (controller.signal.aborted) return;
        setPolicy(acceptedPolicy);
        setApiState("Reference GET checks passed — portfolio unverified");
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setPolicy(DEMO_POLICY);
        setApiState("Offline preview — reference GET endpoints unavailable or unverified");
      });
    return () => controller.abort();
  }, []);
  const item = WATCHLIST.find(x => x.symbol === selected) ?? {symbol:selected,name:"Locally imported instrument",base:0,change:0};
  const showingLocal = imported?.symbol === selected;
  const filtered = WATCHLIST.filter(x => (x.symbol + x.name).toLowerCase().includes(search.toLowerCase()));
  const pct = n => (Number(n) * 100).toFixed(1) + "%";
  return <div className="app">
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="topbar">
      <div className="brand"><span className="logo">Α</span><div>ALPHORA <small>RESEARCH TERMINAL</small></div></div>
      <div className="top-center"><span className="chip blue">PAPER ONLY</span><span className="chip amber">{showingLocal ? "USER-SUPPLIED DATA" : "SYNTHETIC DATA"}</span></div>
      <div className="top-right"><span className="status-dot"/><span role="status" aria-live="polite">{apiState}</span><span className="avatar">A</span></div>
    </header>
    <div className="workspace">
      <aside className="rail" aria-hidden="true"><span className="rail-active">▥</span><span>⌁</span><span>◫</span><span>≡</span></aside>
      <main id="main-content" className="main" tabIndex="-1">
        <div className="pagehead"><div><h1>Market workspace</h1><p>Research, review and monitor simulated decisions</p></div><div className="page-actions"><span>US Stocks & ETFs</span><span className="clock">● Market feeds disconnected</span></div></div>
        <div className="metrics">
          <div className="metric"><span>Initial virtual equity</span><strong>${Number(policy.starting_virtual_equity).toLocaleString("en-US")}</strong><small>Policy reference · not account balance</small></div>
          <div className="metric"><span>Planned risk per trade</span><strong>{pct(policy.max_planned_trade_risk_fraction)}</strong><small>Hard policy maximum</small></div>
          <div className="metric"><span>Instrument allocation cap</span><strong>{pct(policy.max_instrument_allocation_fraction)}</strong><small>Per ticker including reservations</small></div>
          <div className="metric"><span>Daily loss shutdown</span><strong>{pct(policy.daily_loss_shutdown_fraction)}</strong><small>Not continuously monitored here</small></div>
        </div>
        <section className="panel portfolio-monitor" aria-labelledby="paper-monitor-title">
          <div className="monitor-heading">
            <h2 id="paper-monitor-title">Paper portfolio monitor</h2>
            <span className="chip amber">NO VERIFIED ACCOUNT SNAPSHOT</span>
          </div>
          <p>Account balance, positions, profit/loss and safety status require a separately reviewed authenticated read-only snapshot. These placeholders are <strong>not account data</strong>; initial virtual equity above is policy reference only.</p>
          <div className="monitor-grid" aria-label="Portfolio account figures unavailable">
            <div><span>Available cash</span><strong>Unavailable</strong><small>No verified ledger</small></div>
            <div><span>Portfolio equity</span><strong>Unavailable</strong><small>No verified marks</small></div>
            <div><span>Session profit/loss</span><strong>Unavailable</strong><small>No authoritative session baseline</small></div>
            <div><span>Open positions</span><strong>Unavailable</strong><small>No attested holdings</small></div>
          </div>
          <dl className="monitor-safety" aria-label="Paper execution safety status">
            <div><dt>Emergency halt</dt><dd>UNKNOWN — not attested</dd></div>
            <div><dt>Reconciliation</dt><dd>NOT VERIFIED</dd></div>
            <div><dt>Effective policy revision</dt><dd>NOT AVAILABLE</dd></div>
          </dl>
          <section className="monitor-records" aria-labelledby="monitor-positions-title">
            <h3 id="monitor-positions-title">Positions and pending exposure</h3>
            <p>Read-only account holdings and working reservations require a verified ledger and independent marks.</p>
            <div className="monitor-table-scroll" role="region" aria-label="Positions and pending exposure table, horizontally scrollable" tabIndex="0">
              <table>
                <caption>Unavailable paper positions and pending-order reservations</caption>
                <thead><tr><th scope="col">Instrument</th><th scope="col">Shares</th><th scope="col">Verified mark</th><th scope="col">Pending exposure</th><th scope="col">Reconciliation</th></tr></thead>
                <tbody><tr><td className="monitor-empty-row" colSpan="5"><strong>UNAVAILABLE — NOT ZERO POSITIONS.</strong> No authenticated portfolio snapshot, mark provenance or pending-order ledger is connected.</td></tr></tbody>
              </table>
            </div>
          </section>
          <section className="monitor-records monitor-journal" aria-labelledby="monitor-journal-title">
            <h3 id="monitor-journal-title">Paper decision journal</h3>
            <p>Read-only decision and fill evidence needs server-attested intent IDs, session identity, reconciliation verdicts and durable event receipts.</p>
            <div className="monitor-table-scroll" role="region" aria-label="Decision journal table, horizontally scrollable" tabIndex="0">
              <table>
                <caption>Unavailable authenticated paper decision journal</caption>
                <thead><tr><th scope="col">Event time</th><th scope="col">Intent / revision</th><th scope="col">Decision</th><th scope="col">Risk &amp; reconciliation</th><th scope="col">Outcome / receipt</th></tr></thead>
                <tbody><tr><td className="monitor-empty-row" colSpan="5"><strong>NOT VERIFIED — NOT AN EMPTY JOURNAL.</strong> No authenticated event stream or cursor is connected. This does not prove no orders or fills occurred.</td></tr></tbody>
              </table>
            </div>
          </section>
        </section>
        <section className="panel risk-draft-panel" aria-labelledby="risk-draft-title">
          <div>
            <h2 id="risk-draft-title">Draft paper-risk settings</h2>
            <p>Prepare a <strong>local, unsaved proposal only</strong> for a future paper simulator. Validation never applies settings or changes the existing account. Multi-session execution remains blocked pending issue #42.</p>
          </div>
          <form onSubmit={previewRiskDraft} noValidate>
            <label>Starting capital for a new simulated portfolio (USD)
              <input aria-label="Draft new portfolio starting equity USD" inputMode="decimal" type="text"
                name="startingEquityUsd" aria-invalid={riskDraftInvalidField === "startingEquityUsd"} value={riskDraft.startingEquityUsd} onChange={event => changeRiskDraft("startingEquityUsd", event.target.value)}
                aria-describedby={riskDraftInvalidField === "startingEquityUsd" ? "draft-limit-note draft-error" : "draft-limit-note"} />
            </label>
            <label>Planned risk per trade (%)
              <input aria-label="Draft planned risk per trade percent" inputMode="decimal" type="text"
                name="perTradePct" aria-invalid={riskDraftInvalidField === "perTradePct"} value={riskDraft.perTradePct} onChange={event => changeRiskDraft("perTradePct", event.target.value)}
                aria-describedby={riskDraftInvalidField === "perTradePct" ? "draft-limit-note draft-error" : "draft-limit-note"} />
            </label>
            <label>Maximum per-instrument allocation (%)
              <input aria-label="Draft maximum instrument allocation percent" inputMode="decimal" type="text"
                name="allocationPct" aria-invalid={riskDraftInvalidField === "allocationPct"} value={riskDraft.allocationPct} onChange={event => changeRiskDraft("allocationPct", event.target.value)}
                aria-describedby={riskDraftInvalidField === "allocationPct" ? "draft-limit-note draft-error" : "draft-limit-note"} />
            </label>
            <label>Daily-loss shutdown (%)
              <input aria-label="Draft daily loss shutdown percent" inputMode="decimal" type="text"
                name="dailyLossPct" aria-invalid={riskDraftInvalidField === "dailyLossPct"} value={riskDraft.dailyLossPct} onChange={event => changeRiskDraft("dailyLossPct", event.target.value)}
                aria-describedby={riskDraftInvalidField === "dailyLossPct" ? "draft-limit-note draft-error" : "draft-limit-note"} />
            </label>
            <label>Rebalancing cadence
              <select aria-label="Draft rebalancing cadence" name="rebalanceFrequency" aria-invalid={riskDraftInvalidField === "rebalanceFrequency"} value={riskDraft.rebalanceFrequency}
                onChange={event => changeRiskDraft("rebalanceFrequency", event.target.value)} aria-describedby={riskDraftInvalidField === "rebalanceFrequency" ? "draft-limit-note draft-error" : "draft-limit-note"}>
                <option value="daily">Daily (default)</option>
                <option value="weekly">Weekly (proposal only)</option>
                <option value="manual">Manual (proposal only)</option>
              </select>
            </label>
            <div className="draft-actions">
              <button type="submit">Validate preview</button>
              <button type="button" onClick={resetRiskDraft}>Reset local draft inputs only</button>
            </div>
          </form>
          <p id="draft-limit-note">Hard ceilings: trade risk &gt; 0% and ≤ 0.5%, allocation &gt; 0% and ≤ 10%, daily-loss limit &gt; 0% and ≤ 2%. Starting capital must be positive dollars with at most two decimals and applies only to a <strong>future new portfolio</strong>. Percentages accept up to four decimal places. Rebalancing frequency is proposal metadata until backend review. No configuration can clear a halt.</p>
          {riskDraftError && <p id="draft-error" className="draft-error" role="alert">{riskDraftError}</p>}
          <p className="draft-feedback" role="status" aria-live="polite">
            {riskPreview ? <strong>Validated local preview — NOT APPLIED</strong> : "Draft not validated or applied."}
          </p>
          {riskPreview && <div className="draft-result">
            <strong>Local comparison only — NOT EFFECTIVE POLICY</strong>
            <p>This is a comparison with approved <strong>hard ceilings and new-portfolio defaults</strong>, not the account's effective policy. No effective revision or account snapshot has been verified.</p>
            <div className="draft-review-wrap" role="region" aria-label="Draft risk comparison table, horizontally scrollable" tabIndex="0">
              <table className="draft-review-table">
                <caption>Approved reference versus locally validated, unapplied paper-risk proposal</caption>
                <thead><tr><th scope="col">Setting</th><th scope="col">Approved reference</th><th scope="col">Local proposal</th><th scope="col">Meaning</th></tr></thead>
                <tbody>{riskPreview.rows.map(row => <tr key={row.label}><th scope="row">{row.label}</th><td>{row.reference}</td><td>{row.proposed}</td><td>{row.meaning}</td></tr>)}</tbody>
              </table>
            </div>
            <p>Wire schema: {riskPreview.wire.schema}. <strong>Nothing was saved, sent, activated or scheduled.</strong> P0 #42 and separate backend/independent approval remain required.</p>
          </div>}
        </section>
        <section className="panel chart-panel">
          <div className="chart-header"><div className="symbol-title"><div className="symbol-badge">{selected.slice(0,1)}</div><div><h2>{selected} <span>{item.name}</span></h2><p>{showingLocal ? "Local daily bars · source authenticity NOT verified" : "Illustrative candles · NOT real-time prices"}</p></div></div><div className="price"><strong>{showingLocal ? `Close ${imported.bars.at(-1).close.toFixed(2)}` : `${item.base.toFixed(2)}`}</strong><span className={showingLocal ? "" : item.change >= 0 ? "positive":"negative"}>{showingLocal ? `As of ${imported.bars.at(-1).day}` : `${item.change>=0?"+":""}${item.change.toFixed(2)}% demo`}</span></div></div>
          <div className="toolbar"><div className="timeframes">{!showingLocal && TIMEFRAMES.map(t=><button key={t} type="button" className={timeframe===t?"active":""} onClick={()=>setTimeframe(t)} aria-pressed={timeframe===t}>{t}</button>)}{showingLocal && <span className="toolnote">DAILY · Last 60 records</span>}</div><div className="toolnote">{showingLocal ? "Unverified local source · Not for trading" : "Illustrative candles and prices only"}</div></div>
          <Candles symbol={selected} timeframe={timeframe} imported={showingLocal ? imported : null} />
          <div className="chart-footer"><span>{showingLocal ? `USER-SUPPLIED ${imported.source} / ${imported.source_revision} · ${imported.price_basis} · Declared receipt ${imported.declared_sha256.slice(0,12)}… (NOT AUTHENTICATED)` : "SIMULATED VISUALIZATION · No live feed or historical vendor data"}</span><span>Read-only</span></div>
        </section>
        <section className="panel import-panel"><div><strong>Local historical daily snapshot</strong><p>Choose a JSON snapshot with source, revision, availability timestamps and a declared SHA-256 receipt. The source and receipt are not independently authenticated. No file is uploaded.</p></div><label className="filebutton">Import JSON<input aria-label="Import local historical daily snapshot JSON" type="file" accept=".json,application/json" onChange={onSnapshotFile}/></label>{imported && <button type="button" className="clearbutton" onClick={()=>{setImported(null);setImportError("");}}>Clear import</button>}{importError && <p className="import-error" role="alert">{importError}</p>}</section>
        <section className="panel bottom-panel"><div className="tabbar">{["Overview","Signals","Paper decisions","Research"].map(t=><button key={t} onClick={()=>setTab(t)} className={tab===t?"active":""} aria-pressed={tab===t}>{t}</button>)}</div><div className="empty"><div className="empty-icon">⌁</div><strong>{tab === "Overview" ? "Research workspace ready" : `${tab} integration pending`}</strong><p>{tab === "Overview" ? "Connect validated, point-in-time market datasets to replace all illustrative prices and chart bars." : "No verified dataset or authenticated read-only endpoint is connected. Nothing here represents actual positions, recommendations, or orders."}</p></div></section>
      </main>
      <aside className="sidebar">
        <section className="panel watchlist"><div className="sidehead"><h3>Watchlist</h3><span>DEMO</span></div><input aria-label="Filter watchlist" placeholder="Search symbols…" value={search} onChange={e=>setSearch(e.target.value)} /><div className="watchheader"><span>Symbol</span><span>Demo price</span></div>{filtered.map(x=><button type="button" className={selected===x.symbol?"watchrow chosen":"watchrow"} key={x.symbol} onClick={()=>{setSelected(x.symbol);setImported(null);}} aria-pressed={selected===x.symbol}><span className="watchname"><strong>{x.symbol}</strong><small>{x.name}</small></span><span><strong>${x.base.toFixed(2)}</strong><small className={x.change>=0?"positive":"negative"}>{x.change>=0?"+":""}{x.change.toFixed(2)}%</small></span></button>)}{filtered.length===0&&<p className="noresults">No demo symbols match</p>}</section>
        <section className="panel riskpanel"><div className="sidehead"><h3>Execution boundary</h3><span className="safe-tag">LOCKED</span></div><div className="boundary"><span className="lock">◈</span><strong>Broker disconnected</strong><p>No credentials, order routing, execution endpoints or automated trading.</p></div><div className="rule"><span>Execution mode</span><strong>Research only</strong></div><div className="rule"><span>Reference GET endpoints</span><strong>{apiState.startsWith("Reference GET") ? "Checked" : "Unverified"}</strong></div><div className="rule"><span>Rebalancing policy</span><strong>{policy.rebalance_frequency}</strong></div><div className="rule"><span>Live trading</span><strong className="negative">Disabled</strong></div></section>
      </aside>
    </div>
  </div>;
}
createRoot(document.getElementById("root")).render(<React.StrictMode><App /></React.StrictMode>);
