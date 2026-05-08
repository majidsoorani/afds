/**
 * DeviceIntelDashboard — Device fingerprint intelligence & multi-accounting detection.
 * Replicates third-party vendor's device intelligence: collect fingerprints, detect emulators,
 * VPNs, Tor, headless browsers, and cross-user device linkage.
 */
import { useState } from "react";
import {
  Smartphone,
  Search,
  User,
  AlertTriangle,
  XCircle,
  Loader2,
  Monitor,
  Fingerprint,
  Link,
} from "lucide-react";

interface DeviceSighting {
  device_hash: string;
  user_id: string;
  session_id: string;
  ip_address: string;
  risk_score: number;
  risk_level: string;
  anomalies: string;
  webgl_renderer: string;
  platform: string;
  typing_entropy: number;
  mouse_entropy: number;
  webdriver: boolean;
  created_at: string;
}

interface DeviceResult {
  device_hash: string;
  sightings: number;
  distinct_users: number;
  multi_accounting: boolean;
  latest: DeviceSighting;
  history: DeviceSighting[];
}

interface UserDeviceResult {
  user_id: string;
  device_count: number;
  devices: {
    device_hash: string;
    ip_address: string;
    platform: string;
    webgl_renderer: string;
    risk_score: number;
    risk_level: string;
    webdriver: boolean;
    created_at: string;
  }[];
  device_switching_risk: string;
}

const API = "/api/v1/device";

const RISK_COLORS: Record<string, string> = {
  CRITICAL: "text-red-400 bg-red-900/30 border-red-700",
  HIGH: "text-orange-400 bg-orange-900/30 border-orange-700",
  MEDIUM: "text-yellow-400 bg-yellow-900/30 border-yellow-700",
  LOW: "text-green-400 bg-green-900/30 border-green-700",
};

type SearchMode = "device" | "user";

