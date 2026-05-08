/**
 * EntityHoverCard — Contextual risk summary on hover.
 * Replicates third-party vendor's quick-view signals: hover over user_id, sender, IP
 * to see OSINT signals, device info, risk score without navigating away.
 */
import { useState, useRef } from "react";
import {
  User,
  Shield,
  Smartphone,
  Mail,
  Globe,
  AlertTriangle,
  CheckCircle,
  XCircle,
  Loader2,
} from "lucide-react";

interface EntityData {
  entity_id: string;
  risk_score: number;
  risk_level: string;
  device_count: number;
  device_risk: string;
  enrichments: {
    email?: { is_disposable: boolean; social_profiles_estimated: number; risk_score: number };
    ip?: { is_vpn_likely: boolean; is_datacenter: boolean; is_tor_exit: boolean; risk_score: number };
    phone?: { is_voip_likely: boolean; risk_score: number };
  };
  transaction_count: number;
  alert_count: number;
  kyc_level: string;
  pep_status: boolean;
}

interface EntityHoverCardProps {
  entityId: string;
  entityType?: "user" | "sender" | "receiver" | "ip";
  children: React.ReactNode;
}

const API = "/api/v1";

const RISK_COLORS: Record<string, string> = {
  CRITICAL: "text-red-400",
  HIGH: "text-orange-400",
  MEDIUM: "text-yellow-400",
  LOW: "text-green-400",
};

