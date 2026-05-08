import { useState, useEffect, useCallback } from "react";
import {
  FileText,
  Plus,
  ChevronRight,
  RefreshCw,
  Download,
  CheckCircle,
  Clock,
  XCircle,
  Send,
} from "lucide-react";

interface SARFiling {
  id: string;
  alert_id: string | null;
  filing_type: string;
  filing_format: string;
  status: string;
  subject_name: string | null;
  narrative: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

const API = "/api/v1/reporting";

const statusIcon: Record<string, typeof CheckCircle> = {
  DRAFT: FileText,
  PENDING_REVIEW: Clock,
  APPROVED: CheckCircle,
  FILED: Send,
  REJECTED: XCircle,
};

const statusColor: Record<string, string> = {
  DRAFT: "text-zinc-400 bg-zinc-800",
  PENDING_REVIEW: "text-yellow-400 bg-yellow-900/30",
  APPROVED: "text-green-400 bg-green-900/30",
  FILED: "text-blue-400 bg-blue-900/30",
  REJECTED: "text-red-400 bg-red-900/30",
};

export function RegulatoryReports() {
  const [filings, setFilings] = useState<SARFiling[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [selected, setSelected] = useState<SARFiling | null>(null);
  const [form, setForm] = useState({ alert_id: "", filing_format: "FinCEN_BSA", narrative: "" });

  const fetchFilings = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API}/sar`);
      const data = await res.json();
      setFilings(data.filings || []);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchFilings();
  }, [fetchFilings]);

  const createFiling = async () => {
    const res = await fetch(`${API}/sar`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    if (res.ok) {
      setShowCreate(false);
      setForm({ alert_id: "", filing_format: "FinCEN_BSA", narrative: "" });
      fetchFilings();
    }
  };

  const updateStatus = async (id: string, status: string) => {
    await fetch(`${API}/sar/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
    fetchFilings();
    setSelected(null);
  };

