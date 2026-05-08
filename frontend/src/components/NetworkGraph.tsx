import { useState, useEffect, useCallback, useRef } from "react";
import { GitBranch, Search } from "lucide-react";
import * as d3 from "d3";

interface GraphNode {
  id: string;
  label: string;
  type: string;
  risk_level: string;
  transaction_count: number;
  total_amount: number;
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
}

interface GraphEdge {
  source: string | GraphNode;
  target: string | GraphNode;
  amount: number;
  count: number;
  currency: string;
}

interface NetworkData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  root: string;
  depth: number;
}

export function NetworkGraph() {
  const [entityId, setEntityId] = useState("");
  const [depth, setDepth] = useState(2);
  const [data, setData] = useState<NetworkData | null>(null);
  const [loading, setLoading] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);

  const fetchNetwork = useCallback(async () => {
    if (!entityId.trim()) return;
    setLoading(true);
    try {
      const res = await fetch(`/api/v1/network/graph/${encodeURIComponent(entityId)}?depth=${depth}`);
      const json = await res.json();
      setData(json);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }, [entityId, depth]);

  useEffect(() => {
    if (!data || !svgRef.current) return;

    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove();

    const width = svgRef.current.clientWidth;
    const height = 500;

    const g = svg.append("g");

    // Zoom
    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.3, 5])
      .on("zoom", (event) => g.attr("transform", event.transform));
    (svg as any).call(zoom);

    const riskColor: Record<string, string> = {
      LOW: "#22c55e",
      MEDIUM: "#eab308",
      HIGH: "#f97316",
      CRITICAL: "#ef4444",
    };

    // Force simulation
    const simulation = d3.forceSimulation<GraphNode>(data.nodes)
      .force("link", d3.forceLink<GraphNode, GraphEdge>(data.edges as any)
        .id((d) => d.id)
        .distance(120))
      .force("charge", d3.forceManyBody().strength(-300))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collision", d3.forceCollide().radius(40));

    // Edges
    const link = g.append("g")
      .selectAll("line")
      .data(data.edges)
      .join("line")
      .attr("stroke", "#555")
      .attr("stroke-opacity", 0.6)
      .attr("stroke-width", (d) => Math.max(1, Math.min(5, d.count)));

    // Edge labels
    const linkLabel = g.append("g")
      .selectAll("text")
      .data(data.edges)
      .join("text")
      .attr("text-anchor", "middle")
      .attr("fill", "#888")
      .attr("font-size", "9px")
      .text((d) => `${d.currency} ${d.amount.toLocaleString()}`);

    // Nodes
    const node = g.append("g")
      .selectAll("g")
      .data(data.nodes)
      .join("g")
      .call(
        d3.drag<any, GraphNode>()
          .on("start", (event, d) => {
            if (!event.active) simulation.alphaTarget(0.3).restart();
            d.fx = d.x;
            d.fy = d.y;
          })
          .on("drag", (event, d) => {
            d.fx = event.x;
            d.fy = event.y;
          })
          .on("end", (event, d) => {
            if (!event.active) simulation.alphaTarget(0);
            d.fx = null;
            d.fy = null;
          })
      );

    node.append("circle")
      .attr("r", (d) => d.id === data.root ? 18 : 12)
      .attr("fill", (d) => riskColor[d.risk_level] || "#666")
      .attr("stroke", (d) => d.id === data.root ? "#fff" : "transparent")
      .attr("stroke-width", 2);

    node.append("text")
      .attr("dy", 25)
      .attr("text-anchor", "middle")
      .attr("fill", "#ccc")
      .attr("font-size", "10px")
      .text((d) => d.id.length > 15 ? d.id.slice(0, 15) + "…" : d.id);

    simulation.on("tick", () => {
      link
        .attr("x1", (d: any) => d.source.x)
        .attr("y1", (d: any) => d.source.y)
        .attr("x2", (d: any) => d.target.x)
        .attr("y2", (d: any) => d.target.y);

      linkLabel
        .attr("x", (d: any) => (d.source.x + d.target.x) / 2)
        .attr("y", (d: any) => (d.source.y + d.target.y) / 2);

      node.attr("transform", (d: any) => `translate(${d.x},${d.y})`);
    });

    return () => { simulation.stop(); };
  }, [data]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <GitBranch className="h-5 w-5 text-cyan-400" />
        <h2 className="text-lg font-semibold">Network Graph Analysis</h2>
      </div>

      {/* Search */}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            value={entityId}
            onChange={(e) => setEntityId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && fetchNetwork()}
            placeholder="Enter entity / sender ID..."
            className="w-full rounded bg-zinc-800 pl-9 pr-3 py-2 text-sm"
          />
        </div>
        <select
          value={depth}
          onChange={(e) => setDepth(Number(e.target.value))}
          className="rounded bg-zinc-800 px-3 py-2 text-sm"
        >
          <option value={1}>1 hop</option>
          <option value={2}>2 hops</option>
          <option value={3}>3 hops</option>
        </select>
        <button
          onClick={fetchNetwork}
          disabled={loading || !entityId.trim()}
          className="rounded bg-cyan-600 px-4 py-2 text-sm font-medium hover:bg-cyan-500 disabled:opacity-50"
        >
          {loading ? "Loading..." : "Map Network"}
        </button>
      </div>

      {/* Graph */}
      {data && (
        <>
          <div className="grid grid-cols-4 gap-3">
            <div className="rounded-lg border border-border bg-card p-3">
              <div className="text-xs text-muted-foreground">Nodes</div>
              <div className="text-xl font-bold">{data.nodes.length}</div>
            </div>
            <div className="rounded-lg border border-border bg-card p-3">
              <div className="text-xs text-muted-foreground">Edges</div>
              <div className="text-xl font-bold">{data.edges.length}</div>
            </div>
            <div className="rounded-lg border border-border bg-card p-3">
              <div className="text-xs text-muted-foreground">Root</div>
              <div className="text-sm font-bold truncate">{data.root}</div>
            </div>
            <div className="rounded-lg border border-border bg-card p-3">
              <div className="text-xs text-muted-foreground">Depth</div>
              <div className="text-xl font-bold">{data.depth} hops</div>
            </div>
          </div>

          <div className="rounded-lg border border-border bg-zinc-900 overflow-hidden">
            <svg ref={svgRef} width="100%" height={500} />
          </div>

          {/* Legend */}
          <div className="flex gap-4 text-xs text-muted-foreground">
            {[
              { label: "LOW", color: "#22c55e" },
              { label: "MEDIUM", color: "#eab308" },
              { label: "HIGH", color: "#f97316" },
              { label: "CRITICAL", color: "#ef4444" },
            ].map((l) => (
              <div key={l.label} className="flex items-center gap-1">
                <div className="h-3 w-3 rounded-full" style={{ backgroundColor: l.color }} />
                {l.label}
              </div>
            ))}
            <span className="ml-4">○ = root entity (white border)</span>
          </div>
        </>
      )}

      {!data && !loading && (
        <div className="text-center py-16 text-muted-foreground">
          Enter an entity ID to visualize its transaction network
        </div>
      )}
    </div>
  );
}
