import { useState, useEffect, useCallback } from "react";
import {
  BookOpen,
  Edit3,
  MessageSquare,
  Plus,
  Power,
  PowerOff,
  RefreshCw,
  Save,
  Shield,
  X,
  Zap,
  Loader2,
  Sparkles,
} from "lucide-react";

interface Rule {
  id: string;
  rule_name: string;
  description: string | null;
  condition_json: { field: string; operator: string; value: string };
  action: string;
  risk_score_adjustment: number;
  severity: string;
  active: boolean;
  created_by: string;
  version: number;
  created_at: string;
  updated_at: string;
}

const API = "/api/v1/rules";
const CHAT_API = "/api/v1/rule-chat";

export function RulesEngine() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({
    description: "",
    field: "",
    operator: "",
    value: "",
    action: "",
    risk_score_adjustment: 0,
    severity: "",
  });
  const [aiEditId, setAiEditId] = useState<string | null>(null);
  const [aiEditMessage, setAiEditMessage] = useState("");
  const [aiEditLoading, setAiEditLoading] = useState(false);
  const [aiEditResult, setAiEditResult] = useState<string | null>(null);
  const [form, setForm] = useState({
    rule_name: "",
    description: "",
    field: "amount",
    operator: "gt",
    value: "",
    action: "FLAG",
    risk_score_adjustment: 30,
    severity: "HIGH",
  });

  const fetchRules = useCallback(async () => {
    setLoading(true);
    // Retry up to 3 times on network failure or non-OK response — the prod
    // ingress can drop ~10% of cold TLS connections, which previously left
    // the user staring at a "0 rules" empty state.
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const res = await fetch(`${API}/?active_only=false`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (Array.isArray(data?.rules)) {
          setRules(data.rules);
          setLoading(false);
          return;
        }
        throw new Error("invalid response shape");
      } catch {
        if (attempt < 2) {
          await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
          continue;
        }
      }
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchRules();
  }, [fetchRules]);

  const createRule = async () => {
    const res = await fetch(`${API}/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rule_name: form.rule_name,
        description: form.description,
        condition: { field: form.field, operator: form.operator, value: form.value },
        action: form.action,
        risk_score_adjustment: form.risk_score_adjustment,
        severity: form.severity,
      }),
    });
    if (res.ok) {
      setShowCreate(false);
      setForm({ rule_name: "", description: "", field: "amount", operator: "gt", value: "", action: "FLAG", risk_score_adjustment: 30, severity: "HIGH" });
      fetchRules();
    }
  };

  const toggleRule = async (id: string, active: boolean) => {
    if (active) {
      await fetch(`${API}/${id}`, { method: "DELETE" });
    } else {
      await fetch(`${API}/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: true }),
      });
    }
    fetchRules();
  };

  const startEdit = (rule: Rule) => {
    setEditingId(rule.id);
    setEditForm({
      description: rule.description || "",
      field: rule.condition_json.field,
      operator: rule.condition_json.operator,
      value: rule.condition_json.value,
      action: rule.action,
      risk_score_adjustment: rule.risk_score_adjustment,
      severity: rule.severity,
    });
    setAiEditId(null);
  };

  const cancelEdit = () => {
    setEditingId(null);
  };

  const saveEdit = async () => {
    if (!editingId) return;
    const res = await fetch(`${API}/${editingId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        description: editForm.description || undefined,
        condition: { field: editForm.field, operator: editForm.operator, value: editForm.value },
        action: editForm.action,
        risk_score_adjustment: editForm.risk_score_adjustment,
        severity: editForm.severity,
      }),
    });
    if (res.ok) {
      setEditingId(null);
      fetchRules();
    }
  };

  const startAiEdit = (rule: Rule) => {
    setAiEditId(rule.id);
    setAiEditMessage("");
    setAiEditResult(null);
    setEditingId(null);
  };

  const submitAiEdit = async () => {
    if (!aiEditId || !aiEditMessage.trim()) return;
    setAiEditLoading(true);
    setAiEditResult(null);
    try {
      const res = await fetch(`${CHAT_API}/edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rule_id: aiEditId, message: aiEditMessage }),
      });
      const data = await res.json();
      if (res.ok) {
        setAiEditResult(`✓ ${data.message}`);
        fetchRules();
        setTimeout(() => { setAiEditId(null); setAiEditResult(null); }, 2000);
      } else {
        setAiEditResult(`✗ ${data.detail || "Edit failed"}`);
      }
    } catch {
      setAiEditResult("✗ Network error");
    } finally {
      setAiEditLoading(false);
    }
  };

  const severityColor: Record<string, string> = {
    CRITICAL: "text-red-400 bg-red-900/30",
    HIGH: "text-orange-400 bg-orange-900/30",
    MEDIUM: "text-yellow-400 bg-yellow-900/30",
    LOW: "text-green-400 bg-green-900/30",
  };

  const actionColor: Record<string, string> = {
    BLOCK: "text-red-400",
    SUSPEND: "text-orange-400",
    FLAG: "text-yellow-400",
    ALLOW: "text-green-400",
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <BookOpen className="h-5 w-5 text-purple-400" />
          <h2 className="text-lg font-semibold">Detection Rules Engine</h2>
          <span className="text-xs text-muted-foreground">
            AI-created rules → Kafka → Flink (real-time)
          </span>
        </div>
        <div className="flex gap-2">
          <button onClick={fetchRules} className="flex items-center gap-1 rounded bg-zinc-800 px-3 py-1.5 text-xs hover:bg-zinc-700">
            <RefreshCw className="h-3 w-3" /> Refresh
          </button>
          <button onClick={() => setShowCreate(!showCreate)} className="flex items-center gap-1 rounded bg-purple-600 px-3 py-1.5 text-xs hover:bg-purple-500">
            <Plus className="h-3 w-3" /> New Rule
          </button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-3">
        {[
          { label: "Total Rules", value: rules.length, icon: BookOpen },
          { label: "Active", value: rules.filter((r) => r.active).length, icon: Zap },
          { label: "AI-Created", value: rules.filter((r) => r.created_by === "ai-mcp-agent").length, icon: Shield },
          { label: "Avg Adjustment", value: rules.length > 0 ? Math.round(rules.reduce((s, r) => s + r.risk_score_adjustment, 0) / rules.length) : 0, icon: Zap },
        ].map((s) => (
          <div key={s.label} className="rounded-lg border border-border bg-card p-3">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <s.icon className="h-3 w-3" /> {s.label}
            </div>
            <div className="mt-1 text-2xl font-bold">{s.value}</div>
          </div>
        ))}
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="rounded-lg border border-purple-600/50 bg-card p-4 space-y-3">
          <h3 className="font-semibold text-sm">Create Detection Rule</h3>
          <div className="grid grid-cols-2 gap-3">
            <input placeholder="Rule name" value={form.rule_name} onChange={(e) => setForm({ ...form, rule_name: e.target.value })} className="rounded bg-zinc-800 px-3 py-1.5 text-sm" />
            <input placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="rounded bg-zinc-800 px-3 py-1.5 text-sm" />
            <select value={form.field} onChange={(e) => setForm({ ...form, field: e.target.value })} className="rounded bg-zinc-800 px-3 py-1.5 text-sm">
              <option value="amount">amount</option>
              <option value="sender_id">sender_id</option>
              <option value="receiver_id">receiver_id</option>
              <option value="currency">currency</option>
              <option value="transaction_type">transaction_type</option>
            </select>
            <select value={form.operator} onChange={(e) => setForm({ ...form, operator: e.target.value })} className="rounded bg-zinc-800 px-3 py-1.5 text-sm">
              <option value="gt">greater than</option>
              <option value="lt">less than</option>
              <option value="eq">equals</option>
              <option value="neq">not equals</option>
              <option value="contains">contains</option>
              <option value="in">in list</option>
            </select>
            <input placeholder="Value / threshold" value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} className="rounded bg-zinc-800 px-3 py-1.5 text-sm" />
            <select value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value })} className="rounded bg-zinc-800 px-3 py-1.5 text-sm">
              <option value="BLOCK">BLOCK</option>
              <option value="SUSPEND">SUSPEND</option>
              <option value="FLAG">FLAG</option>
              <option value="ALLOW">ALLOW</option>
            </select>
            <div className="flex items-center gap-2">
              <label className="text-xs text-muted-foreground">Risk +</label>
              <input type="number" min={0} max={100} value={form.risk_score_adjustment} onChange={(e) => setForm({ ...form, risk_score_adjustment: Number(e.target.value) })} className="w-20 rounded bg-zinc-800 px-2 py-1.5 text-sm" />
            </div>
            <select value={form.severity} onChange={(e) => setForm({ ...form, severity: e.target.value })} className="rounded bg-zinc-800 px-3 py-1.5 text-sm">
              <option value="CRITICAL">CRITICAL</option>
              <option value="HIGH">HIGH</option>
              <option value="MEDIUM">MEDIUM</option>
              <option value="LOW">LOW</option>
            </select>
          </div>
          <button onClick={createRule} disabled={!form.rule_name || !form.value} className="rounded bg-purple-600 px-4 py-1.5 text-sm font-medium hover:bg-purple-500 disabled:opacity-50">
            Create & Publish to Kafka
          </button>
        </div>
      )}

      {/* Rules list */}
      {loading ? (
        <div className="text-center py-8 text-muted-foreground">Loading...</div>
      ) : rules.length === 0 ? (
        <div className="text-center py-8 text-muted-foreground">
          No rules yet. Create one or let AI create rules via MCP.
        </div>
      ) : (
        <div className="space-y-2">
          {rules.map((rule) => (
            <div key={rule.id} className={`rounded-lg border bg-card p-3 ${!rule.active ? "opacity-50" : ""} ${editingId === rule.id ? "border-purple-500/60" : ""} ${aiEditId === rule.id ? "border-violet-500/60" : ""}`}>
              {/* Normal view */}
              {editingId !== rule.id && (
                <>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <span className={`rounded px-2 py-0.5 text-[10px] font-bold ${severityColor[rule.severity] || ""}`}>
                        {rule.severity}
                      </span>
                      <span className="font-medium text-sm">{rule.rule_name}</span>
                      <span className="text-xs text-muted-foreground">v{rule.version}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className={`text-xs font-bold ${actionColor[rule.action] || ""}`}>{rule.action}</span>
                      <span className="text-xs text-muted-foreground">+{rule.risk_score_adjustment} risk</span>
                      <button onClick={() => startEdit(rule)} className="rounded p-1 hover:bg-zinc-700" title="Edit rule">
                        <Edit3 className="h-3.5 w-3.5 text-blue-400" />
                      </button>
                      <button onClick={() => startAiEdit(rule)} className="rounded p-1 hover:bg-zinc-700" title="Edit with AI">
                        <Sparkles className="h-3.5 w-3.5 text-violet-400" />
                      </button>
                      <button onClick={() => toggleRule(rule.id, rule.active)} className="rounded p-1 hover:bg-zinc-700" title={rule.active ? "Deactivate" : "Activate"}>
                        {rule.active ? <Power className="h-4 w-4 text-green-400" /> : <PowerOff className="h-4 w-4 text-red-400" />}
                      </button>
                    </div>
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    <code className="bg-zinc-800 px-1.5 py-0.5 rounded">
                      {rule.condition_json.field} {rule.condition_json.operator} {rule.condition_json.value}
                    </code>
                    {rule.description && <span className="ml-2">— {rule.description}</span>}
                  </div>
                  <div className="mt-1 text-[10px] text-muted-foreground">
                    Created by {rule.created_by} · {new Date(rule.created_at).toLocaleString()}
                  </div>

                  {/* AI Edit inline panel */}
                  {aiEditId === rule.id && (
                    <div className="mt-2 rounded border border-violet-600/40 bg-violet-950/20 p-3 space-y-2">
                      <div className="flex items-center gap-2 text-xs font-medium text-violet-400">
                        <Sparkles className="h-3.5 w-3.5" />
                        Edit with AI — describe your change in English
                      </div>
                      <div className="flex gap-2">
                        <input
                          value={aiEditMessage}
                          onChange={(e) => setAiEditMessage(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && submitAiEdit()}
                          placeholder='e.g. "change action to BLOCK" or "set threshold to 25000"'
                          disabled={aiEditLoading}
                          className="flex-1 rounded border border-border bg-background px-3 py-1.5 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-violet-500/50 disabled:opacity-50"
                        />
                        <button
                          onClick={submitAiEdit}
                          disabled={!aiEditMessage.trim() || aiEditLoading}
                          className="rounded bg-violet-600 px-3 py-1.5 text-xs font-medium hover:bg-violet-500 disabled:opacity-50 flex items-center gap-1"
                        >
                          {aiEditLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <MessageSquare className="h-3 w-3" />}
                          Apply
                        </button>
                        <button onClick={() => { setAiEditId(null); setAiEditResult(null); }} className="rounded bg-zinc-700 px-2 py-1.5 text-xs hover:bg-zinc-600">
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                      <div className="text-[10px] text-zinc-500">
                        Try: "change action to BLOCK" · "set severity to CRITICAL" · "change threshold to 25000" · "deactivate this rule"
                      </div>
                      {aiEditResult && (
                        <div className={`text-xs ${aiEditResult.startsWith("✓") ? "text-green-400" : "text-red-400"}`}>
                          {aiEditResult}
                        </div>
                      )}
                    </div>
                  )}
                </>
              )}

              {/* Edit form */}
              {editingId === rule.id && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 text-sm font-medium text-blue-400">
                      <Edit3 className="h-4 w-4" />
                      Editing: {rule.rule_name}
                    </div>
                    <div className="flex gap-2">
                      <button onClick={saveEdit} className="flex items-center gap-1 rounded bg-green-600 px-3 py-1 text-xs font-medium hover:bg-green-500">
                        <Save className="h-3 w-3" /> Save
                      </button>
                      <button onClick={cancelEdit} className="flex items-center gap-1 rounded bg-zinc-700 px-3 py-1 text-xs hover:bg-zinc-600">
                        <X className="h-3 w-3" /> Cancel
                      </button>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <div>
                      <label className="text-[10px] text-muted-foreground">Description</label>
                      <input value={editForm.description} onChange={(e) => setEditForm({ ...editForm, description: e.target.value })} className="w-full rounded bg-zinc-800 px-2 py-1.5 text-xs" />
                    </div>
                    <div>
                      <label className="text-[10px] text-muted-foreground">Action</label>
                      <select value={editForm.action} onChange={(e) => setEditForm({ ...editForm, action: e.target.value })} className="w-full rounded bg-zinc-800 px-2 py-1.5 text-xs">
                        <option value="BLOCK">BLOCK</option>
                        <option value="SUSPEND">SUSPEND</option>
                        <option value="FLAG">FLAG</option>
                        <option value="ALLOW">ALLOW</option>
                      </select>
                    </div>
                    <div>
                      <label className="text-[10px] text-muted-foreground">Condition Field</label>
                      <input value={editForm.field} onChange={(e) => setEditForm({ ...editForm, field: e.target.value })} className="w-full rounded bg-zinc-800 px-2 py-1.5 text-xs font-mono" />
                    </div>
                    <div>
                      <label className="text-[10px] text-muted-foreground">Operator</label>
                      <select value={editForm.operator} onChange={(e) => setEditForm({ ...editForm, operator: e.target.value })} className="w-full rounded bg-zinc-800 px-2 py-1.5 text-xs">
                        <option value="gt">greater than</option>
                        <option value="lt">less than</option>
                        <option value="eq">equals</option>
                        <option value="neq">not equals</option>
                        <option value="contains">contains</option>
                        <option value="in">in list</option>
                        <option value="is_true">is true</option>
                        <option value="is_false">is false</option>
                        <option value="multi">multi-condition</option>
                      </select>
                    </div>
                    <div>
                      <label className="text-[10px] text-muted-foreground">Value / Threshold</label>
                      <input value={editForm.value} onChange={(e) => setEditForm({ ...editForm, value: e.target.value })} className="w-full rounded bg-zinc-800 px-2 py-1.5 text-xs font-mono" />
                    </div>
                    <div>
                      <label className="text-[10px] text-muted-foreground">Severity</label>
                      <select value={editForm.severity} onChange={(e) => setEditForm({ ...editForm, severity: e.target.value })} className="w-full rounded bg-zinc-800 px-2 py-1.5 text-xs">
                        <option value="CRITICAL">CRITICAL</option>
                        <option value="HIGH">HIGH</option>
                        <option value="MEDIUM">MEDIUM</option>
                        <option value="LOW">LOW</option>
                      </select>
                    </div>
                    <div>
                      <label className="text-[10px] text-muted-foreground">Risk Adjustment</label>
                      <input type="number" min={0} max={100} value={editForm.risk_score_adjustment} onChange={(e) => setEditForm({ ...editForm, risk_score_adjustment: Number(e.target.value) })} className="w-full rounded bg-zinc-800 px-2 py-1.5 text-xs" />
                    </div>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