export function EntityHoverCard({ entityId, children }: EntityHoverCardProps) {
  const [visible, setVisible] = useState(false);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<EntityData | null>(null);
  const [position, setPosition] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const triggerRef = useRef<HTMLSpanElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchData = async () => {
    if (data) return; // already cached
    setLoading(true);
    try {
      const res = await fetch(`${API}/debug/entity/${encodeURIComponent(entityId)}`);
      if (res.ok) {
        setData(await res.json());
      }
    } catch {
      // Silently fail — hover card is a nice-to-have
    } finally {
      setLoading(false);
    }
  };

  const handleMouseEnter = () => {
    timeoutRef.current = setTimeout(() => {
      if (triggerRef.current) {
        const rect = triggerRef.current.getBoundingClientRect();
        setPosition({
          top: rect.bottom + 8,
          left: Math.max(rect.left - 100, 16),
        });
      }
      setVisible(true);
      fetchData();
    }, 400); // 400ms delay to avoid flicker
  };

  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    // Delay hide to allow moving to the card
    setTimeout(() => {
      if (!cardRef.current?.matches(":hover") && !triggerRef.current?.matches(":hover")) {
        setVisible(false);
      }
    }, 200);
  };

  return (
    <>
      <span
        ref={triggerRef}
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
        className="cursor-pointer underline decoration-dotted underline-offset-2 decoration-zinc-500 hover:decoration-violet-400 transition-colors"
      >
        {children}
      </span>

      {visible && (
        <div
          ref={cardRef}
          onMouseLeave={handleMouseLeave}
          style={{ top: position.top, left: position.left }}
          className="fixed z-50 w-72 rounded-xl border border-border bg-card shadow-xl overflow-hidden"
        >
          {loading && !data ? (
            <div className="flex items-center justify-center gap-2 py-8 text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span className="text-xs">Loading...</span>
            </div>
          ) : data ? (
            <div className="p-3 space-y-3">
              {/* Header */}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <User className="h-4 w-4 text-violet-400" />
                  <span className="text-xs font-mono font-medium truncate max-w-[140px]">{entityId}</span>
                </div>
                <span className={`text-xs font-bold ${RISK_COLORS[data.risk_level] || ""}`}>
                  {data.risk_score}/100
                </span>
              </div>

              {/* Risk badge */}
              <div className="flex items-center gap-2">
                <span className={`text-[10px] rounded-full px-2 py-0.5 border ${
                  data.risk_level === "CRITICAL" ? "border-red-700 bg-red-900/30 text-red-400" :
                  data.risk_level === "HIGH" ? "border-orange-700 bg-orange-900/30 text-orange-400" :
                  data.risk_level === "MEDIUM" ? "border-yellow-700 bg-yellow-900/30 text-yellow-400" :
                  "border-green-700 bg-green-900/30 text-green-400"
                }`}>
                  {data.risk_level}
                </span>
                {data.pep_status && (
                  <span className="text-[10px] rounded-full px-2 py-0.5 border border-red-700 bg-red-900/30 text-red-400">
                    PEP
                  </span>
                )}
                <span className="text-[10px] rounded px-1.5 py-0.5 bg-zinc-800 text-muted-foreground">
                  KYC: {data.kyc_level}
                </span>
              </div>

              {/* Quick stats */}
              <div className="grid grid-cols-3 gap-2">
                <div className="text-center rounded bg-zinc-800/60 p-1.5">
                  <div className="text-xs font-bold">{data.transaction_count}</div>
                  <div className="text-[9px] text-muted-foreground">Txns</div>
                </div>
                <div className="text-center rounded bg-zinc-800/60 p-1.5">
                  <div className="text-xs font-bold">{data.alert_count}</div>
                  <div className="text-[9px] text-muted-foreground">Alerts</div>
                </div>
                <div className="text-center rounded bg-zinc-800/60 p-1.5">
                  <div className="text-xs font-bold">{data.device_count}</div>
                  <div className="text-[9px] text-muted-foreground">Devices</div>
                </div>
              </div>

              {/* OSINT signals */}
              <div className="space-y-1.5">
                <div className="text-[10px] font-semibold text-muted-foreground">OSINT Signals</div>

                {data.enrichments.email && (
                  <div className="flex items-center gap-2 text-[10px]">
                    <Mail className="h-3 w-3 text-blue-400" />
                    <span className="flex-1">Email</span>
                    {data.enrichments.email.is_disposable ? (
                      <span className="text-red-400 flex items-center gap-1"><XCircle className="h-2.5 w-2.5" /> Disposable</span>
                    ) : (
                      <span className="text-green-400 flex items-center gap-1"><CheckCircle className="h-2.5 w-2.5" /> Valid</span>
                    )}
                    <span className="text-zinc-500">{data.enrichments.email.social_profiles_estimated} profiles</span>
                  </div>
                )}

                {data.enrichments.ip && (
                  <div className="flex items-center gap-2 text-[10px]">
                    <Globe className="h-3 w-3 text-green-400" />
                    <span className="flex-1">IP</span>
                    {data.enrichments.ip.is_vpn_likely && (
                      <span className="text-orange-400 flex items-center gap-1"><AlertTriangle className="h-2.5 w-2.5" /> VPN</span>
                    )}
                    {data.enrichments.ip.is_tor_exit && (
                      <span className="text-red-400 flex items-center gap-1"><XCircle className="h-2.5 w-2.5" /> Tor</span>
                    )}
                    {data.enrichments.ip.is_datacenter && (
                      <span className="text-yellow-400">DC</span>
                    )}
                    {!data.enrichments.ip.is_vpn_likely && !data.enrichments.ip.is_tor_exit && (
                      <span className="text-green-400 flex items-center gap-1"><CheckCircle className="h-2.5 w-2.5" /> Clean</span>
                    )}
                  </div>
                )}

                {data.enrichments.phone && (
                  <div className="flex items-center gap-2 text-[10px]">
                    <Smartphone className="h-3 w-3 text-purple-400" />
                    <span className="flex-1">Phone</span>
                    {data.enrichments.phone.is_voip_likely ? (
                      <span className="text-orange-400 flex items-center gap-1"><AlertTriangle className="h-2.5 w-2.5" /> VoIP</span>
                    ) : (
                      <span className="text-green-400 flex items-center gap-1"><CheckCircle className="h-2.5 w-2.5" /> Mobile</span>
                    )}
                  </div>
                )}

                {!data.enrichments.email && !data.enrichments.ip && !data.enrichments.phone && (
                  <div className="text-[10px] text-zinc-500 italic">No enrichment data yet</div>
                )}
              </div>

              {/* Device signal */}
              <div className="flex items-center gap-2 text-[10px]">
                <Shield className="h-3 w-3 text-violet-400" />
                <span className="flex-1">Device Risk</span>
                <span className={RISK_COLORS[data.device_risk] || ""}>{data.device_risk}</span>
                {data.device_count > 1 && (
                  <span className="text-orange-400">multi-device</span>
                )}
              </div>
            </div>
          ) : (
            <div className="p-4 text-center text-xs text-muted-foreground">
              No data available for this entity
            </div>
          )}
        </div>
      )}
    </>
  );
}
