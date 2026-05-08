/**
 * VisualDebugger — Score breakdown waterfall + rule execution trace.
 * Replicates third-party vendor's Visual Debugger: shows exactly which conditions fired,
 * how each scoring factor contributed, and the final decision path.
 */
import { useState } from "react";
import {
  Bug,
  Search,
  ChevronDown,
  ChevronRight,
  CheckCircle,
  XCircle,
  AlertTriangle,
  Zap,
  Shield,
  BarChart3,
  Loader2,
  Clock,
  ArrowRight,
} from "lucide-react";

interface ScoreFactor {
  name: string;
  category: string;
  score: number;
  max_score: number;
  triggered: boolean;
  detail: string;
}

interface RuleMatch {
  rule_id: string;
  rule_name: string;
  matched_field: string;
  matched_value: string;
  action: string;
  risk_adjustment: number;
  matched_at: string;
}

interface CEPPattern {
  pattern_type: string;
  details: string;
  severity: string;
  risk_adjustment: number;
  detected_at: string;
}

interface EnrichmentSignal {
  type: string;
  data: Record<string, unknown>;
  risk_score: number;
}

interface DebugResult {
  transaction_id: string;
  sender_id: string;
  amount: number;
  currency: string;
  final_risk_score: number;
  final_risk_level: string;
  final_action: string;
  score_factors: ScoreFactor[];
  rule_matches: RuleMatch[];
  cep_patterns: CEPPattern[];
  enrichment_signals: EnrichmentSignal[];
  decision_path: string[];
  processing_time_ms: number;
}

const API = "/api/v1";

const SEVERITY_COLORS: Record<string, string> = {
  CRITICAL: "text-red-400 bg-red-900/30",
  HIGH: "text-orange-400 bg-orange-900/30",
  MEDIUM: "text-yellow-400 bg-yellow-900/30",
  LOW: "text-green-400 bg-green-900/30",
};

const ACTION_COLORS: Record<string, string> = {
  BLOCK: "text-red-400 bg-red-900/40 border-red-700",
  SUSPEND: "text-orange-400 bg-orange-900/40 border-orange-700",
  FLAG: "text-yellow-400 bg-yellow-900/40 border-yellow-700",
  ALLOW: "text-green-400 bg-green-900/40 border-green-700",
};

