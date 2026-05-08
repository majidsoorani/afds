import { ArrowUpDown } from "lucide-react";
import type { Transaction } from "@/lib/api";

interface TransactionFeedProps {
  transactions: Transaction[];
  loading: boolean;
}

const statusColors: Record<string, string> = {
  PENDING: "bg-yellow-500/20 text-yellow-400",
  SUCCESS: "bg-green-500/20 text-green-400",
  FAILED: "bg-red-500/20 text-red-400",
  BLOCKED: "bg-red-600/20 text-red-500",
  SUSPENDED: "bg-orange-500/20 text-orange-400",
};

export function TransactionFeed({ transactions, loading }: TransactionFeedProps) {
  if (loading) {
    return (
      <div className="rounded-lg border border-border bg-card p-6">
        <p className="text-muted-foreground">Loading transactions...</p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between p-4 border-b border-border">
        <h2 className="text-lg font-semibold">Live Transaction Feed</h2>
        <ArrowUpDown className="h-4 w-4 text-muted-foreground" />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="px-4 py-3 text-left">External ID</th>
              <th className="px-4 py-3 text-left">Sender</th>
              <th className="px-4 py-3 text-right">Amount</th>
              <th className="px-4 py-3 text-left">Currency</th>
              <th className="px-4 py-3 text-left">Type</th>
              <th className="px-4 py-3 text-left">Status</th>
              <th className="px-4 py-3 text-left">Time</th>
            </tr>
          </thead>
          <tbody>
            {transactions.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">
                  No transactions yet. Ingest data via POST /api/v1/transactions/ingest
                </td>
              </tr>
            ) : (
              transactions.map((tx) => (
                <tr key={tx.id} className="border-b border-border/50 hover:bg-muted/50">
                  <td className="px-4 py-3 font-mono text-xs">
                    {tx.external_id.slice(0, 12)}...
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {tx.sender_id.slice(0, 10)}...
                  </td>
                  <td className="px-4 py-3 text-right font-semibold">
                    {Number(tx.amount).toLocaleString(undefined, {
                      minimumFractionDigits: 2,
                    })}
                  </td>
                  <td className="px-4 py-3">{tx.currency}</td>
                  <td className="px-4 py-3 text-xs">{tx.transaction_type}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`px-2 py-1 rounded-full text-xs font-medium ${
                        statusColors[tx.status] ?? "bg-gray-500/20 text-gray-400"
                      }`}
                    >
                      {tx.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-muted-foreground">
                    {new Date(tx.created_at).toLocaleTimeString()}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
