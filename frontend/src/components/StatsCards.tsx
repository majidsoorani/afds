import {
  Shield,
  AlertTriangle,
  Activity,
  Ban,
  TrendingUp,
  Clock,
} from "lucide-react";
import type { DashboardStats } from "@/lib/api";

interface StatsCardsProps {
  stats: DashboardStats | null;
  loading: boolean;
}

export function StatsCards({ stats, loading }: StatsCardsProps) {
  const cards = [
    {
      title: "Transactions (24h)",
      value: stats?.total_transactions_24h ?? 0,
      icon: Activity,
      color: "text-blue-400",
      bg: "bg-blue-400/10",
    },
    {
      title: "Blocked (24h)",
      value: stats?.blocked_transactions_24h ?? 0,
      icon: Ban,
      color: "text-red-400",
      bg: "bg-red-400/10",
    },
    {
      title: "Open Alerts",
      value: stats?.open_alerts ?? 0,
      icon: AlertTriangle,
      color: "text-yellow-400",
      bg: "bg-yellow-400/10",
    },
    {
      title: "Critical Alerts",
      value: stats?.critical_alerts ?? 0,
      icon: Shield,
      color: "text-red-500",
      bg: "bg-red-500/10",
    },
    {
      title: "Avg Risk Score",
      value: stats?.avg_risk_score?.toFixed(1) ?? "0",
      icon: TrendingUp,
      color: "text-purple-400",
      bg: "bg-purple-400/10",
    },
    {
      title: "Txns/Min",
      value: stats?.transactions_per_minute?.toFixed(1) ?? "0",
      icon: Clock,
      color: "text-green-400",
      bg: "bg-green-400/10",
    },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
      {cards.map((card) => (
        <div
          key={card.title}
          className="rounded-lg border border-border bg-card p-4"
        >
          <div className="flex items-center gap-2 mb-2">
            <div className={`p-2 rounded-md ${card.bg}`}>
              <card.icon className={`h-4 w-4 ${card.color}`} />
            </div>
          </div>
          <p className="text-2xl font-bold">
            {loading ? "..." : card.value}
          </p>
          <p className="text-xs text-muted-foreground mt-1">{card.title}</p>
        </div>
      ))}
    </div>
  );
}
