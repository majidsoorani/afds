/**
 * RuleEditor — Visual query builder for editing parsed rules.
 * Bridges the NLP parser output with an interactive condition editor.
 * Users can add/remove/edit conditions, change logic, action, severity.
 */
import { useState, useCallback, useMemo } from "react";
import {
  Plus,
  Trash2,
  GripVertical,
  Pencil,
} from "lucide-react";

// ── Types ───────────────────────────────────────────────────────────

export interface Condition {
  field: string;
  operator: string;
  value: string;
  category?: string;
}

export interface RuleData {
  rule_name: string;
  description: string;
  conditions: Condition[];
  logic: string;
  action: string;
  risk_score_adjustment: number;
  severity: string;
}

interface FieldInfo {
  field: string;
  label: string;
  type: string;
  description: string;
  values?: string[];
}

interface CategoryInfo {
  label: string;
  icon: string;
  color: string;
  fields: FieldInfo[];
}

interface FieldCatalog {
  categories: Record<string, CategoryInfo>;
  total_fields: number;
}

interface RuleEditorProps {
  rule: RuleData;
  catalog: FieldCatalog;
  onChange: (rule: RuleData) => void;
}

// ── Constants ───────────────────────────────────────────────────────

const OPERATORS: Record<string, { label: string; types: string[] }> = {
  gt:       { label: ">  greater than", types: ["number"] },
  lt:       { label: "<  less than",    types: ["number"] },
  eq:       { label: "=  equals",       types: ["number", "string", "enum"] },
  neq:      { label: "≠  not equal",    types: ["number", "string", "enum"] },
  contains: { label: "≈  contains",     types: ["string"] },
  in:       { label: "∈  one of",       types: ["string", "enum"] },
  is_true:  { label: "✓  is true",      types: ["boolean"] },
  is_false: { label: "✗  is false",     types: ["boolean"] },
};

const ACTIONS = ["BLOCK", "SUSPEND", "FLAG", "ALLOW"] as const;
const SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"] as const;

const ACTION_STYLES: Record<string, string> = {
  BLOCK:   "bg-red-900/40 border-red-700 text-red-300",
  SUSPEND: "bg-orange-900/40 border-orange-700 text-orange-300",
  FLAG:    "bg-yellow-900/40 border-yellow-700 text-yellow-300",
  ALLOW:   "bg-green-900/40 border-green-700 text-green-300",
};

const SEVERITY_STYLES: Record<string, string> = {
  CRITICAL: "bg-red-900/30 text-red-400",
  HIGH:     "bg-orange-900/30 text-orange-400",
  MEDIUM:   "bg-yellow-900/30 text-yellow-400",
  LOW:      "bg-zinc-800 text-zinc-400",
};

const RISK_PRESETS: Record<string, number> = {
  BLOCK: 50, SUSPEND: 40, FLAG: 25, ALLOW: 0,
};

// ── Helpers ─────────────────────────────────────────────────────────

function flatFields(catalog: FieldCatalog): Array<FieldInfo & { categoryKey: string; categoryLabel: string; categoryColor: string }> {
  const result: Array<FieldInfo & { categoryKey: string; categoryLabel: string; categoryColor: string }> = [];
  for (const [catKey, cat] of Object.entries(catalog.categories)) {
    for (const f of cat.fields) {
      result.push({ ...f, categoryKey: catKey, categoryLabel: cat.label, categoryColor: cat.color });
    }
  }
  return result;
}

function operatorsForType(type: string): string[] {
  return Object.entries(OPERATORS)
    .filter(([, v]) => v.types.includes(type))
    .map(([k]) => k);
}

// ── Component ───────────────────────────────────────────────────────

