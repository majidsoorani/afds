import { useState, useEffect, useCallback } from "react";
import { Shield, RefreshCw, Wifi, WifiOff, LogOut } from "lucide-react";
import { api, type DashboardStats, type Transaction, type Alert } from "@/lib/api";
import { isAuthenticated, getUsername, getRole, logout } from "@/lib/auth";
import { Login } from "@/components/Login";
import { StatsCards } from "@/components/StatsCards";
import { TransactionFeed } from "@/components/TransactionFeed";
import { AlertQueue } from "@/components/AlertQueue";
import { SanctionsScreener } from "@/components/SanctionsScreener";
import { LiveTestDashboard } from "@/components/LiveTestDashboard";
import { RulesEngine } from "@/components/RulesEngine";
import { NetworkGraph } from "@/components/NetworkGraph";
import { RegulatoryReports } from "@/components/RegulatoryReports";
import { RuleChat } from "@/components/RuleChat";
import { EnrichmentDashboard } from "@/components/EnrichmentDashboard";
import { DeviceIntelDashboard } from "@/components/DeviceIntelDashboard";
import { VisualDebugger } from "@/components/VisualDebugger";
import { CommandPalette } from "@/components/CommandPalette";
import { useWebSocket } from "@/hooks/useWebSocket";

type TabId = "overview" | "transactions" | "alerts" | "screening" | "live-test" | "rules" | "network" | "reports" | "rule-chat" | "enrichment" | "device-intel" | "debugger";

export default function App() {
  const [authed, setAuthed] = useState<boolean>(isAuthenticated());
  const [activeTab, setActiveTab] = useState<TabId>("overview");
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [loading, setLoading] = useState(true);

  const wsUrl = (() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const tok = localStorage.getItem("afds_token") || "";
    const qs = tok ? `?token=${encodeURIComponent(tok)}` : "";
    return `${proto}//${window.location.host}/api/v1/dashboard/ws${qs}`;
  })();
  const { isConnected } = useWebSocket(wsUrl);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [statsData, txData, alertData] = await Promise.all([
        api.getDashboardStats().catch(() => null),
        api.getTransactions().catch(() => []),
        api.getAlerts("OPEN").catch(() => []),
      ]);
      setStats(statsData);
      setTransactions(txData);
      setAlerts(alertData);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!authed) return;
    fetchData();
    const interval = setInterval(fetchData, 15000);
    return () => clearInterval(interval);
  }, [fetchData, authed]);

  const handleAlertUpdate = async (alertId: string, status: string) => {
    await api.updateAlert(alertId, status);
    fetchData();
  };

  const tabs: { id: TabId; label: string }[] = [
    { id: "overview", label: "Overview" },
    { id: "transactions", label: "Transactions" },
    { id: "alerts", label: "Alerts" },
    { id: "screening", label: "Screening" },
    { id: "live-test", label: "Live Test" },
    { id: "rules", label: "Rules" },
    { id: "network", label: "Network" },
    { id: "reports", label: "Reports" },
    { id: "rule-chat", label: "Rule Chat" },
    { id: "enrichment", label: "Enrichment" },
    { id: "device-intel", label: "Device Intel" },
    { id: "debugger", label: "Debugger" },
  ];

  if (!authed) {
    return <Login onSuccess={() => setAuthed(true)} />;
  }

  return (
    <div className="min-h-screen bg-background">
      {/* Header */}
      <header className="border-b border-border px-3 sm:px-6 py-3 sticky top-0 z-30 bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 sm:gap-3 min-w-0">
            <Shield className="h-6 w-6 text-primary shrink-0" />
            <div className="min-w-0">
              <h1 className="text-base sm:text-lg font-bold truncate">AFDS Command Center</h1>
              <p className="hidden sm:block text-[10px] text-muted-foreground">
                Autonomous Fraud Defense System
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 sm:gap-4">
            <div className="hidden md:block">
              <CommandPalette onNavigate={(tab) => setActiveTab(tab as TabId)} />
            </div>
            <div className="flex items-center gap-1.5 text-xs">
              {isConnected ? (
                <>
                  <Wifi className="h-3 w-3 text-green-400" />
                  <span className="hidden sm:inline text-green-400">Live</span>
                </>
              ) : (
                <>
                  <WifiOff className="h-3 w-3 text-red-400" />
                  <span className="hidden sm:inline text-red-400">Off</span>
                </>
              )}
            </div>
            <button
              onClick={fetchData}
              className="p-2 rounded-md hover:bg-muted"
              aria-label="Refresh"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <div className="flex items-center gap-2 text-xs border-l border-border pl-2 sm:pl-3 ml-1">
              <span className="hidden sm:inline text-muted-foreground">
                {getUsername()} <span className="opacity-60">({getRole()})</span>
              </span>
              <span className="sm:hidden text-muted-foreground font-medium">
                {(getUsername() || "?").slice(0, 1).toUpperCase()}
              </span>
              <button
                onClick={logout}
                title="Sign out"
                aria-label="Sign out"
                className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground"
              >
                <LogOut className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>

        {/* Tabs — horizontally scrollable on mobile */}
        <nav className="flex gap-4 sm:gap-6 mt-3 sm:mt-4 -mx-3 sm:mx-0 px-3 sm:px-0 overflow-x-auto scrollbar-thin no-scrollbar">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`pb-2 text-xs sm:text-sm font-medium border-b-2 transition-colors whitespace-nowrap shrink-0 ${
                activeTab === tab.id
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              {tab.label}
              {tab.id === "alerts" && alerts.length > 0 && (
                <span className="ml-1.5 px-1.5 py-0.5 rounded-full bg-red-500/20 text-red-400 text-[10px]">
                  {alerts.length}
                </span>
              )}
            </button>
          ))}
        </nav>
      </header>

      {/* Content */}
      <main className="p-3 sm:p-6 space-y-4 sm:space-y-6">
        {(activeTab === "overview" || activeTab === "transactions") && (
          <StatsCards stats={stats} loading={loading} />
        )}

        {activeTab === "overview" && (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <TransactionFeed
              transactions={transactions.slice(0, 10)}
              loading={loading}
            />
            <AlertQueue
              alerts={alerts.slice(0, 10)}
              loading={loading}
              onUpdateStatus={handleAlertUpdate}
            />
          </div>
        )}

        {activeTab === "transactions" && (
          <TransactionFeed transactions={transactions} loading={loading} />
        )}

        {activeTab === "alerts" && (
          <AlertQueue
            alerts={alerts}
            loading={loading}
            onUpdateStatus={handleAlertUpdate}
          />
        )}

        {activeTab === "screening" && <SanctionsScreener />}

        {activeTab === "live-test" && <LiveTestDashboard />}

        {activeTab === "rules" && <RulesEngine />}

        {activeTab === "network" && <NetworkGraph />}

        {activeTab === "reports" && <RegulatoryReports />}

        {activeTab === "rule-chat" && <RuleChat />}

        {activeTab === "enrichment" && <EnrichmentDashboard />}

        {activeTab === "device-intel" && <DeviceIntelDashboard />}

        {activeTab === "debugger" && <VisualDebugger />}
      </main>
    </div>
  );
}
