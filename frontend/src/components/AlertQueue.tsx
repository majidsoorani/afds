import { AlertTriangle, CheckCircle, XCircle, Search } from "lucide-react";
import type { Alert } from "@/lib/api";

interface AlertQueueProps {
  alerts: Alert[];
  loading: boolean;
  onUpdateStatus: (alertId: string, status: string) => void;
}

const severityIcons: Record<string, typeof AlertTriangle> = {
  LOW: CheckCircle,
  MEDIUM: AlertTriangle,
  HIGH: XCircle,
  CRITICAL: XCircle,
};

const severityColors: Record<string, string> = {
  LOW: "text-green-400 bg-green-400/10",
  MEDIUM: "text-yellow-400 bg-yellow-400/10",
  HIGH: "text-red-400 bg-red-400/10",
  CRITICAL: "text-red-600 bg-red-600/10 animate-pulse",
};

export function AlertQueue({ alerts, loading, onUpdateStatus }: AlertQueueProps) {
  if (loading) {
    return (
      <div className="rounded-lg border border-border bg-card p-6">
        <p className="text-muted-foreground">Loading alerts...</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between p-4 border-b border-border">
        <h2 className="text-lg font-semibold">Alert Investigation Queue</h2>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Search className="h-3 w-3" />
          {alerts.length} alerts
        </div>
      </div>
      <div className="divide-y divide-border/50">
        {alerts.length === 0 ? (
          <div className="p-8 text-center text-muted-foreground">
            No open alerts. System clear.
          </div>
        ) : (
          alerts.map((alert) => {
            const Icon = severityIcons[alert.severity] ?? AlertTriangle;
            return (
              <div key={alert.id} className="p-4 hover:bg-muted/50">
                <div className="flex items-start gap-3">
                  <div className={`p-2 rounded-md ${severityColors[alert.severity]}`}>
                    <Icon className="h-4 w-4" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <h3 className="font-medium text-sm truncate">{alert.title}</h3>
                      <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${severityColors[alert.severity]}`}>
                        {alert.severity}
                      </span>
                    </div>
                    {alert.description && (
                      <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                        {alert.description}
                      </p>
                    )}
                    <div className="flex items-center gap-4 mt-2">
                      <span className="text-[10px] text-muted-foreground">
                        {new Date(alert.created_at).toLocaleString()}
                      </span>
                      <span className="text-[10px] text-muted-foreground">
                        {alert.alert_type}
                      </span>
                    </div>
                  </div>
                  <div className="flex gap-1">
                    {alert.status === "OPEN" && (
                      <>
                        <button
                          onClick={() => onUpdateStatus(alert.id, "INVESTIGATING")}
                          className="px-2 py-1 text-[10px] rounded bg-blue-500/20 text-blue-400 hover:bg-blue-500/30"
                        >
                          Investigate
                        </button>
                        <button
                          onClick={() => onUpdateStatus(alert.id, "DISMISSED")}
                          className="px-2 py-1 text-[10px] rounded bg-muted text-muted-foreground hover:bg-muted/80"
                        >
                          Dismiss
                        </button>
                      </>
                    )}
                    {alert.status === "INVESTIGATING" && (
                      <button
                        onClick={() => onUpdateStatus(alert.id, "RESOLVED")}
                        className="px-2 py-1 text-[10px] rounded bg-green-500/20 text-green-400 hover:bg-green-500/30"
                      >
                        Resolve
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
