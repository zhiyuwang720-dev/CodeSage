from __future__ import annotations

from dataclasses import dataclass

from codesage_eval.contracts import JudgmentRecord


@dataclass
class _Edge:
    target: int
    reverse: int
    capacity: int
    cost: int
    pair: tuple[str, str] | None = None


def maximum_matching(judgments: list[JudgmentRecord]) -> list[tuple[str, str]]:
    """Maximize pair count, then total confidence, then stable identifiers."""
    matched_edges = [item for item in judgments if item.matched is True]
    candidate_ids = sorted({item.candidate_id for item in matched_edges})
    golden_ids = sorted({item.golden_id for item in matched_edges})
    source = 0
    candidate_offset = 1
    golden_offset = candidate_offset + len(candidate_ids)
    sink = golden_offset + len(golden_ids)
    graph: list[list[_Edge]] = [[] for _ in range(sink + 1)]

    def add_edge(start: int, end: int, capacity: int, cost: int, pair=None) -> None:
        forward = _Edge(end, len(graph[end]), capacity, cost, pair)
        reverse = _Edge(start, len(graph[start]), 0, -cost)
        graph[start].append(forward)
        graph[end].append(reverse)

    for index in range(len(candidate_ids)):
        add_edge(source, candidate_offset + index, 1, 0)
    for index in range(len(golden_ids)):
        add_edge(golden_offset + index, sink, 1, 0)
    candidate_index = {value: index for index, value in enumerate(candidate_ids)}
    golden_index = {value: index for index, value in enumerate(golden_ids)}
    best: dict[tuple[str, str], float] = {}
    for item in matched_edges:
        key = (item.candidate_id, item.golden_id)
        best[key] = max(best.get(key, 0.0), float(item.confidence or 0.0))
    stable_pairs = sorted(best)
    rank = {pair: index for index, pair in enumerate(stable_pairs)}
    for pair in stable_pairs:
        confidence = max(0.0, min(1.0, best[pair]))
        reward = 10**12 + int(confidence * 1_000_000) * 10_000 - rank[pair]
        add_edge(
            candidate_offset + candidate_index[pair[0]],
            golden_offset + golden_index[pair[1]],
            1,
            -reward,
            pair,
        )

    while True:
        distance = [10**30] * len(graph)
        previous: list[tuple[int, int] | None] = [None] * len(graph)
        distance[source] = 0
        for _ in range(len(graph)):
            changed = False
            for node, edges in enumerate(graph):
                if distance[node] == 10**30:
                    continue
                for edge_index, edge in enumerate(edges):
                    candidate = distance[node] + edge.cost
                    if edge.capacity and candidate < distance[edge.target]:
                        distance[edge.target] = candidate
                        previous[edge.target] = (node, edge_index)
                        changed = True
            if not changed:
                break
        if previous[sink] is None or distance[sink] >= 0:
            break
        node = sink
        while node != source:
            prior, edge_index = previous[node]
            edge = graph[prior][edge_index]
            edge.capacity -= 1
            graph[node][edge.reverse].capacity += 1
            node = prior

    pairs = []
    for edges in graph[candidate_offset:golden_offset]:
        pairs.extend(edge.pair for edge in edges if edge.pair is not None and edge.capacity == 0)
    return sorted(pairs)
