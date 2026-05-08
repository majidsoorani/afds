import { useState, useRef, useEffect } from "react";
import {
  MessageSquare,
  Send,
  Zap,
  FlaskConical,
  Rocket,
  ChevronDown,
  ChevronUp,
  AlertTriangle,
  CheckCircle,
  XCircle,
  Loader2,
  Sparkles,
  BarChart3,
  Shield,
  ArrowLeftRight,
  ShieldAlert,
  User,
  UserCheck,
  CreditCard,
  Search,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
} from "lucide-react";
import { RuleEditor } from "./RuleEditor";
import type { RuleData } from "./RuleEditor";

// ── Types ───────────────────────────────────────────────────────────

interface Condition {
  field: string;
  operator: string;
  value: string;
  category?: string;
}

interface ParsedRule {
  rule_name: string;
  description: string;
  conditions: Condition[];
  logic: string;
  action: string;
  risk_score_adjustment: number;
  severity: string;
}

interface BacktestResult {
  total_transactions: number;
  total_matched: number;
  match_rate: number;
  sample_matches: Record<string, unknown>[];
  sample_size: number;
  risk_distribution: Record<string, number>;
  categories_used?: string[];
  joins_used?: Record<string, boolean>;
}

interface DeployResult {
  status: string;
  rule_id: string;
  rule_name: string;
  message: string;
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

interface Suggestion {
  text: string;
  category: string;
}

type ChatStep = "idle" | "parsing" | "parsed" | "editing" | "testing" | "tested" | "deploying" | "deployed" | "error";

interface ChatMessage {
  id: string;
  role: "user" | "system";
  content: string;
  timestamp: Date;
  step?: ChatStep;
  parsedRule?: ParsedRule;
  explanation?: string;
  backtestResult?: BacktestResult;
  deployResult?: DeployResult;
  error?: string;
}

const API = "/api/v1/rule-chat";

const ACTION_COLORS: Record<string, string> = {
  BLOCK: "text-red-400 bg-red-900/30 border-red-700",
  SUSPEND: "text-orange-400 bg-orange-900/30 border-orange-700",
  FLAG: "text-yellow-400 bg-yellow-900/30 border-yellow-700",
  ALLOW: "text-green-400 bg-green-900/30 border-green-700",
};

const SEVERITY_COLORS: Record<string, string> = {
  CRITICAL: "text-red-400",
  HIGH: "text-orange-400",
  MEDIUM: "text-yellow-400",
  LOW: "text-zinc-400",
};

const OP_LABELS: Record<string, string> = {
  gt: ">",
  lt: "<",
  eq: "=",
  neq: "≠",
  contains: "contains",
  in: "in",
  is_true: "is ✓",
  is_false: "is ✗",
};

const CATEGORY_ICONS: Record<string, typeof ArrowLeftRight> = {
  ArrowLeftRight,
  ShieldAlert,
  User,
  UserCheck,
  CreditCard,
};

// ── Component ───────────────────────────────────────────────────────

export function RuleChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [step, setStep] = useState<ChatStep>("idle");
  const [currentRule, setCurrentRule] = useState<ParsedRule | null>(null);
  const [, setBacktestResult] = useState<BacktestResult | null>(null);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [showSuggestions, setShowSuggestions] = useState(true);
  const [expandedMatches, setExpandedMatches] = useState(false);
  const [fieldCatalog, setFieldCatalog] = useState<FieldCatalog | null>(null);
  const [showFields, setShowFields] = useState(false);
  const [fieldSearch, setFieldSearch] = useState("");
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set());
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    fetch(`${API}/suggestions`)
      .then((r) => r.json())
      .then((d) => setSuggestions(d.suggestions || []))
      .catch(() => {});

    fetch(`${API}/fields`)
      .then((r) => r.json())
      .then((d: FieldCatalog) => {
        setFieldCatalog(d);
        setExpandedCategories(new Set(Object.keys(d.categories)));
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const addMessage = (role: "user" | "system", content: string, extra?: Partial<ChatMessage>) => {
    const msg: ChatMessage = {
      id: crypto.randomUUID(),
      role,
      content,
      timestamp: new Date(),
      ...extra,
    };
    setMessages((prev) => [...prev, msg]);
    return msg;
  };

  const updateLastSystemMessage = (updates: Partial<ChatMessage>) => {
    setMessages((prev) => {
      const idx = prev.findLastIndex((m: ChatMessage) => m.role === "system");
      if (idx === -1) return prev;
      const copy = [...prev];
      copy[idx] = { ...copy[idx], ...updates };
      return copy;
    });
  };

  const insertFieldIntoInput = (fieldName: string) => {
    setInput((prev) => {
      const trimmed = prev.trimEnd();
      return trimmed ? `${trimmed} ${fieldName} ` : `${fieldName} `;
    });
    inputRef.current?.focus();
  };

  const toggleCategory = (cat: string) => {
    setExpandedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(cat)) next.delete(cat);
      else next.add(cat);
      return next;
    });
  };

  // ── Step 1: Parse ──
  const handleSend = async () => {
    const text = input.trim();
    if (!text || step === "parsing" || step === "testing" || step === "deploying") return;

    setInput("");
    setShowSuggestions(false);
    setCurrentRule(null);
    setBacktestResult(null);
    setExpandedMatches(false);

    addMessage("user", text);
    addMessage("system", "Analyzing your rule...", { step: "parsing" });
    setStep("parsing");

    try {
      const res = await fetch(`${API}/parse`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Parse failed" }));
        throw new Error(err.detail || "Failed to parse rule");
      }

      const data = await res.json();
      const rule: ParsedRule = data.parsed_rule;
      setCurrentRule(rule);

      updateLastSystemMessage({
        content: data.explanation,
        step: "parsed",
        parsedRule: rule,
        explanation: data.explanation,
      });
      setStep("editing");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Failed to parse rule";
      updateLastSystemMessage({ content: msg, step: "error", error: msg });
      setStep("error");
    }
  };

  // ── Step 2: Backtest ──
  const handleTest = async () => {
    if (!currentRule) return;
    addMessage("system", "Running backtest against historical transactions...", { step: "testing" });
    setStep("testing");

    try {
      const res = await fetch(`${API}/backtest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conditions: currentRule.conditions,
          logic: currentRule.logic,
          limit: 500,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Backtest failed" }));
        throw new Error(err.detail || "Backtest failed");
      }

      const data: BacktestResult = await res.json();
      setBacktestResult(data);

      updateLastSystemMessage({
        content: `Backtest complete: **${data.total_matched.toLocaleString()}** transactions matched out of **${data.total_transactions.toLocaleString()}** (${data.match_rate}%)`,
        step: "tested",
        backtestResult: data,
      });
      setStep("tested");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Backtest failed";
      updateLastSystemMessage({ content: msg, step: "error", error: msg });
      setStep("error");
    }
  };

  // ── Step 3: Deploy ──
  const handleDeploy = async () => {
    if (!currentRule) return;
    addMessage("system", "Deploying rule to Flink via Kafka...", { step: "deploying" });
    setStep("deploying");

    try {
      const res = await fetch(`${API}/deploy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rule_name: currentRule.rule_name,
          description: currentRule.description,
          conditions: currentRule.conditions,
          logic: currentRule.logic,
          action: currentRule.action,
          risk_score_adjustment: currentRule.risk_score_adjustment,
          severity: currentRule.severity,
        }),
      });

      if (!res.ok) throw new Error("Deploy failed");

      const data: DeployResult = await res.json();

      updateLastSystemMessage({
        content: data.message,
        step: "deployed",
        deployResult: data,
      });
      setStep("deployed");
      setCurrentRule(null);
      setBacktestResult(null);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Deploy failed";
      updateLastSystemMessage({ content: msg, step: "error", error: msg });
      setStep("error");
    }
  };

  const handleSuggestionClick = (s: string) => {
    setInput(s);
    inputRef.current?.focus();
  };

  const handleRuleChange = (updated: RuleData) => {
    setCurrentRule(updated as ParsedRule);
  };

  const handleBuildFromScratch = () => {
    setShowSuggestions(false);
    setCurrentRule({
      rule_name: "custom_rule",
      description: "",
      conditions: [{ field: "amount", operator: "gt", value: "10000", category: "transaction" }],
      logic: "AND",
      action: "FLAG",
      risk_score_adjustment: 30,
      severity: "MEDIUM",
    });
    addMessage("system", "Build your rule visually using the editor below.", { step: "parsed" });
    setStep("editing");
  };

  const isLoading = step === "parsing" || step === "testing" || step === "deploying";

  // Filter fields for explorer search
  const filteredCatalog = fieldCatalog
    ? Object.entries(fieldCatalog.categories).reduce<Record<string, CategoryInfo>>((acc, [key, cat]) => {
        if (!fieldSearch) {
          acc[key] = cat;
        } else {
          const q = fieldSearch.toLowerCase();
          const filtered = cat.fields.filter(
            (f) => f.field.includes(q) || f.label.toLowerCase().includes(q) || f.description.toLowerCase().includes(q)
          );
          if (filtered.length > 0) acc[key] = { ...cat, fields: filtered };
        }
        return acc;
      }, {})
    : {};

  return (
    <div className="flex h-[calc(100vh-180px)] max-h-[800px] gap-0">
      {/* ── Field Explorer Sidebar ── */}
      {showFields && fieldCatalog && (
        <div className="w-72 shrink-0 border-r border-border flex flex-col bg-card/50">
          <div className="flex items-center justify-between px-3 py-2 border-b border-border">
            <div className="flex items-center gap-1.5">
              <Zap className="h-3.5 w-3.5 text-violet-400" />
              <span className="text-xs font-semibold">Fields</span>
              <span className="text-[10px] text-muted-foreground">({fieldCatalog.total_fields})</span>
            </div>
            <button onClick={() => setShowFields(false)} className="text-muted-foreground hover:text-foreground">
              <PanelLeftClose className="h-3.5 w-3.5" />
            </button>
          </div>

          <div className="px-2 py-2">
            <div className="relative">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted-foreground" />
              <input
                value={fieldSearch}
                onChange={(e) => setFieldSearch(e.target.value)}
                placeholder="Search fields..."
                className="w-full rounded border border-border bg-background pl-7 pr-2 py-1 text-[11px] placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-violet-500/50"
              />
            </div>
          </div>

          <div className="flex-1 overflow-y-auto px-1 pb-2">
            {Object.entries(filteredCatalog).map(([catKey, cat]) => {
              const IconComp = CATEGORY_ICONS[cat.icon] || Zap;
              const isOpen = expandedCategories.has(catKey);
              return (
                <div key={catKey} className="mb-1">
                  <button
                    onClick={() => toggleCategory(catKey)}
                    className="flex items-center gap-1.5 w-full px-2 py-1.5 rounded text-[11px] font-medium hover:bg-zinc-800/60 transition-colors"
                    style={{ color: cat.color }}
                  >
                    <IconComp className="h-3 w-3" />
                    {cat.label}
                    <span className="text-[9px] text-muted-foreground ml-auto mr-1">{cat.fields.length}</span>
                    {isOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                  </button>

                  {isOpen && (
                    <div className="ml-2 space-y-0.5">
                      {cat.fields.map((f) => (
                        <button
                          key={f.field}
                          onClick={() => insertFieldIntoInput(f.field)}
                          className="group w-full text-left rounded px-2 py-1 hover:bg-violet-900/20 transition-colors"
                          title={`Click to insert: ${f.field}\n${f.description}`}
                        >
                          <div className="flex items-center justify-between">
                            <span className="text-[10px] font-mono text-foreground group-hover:text-violet-300">
                              {f.field}
                            </span>
                            <span className="text-[9px] rounded px-1 bg-zinc-800 text-muted-foreground">
                              {f.type}
                            </span>
                          </div>
                          <div className="text-[9px] text-muted-foreground truncate">{f.description}</div>
                          {f.values && (
                            <div className="text-[9px] text-zinc-500 truncate mt-0.5">
                              {f.values.join(" · ")}
                            </div>
                          )}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ── Main Chat Area ── */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <div className="flex items-center gap-2 px-3 pb-3 border-b border-border">
          {!showFields && (
            <button onClick={() => setShowFields(true)} className="text-muted-foreground hover:text-foreground mr-1" title="Open field explorer">
              <PanelLeftOpen className="h-4 w-4" />
            </button>
          )}
          <MessageSquare className="h-5 w-5 text-violet-400" />
          <h2 className="text-lg font-semibold">Rule Chat</h2>
          <span className="text-xs text-muted-foreground hidden md:inline">
            Write rules in English → Edit visually → Test → Deploy
          </span>
          {fieldCatalog && (
            <span className="ml-auto text-[10px] text-muted-foreground hidden lg:inline">
              {fieldCatalog.total_fields} fields across {Object.keys(fieldCatalog.categories).length} categories
            </span>
          )}
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto py-4 px-3 space-y-4">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-center gap-4 text-muted-foreground">
              <Sparkles className="h-12 w-12 text-violet-400/40" />
              <div>
                <p className="text-sm font-medium text-foreground">Write a detection rule in plain English</p>
                <p className="text-xs mt-1">
                  The system will parse it, let you edit conditions visually, test on data, and deploy to Flink.
                  <button onClick={() => setShowFields(true)} className="text-violet-400 hover:underline ml-1">
                    Browse available fields →
                  </button>
                </p>
              </div>

              {/* Category badges */}
              {fieldCatalog && (
                <div className="flex flex-wrap gap-2 justify-center mt-1">
                  {Object.entries(fieldCatalog.categories).map(([key, cat]) => {
                    const IconComp = CATEGORY_ICONS[cat.icon] || Zap;
                    return (
                      <button
                        key={key}
                        onClick={() => { setShowFields(true); setExpandedCategories(new Set([key])); }}
                        className="flex items-center gap-1.5 rounded-full border border-border px-3 py-1 text-[10px] hover:bg-zinc-800/60 transition-colors"
                        style={{ color: cat.color, borderColor: `${cat.color}40` }}
                      >
                        <IconComp className="h-3 w-3" />
                        {cat.label}
                        <span className="text-muted-foreground">({cat.fields.length})</span>
                      </button>
                    );
                  })}
                </div>
              )}

              {/* Suggestions */}
              {showSuggestions && suggestions.length > 0 && (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2 mt-3 w-full max-w-4xl">
                  {suggestions.map((s, i) => {
                    const cat = fieldCatalog?.categories[s.category];
                    return (
                      <button
                        key={i}
                        onClick={() => handleSuggestionClick(s.text)}
                        className="text-left text-xs rounded-lg border border-border bg-card p-3 hover:bg-violet-900/20 hover:border-violet-600/50 transition-colors"
                      >
                        <div className="flex items-center gap-1.5 mb-1">
                          {cat && <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: cat.color }} />}
                          <span className="text-[9px] text-muted-foreground">{cat?.label || s.category}</span>
                        </div>
                        <Zap className="h-3 w-3 text-violet-400 inline mr-1.5" />
                        {s.text}
                      </button>
                    );
                  })}
                </div>
              )}

              {/* Build from scratch button */}
              <button
                onClick={handleBuildFromScratch}
                className="flex items-center gap-2 mt-2 rounded-lg border border-dashed border-violet-600/40 px-4 py-2 text-xs text-violet-400 hover:bg-violet-900/20 hover:border-violet-500 transition-colors"
              >
                <Pencil className="h-3.5 w-3.5" />
                Or build a rule visually from scratch
              </button>
            </div>
          )}

          {messages.map((msg) => (
            <div key={msg.id} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[85%] rounded-lg px-4 py-3 ${
                  msg.role === "user"
                    ? "bg-violet-600/20 border border-violet-600/40 text-foreground"
                    : "bg-card border border-border"
                }`}
              >
                {msg.step && ["parsing", "testing", "deploying"].includes(msg.step) && (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin text-violet-400" />
                    {msg.content}
                  </div>
                )}

                {msg.step === "error" && (
                  <div className="flex items-start gap-2 text-sm text-red-400">
                    <XCircle className="h-4 w-4 mt-0.5 shrink-0" />
                    <span>{msg.content}</span>
                  </div>
                )}

                {msg.step === "parsed" && msg.parsedRule && (
                  <ParsedRuleCard rule={msg.parsedRule} explanation={msg.explanation || ""} catalog={fieldCatalog} />
                )}

                {msg.step === "tested" && msg.backtestResult && (
                  <BacktestCard
                    result={msg.backtestResult}
                    expanded={expandedMatches}
                    onToggle={() => setExpandedMatches(!expandedMatches)}
                    catalog={fieldCatalog}
                  />
                )}

                {msg.step === "deployed" && msg.deployResult && (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 text-green-400 text-sm font-medium">
                      <Rocket className="h-4 w-4" />
                      Rule Deployed Successfully
                    </div>
                    <p className="text-xs text-muted-foreground">{msg.deployResult.message}</p>
                    <div className="text-[10px] text-zinc-500 font-mono">
                      ID: {msg.deployResult.rule_id}
                    </div>
                  </div>
                )}

                {msg.role === "user" && <p className="text-sm">{msg.content}</p>}
                {msg.role === "system" && !msg.step && <p className="text-sm">{msg.content}</p>}

                <div className="text-[10px] text-zinc-600 mt-1">
                  {msg.timestamp.toLocaleTimeString()}
                </div>
              </div>
            </div>
          ))}

          {/* Visual Rule Editor */}
          {(step === "parsed" || step === "editing") && currentRule && fieldCatalog && (
            <div className="w-full max-w-[90%] mx-auto">
              <div className="rounded-lg border border-violet-600/30 bg-card p-4 space-y-3">
                <RuleEditor
                  rule={currentRule}
                  catalog={fieldCatalog}
                  onChange={handleRuleChange}
                />
              </div>
            </div>
          )}

          {/* Action buttons */}
          {(step === "parsed" || step === "editing") && currentRule && (
            <div className="flex gap-2 justify-center">
              <button
                onClick={handleTest}
                className="flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium hover:bg-blue-500 transition-colors"
              >
                <FlaskConical className="h-4 w-4" />
                Test on Historical Data
              </button>
              <button
                onClick={() => { setStep("idle"); setCurrentRule(null); }}
                className="flex items-center gap-2 rounded-lg bg-zinc-700 px-4 py-2 text-sm hover:bg-zinc-600 transition-colors"
              >
                Discard
              </button>
            </div>
          )}

          {step === "tested" && currentRule && (
            <div className="flex gap-2 justify-center">
              <button
                onClick={handleDeploy}
                className="flex items-center gap-2 rounded-lg bg-green-600 px-4 py-2 text-sm font-medium hover:bg-green-500 transition-colors"
              >
                <Rocket className="h-4 w-4" />
                Deploy to Flink
              </button>
              <button
                onClick={handleTest}
                className="flex items-center gap-2 rounded-lg bg-blue-600/60 px-4 py-2 text-sm hover:bg-blue-500 transition-colors"
              >
                <FlaskConical className="h-4 w-4" />
                Re-test
              </button>
              <button
                onClick={() => { setStep("idle"); setCurrentRule(null); setBacktestResult(null); }}
                className="flex items-center gap-2 rounded-lg bg-zinc-700 px-4 py-2 text-sm hover:bg-zinc-600 transition-colors"
              >
                Discard
              </button>
            </div>
          )}

          {step === "error" && (
            <div className="flex gap-2 justify-center">
              <button
                onClick={() => setStep("idle")}
                className="flex items-center gap-2 rounded-lg bg-zinc-700 px-4 py-2 text-sm hover:bg-zinc-600 transition-colors"
              >
                Try Again
              </button>
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div className="border-t border-border px-3 pt-3">
          <div className="flex gap-2">
            <input
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
              placeholder="Describe a rule in English, e.g. 'Block PEP sender with amount above 10k'"
              disabled={isLoading}
              className="flex-1 rounded-lg border border-border bg-card px-4 py-2.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-violet-500/50 disabled:opacity-50"
            />
            <button
              onClick={handleSend}
              disabled={!input.trim() || isLoading}
              className="rounded-lg bg-violet-600 px-4 py-2.5 text-sm font-medium hover:bg-violet-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              <Send className="h-4 w-4" />
            </button>
          </div>
          <div className="flex items-center gap-4 mt-2 text-[10px] text-zinc-500">
            <span className="flex items-center gap-1">
              <Sparkles className="h-3 w-3" /> NLP Parse
            </span>
            <span>→</span>
            <span className="flex items-center gap-1">
              <Pencil className="h-3 w-3" /> Edit
            </span>
            <span>→</span>
            <span className="flex items-center gap-1">
              <FlaskConical className="h-3 w-3" /> Backtest
            </span>
            <span>→</span>
            <span className="flex items-center gap-1">
              <Rocket className="h-3 w-3" /> Deploy
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Sub-components ──────────────────────────────────────────────────

function ParsedRuleCard({ rule, explanation, catalog }: { rule: ParsedRule; explanation: string; catalog: FieldCatalog | null }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium text-violet-400">
        <Sparkles className="h-4 w-4" />
        Rule Parsed
      </div>

      <p className="text-xs text-muted-foreground" dangerouslySetInnerHTML={{ __html: explanation.replace(/\*\*(.*?)\*\*/g, '<strong class="text-foreground">$1</strong>') }} />

      {/* Conditions with category labels */}
      <div className="space-y-1.5">
        {rule.conditions.map((c, i) => {
          const catInfo = c.category && catalog?.categories[c.category];
          return (
            <div key={i} className="flex items-center gap-2 text-xs flex-wrap">
              {i > 0 && (
                <span className="text-violet-400 font-bold text-[10px] w-8">{rule.logic}</span>
              )}
              {catInfo && (
                <span className="text-[9px] rounded-full px-1.5 py-0.5 border" style={{ color: catInfo.color, borderColor: `${catInfo.color}40` }}>
                  {catInfo.label}
                </span>
              )}
              <span className="rounded bg-zinc-800 px-2 py-0.5 font-mono">{c.field}</span>
              <span className="text-violet-400 font-bold">{OP_LABELS[c.operator] || c.operator}</span>
              <span className="rounded bg-zinc-800 px-2 py-0.5 font-mono">{c.value}</span>
            </div>
          );
        })}
      </div>

      {/* Metadata */}
      <div className="flex flex-wrap gap-2 text-[10px]">
        <span className={`rounded border px-2 py-0.5 font-bold ${ACTION_COLORS[rule.action] || ""}`}>
          {rule.action}
        </span>
        <span className={`font-medium ${SEVERITY_COLORS[rule.severity] || ""}`}>
          {rule.severity}
        </span>
        <span className="text-muted-foreground">
          Risk: +{rule.risk_score_adjustment}
        </span>
      </div>

      {/* Flink SQL preview */}
      <div className="rounded bg-zinc-900 border border-zinc-700 p-2 text-[10px] font-mono text-zinc-400">
        <div className="text-zinc-500 mb-1">-- Flink SQL (detection-rules topic)</div>
        <div className="text-green-400">
          {rule.conditions.map((c, i) => (
            <span key={i}>
              {i > 0 ? ` ${rule.logic} ` : ""}
              {c.field} {OP_LABELS[c.operator] || c.operator} '{c.value}'
            </span>
          ))}
          {" → "}{rule.action}
        </div>
      </div>
    </div>
  );
}

function BacktestCard({
  result,
  expanded,
  onToggle,
  catalog,
}: {
  result: BacktestResult;
  expanded: boolean;
  onToggle: () => void;
  catalog: FieldCatalog | null;
}) {
  const isHigh = result.match_rate > 10;
  const isVeryHigh = result.match_rate > 30;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm font-medium text-blue-400">
        <BarChart3 className="h-4 w-4" />
        Backtest Results
      </div>

      {/* Joins / categories used */}
      {result.categories_used && result.categories_used.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {result.categories_used.map((cat) => {
            const catInfo = catalog?.categories[cat];
            return (
              <span key={cat} className="text-[9px] rounded-full px-2 py-0.5 border" style={{ color: catInfo?.color || "#888", borderColor: `${catInfo?.color || "#888"}40` }}>
                {catInfo?.label || cat}
              </span>
            );
          })}
          {result.joins_used && Object.keys(result.joins_used).length > 0 && (
            <span className="text-[9px] text-zinc-500 ml-1">
              (joined: {Object.keys(result.joins_used).join(", ")})
            </span>
          )}
        </div>
      )}

      {isVeryHigh && (
        <div className="flex items-center gap-2 rounded bg-red-900/30 border border-red-700/50 px-3 py-2 text-xs text-red-400">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>Very high match rate ({result.match_rate}%). This rule may be too broad.</span>
        </div>
      )}
      {isHigh && !isVeryHigh && (
        <div className="flex items-center gap-2 rounded bg-yellow-900/30 border border-yellow-700/50 px-3 py-2 text-xs text-yellow-400">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>High match rate ({result.match_rate}%). Consider refining conditions.</span>
        </div>
      )}

      {/* Stats */}
      <div className="grid grid-cols-3 gap-2">
        <div className="rounded bg-zinc-800/80 p-2 text-center">
          <div className="text-lg font-bold">{result.total_matched.toLocaleString()}</div>
          <div className="text-[10px] text-muted-foreground">Matched</div>
        </div>
        <div className="rounded bg-zinc-800/80 p-2 text-center">
          <div className="text-lg font-bold">{result.total_transactions.toLocaleString()}</div>
          <div className="text-[10px] text-muted-foreground">Total Txns</div>
        </div>
        <div className="rounded bg-zinc-800/80 p-2 text-center">
          <div className={`text-lg font-bold ${isVeryHigh ? "text-red-400" : isHigh ? "text-yellow-400" : "text-green-400"}`}>
            {result.match_rate}%
          </div>
          <div className="text-[10px] text-muted-foreground">Match Rate</div>
        </div>
      </div>

      {/* Risk distribution */}
      {Object.keys(result.risk_distribution).length > 0 && (
        <div className="space-y-1">
          <div className="text-[10px] text-muted-foreground font-medium">Risk Distribution of Matches</div>
          <div className="flex gap-2 flex-wrap">
            {Object.entries(result.risk_distribution).map(([level, count]) => (
              <span key={level} className={`text-[10px] rounded px-2 py-0.5 ${SEVERITY_COLORS[level] || ""} bg-zinc-800`}>
                {level}: {count}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Sample matches */}
      {result.sample_matches.length > 0 && (
        <div>
          <button onClick={onToggle} className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            {expanded ? "Hide" : "Show"} sample matches ({result.sample_matches.length})
          </button>
          {expanded && (
            <div className="mt-2 max-h-60 overflow-auto rounded border border-border">
              <table className="w-full text-[10px]">
                <thead className="bg-zinc-800 sticky top-0">
                  <tr>
                    <th className="text-left px-2 py-1">External ID</th>
                    <th className="text-left px-2 py-1">Sender</th>
                    <th className="text-right px-2 py-1">Amount</th>
                    <th className="text-left px-2 py-1">Currency</th>
                    <th className="text-left px-2 py-1">Risk</th>
                    <th className="text-left px-2 py-1">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {result.sample_matches.map((m, i) => (
                    <tr key={i} className="border-t border-border hover:bg-zinc-800/50">
                      <td className="px-2 py-1 font-mono">{String(m.external_id || "").slice(0, 12)}</td>
                      <td className="px-2 py-1">{String(m.sender_id || "").slice(0, 20)}</td>
                      <td className="px-2 py-1 text-right font-mono">
                        {Number(m.amount || 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                      </td>
                      <td className="px-2 py-1">{String(m.currency || "")}</td>
                      <td className="px-2 py-1">
                        <span className={SEVERITY_COLORS[String(m.risk_level || "")] || ""}>
                          {String(m.risk_level || m.sender_risk_rating || "—")}
                        </span>
                      </td>
                      <td className="px-2 py-1">{String(m.tx_status || m.status || "")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Verdict */}
      <div className="flex items-center gap-2 text-xs">
        {result.total_matched === 0 ? (
          <>
            <CheckCircle className="h-4 w-4 text-green-400" />
            <span className="text-green-400">No historical matches — this is a new pattern detection rule.</span>
          </>
        ) : !isVeryHigh ? (
          <>
            <Shield className="h-4 w-4 text-blue-400" />
            <span className="text-blue-400">Match rate looks reasonable. Ready to deploy.</span>
          </>
        ) : (
          <>
            <AlertTriangle className="h-4 w-4 text-red-400" />
            <span className="text-red-400">Consider narrowing the rule before deploying.</span>
          </>
        )}
      </div>
    </div>
  );
}