export function VisualDebugger() {
  const [txId, setTxId] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<DebugResult | null>(null);
  const [error, setError] = useState("");
  const [expandedSections, setExpandedSections] = useState<Set<string>>(
    new Set(["factors", "rules", "cep", "enrichment", "decision"])
  );

  const toggleSection = (s: string) => {
    setExpandedSections((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  };

  const handleDebug = async () => {
    if (!txId.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);

    try {
      const res = await fetch(`${API}/debug/transaction/${encodeURIComponent(txId.trim())}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Debug failed" }));
        throw new Error(err.detail || `Error ${res.status}`);
      }
      const data: DebugResult = await res.json();
      setResult(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to debug transaction");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Bug className="h-5 w-5 text-violet-400" />
        <h2 className="text-lg font-semibold">Visual Debugger</h2>
        <span className="text-xs text-muted-foreground">
          Trace exactly how a transaction was scored, which rules fired, and what decision was made
        </span>
      </div>

      {/* Search */}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <input
            value={txId}
            onChange={(e) => setTxId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleDebug()}
            placeholder="Enter transaction ID or external_id..."
            className="w-full rounded-lg border border-border bg-card pl-10 pr-4 py-2.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-violet-500/50"
          />
        </div>
        <button
          onClick={handleDebug}
          disabled={loading || !txId.trim()}
          className="rounded-lg bg-violet-600 px-6 py-2.5 text-sm font-medium hover:bg-violet-500 disabled:opacity-50 transition-colors"
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : "Debug"}
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 rounded-lg border border-red-700/50 bg-red-900/20 px-4 py-3 text-sm text-red-400">
          <XCircle className="h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      {result && (
        <div className="space-y-4">
          {/* Summary card */}
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center justify-between mb-3">
              <div>
                <div className="text-xs text-muted-foreground">Transaction</div>
                <div className="text-sm font-mono">{result.transaction_id}</div>
              </div>
              <div className={`rounded-lg border px-4 py-2 text-center ${ACTION_COLORS[result.final_action] || ""}`}>
                <div className="text-lg font-bold">{result.final_action}</div>
                <div className="text-[10px]">{result.final_risk_level}</div>
              </div>
            </div>

            <div className="grid grid-cols-4 gap-3">
              <div className="rounded bg-zinc-800/60 p-2 text-center">
                <div className="text-lg font-bold">{result.final_risk_score}</div>
                <div className="text-[10px] text-muted-foreground">Risk Score</div>
              </div>
              <div className="rounded bg-zinc-800/60 p-2 text-center">
                <div className="text-sm font-bold font-mono">{result.amount.toLocaleString()}</div>
                <div className="text-[10px] text-muted-foreground">{result.currency}</div>
              </div>
              <div className="rounded bg-zinc-800/60 p-2 text-center">
                <div className="text-sm font-bold">{result.sender_id.slice(0, 12)}...</div>
                <div className="text-[10px] text-muted-foreground">Sender</div>
              </div>
              <div className="rounded bg-zinc-800/60 p-2 text-center">
                <div className="text-sm font-bold">{result.processing_time_ms}ms</div>
                <div className="text-[10px] text-muted-foreground">Latency</div>
              </div>
            </div>
          </div>

          {/* Score Waterfall */}
          <SectionHeader
            title="Score Breakdown"
            icon={BarChart3}
            count={result.score_factors.filter((f) => f.triggered).length}
            total={result.score_factors.length}
            expanded={expandedSections.has("factors")}
            onToggle={() => toggleSection("factors")}
          />
          {expandedSections.has("factors") && (
            <div className="space-y-2 pl-2">
              {result.score_factors.map((f, i) => (
                <div key={i} className="flex items-center gap-3">
                  {f.triggered ? (
                    <AlertTriangle className="h-3.5 w-3.5 text-orange-400 shrink-0" />
                  ) : (
                    <CheckCircle className="h-3.5 w-3.5 text-green-400 shrink-0" />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-medium">{f.name}</span>
                      <span className="text-[9px] rounded px-1 bg-zinc-800 text-muted-foreground">{f.category}</span>
                    </div>
                    <div className="text-[10px] text-muted-foreground">{f.detail}</div>
                  </div>
                  {/* Score bar */}
                  <div className="w-32 flex items-center gap-2">
                    <div className="flex-1 h-2 rounded-full bg-zinc-800 overflow-hidden">
                      <div
                        className={`h-full rounded-full ${f.triggered ? "bg-orange-500" : "bg-zinc-600"}`}
                        style={{ width: `${(f.score / Math.max(f.max_score, 1)) * 100}%` }}
                      />
                    </div>
                    <span className={`text-xs font-mono w-8 text-right ${f.triggered ? "text-orange-400" : "text-zinc-500"}`}>
                      +{f.score}
                    </span>
                  </div>
                </div>
              ))}

              {/* Total */}
              <div className="flex items-center gap-3 mt-2 pt-2 border-t border-border">
                <Shield className="h-3.5 w-3.5 text-violet-400 shrink-0" />
                <span className="text-xs font-semibold flex-1">Total Risk Score</span>
                <span className="text-sm font-bold text-violet-400">{result.final_risk_score}/100</span>
              </div>
            </div>
          )}

          {/* Rule Matches */}
          <SectionHeader
            title="Dynamic Rule Matches"
            icon={Zap}
            count={result.rule_matches.length}
            expanded={expandedSections.has("rules")}
            onToggle={() => toggleSection("rules")}
          />
          {expandedSections.has("rules") && (
            <div className="space-y-2 pl-2">
              {result.rule_matches.length === 0 ? (
                <div className="text-xs text-muted-foreground py-2">No dynamic rules triggered</div>
              ) : (
                result.rule_matches.map((r, i) => (
                  <div key={i} className="rounded border border-border bg-zinc-900/50 p-3">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <Zap className="h-3 w-3 text-yellow-400" />
                        <span className="text-xs font-medium">{r.rule_name}</span>
                      </div>
                      <span className={`text-[10px] rounded border px-2 py-0.5 font-bold ${ACTION_COLORS[r.action] || ""}`}>
                        {r.action} (+{r.risk_adjustment})
                      </span>
                    </div>
                    <div className="mt-1 text-[10px] text-muted-foreground">
                      <span className="font-mono">{r.matched_field}</span> = <span className="font-mono">{r.matched_value}</span>
                    </div>
                  </div>
                ))
              )}
            </div>
          )}

          {/* CEP Patterns */}
          <SectionHeader
            title="CEP Pattern Matches"
            icon={AlertTriangle}
            count={result.cep_patterns.length}
            expanded={expandedSections.has("cep")}
            onToggle={() => toggleSection("cep")}
          />
          {expandedSections.has("cep") && (
            <div className="space-y-2 pl-2">
              {result.cep_patterns.length === 0 ? (
                <div className="text-xs text-muted-foreground py-2">No CEP patterns detected</div>
              ) : (
                result.cep_patterns.map((p, i) => (
                  <div key={i} className="rounded border border-border bg-zinc-900/50 p-3">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <AlertTriangle className="h-3 w-3 text-red-400" />
                        <span className="text-xs font-bold">{p.pattern_type.replace(/_/g, " ")}</span>
                      </div>
                      <span className={`text-[10px] rounded px-2 py-0.5 ${SEVERITY_COLORS[p.severity] || ""}`}>
                        {p.severity} (+{p.risk_adjustment})
                      </span>
                    </div>
                    <div className="mt-1 text-[10px] text-muted-foreground">{p.details}</div>
                  </div>
                ))
              )}
            </div>
          )}

          {/* Enrichment Signals */}
          <SectionHeader
            title="OSINT Enrichment Signals"
            icon={Search}
            count={result.enrichment_signals.length}
            expanded={expandedSections.has("enrichment")}
            onToggle={() => toggleSection("enrichment")}
          />
          {expandedSections.has("enrichment") && (
            <div className="space-y-2 pl-2">
              {result.enrichment_signals.length === 0 ? (
                <div className="text-xs text-muted-foreground py-2">No enrichment data available</div>
              ) : (
                result.enrichment_signals.map((s, i) => (
                  <div key={i} className="rounded border border-border bg-zinc-900/50 p-3">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-medium capitalize">{s.type.replace(/_/g, " ")}</span>
                      <span className="text-[10px] font-mono text-muted-foreground">
                        risk: {s.risk_score}
                      </span>
                    </div>
                    <div className="mt-1 text-[10px] text-muted-foreground font-mono">
                      {JSON.stringify(s.data).slice(0, 200)}
                    </div>
                  </div>
                ))
              )}
            </div>
          )}

          {/* Decision Path */}
          <SectionHeader
            title="Decision Path"
            icon={ArrowRight}
            count={result.decision_path.length}
            expanded={expandedSections.has("decision")}
            onToggle={() => toggleSection("decision")}
          />
          {expandedSections.has("decision") && (
            <div className="pl-2">
              <div className="relative border-l-2 border-violet-600/40 ml-2 space-y-2 py-1">
                {result.decision_path.map((step, i) => (
                  <div key={i} className="flex items-start gap-2 ml-4 relative">
                    <div className="absolute -left-[21px] top-1 w-2 h-2 rounded-full bg-violet-500" />
                    <Clock className="h-3 w-3 text-muted-foreground mt-0.5 shrink-0" />
                    <span className="text-[11px]">{step}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SectionHeader({
  title,
  icon: Icon,
  count,
  total,
  expanded,
  onToggle,
}: {
  title: string;
  icon: typeof BarChart3;
  count: number;
  total?: number;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <button onClick={onToggle} className="flex items-center gap-2 w-full text-left group">
      {expanded ? (
        <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
      ) : (
        <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
      )}
      <Icon className="h-4 w-4 text-violet-400" />
      <span className="text-sm font-semibold group-hover:text-foreground">{title}</span>
      <span className="text-[10px] rounded-full px-2 py-0.5 bg-zinc-800 text-muted-foreground">
        {count}{total !== undefined ? `/${total}` : ""}
      </span>
    </button>
  );
}