  const exportFiling = async (id: string) => {
    const res = await fetch(`${API}/sar/${id}/export`);
    const data = await res.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `SAR-${id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const statusCounts = filings.reduce<Record<string, number>>((acc, f) => {
    acc[f.status] = (acc[f.status] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <FileText className="h-5 w-5 text-amber-400" />
          <h2 className="text-lg font-semibold">Regulatory Reporting</h2>
          <span className="text-xs text-muted-foreground">SAR / STR Management</span>
        </div>
        <div className="flex gap-2">
          <button onClick={fetchFilings} className="flex items-center gap-1 rounded bg-zinc-800 px-3 py-1.5 text-xs hover:bg-zinc-700">
            <RefreshCw className="h-3 w-3" /> Refresh
          </button>
          <button onClick={() => setShowCreate(!showCreate)} className="flex items-center gap-1 rounded bg-amber-600 px-3 py-1.5 text-xs hover:bg-amber-500">
            <Plus className="h-3 w-3" /> New SAR
          </button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-5 gap-3">
        {["DRAFT", "PENDING_REVIEW", "APPROVED", "FILED", "REJECTED"].map((s) => {
          const Icon = statusIcon[s] || FileText;
          return (
            <div key={s} className="rounded-lg border border-border bg-card p-3">
              <div className="flex items-center gap-1 text-xs text-muted-foreground">
                <Icon className="h-3 w-3" /> {s.replace("_", " ")}
              </div>
              <div className="mt-1 text-xl font-bold">{statusCounts[s] || 0}</div>
            </div>
          );
        })}
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="rounded-lg border border-amber-600/50 bg-card p-4 space-y-3">
          <h3 className="font-semibold text-sm">Create SAR Filing</h3>
          <div className="grid grid-cols-2 gap-3">
            <input placeholder="Alert ID" value={form.alert_id} onChange={(e) => setForm({ ...form, alert_id: e.target.value })} className="rounded bg-zinc-800 px-3 py-1.5 text-sm" />
            <select value={form.filing_format} onChange={(e) => setForm({ ...form, filing_format: e.target.value })} className="rounded bg-zinc-800 px-3 py-1.5 text-sm">
              <option value="FinCEN_BSA">FinCEN BSA (US)</option>
              <option value="FCA_UK">FCA STR (UK)</option>
              <option value="BaFin_DE">BaFin (DE)</option>
            </select>
          </div>
          <textarea placeholder="Narrative (optional — AI can generate via MCP)" value={form.narrative} onChange={(e) => setForm({ ...form, narrative: e.target.value })} rows={3} className="w-full rounded bg-zinc-800 px-3 py-1.5 text-sm" />
          <button onClick={createFiling} disabled={!form.alert_id} className="rounded bg-amber-600 px-4 py-1.5 text-sm font-medium hover:bg-amber-500 disabled:opacity-50">
            Create Filing
          </button>
        </div>
      )}

      {/* Filings list */}
      {loading ? (
        <div className="text-center py-8 text-muted-foreground">Loading...</div>
      ) : filings.length === 0 ? (
        <div className="text-center py-8 text-muted-foreground">No SAR filings yet</div>
      ) : (
        <div className="space-y-2">
          {filings.map((filing) => (
            <div key={filing.id} className="rounded-lg border bg-card p-3 hover:bg-zinc-800/50 cursor-pointer" onClick={() => setSelected(filing)}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className={`rounded px-2 py-0.5 text-[10px] font-bold ${statusColor[filing.status] || ""}`}>
                    {filing.status.replace("_", " ")}
                  </span>
                  <span className="text-sm font-mono">{filing.id.slice(0, 8)}...</span>
                  <span className="text-xs text-muted-foreground">{filing.filing_format}</span>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={(e) => { e.stopPropagation(); exportFiling(filing.id); }} className="rounded p-1 hover:bg-zinc-700" title="Export">
                    <Download className="h-4 w-4" />
                  </button>
                  <ChevronRight className="h-4 w-4 text-muted-foreground" />
                </div>
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                Alert: {filing.alert_id || "—"} · Created {new Date(filing.created_at).toLocaleString()}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Detail panel */}
      {selected && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={() => setSelected(null)}>
          <div className="w-full max-w-lg rounded-lg bg-card border border-border p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-semibold">SAR Filing: {selected.id.slice(0, 8)}</h3>
            <div className="text-xs space-y-1">
              <div>Status: <span className={`font-bold ${statusColor[selected.status]?.split(" ")[0] || ""}`}>{selected.status}</span></div>
              <div>Format: {selected.filing_format}</div>
              <div>Alert: {selected.alert_id || "—"}</div>
              <div>Created: {new Date(selected.created_at).toLocaleString()}</div>
            </div>
            {selected.narrative && (
              <div className="rounded bg-zinc-800 p-3 text-xs max-h-40 overflow-auto">{selected.narrative}</div>
            )}
            <div className="flex gap-2 flex-wrap">
              {selected.status === "DRAFT" && (
                <button onClick={() => updateStatus(selected.id, "PENDING_REVIEW")} className="rounded bg-yellow-600 px-3 py-1 text-xs">Submit for Review</button>
              )}
              {selected.status === "PENDING_REVIEW" && (
                <>
                  <button onClick={() => updateStatus(selected.id, "APPROVED")} className="rounded bg-green-600 px-3 py-1 text-xs">Approve</button>
                  <button onClick={() => updateStatus(selected.id, "REJECTED")} className="rounded bg-red-600 px-3 py-1 text-xs">Reject</button>
                </>
              )}
              {selected.status === "APPROVED" && (
                <button onClick={() => updateStatus(selected.id, "FILED")} className="rounded bg-blue-600 px-3 py-1 text-xs">Mark as Filed</button>
              )}
              <button onClick={() => setSelected(null)} className="rounded bg-zinc-700 px-3 py-1 text-xs">Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