export function DeviceIntelDashboard() {
  const [mode, setMode] = useState<SearchMode>("device");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [deviceResult, setDeviceResult] = useState<DeviceResult | null>(null);
  const [userResult, setUserResult] = useState<UserDeviceResult | null>(null);
  const [error, setError] = useState("");

  const handleSearch = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError("");
    setDeviceResult(null);
    setUserResult(null);

    try {
      const endpoint = mode === "device"
        ? `${API}/${encodeURIComponent(query.trim())}`
        : `${API}/user/${encodeURIComponent(query.trim())}`;

      const res = await fetch(endpoint);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Not found" }));
        throw new Error(err.detail || `Error ${res.status}`);
      }

      if (mode === "device") {
        setDeviceResult(await res.json());
      } else {
        setUserResult(await res.json());
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Search failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Fingerprint className="h-5 w-5 text-violet-400" />
        <h2 className="text-lg font-semibold">Device Intelligence</h2>
        <span className="text-xs text-muted-foreground">
          Fingerprint analysis, emulator/bot detection, multi-accounting linkage
        </span>
      </div>

      {/* Mode toggle */}
      <div className="flex gap-2">
        <button
          onClick={() => { setMode("device"); setDeviceResult(null); setUserResult(null); setError(""); }}
          className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm transition-colors ${
            mode === "device" ? "bg-violet-600/20 text-violet-400 border border-violet-600/40" : "bg-card border border-border text-muted-foreground hover:text-foreground"
          }`}
        >
          <Monitor className="h-4 w-4" />
          Lookup Device
        </button>
        <button
          onClick={() => { setMode("user"); setDeviceResult(null); setUserResult(null); setError(""); }}
          className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm transition-colors ${
            mode === "user" ? "bg-violet-600/20 text-violet-400 border border-violet-600/40" : "bg-card border border-border text-muted-foreground hover:text-foreground"
          }`}
        >
          <User className="h-4 w-4" />
          User Devices
        </button>
      </div>

      {/* Search */}
      <div className="flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          placeholder={mode === "device" ? "Enter device hash..." : "Enter user ID..."}
          className="flex-1 rounded-lg border border-border bg-card px-4 py-2.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-violet-500/50"
        />
        <button
          onClick={handleSearch}
          disabled={loading || !query.trim()}
          className="rounded-lg bg-violet-600 px-6 py-2.5 text-sm font-medium hover:bg-violet-500 disabled:opacity-50 transition-colors"
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 rounded-lg border border-red-700/50 bg-red-900/20 px-4 py-3 text-sm text-red-400">
          <XCircle className="h-4 w-4 shrink-0" /> {error}
        </div>
      )}

      {/* Device result */}
      {deviceResult && (
        <div className="space-y-4">
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center justify-between mb-4">
              <div>
                <div className="text-xs text-muted-foreground">Device Hash</div>
                <div className="text-sm font-mono">{deviceResult.device_hash}</div>
              </div>
              {deviceResult.multi_accounting && (
                <div className="flex items-center gap-2 rounded-lg border border-red-700 bg-red-900/30 px-3 py-2 text-xs text-red-400">
                  <Link className="h-4 w-4" />
                  MULTI-ACCOUNTING: {deviceResult.distinct_users} users
                </div>
              )}
            </div>

            <div className="grid grid-cols-4 gap-3 mb-4">
              <Stat label="Sightings" value={deviceResult.sightings} />
              <Stat label="Distinct Users" value={deviceResult.distinct_users} alert={deviceResult.distinct_users > 1} />
              <Stat label="Risk Score" value={deviceResult.latest.risk_score} alert={deviceResult.latest.risk_score >= 50} />
              <Stat label="Risk Level" value={deviceResult.latest.risk_level} color={RISK_COLORS[deviceResult.latest.risk_level]} />
            </div>

            {/* Latest fingerprint details */}
            <div className="rounded bg-zinc-800/50 p-3 space-y-2">
              <div className="text-[10px] font-semibold text-muted-foreground">Latest Fingerprint</div>
              <div className="grid grid-cols-2 gap-2 text-[11px]">
                <Detail label="Platform" value={deviceResult.latest.platform} />
                <Detail label="WebGL Renderer" value={deviceResult.latest.webgl_renderer} />
                <Detail label="IP Address" value={deviceResult.latest.ip_address} />
                <Detail label="Typing Entropy" value={String(deviceResult.latest.typing_entropy)} />
                <Detail label="Mouse Entropy" value={String(deviceResult.latest.mouse_entropy)} />
                <Detail label="Webdriver" value={deviceResult.latest.webdriver ? "YES ⚠️" : "No"} alert={deviceResult.latest.webdriver} />
              </div>
            </div>

            {/* Anomalies */}
            {deviceResult.latest.anomalies && (
              <div className="mt-3">
                <div className="text-[10px] font-semibold text-muted-foreground mb-1">Anomalies Detected</div>
                <div className="flex flex-wrap gap-1">
                  {JSON.parse(deviceResult.latest.anomalies || "[]").map((a: { type: string; detail: string; weight: number }, i: number) => (
                    <span key={i} className="text-[10px] rounded-full px-2 py-0.5 border border-orange-700 bg-orange-900/30 text-orange-400">
                      {a.type} (+{a.weight})
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* History */}
          {deviceResult.history.length > 1 && (
            <div>
              <div className="text-xs font-semibold text-muted-foreground mb-2">Sighting History</div>
              <div className="rounded border border-border overflow-hidden">
                <table className="w-full text-[11px]">
                  <thead className="bg-zinc-800">
                    <tr>
                      <th className="text-left px-3 py-1.5">User</th>
                      <th className="text-left px-3 py-1.5">IP</th>
                      <th className="text-left px-3 py-1.5">Platform</th>
                      <th className="text-right px-3 py-1.5">Risk</th>
                      <th className="text-left px-3 py-1.5">Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {deviceResult.history.map((s, i) => (
                      <tr key={i} className="border-t border-border hover:bg-zinc-800/50">
                        <td className="px-3 py-1.5 font-mono">{s.user_id.slice(0, 16)}</td>
                        <td className="px-3 py-1.5">{s.ip_address}</td>
                        <td className="px-3 py-1.5">{s.platform}</td>
                        <td className="px-3 py-1.5 text-right">
                          <span className={s.risk_score >= 50 ? "text-red-400" : s.risk_score >= 25 ? "text-yellow-400" : "text-green-400"}>
                            {s.risk_score}
                          </span>
                        </td>
                        <td className="px-3 py-1.5 text-muted-foreground">{new Date(s.created_at).toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* User devices result */}
      {userResult && (
        <div className="space-y-4">
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="flex items-center justify-between mb-4">
              <div>
                <div className="text-xs text-muted-foreground">User</div>
                <div className="text-sm font-mono">{userResult.user_id}</div>
              </div>
              <div className={`text-xs font-bold rounded-lg border px-3 py-2 ${RISK_COLORS[userResult.device_switching_risk] || ""}`}>
                Device Switching: {userResult.device_switching_risk}
              </div>
            </div>

            <div className="text-xs text-muted-foreground mb-3">
              {userResult.device_count} device{userResult.device_count !== 1 ? "s" : ""} detected
            </div>

            <div className="space-y-2">
              {userResult.devices.map((d, i) => (
                <div key={i} className="rounded bg-zinc-800/50 p-3 flex items-center gap-4">
                  <Smartphone className="h-5 w-5 text-violet-400 shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="text-xs font-mono truncate">{d.device_hash}</div>
                    <div className="text-[10px] text-muted-foreground">
                      {d.platform} • {d.webgl_renderer?.slice(0, 40)}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className={`text-xs font-bold ${d.risk_score >= 50 ? "text-red-400" : d.risk_score >= 25 ? "text-yellow-400" : "text-green-400"}`}>
                      {d.risk_score}
                    </div>
                    <div className="text-[10px] text-muted-foreground">{d.ip_address}</div>
                  </div>
                  {d.webdriver && (
                    <AlertTriangle className="h-4 w-4 text-red-400 shrink-0" />
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, alert, color }: { label: string; value: string | number; alert?: boolean; color?: string }) {
  return (
    <div className="rounded bg-zinc-800/60 p-2 text-center">
      <div className={`text-lg font-bold ${alert ? "text-red-400" : color ? "" : ""}`}>
        <span className={color || ""}>{value}</span>
      </div>
      <div className="text-[10px] text-muted-foreground">{label}</div>
    </div>
  );
}

function Detail({ label, value, alert }: { label: string; value: string; alert?: boolean }) {
  return (
    <div className="flex justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className={`font-mono ${alert ? "text-red-400" : ""}`}>{value || "—"}</span>
    </div>
  );
}
