"""
Network & Graph Analysis — entity resolution, fund flow tracing, community detection.

Provides graph data for D3.js visualization on the frontend.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/network", tags=["network"])


# ── In-memory graph store (production: PostgreSQL + Neo4j) ───────────

_transactions_store: list[dict] = []


class GraphNode(BaseModel):
    id: str
    label: str
    type: str = "entity"
    risk_level: str = "LOW"
    transaction_count: int = 0
    total_amount: float = 0.0


class GraphEdge(BaseModel):
    source: str
    target: str
    amount: float
    count: int
    currency: str = "GBP"


class NetworkGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    root: str
    depth: int


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("/graph/{entity_id}")
async def get_entity_network(
    entity_id: str,
    depth: int = Query(default=2, ge=1, le=4),
):
    """Get the transaction network around an entity.

    Returns nodes (entities) and edges (fund flows) for D3.js visualization.
    Traces up to `depth` hops from the root entity.
    """
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(entity_id, 0)]

    while queue:
        current, d = queue.pop(0)
        if current in visited or d > depth:
            continue
        visited.add(current)

        # Aggregate outgoing/incoming transactions
        edge_map: dict[tuple[str, str], dict] = defaultdict(lambda: {"amount": 0.0, "count": 0, "currency": "GBP"})
        for tx in _transactions_store:
            if tx.get("sender_id") == current or tx.get("receiver_id") == current:
                s = tx["sender_id"]
                r = tx.get("receiver_id", "unknown")
                key = (s, r)
                edge_map[key]["amount"] += float(tx.get("amount", 0))
                edge_map[key]["count"] += 1
                edge_map[key]["currency"] = tx.get("currency", "GBP")

                # Ensure nodes exist
                for n in [s, r]:
                    if n and n not in nodes:
                        nodes[n] = GraphNode(id=n, label=n)

                # Add counterparty to queue
                counterparty = r if s == current else s
                if counterparty and counterparty not in visited and d + 1 <= depth:
                    queue.append((counterparty, d + 1))

        for (s, r), data in edge_map.items():
            edges.append(GraphEdge(source=s, target=r, **data))

    # Update node stats
    for node in nodes.values():
        sent = sum(1 for tx in _transactions_store if tx.get("sender_id") == node.id)
        received = sum(1 for tx in _transactions_store if tx.get("receiver_id") == node.id)
        node.transaction_count = sent + received
        node.total_amount = sum(
            float(tx.get("amount", 0))
            for tx in _transactions_store
            if tx.get("sender_id") == node.id or tx.get("receiver_id") == node.id
        )

    # Ensure root node exists
    if entity_id not in nodes:
        nodes[entity_id] = GraphNode(id=entity_id, label=entity_id)

    return NetworkGraph(
        nodes=list(nodes.values()),
        edges=edges,
        root=entity_id,
        depth=depth,
    )


@router.get("/communities")
async def detect_communities():
    """Simple community detection using connected components.

    Groups entities that transact with each other into communities.
    """
    # Build adjacency list
    adj: dict[str, set[str]] = defaultdict(set)
    for tx in _transactions_store:
        s = tx.get("sender_id")
        r = tx.get("receiver_id")
        if s and r:
            adj[s].add(r)
            adj[r].add(s)

    # BFS-based connected components
    visited: set[str] = set()
    communities: list[dict] = []

    for entity in adj:
        if entity in visited:
            continue
        component: set[str] = set()
        queue = [entity]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            for neighbor in adj.get(current, set()):
                if neighbor not in visited:
                    queue.append(neighbor)

        if len(component) > 1:
            # Compute community stats
            total_amount = sum(
                float(tx.get("amount", 0))
                for tx in _transactions_store
                if tx.get("sender_id") in component or tx.get("receiver_id") in component
            )
            communities.append({
                "id": str(uuid4()),
                "members": sorted(component),
                "size": len(component),
                "total_amount": round(total_amount, 2),
                "internal_edges": sum(
                    1 for tx in _transactions_store
                    if tx.get("sender_id") in component and tx.get("receiver_id") in component
                ),
            })

    return {
        "communities": sorted(communities, key=lambda c: c["size"], reverse=True),
        "count": len(communities),
    }


@router.get("/fund-flow/{entity_id}")
async def trace_fund_flow(entity_id: str, direction: str = Query(default="both", pattern="^(in|out|both)$")):
    """Trace fund flow for an entity — incoming, outgoing, or both."""
    incoming: list[dict] = []
    outgoing: list[dict] = []

    for tx in _transactions_store:
        if direction in ("in", "both") and tx.get("receiver_id") == entity_id:
            incoming.append({
                "from": tx["sender_id"],
                "amount": float(tx.get("amount", 0)),
                "currency": tx.get("currency", "GBP"),
                "timestamp": tx.get("created_at"),
                "type": tx.get("transaction_type"),
            })
        if direction in ("out", "both") and tx.get("sender_id") == entity_id:
            outgoing.append({
                "to": tx.get("receiver_id"),
                "amount": float(tx.get("amount", 0)),
                "currency": tx.get("currency", "GBP"),
                "timestamp": tx.get("created_at"),
                "type": tx.get("transaction_type"),
            })

    total_in = sum(t["amount"] for t in incoming)
    total_out = sum(t["amount"] for t in outgoing)

    return {
        "entity": entity_id,
        "incoming": {"transactions": incoming, "total": round(total_in, 2), "count": len(incoming)},
        "outgoing": {"transactions": outgoing, "total": round(total_out, 2), "count": len(outgoing)},
        "net_flow": round(total_in - total_out, 2),
    }


@router.post("/ingest")
async def ingest_transaction_for_graph(transaction: dict):
    """Ingest a transaction into the network graph store."""
    _transactions_store.append({
        **transaction,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"status": "ingested", "graph_size": len(_transactions_store)}
