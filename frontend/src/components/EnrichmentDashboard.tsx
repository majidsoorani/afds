/**
 * EnrichmentDashboard — OSINT Enrichment UI for email/IP/phone analysis.
 * Replicates third-party vendor's Digital Footprinting: analysts can look up any
 * email, IP, or phone and see risk signals, social profiles, VPN/Tor detection.
 */
import { useState } from "react";
import {
  Search,
  Mail,
  Globe,
  Smartphone,
  AlertTriangle,
  CheckCircle,
  XCircle,
  Loader2,
  Zap,
} from "lucide-react";

type EnrichType = "email" | "ip" | "phone" | "transaction";

interface EnrichResult {
  [key: string]: unknown;
}

const API = "/api/v1/enrichment";

const TAB_CONFIG: { id: EnrichType; label: string; icon: typeof Mail; placeholder: string }[] = [
  { id: "email", label: "Email", icon: Mail, placeholder: "user@example.com" },
  { id: "ip", label: "IP Address", icon: Globe, placeholder: "203.0.113.42" },
  { id: "phone", label: "Phone", icon: Smartphone, placeholder: "+44 7911 123456" },
  { id: "transaction", label: "Transaction", icon: Zap, placeholder: "Transaction ID" },
];

export function EnrichmentDashboard() {
  const [activeType, setActiveType] = useState<EnrichType>("email");
  const [input, setInput] = useState("");
  const [entityId, setEntityId] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<EnrichResult | null>(null);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<{ type: string; input: string; risk_score: number; time: Date }[]>([]);

  const handleEnrich = async () => {
    if (!input.trim()) return;
    setLoading(true);
    setError("");
    setResult(null);

    try {
      let body: Record<string, string>;
      let endpoint: string;

      switch (activeType) {
        case "email":
          body = { email: input, entity_id: entityId };
          endpoint = `${API}/email`;
          break;
        case "ip":
          body = { ip_address: input, entity_id: entityId };
          endpoint = `${API}/ip`;
          break;
        case "phone":
          body = { phone: input, entity_id: entityId };
          endpoint = `${API}/phone`;
          break;
        case "transaction":
          body = { transaction_id: input };
          endpoint = `${API}/transaction`;
          break;
      }

      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Enrichment failed" }));
        throw new Error(err.detail || `Error ${res.status}`);
      }

      const data: EnrichResult = await res.json();
      setResult(data);

      const riskScore = typeof data.risk_score === "number" ? data.risk_score :
                        typeof data.combined_risk_score === "number" ? data.combined_risk_score : 0;
      setHistory((prev) => [{ type: activeType, input, risk_score: riskScore, time: new Date() }, ...prev.slice(0, 19)]);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Enrichment failed");
    } finally {
      setLoading(false);
    }
  };

  const riskScoreFromResult = result
    ? (typeof result.risk_score === "number" ? result.risk_score :
       typeof result.combined_risk_score === "number" ? result.combined_risk_score : null)
    : null;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Search className="h-5 w-5 text-violet-400" />
        <h2 className="text-lg font-semibold">OSINT Enrichment</h2>
        <span className="text-xs text-muted-foreground">
          Digital Footprinting — analyze emails, IPs, and phone numbers for fraud signals
        </span>
      </div>

      {/* Type tabs */}
      <div className="flex gap-2">
        {TAB_CONFIG.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              onClick={() => { setActiveType(tab.id); setResult(null); setError(""); }}
              className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm transition-colors ${
                activeType === tab.id
                  ? "bg-violet-600/20 text-violet-400 border border-violet-600/40"
                  : "bg-card border border-border text-muted-foreground hover:text-foreground"
              }`}
            >
              <Icon className="h-4 w-4" />
              {tab.label}
            </button>
          );
        })}
      </div>

      {/* Input */}
      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleEnrich()}
          placeholder={TAB_CONFIG.find((t) => t.id === activeType)?.placeholder}
          className="flex-1 rounded-lg border border-border bg-card px-4 py-2.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-violet-500/50"
        />
        {activeType !== "transaction" && (
          <input
            value={entityId}
            onChange={(e) => setEntityId(e.target.value)}
            placeholder="Entity ID (optional)"
            className="w-48 rounded-lg border border-border bg-card px-4 py-2.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-violet-500/50"
          />
        )}
        <button
          onClick={handleEnrich}
          disabled={loading || !input.trim()}
          className="rounded-lg bg-violet-600 px-6 py-2.5 text-sm font-medium hover:bg-violet-500 disabled:opacity-50 transition-colors"
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : "Enrich"}
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 rounded-lg border border-red-700/50 bg-red-900/20 px-4 py-3 text-sm text-red-400">
          <XCircle className="h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      {/* Result */}
      {result && (
        <div className="rounded-xl border border-border bg-card p-4 space-y-4">
          {/* Risk score header */}
          {riskScoreFromResult !== null && (
            <div className="flex items-center gap-4">
              <div className={`rounded-lg p-3 text-center min-w-[80px] ${
                riskScoreFromResult >= 50 ? "bg-red-900/30 border border-red-700" :
                riskScoreFromResult >= 25 ? "bg-yellow-900/30 border border-yellow-700" :
                "bg-green-900/30 border border-green-700"
              }`}>
                <div className={`text-2xl font-bold ${
                  riskScoreFromResult >= 50 ? "text-red-400" :
                  riskScoreFromResult >= 25 ? "text-yellow-400" :
                  "text-green-400"
                }`}>{riskScoreFromResult}</div>
                <div className="text-[10px] text-muted-foreground">Risk Score</div>
              </div>
              <div className="flex-1">
                <div className="text-sm font-medium">{input}</div>
                <div className="text-xs text-muted-foreground capitalize">{activeType} enrichment</div>
              </div>
            </div>
          )}

          {/* Signals grid */}
          <div className="space-y-2">
            {Object.entries(result).map(([key, value]) => {
              if (key === "risk_score" || key === "combined_risk_score") return null;
              if (typeof value === "object" && value !== null && !Array.isArray(value)) {
                return (
                  <div key={key} className="rounded bg-zinc-800/50 p-3">
                    <div className="text-[10px] font-semibold text-muted-foreground mb-2 capitalize">
                      {key.replace(/_/g, " ")}
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      {Object.entries(value as Record<string, unknown>).map(([k, v]) => (
                        <SignalRow key={k} label={k} value={v} />
                      ))}
                    </div>
                  </div>
                );
              }
              return <SignalRow key={key} label={key} value={value} />;
            })}
          </div>
        </div>
      )}

      {/* History */}
      {history.length > 0 && (
        <div>
          <div className="text-xs font-semibold text-muted-foreground mb-2">Recent Lookups</div>
          <div className="space-y-1">
            {history.map((h, i) => (
              <div key={i} className="flex items-center gap-3 text-xs rounded bg-zinc-800/30 px-3 py-1.5">
                <span className="text-[10px] rounded px-1.5 py-0.5 bg-zinc-800 text-muted-foreground capitalize w-16 text-center">
                  {h.type}
                </span>
                <span className="flex-1 font-mono truncate">{h.input}</span>
                <span className={`font-bold ${
                  h.risk_score >= 50 ? "text-red-400" : h.risk_score >= 25 ? "text-yellow-400" : "text-green-400"
                }`}>{h.risk_score}</span>
                <span className="text-zinc-500">{h.time.toLocaleTimeString()}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function SignalRow({ label, value }: { label: string; value: unknown }) {
  const isBoolean = typeof value === "boolean";
  const isBadSignal = isBoolean && value === true &&
    (label.includes("disposable") || label.includes("vpn") || label.includes("tor") ||
     label.includes("datacenter") || label.includes("voip") || label.includes("webdriver") ||
     label.includes("emulator") || label.includes("proxy") || label.includes("bot"));
  const isGoodSignal = isBoolean && value === false &&
    (label.includes("disposable") || label.includes("vpn") || label.includes("tor"));

  return (
    <div className="flex items-center justify-between text-[11px] py-0.5">
      <span className="text-muted-foreground capitalize">{label.replace(/_/g, " ")}</span>
      <span className="font-mono">
        {isBoolean ? (
          isBadSignal ? (
            <span className="text-red-400 flex items-center gap-1">
              <AlertTriangle className="h-2.5 w-2.5" /> Yes
            </span>
          ) : isGoodSignal ? (
            <span className="text-green-400 flex items-center gap-1">
              <CheckCircle className="h-2.5 w-2.5" /> No
            </span>
          ) : (
            String(value)
          )
        ) : typeof value === "number" ? (
          value
        ) : Array.isArray(value) ? (
          <span className="text-zinc-400 truncate max-w-[200px]">{(value as string[]).join(", ")}</span>
        ) : (
          <span className="truncate max-w-[200px]">{String(value)}</span>
        )}
      </span>
    </div>
  );
}
