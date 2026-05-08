const API_BASE = "/api/v1";

import { authFetch } from "./auth";

export interface Transaction {
  id: string;
  external_id: string;
  sender_id: string;
  receiver_id: string | null;
  amount: number;
  currency: string;
  transaction_type: string;
  status: string;
  created_at: string;
  processed_at: string | null;
}

export interface Alert {
  id: string;
  transaction_id: string;
  alert_type: string;
  severity: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
  title: string;
  description: string | null;
  status: "OPEN" | "INVESTIGATING" | "RESOLVED" | "DISMISSED";
  assigned_to: string | null;
  created_at: string;
  updated_at: string;
}

export interface DashboardStats {
  total_transactions_24h: number;
  blocked_transactions_24h: number;
  open_alerts: number;
  critical_alerts: number;
  avg_risk_score: number;
  transactions_per_minute: number;
}

export interface SanctionsMatch {
  entity_id: string;
  matched_name: string;
  similarity: number;
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`);
  }
  return response.json();
}

export const api = {
  getDashboardStats: () =>
    authFetch(`${API_BASE}/dashboard/stats`).then(handleResponse<DashboardStats>),

  getTransactions: (limit = 50, offset = 0) =>
    authFetch(`${API_BASE}/transactions/?limit=${limit}&offset=${offset}`).then(
      handleResponse<Transaction[]>
    ),

  getAlerts: (status?: string) => {
    const params = status ? `?status=${status}` : "";
    return authFetch(`${API_BASE}/alerts/${params}`).then(handleResponse<Alert[]>);
  },

  updateAlert: (alertId: string, status: string) =>
    authFetch(`${API_BASE}/alerts/${alertId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    }).then(handleResponse<Alert>),

  screenName: (name: string, threshold = 0.5) =>
    authFetch(`${API_BASE}/sanctions/screen`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, threshold }),
    }).then(handleResponse<SanctionsMatch[]>),

};