export function RuleEditor({ rule, catalog, onChange }: RuleEditorProps) {
  const [dragIdx, setDragIdx] = useState<number | null>(null);

  const allFields = useMemo(() => flatFields(catalog), [catalog]);
  const fieldMap = useMemo(() => {
    const m: Record<string, FieldInfo & { categoryKey: string; categoryLabel: string; categoryColor: string }> = {};
    for (const f of allFields) m[f.field] = f;
    return m;
  }, [allFields]);

  const updateCondition = useCallback((idx: number, patch: Partial<Condition>) => {
    const next = [...rule.conditions];
    next[idx] = { ...next[idx], ...patch };

    // Auto-set category when field changes
    if (patch.field) {
      const info = fieldMap[patch.field];
      if (info) {
        next[idx].category = info.categoryKey;
        // Auto-fix operator if not compatible
        const validOps = operatorsForType(info.type);
        if (!validOps.includes(next[idx].operator)) {
          next[idx].operator = validOps[0] || "eq";
          // Clear value for boolean auto-set
          if (info.type === "boolean") {
            next[idx].value = "true";
            next[idx].operator = "is_true";
          }
        }
      }
    }

    // Auto-fix value for boolean operator changes
    if (patch.operator === "is_true") next[idx].value = "true";
    if (patch.operator === "is_false") next[idx].value = "false";

    onChange({ ...rule, conditions: next });
  }, [rule, onChange, fieldMap]);

  const addCondition = useCallback(() => {
    onChange({
      ...rule,
      conditions: [
        ...rule.conditions,
        { field: "amount", operator: "gt", value: "", category: "transaction" },
      ],
    });
  }, [rule, onChange]);

  const removeCondition = useCallback((idx: number) => {
    if (rule.conditions.length <= 1) return;
    onChange({ ...rule, conditions: rule.conditions.filter((_, i) => i !== idx) });
  }, [rule, onChange]);

  const handleDragStart = (idx: number) => setDragIdx(idx);
  const handleDragOver = (e: React.DragEvent, idx: number) => {
    e.preventDefault();
    if (dragIdx === null || dragIdx === idx) return;
    const next = [...rule.conditions];
    const [moved] = next.splice(dragIdx, 1);
    next.splice(idx, 0, moved);
    onChange({ ...rule, conditions: next });
    setDragIdx(idx);
  };
  const handleDragEnd = () => setDragIdx(null);

  return (
    <div className="space-y-3">
      {/* Header */}
      <div className="flex items-center gap-2">
        <Pencil className="h-3.5 w-3.5 text-violet-400" />
        <span className="text-xs font-semibold text-violet-400">Edit Rule Conditions</span>
        <span className="text-[10px] text-muted-foreground ml-auto">
          Drag to reorder · Click fields to change
        </span>
      </div>

      {/* Logic toggle */}
      <div className="flex items-center gap-2">
        <span className="text-[10px] text-muted-foreground">Match:</span>
        <div className="flex rounded-md border border-border overflow-hidden">
          {(["AND", "OR"] as const).map((l) => (
            <button
              key={l}
              onClick={() => onChange({ ...rule, logic: l })}
              className={`px-3 py-1 text-[11px] font-semibold transition-colors ${
                rule.logic === l
                  ? "bg-violet-600 text-white"
                  : "bg-zinc-800 text-muted-foreground hover:bg-zinc-700"
              }`}
            >
              {l === "AND" ? "ALL conditions (AND)" : "ANY condition (OR)"}
            </button>
          ))}
        </div>
      </div>

      {/* Conditions */}
      <div className="space-y-2">
        {rule.conditions.map((cond, idx) => {
          const info = fieldMap[cond.field];
          const catInfo = cond.category ? catalog.categories[cond.category] : null;
          const fieldType = info?.type || "string";
          const validOps = operatorsForType(fieldType);
          const isBoolean = fieldType === "boolean";

          return (
            <div
              key={idx}
              draggable
              onDragStart={() => handleDragStart(idx)}
              onDragOver={(e) => handleDragOver(e, idx)}
              onDragEnd={handleDragEnd}
              className={`group flex items-center gap-2 rounded-lg border px-3 py-2 transition-all ${
                dragIdx === idx
                  ? "border-violet-500 bg-violet-900/20 opacity-70"
                  : "border-border bg-card hover:border-zinc-600"
              }`}
            >
              {/* Drag handle */}
              <GripVertical className="h-3.5 w-3.5 text-zinc-600 cursor-grab shrink-0" />

              {/* Logic label (between conditions) */}
              {idx > 0 && (
                <span className="text-[10px] font-bold text-violet-400 w-7 shrink-0">
                  {rule.logic}
                </span>
              )}
              {idx === 0 && <span className="w-7 shrink-0 text-[10px] text-zinc-600">IF</span>}

              {/* Category dot */}
              {catInfo && (
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{ backgroundColor: catInfo.color }}
                  title={catInfo.label}
                />
              )}

              {/* Field selector */}
              <select
                value={cond.field}
                onChange={(e) => updateCondition(idx, { field: e.target.value })}
                className="rounded border border-border bg-zinc-800 px-2 py-1 text-[11px] font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-violet-500/50 min-w-[140px]"
              >
                {Object.entries(catalog.categories).map(([catKey, cat]) => (
                  <optgroup key={catKey} label={cat.label}>
                    {cat.fields.map((f) => (
                      <option key={f.field} value={f.field}>
                        {f.field}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>

              {/* Operator selector */}
              <select
                value={cond.operator}
                onChange={(e) => updateCondition(idx, { operator: e.target.value })}
                className="rounded border border-border bg-zinc-800 px-2 py-1 text-[11px] text-foreground focus:outline-none focus:ring-1 focus:ring-violet-500/50 min-w-[100px]"
              >
                {validOps.map((op) => (
                  <option key={op} value={op}>
                    {OPERATORS[op]?.label || op}
                  </option>
                ))}
              </select>

              {/* Value input */}
              {!isBoolean && (
                <>
                  {info?.values && info.values.length > 0 ? (
                    <select
                      value={cond.value}
                      onChange={(e) => updateCondition(idx, { value: e.target.value })}
                      className="rounded border border-border bg-zinc-800 px-2 py-1 text-[11px] font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-violet-500/50 min-w-[100px]"
                    >
                      <option value="">— select —</option>
                      {info.values.map((v) => (
                        <option key={v} value={v}>{v}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={cond.value}
                      onChange={(e) => updateCondition(idx, { value: e.target.value })}
                      placeholder={fieldType === "number" ? "0" : "value"}
                      type={fieldType === "number" ? "number" : "text"}
                      className="rounded border border-border bg-zinc-800 px-2 py-1 text-[11px] font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-violet-500/50 min-w-[80px] max-w-[140px]"
                    />
                  )}
                </>
              )}

              {/* Remove button */}
              <button
                onClick={() => removeCondition(idx)}
                disabled={rule.conditions.length <= 1}
                className="ml-auto text-zinc-600 hover:text-red-400 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                title="Remove condition"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          );
        })}

        {/* Add condition */}
        <button
          onClick={addCondition}
          className="flex items-center gap-1.5 w-full rounded-lg border border-dashed border-zinc-700 px-3 py-2 text-[11px] text-muted-foreground hover:border-violet-600/50 hover:text-violet-400 transition-colors"
        >
          <Plus className="h-3.5 w-3.5" />
          Add condition
        </button>
      </div>

      {/* Action + Severity + Risk row */}
      <div className="flex flex-wrap items-center gap-3 pt-1">
        {/* Action */}
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] text-muted-foreground">Action:</span>
          <div className="flex rounded-md border border-border overflow-hidden">
            {ACTIONS.map((a) => (
              <button
                key={a}
                onClick={() => onChange({ ...rule, action: a, risk_score_adjustment: RISK_PRESETS[a] ?? rule.risk_score_adjustment })}
                className={`px-2.5 py-1 text-[10px] font-semibold transition-colors ${
                  rule.action === a
                    ? ACTION_STYLES[a]
                    : "bg-zinc-800 text-muted-foreground hover:bg-zinc-700"
                }`}
              >
                {a}
              </button>
            ))}
          </div>
        </div>

        {/* Severity */}
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] text-muted-foreground">Severity:</span>
          <div className="flex rounded-md border border-border overflow-hidden">
            {SEVERITIES.map((s) => (
              <button
                key={s}
                onClick={() => onChange({ ...rule, severity: s })}
                className={`px-2 py-1 text-[10px] font-medium transition-colors ${
                  rule.severity === s
                    ? SEVERITY_STYLES[s]
                    : "bg-zinc-800 text-muted-foreground hover:bg-zinc-700"
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>

        {/* Risk score */}
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] text-muted-foreground">Risk:</span>
          <span className="text-[10px] font-mono text-foreground">+</span>
          <input
            type="number"
            min={0}
            max={100}
            value={rule.risk_score_adjustment}
            onChange={(e) => onChange({ ...rule, risk_score_adjustment: Math.max(0, Math.min(100, parseInt(e.target.value) || 0)) })}
            className="w-14 rounded border border-border bg-zinc-800 px-2 py-1 text-[11px] font-mono text-foreground focus:outline-none focus:ring-1 focus:ring-violet-500/50"
          />
        </div>
      </div>
    </div>
  );
}
