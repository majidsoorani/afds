/**
 * CommandPalette — Cmd+K quick-search across transactions, entities, rules.
 * Replicates third-party vendor's Command search for instant analyst navigation.
 */
import { useState, useEffect, useRef, useCallback } from "react";
import { Search, ArrowRight, Hash, User, Shield, AlertTriangle, FileText, Zap, X } from "lucide-react";

interface CommandItem {
  id: string;
  type: "transaction" | "entity" | "rule" | "action" | "navigation";
  label: string;
  description?: string;
  icon: typeof Search;
  action: () => void;
}

interface CommandPaletteProps {
  onNavigate: (tab: string) => void;
  onSearch?: (query: string, type: string) => void;
}

export function CommandPalette({ onNavigate, onSearch }: CommandPaletteProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [results, setResults] = useState<CommandItem[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  // Static navigation commands
  const staticCommands: CommandItem[] = [
    { id: "nav-overview", type: "navigation", label: "Go to Overview", icon: Shield, action: () => onNavigate("overview") },
    { id: "nav-transactions", type: "navigation", label: "Go to Transactions", icon: Hash, action: () => onNavigate("transactions") },
    { id: "nav-alerts", type: "navigation", label: "Go to Alerts", icon: AlertTriangle, action: () => onNavigate("alerts") },
    { id: "nav-screening", type: "navigation", label: "Go to Screening", icon: Search, action: () => onNavigate("screening") },
    { id: "nav-network", type: "navigation", label: "Go to Network Graph", icon: User, action: () => onNavigate("network") },
    { id: "nav-rules", type: "navigation", label: "Go to Rules Engine", icon: Zap, action: () => onNavigate("rules") },
    { id: "nav-rule-chat", type: "navigation", label: "Go to Rule Chat", icon: Zap, action: () => onNavigate("rule-chat") },
    { id: "nav-reports", type: "navigation", label: "Go to Reports", icon: FileText, action: () => onNavigate("reports") },
    { id: "nav-enrichment", type: "navigation", label: "Go to Enrichment", icon: Search, action: () => onNavigate("enrichment") },
    { id: "nav-devices", type: "navigation", label: "Go to Device Intel", icon: Shield, action: () => onNavigate("device-intel") },
    { id: "nav-debugger", type: "navigation", label: "Go to Visual Debugger", icon: Zap, action: () => onNavigate("debugger") },
    { id: "action-screen", type: "action", label: "Screen a name against sanctions", icon: Shield, action: () => onNavigate("screening") },
    { id: "action-create-rule", type: "action", label: "Create a new detection rule", icon: Zap, action: () => onNavigate("rule-chat") },
  ];

  // Keyboard shortcut: Cmd+K or Ctrl+K
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
      if (e.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
      setQuery("");
      setSelectedIndex(0);
    }
  }, [open]);

  // Filter commands based on query
  useEffect(() => {
    if (!query.trim()) {
      setResults(staticCommands);
      return;
    }

    const q = query.toLowerCase();
    const filtered = staticCommands.filter(
      (c) => c.label.toLowerCase().includes(q) || (c.description || "").toLowerCase().includes(q)
    );

    // Dynamic search items
    const dynamicItems: CommandItem[] = [];

    // If looks like a transaction ID or hash
    if (q.length >= 8) {
      dynamicItems.push({
        id: `search-tx-${q}`,
        type: "transaction",
        label: `Search transaction: ${query}`,
        description: "Look up by external_id or transaction hash",
        icon: Hash,
        action: () => {
          onSearch?.(query, "transaction");
          setOpen(false);
        },
      });
    }

    // Entity search
    if (q.length >= 3) {
      dynamicItems.push({
        id: `search-entity-${q}`,
        type: "entity",
        label: `Search entity: ${query}`,
        description: "Look up user/sender/receiver",
        icon: User,
        action: () => {
          onSearch?.(query, "entity");
          setOpen(false);
        },
      });
    }

    setResults([...dynamicItems, ...filtered]);
    setSelectedIndex(0);
  }, [query]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((i) => Math.min(i + 1, results.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((i) => Math.max(i - 1, 0));
      } else if (e.key === "Enter" && results[selectedIndex]) {
        e.preventDefault();
        results[selectedIndex].action();
        setOpen(false);
      }
    },
    [results, selectedIndex]
  );

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground hover:bg-muted transition-colors"
      >
        <Search className="h-3 w-3" />
        <span>Search...</span>
        <kbd className="ml-2 rounded border border-border px-1.5 py-0.5 text-[10px] font-mono">⌘K</kbd>
      </button>
    );
  }

  const typeColors: Record<string, string> = {
    navigation: "text-blue-400",
    action: "text-violet-400",
    transaction: "text-green-400",
    entity: "text-orange-400",
    rule: "text-yellow-400",
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]" onClick={() => setOpen(false)}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="relative w-full max-w-lg rounded-xl border border-border bg-card shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div className="flex items-center gap-3 border-b border-border px-4 py-3">
          <Search className="h-4 w-4 text-muted-foreground shrink-0" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Search transactions, entities, rules, or navigate..."
            className="flex-1 bg-transparent text-sm placeholder:text-muted-foreground focus:outline-none"
          />
          <button onClick={() => setOpen(false)} className="text-muted-foreground hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Results */}
        <div className="max-h-80 overflow-y-auto py-2">
          {results.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-muted-foreground">
              No results found
            </div>
          ) : (
            results.map((item, i) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.id}
                  onClick={() => {
                    item.action();
                    setOpen(false);
                  }}
                  className={`flex w-full items-center gap-3 px-4 py-2 text-left text-sm transition-colors ${
                    i === selectedIndex ? "bg-violet-600/20 text-foreground" : "text-muted-foreground hover:bg-muted"
                  }`}
                >
                  <Icon className={`h-4 w-4 shrink-0 ${typeColors[item.type] || ""}`} />
                  <div className="flex-1 min-w-0">
                    <div className="truncate">{item.label}</div>
                    {item.description && (
                      <div className="text-[10px] text-zinc-500 truncate">{item.description}</div>
                    )}
                  </div>
                  <span className="text-[9px] rounded px-1.5 py-0.5 bg-zinc-800 text-zinc-500">
                    {item.type}
                  </span>
                  <ArrowRight className="h-3 w-3 shrink-0 opacity-40" />
                </button>
              );
            })
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center gap-4 border-t border-border px-4 py-2 text-[10px] text-zinc-500">
          <span>↑↓ navigate</span>
          <span>↵ select</span>
          <span>esc close</span>
        </div>
      </div>
    </div>
  );
}
