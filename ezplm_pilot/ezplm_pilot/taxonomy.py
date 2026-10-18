from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class Node:
    id: str
    name: str
    parent: str = ""
    depth: int = 0
    children: list = field(default_factory=list)


class Taxonomy:
    """物料分类树。支持从 TSV（id/parent/depth/name）或后台 admin-categories JSON 加载。"""

    def __init__(self, nodes: Iterable[Node]):
        self.nodes: dict[str, Node] = {n.id: n for n in nodes}
        for n in self.nodes.values():
            if n.parent and n.parent in self.nodes:
                self.nodes[n.parent].children.append(n.id)
        self.by_name: dict[str, list[Node]] = {}
        for n in self.nodes.values():
            self.by_name.setdefault(n.name, []).append(n)

    # ---------- 加载 ----------
    @classmethod
    def from_tsv(cls, path: str | Path) -> "Taxonomy":
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        return cls(Node(r["id"], r["name"], r.get("parent", ""), int(r.get("depth", 0))) for r in rows)

    @classmethod
    def from_api_json(cls, data: list | dict) -> "Taxonomy":
        """后台 /admin-api/admin-categories?taxonomy=partlib 的返回（data 为树形数组）。"""
        if isinstance(data, dict):
            data = data.get("data", data)
        out: list[Node] = []

        def walk(n, depth, parent):
            out.append(Node(n["id"], n["name"], parent, depth))
            for ch in n.get("children", []) or []:
                walk(ch, depth + 1, n["id"])

        for root in data:
            walk(root, 0, "")
        return cls(out)

    # ---------- 查询 ----------
    def path(self, node_id: str) -> str:
        parts = []
        cur = self.nodes.get(node_id)
        while cur:
            parts.append(cur.name)
            cur = self.nodes.get(cur.parent) if cur.parent else None
        return " > ".join(reversed(parts))

    def root_of(self, node_id: str) -> str:
        cur = self.nodes.get(node_id)
        while cur and cur.parent and cur.parent in self.nodes:
            cur = self.nodes[cur.parent]
        return cur.id if cur else ""

    def is_leaf(self, node_id: str) -> bool:
        n = self.nodes.get(node_id)
        return bool(n) and not n.children

    def leaves(self) -> list[Node]:
        return [n for n in self.nodes.values() if not n.children]

    def find(self, name: str) -> list[Node]:
        return self.by_name.get(name, [])

    def resolve(self, name: str, hint_path: str = "") -> Node | None:
        """按名字找节点；重名（如 存储器/比较器/专用）时用路径提示消歧。"""
        cands = self.find(name)
        if not cands:
            return None
        if len(cands) == 1 or not hint_path:
            # 多个同名时优先返回叶子
            leaves = [c for c in cands if not c.children]
            return (leaves or cands)[0]
        for c in cands:
            if self.path(c.id).endswith(f"{hint_path} > {name}") or f"{hint_path} > {name}" in self.path(c.id):
                return c
        for c in cands:
            if hint_path in self.path(c.id):
                return c
        return cands[0]

    def leaf_labels(self) -> list[tuple[str, str]]:
        """[(id, 全路径)] —— 给 LLM 的候选清单用。"""
        return [(n.id, self.path(n.id)) for n in self.leaves()]

    def dump_json(self, path: str | Path):
        Path(path).write_text(json.dumps([{"id": i, "path": p} for i, p in self.leaf_labels()], ensure_ascii=False, indent=1), encoding="utf-8")
