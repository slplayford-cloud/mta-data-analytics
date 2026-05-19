#!/usr/bin/env python3

from src.graph import SubwayGraph
from src.db import get_connection, create_tables, initialize
from typing import cast
import networkx as nx


def main() -> None:
    conn = get_connection("mta.duckdb")
    create_tables(conn)
    initialize(conn, static_dir="static/")

    g = SubwayGraph(conn)
    g.build()
    G = g.graph

    # --- node count ---
    node_count = G.number_of_nodes()
    print(f"Nodes (parent stations) : {node_count}  (expect ~496)")

    # --- edge breakdown ---
    route_edges    = [(u, v) for u, v, d in G.edges(data=True) if d.get("edge_type") == "route"]
    transfer_edges = [(u, v) for u, v, d in G.edges(data=True) if d.get("edge_type") == "transfer"]
    print(f"Route edges             : {len(route_edges)}")
    print(f"Transfer edges          : {len(transfer_edges)}")

    # --- station lookup ---
    times_sq = g.get_station("127")  # Times Sq-42 St
    if times_sq:
        print(f"\nStation 127 : {times_sq.name}")
        print(f"  Routes    : {sorted(times_sq.routes)}")
    else:
        print("FAIL: station 127 not found")

    # --- edge attributes on a known segment ---
    # 101 (Van Cortlandt Park-242 St) → 103 (238 St) — first segment of the 1 line
    if G.has_edge("101", "103"):
        edge = G["101"]["103"]
        print(f"\nEdge 101 → 103:")
        print(f"  routes    : {edge['routes']}")
        print(f"  weight    : {edge['weight']:.0f}s  ({edge['weight'] / 60:.1f} min)")
        print(f"  edge_type : {edge['edge_type']}")
    else:
        print("FAIL: edge 101→103 not found")

    # --- transfer edge exists between known connected stations ---
    # 127 (Times Sq-42 St) ↔ 725 (Times Sq-42 St / Port Authority area)
    if G.has_edge("127", "725"):
        t_edge = G["127"]["725"]
        print(f"\nTransfer 127 → 725:")
        print(f"  edge_type : {t_edge['edge_type']}")
        print(f"  weight    : {t_edge['weight']}s")
    else:
        print("\nNo transfer edge 127→725 (may not exist — check transfers.txt)")

    # --- update_trains with empty list clears without error ---
    g.update_trains([])
    assert g.get_trains_at("127") == [], "trains list should be empty after update_trains([])"
    print("\nupdate_trains([])       : OK")

    # --- unknown station returns safe defaults ---
    assert g.get_station("FAKE") is None
    assert g.get_trains_at("FAKE") == []
    print("Unknown station lookups : OK")

    # --- graph is connected (weakly) ---
    components = list(nx.weakly_connected_components(G))
    print(f"\nWeakly connected components: {len(components)}  (expect 1, Staten Island may be 2)")


if __name__ == "__main__":
    main()
