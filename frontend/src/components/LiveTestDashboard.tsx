import { useState, useRef } from "react";
import {
  Play,
  RotateCcw,
  Send,
  Zap,
  ShieldAlert,
  Activity,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Loader2,
} from "lucide-react";

const API = "/api/v1/realtime";

interface ScoreBreakdown {
  velocity: number;
  amount: number;
  pattern: number;
  duplicate: number;
  entity: number;
  cop: number;
}

interface ScoredResult {
  external_id: string;
  sender_id: string;
  receiver_id: string | null;
  amount: number;
  currency: string;
  transaction_type: string;
  risk_score: number;
  risk_level: string;
  interdiction_action: string;
  factors: string[];
  score_breakdown: ScoreBreakdown;
  velocity_count_2min: number;
  pattern_detected: string;
  scored_at: string;
}

interface EngineState {
  stats: Record<string, number | string | null>;
  total_scored: number;
  total_alerts: number;
  total_interdictions: number;
  recent_alerts: Array<{ id: string; title: string; severity: string; created_at: string }>;
  recent_interdictions: Array<{ id: string; action: string; reason: string }>;
  cop_cache_size: number;
  active_velocity_windows: number;
}

function RiskBadge({ level }: { level: string }) {
  const c: Record<string, string> = {
    CRITICAL: "bg-red-900/60 text-red-200 border-red-600",
    HIGH: "bg-orange-900/50 text-orange-200 border-orange-600",
    MEDIUM: "bg-yellow-900/50 text-yellow-200 border-yellow-600",
    LOW: "bg-green-900/40 text-green-200 border-green-600",
  };
  return <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${c[level] || ""}`}>{level}</span>;
}

function ActionBadge({ action }: { action: string }) {
  const c: Record<string, string> = {
    BLOCK: "bg-red-600 text-white",
    SUSPEND: "bg-orange-600 text-white",
    FLAG: "bg-yellow-600 text-black",
    ALLOW: "bg-green-700 text-white",
  };
  return <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${c[action] || ""}`}>{action}</span>;
}

function ScoreBar({ label, value, max, color }: { label: string; value: number; max: number; color: string }) {
  const pct = max > 0 ? (value / max) * 100 : 0;
  return (
    <div className="flex items-center gap-2 text-[10px]">
      <span className="w-16 text-muted-foreground text-right">{label}</span>
      <div className="flex-1 bg-muted rounded-full h-2 overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.min(pct, 100)}%` }} />
      </div>
      <span className="w-8 font-mono">{value}</span>
    </div>
  );
}

const PRESET_SCENARIOS: Array<{ label: string; desc: string; tx: Record<string, unknown> }> = [
  { label: "Normal Payment", desc: "£50 GBP transfer", tx: { sender_id: "user-alice", amount: 50, currency: "GBP" } },
  { label: "High Value", desc: "£75,000 transfer", tx: { sender_id: "user-bigspend", amount: 75000, currency: "GBP" } },
  { label: "Suspicious Entity", desc: "Receiver flagged", tx: { sender_id: "user-clean", receiver_name: "Fraud Account", amount: 500, currency: "GBP" } },
  { label: "Micro Tx (Pattern)", desc: "£2 small payment", tx: { sender_id: "user-pattern", amount: 2, currency: "GBP" } },
  { label: "Duplicate", desc: "Same amount repeat", tx: { sender_id: "user-dup", amount: 999.99, currency: "GBP" } },
  { label: "COP Failed (AC01)", desc: "Non-existent account", tx: { sender_id: "user-cop", receiver_account_id: "BAD_ACCT_001", amount: 5000, currency: "GBP" } },
];

