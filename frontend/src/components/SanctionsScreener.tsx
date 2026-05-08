import { useState } from "react";
import { Search, User } from "lucide-react";
import { api, type SanctionsMatch } from "@/lib/api";

export function SanctionsScreener() {
  const [query, setQuery] = useState("");
  const [threshold, setThreshold] = useState(0.5);
  const [results, setResults] = useState<SanctionsMatch[]>([]);
  const [searching, setSearching] = useState(false);

  const handleSearch = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const matches = await api.screenName(query, threshold);
      setResults(matches);
    } catch {
      setResults([]);
    } finally {
      setSearching(false);
    }
  };

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="p-4 border-b border-border">
        <h2 className="text-lg font-semibold">Sanctions Screening</h2>
        <p className="text-xs text-muted-foreground mt-1">
          Fuzzy match against OpenSanctions, OFAC & UN lists (&lt;1ms)
        </p>
      </div>
      <div className="p-4">
        <div className="flex gap-2">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              placeholder="Enter entity name to screen..."
              className="w-full pl-10 pr-4 py-2 rounded-md bg-muted border border-border text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
          <select
            value={threshold}
            onChange={(e) => setThreshold(Number(e.target.value))}
            className="px-3 py-2 rounded-md bg-muted border border-border text-sm"
          >
            <option value={0.3}>0.3 (Broad)</option>
            <option value={0.5}>0.5 (Default)</option>
            <option value={0.7}>0.7 (Strict)</option>
            <option value={0.9}>0.9 (Exact)</option>
          </select>
          <button
            onClick={handleSearch}
            disabled={searching}
            className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
          >
            {searching ? "Screening..." : "Screen"}
          </button>
        </div>

        {results.length > 0 && (
          <div className="mt-4 divide-y divide-border/50">
            {results.map((match) => (
              <div key={`${match.entity_id}-${match.matched_name}`} className="py-3 flex items-center gap-3">
                <User className="h-4 w-4 text-red-400" />
                <div className="flex-1">
                  <p className="text-sm font-medium">{match.matched_name}</p>
                  <p className="text-[10px] text-muted-foreground font-mono">
                    {match.entity_id}
                  </p>
                </div>
                <div className="text-right">
                  <span
                    className={`text-sm font-bold ${
                      match.similarity > 0.8
                        ? "text-red-400"
                        : match.similarity > 0.6
                        ? "text-yellow-400"
                        : "text-green-400"
                    }`}
                  >
                    {(match.similarity * 100).toFixed(1)}%
                  </span>
                  <p className="text-[10px] text-muted-foreground">match</p>
                </div>
              </div>
            ))}
          </div>
        )}

        {results.length === 0 && query && !searching && (
          <div className="mt-4 p-4 text-center text-sm text-muted-foreground">
            No matches found above threshold.
          </div>
        )}
      </div>
    </div>
  );
}