export function LiveTestDashboard() {
  const [results, setResults] = useState<ScoredResult[]>([]);
  const [engineState, setEngineState] = useState<EngineState | null>(null);
  const [loading, setLoading] = useState(false);
  const [simResult, setSimResult] = useState<{ total: number; summary: Record<string, unknown> } | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  const [customTx, setCustomTx] = useState({
    sender_id: "",
    receiver_id: "",
    amount: "",
    currency: "GBP",
    transaction_type: "SEND_MONEY",
    sender_name: "",
    receiver_name: "",
    receiver_account_id: "",
  });

  const apiCall = async (method: string, path: string, body?: unknown) => {
    const res = await fetch(`${API}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  };

  const scoreTx = async (tx: Record<string, unknown>) => {
    setLoading(true);
    try {
      const result = await apiCall("POST", "/score", tx);
      setResults((prev) => [result, ...prev]);
      refreshState();
      setTimeout(() => logRef.current?.scrollTo({ top: 0, behavior: "smooth" }), 100);
    } finally {
      setLoading(false);
    }
  };

  const refreshState = async () => {
    try {
      const state = await apiCall("GET", "/state");
      setEngineState(state);
    } catch {}
  };

  const resetEngine = async () => {
    await apiCall("POST", "/reset");
    setResults([]);
    setSimResult(null);
    refreshState();
  };

  const runSimulation = async () => {
    setLoading(true);
    try {
      const result = await apiCall("GET", "/simulate");
      setSimResult({ total: result.total_transactions, summary: result.summary });
      setResults(result.results.reverse());
      refreshState();
    } finally {
      setLoading(false);
    }
  };

  const feedCopData = async () => {
    const copFailures = [
      { account_id: "BAD_ACCT_001", reason_code: "AC01", matched: false, requested_name: "Unknown Entity" },
      { account_id: "BAD_ACCT_002", reason_code: "ANNM", matched: false, requested_name: "Example Fintech Ltd" },
      { account_id: "GOOD_ACCT_001", reason_code: null, matched: true, requested_name: "Verified Company" },
    ];
    await apiCall("POST", "/cop-feed", copFailures);
    refreshState();
  };

  const submitCustom = () => {
    const tx: Record<string, unknown> = { sender_id: customTx.sender_id || "custom-user" };
    if (customTx.amount) tx.amount = parseFloat(customTx.amount);
    else tx.amount = 100;
    tx.currency = customTx.currency;
    tx.transaction_type = customTx.transaction_type;
    if (customTx.receiver_id) tx.receiver_id = customTx.receiver_id;
    if (customTx.sender_name) tx.sender_name = customTx.sender_name;
    if (customTx.receiver_name) tx.receiver_name = customTx.receiver_name;
    if (customTx.receiver_account_id) tx.receiver_account_id = customTx.receiver_account_id;
    scoreTx(tx);
  };

  const runVelocityBurst = async () => {
    setLoading(true);
    try {
      const sender = `burst-${Date.now()}`;
      for (let i = 0; i < 12; i++) {
        await apiCall("POST", "/score", { sender_id: sender, amount: 25 + i, currency: "GBP" });
      }
      // Refresh all
      const state = await apiCall("GET", "/state");
      setEngineState(state);
      // Get latest results
      const last = await apiCall("POST", "/score", { sender_id: sender, amount: 60000, currency: "GBP" });
      setResults((prev) => [last, ...prev]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-4">
      {/* Control Bar */}
      <div className="flex flex-wrap gap-2 items-center">
        <button onClick={resetEngine} className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-muted hover:bg-muted/80 rounded-md">
          <RotateCcw className="h-3 w-3" /> Reset Engine
        </button>
        <button onClick={refreshState} className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-muted hover:bg-muted/80 rounded-md">
          <Activity className="h-3 w-3" /> Refresh State
        </button>
        <button onClick={feedCopData} className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-blue-900/50 hover:bg-blue-900/70 text-blue-200 rounded-md">
          <ShieldAlert className="h-3 w-3" /> Load COP Data
        </button>
        <button onClick={runSimulation} disabled={loading} className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-purple-900/50 hover:bg-purple-900/70 text-purple-200 rounded-md disabled:opacity-50">
          {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}
          Replay Demo Data
        </button>
        <button onClick={runVelocityBurst} disabled={loading} className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-red-900/50 hover:bg-red-900/70 text-red-200 rounded-md disabled:opacity-50">
          <Zap className="h-3 w-3" /> Velocity Burst (12 rapid txns)
        </button>
      </div>

      {/* Engine State */}
      {engineState && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
          {[
            { label: "Scored", value: engineState.total_scored, color: "text-blue-400" },
            { label: "Alerts", value: engineState.total_alerts, color: "text-yellow-400" },
            { label: "Interdictions", value: engineState.total_interdictions, color: "text-red-400" },
            { label: "Velocity Windows", value: engineState.active_velocity_windows, color: "text-purple-400" },
            { label: "COP Cache", value: engineState.cop_cache_size, color: "text-cyan-400" },
            { label: "Total", value: `${engineState.stats.total_allowed ?? 0}A / ${engineState.stats.total_flagged ?? 0}F`, color: "text-green-400" },
          ].map((s) => (
            <div key={s.label} className="bg-card border border-border rounded p-2 text-center">
              <div className={`text-lg font-bold ${s.color}`}>{s.value}</div>
              <div className="text-[9px] text-muted-foreground">{s.label}</div>
            </div>
          ))}
        </div>
      )}

      {/* Simulation summary */}
      {simResult && (
        <div className="bg-purple-900/20 border border-purple-800/40 rounded-lg p-3 text-xs">
          <span className="font-bold text-purple-300">Simulation Complete:</span>{" "}
          {simResult.total} demo transactions replayed.{" "}
          Flagged: {String((simResult.summary as Record<string, unknown>).flagged ?? 0)}
        </div>
      )}

      <div className="grid md:grid-cols-3 gap-4">
        {/* Left: Preset Scenarios */}
        <div className="space-y-3">
          <h3 className="text-xs font-bold flex items-center gap-1.5">
            <Zap className="h-3.5 w-3.5 text-yellow-400" /> Quick Scenarios
          </h3>
          <div className="space-y-1.5">
            {PRESET_SCENARIOS.map((s) => (
              <button
                key={s.label}
                onClick={() => scoreTx(s.tx)}
                disabled={loading}
                className="w-full text-left px-3 py-2 bg-card border border-border rounded hover:bg-muted/50 transition-colors disabled:opacity-50"
              >
                <div className="text-xs font-medium">{s.label}</div>
                <div className="text-[10px] text-muted-foreground">{s.desc}</div>
              </button>
            ))}
          </div>

          {/* Custom Transaction */}
          <h3 className="text-xs font-bold mt-4 flex items-center gap-1.5">
            <Send className="h-3.5 w-3.5 text-blue-400" /> Custom Transaction
          </h3>
          <div className="space-y-1.5 text-xs">
            <input
              placeholder="Sender ID"
              value={customTx.sender_id}
              onChange={(e) => setCustomTx((p) => ({ ...p, sender_id: e.target.value }))}
              className="w-full px-2 py-1.5 bg-muted border border-border rounded text-xs"
            />
            <input
              placeholder="Amount"
              type="number"
              value={customTx.amount}
              onChange={(e) => setCustomTx((p) => ({ ...p, amount: e.target.value }))}
              className="w-full px-2 py-1.5 bg-muted border border-border rounded text-xs"
            />
            <div className="grid grid-cols-2 gap-1.5">
              <select
                value={customTx.currency}
                onChange={(e) => setCustomTx((p) => ({ ...p, currency: e.target.value }))}
                className="px-2 py-1.5 bg-muted border border-border rounded text-xs"
              >
                <option>GBP</option>
                <option>EUR</option>
                <option>USD</option>
              </select>
              <select
                value={customTx.transaction_type}
                onChange={(e) => setCustomTx((p) => ({ ...p, transaction_type: e.target.value }))}
                className="px-2 py-1.5 bg-muted border border-border rounded text-xs"
              >
                <option>SEND_MONEY</option>
                <option>ADD_MONEY</option>
                <option>DIRECT_DEBIT</option>
                <option>CARD_PAYMENT</option>
              </select>
            </div>
            <input
              placeholder="Receiver Name (optional)"
              value={customTx.receiver_name}
              onChange={(e) => setCustomTx((p) => ({ ...p, receiver_name: e.target.value }))}
              className="w-full px-2 py-1.5 bg-muted border border-border rounded text-xs"
            />
            <input
              placeholder="Receiver Account ID (for COP)"
              value={customTx.receiver_account_id}
              onChange={(e) => setCustomTx((p) => ({ ...p, receiver_account_id: e.target.value }))}
              className="w-full px-2 py-1.5 bg-muted border border-border rounded text-xs"
            />
            <button
              onClick={submitCustom}
              disabled={loading}
              className="w-full flex items-center justify-center gap-1.5 px-3 py-2 bg-blue-700 hover:bg-blue-600 text-white rounded text-xs font-medium disabled:opacity-50"
            >
              {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Send className="h-3 w-3" />}
              Score Transaction
            </button>
          </div>
        </div>

        {/* Right: Results log */}
        <div className="md:col-span-2">
          <h3 className="text-xs font-bold mb-2 flex items-center gap-1.5">
            <Activity className="h-3.5 w-3.5 text-green-400" /> Scoring Results ({results.length})
          </h3>
          <div ref={logRef} className="space-y-2 max-h-[600px] overflow-y-auto pr-1">
            {results.length === 0 && (
              <div className="text-xs text-muted-foreground p-8 text-center">
                Submit a transaction or run a scenario to see results
              </div>
            )}
            {results.map((r, i) => (
              <div
                key={r.external_id + i}
                className={`bg-card border rounded-lg p-3 ${
                  r.risk_score >= 75
                    ? "border-red-700/60"
                    : r.risk_score >= 50
                    ? "border-orange-700/50"
                    : r.risk_score >= 25
                    ? "border-yellow-700/40"
                    : "border-border"
                }`}
              >
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    {r.risk_score >= 50 ? (
                      <XCircle className="h-4 w-4 text-red-400" />
                    ) : r.risk_score >= 25 ? (
                      <AlertTriangle className="h-4 w-4 text-yellow-400" />
                    ) : (
                      <CheckCircle2 className="h-4 w-4 text-green-400" />
                    )}
                    <span className="text-xs font-mono">{r.external_id.slice(0, 8)}…</span>
                    <RiskBadge level={r.risk_level} />
                    <ActionBadge action={r.interdiction_action} />
                  </div>
                  <span className="text-2xl font-bold font-mono">
                    {r.risk_score}
                  </span>
                </div>

                <div className="grid grid-cols-3 gap-x-4 text-[10px] text-muted-foreground mb-2">
                  <span>Sender: <span className="text-foreground">{r.sender_id}</span></span>
                  <span>Amount: <span className="text-foreground">£{r.amount.toLocaleString()}</span></span>
                  <span>Velocity: <span className="text-foreground">{r.velocity_count_2min} txns/2min</span></span>
                </div>

                {/* Score Breakdown Bars */}
                <div className="space-y-0.5">
                  <ScoreBar label="Velocity" value={r.score_breakdown.velocity} max={40} color="bg-purple-500" />
                  <ScoreBar label="Amount" value={r.score_breakdown.amount} max={35} color="bg-blue-500" />
                  <ScoreBar label="Pattern" value={r.score_breakdown.pattern} max={25} color="bg-yellow-500" />
                  <ScoreBar label="Duplicate" value={r.score_breakdown.duplicate} max={15} color="bg-orange-500" />
                  <ScoreBar label="Entity" value={r.score_breakdown.entity} max={10} color="bg-red-500" />
                  <ScoreBar label="COP" value={r.score_breakdown.cop} max={40} color="bg-cyan-500" />
                </div>

                {r.factors.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {r.factors.map((f, fi) => (
                      <span key={fi} className="px-1.5 py-0.5 bg-muted rounded text-[9px] font-mono">
                        {f}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
