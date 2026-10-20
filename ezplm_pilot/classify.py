"""分类校验：规则 → 近邻投票 → LLM 仲裁 → 符号库交叉验证 → 决策。

用法（见 cli.py）：
    clf = Classifier(taxonomy, rules_path)
    clf.fit(trusted_parts)                 # 已有分类且可信的器件作为近邻库
    for p in parts: proposal = clf.classify(p)

决策规则（可在 Policy 中调阈值）：
    原分类为空   且 conf >= auto_fill_min   -> auto_fill   （自动写入 + 标签"自动分类"）
    原分类非空   且 预测 != 原分类 且 conf >= flag_min -> flag_mismatch（不改，打标签"分类疑似错误"）
    原分类非空   且 预测 == 原分类            -> ok
    其余                                     -> review（进人工复核队列）
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.neighbors import NearestNeighbors

from .models import Part, Proposal
from .taxonomy import Taxonomy

HERE = Path(__file__).resolve().parent.parent


@dataclass
class Policy:
    auto_fill_min: float = 0.85
    flag_min: float = 0.85
    propose_min: float = 0.30        # 低于此置信度的猜测不生成建议（避免噪声进复核队列）
    knn_k: int = 7
    llm_band: tuple = (0.45, 0.85)   # 置信度落在此区间 → 送 LLM 仲裁
    knn_only_max: float = 0.80       # 只有近邻支持（无规则/LLM）时的置信度上限：近邻会被同厂商、相似型号带偏（如 TL432 → LED 驱动器），不能单独自动写入或判错
    use_llm: bool = True


class Candidate:
    __slots__ = ("category_id", "conf", "method", "evidence")

    def __init__(self, category_id, conf, method, evidence=None):
        self.category_id, self.conf, self.method, self.evidence = category_id, conf, method, evidence or {}


# --------------------------------------------------------------------------- 规则层
_CAPACITY_RE = re.compile(r"\d+(?:\.\d+)?\s*[KMGk]i?[Bb]?(?:ytes?|its?)?\s*(?:主|of)?\s*(?:Flash|SRAM|PSRAM|DRAM|RAM|EEPROM|ROM|闪存|存储器?|内存)", re.I)


class RuleEngine:
    def __init__(self, tax: Taxonomy, rules_path: str | Path = HERE / "config" / "rules.yaml"):
        self.tax = tax
        cfg = yaml.safe_load(Path(rules_path).read_text(encoding="utf-8"))
        self.keyword = [(re.compile(r["pattern"], re.I), re.compile(r["exclude"], re.I) if r.get("exclude") else None,
                         self._resolve(r["category"]), r.get("conf", 0.8)) for r in cfg.get("keyword", [])]
        self.mpn = [(re.compile(r.get("manufacturer", ".*"), re.I), re.compile(r["pattern"], re.I), self._resolve(r["category"]), r.get("conf", 0.8))
                    for r in cfg.get("mpn", [])]
        self.symbol_lib = cfg.get("symbol_lib", {})
        self.unresolved = [r["category"] for r in cfg.get("keyword", []) + cfg.get("mpn", []) if self._resolve(r["category"]) is None]

    def _resolve(self, name: str) -> str | None:
        if " > " in name:
            parent, leaf = [x.strip() for x in name.split(">")]
            n = self.tax.resolve(leaf, hint_path=parent)
        else:
            n = self.tax.resolve(name)
        return n.id if n else None

    def apply(self, p: Part) -> list[Candidate]:
        out = []
        for mf_re, mpn_re, cid, conf in self.mpn:
            if cid and mf_re.search(p.manufacturer or "") and mpn_re.search(p.mpn or ""):
                out.append(Candidate(cid, conf, "rule:mpn", {"pattern": mpn_re.pattern}))
                break
        text = f"{p.description} {p.description_en} {p.mpn}"
        # “128 KB Flash、32 KB SRAM”这类容量+存储词是器件的属性而不是器件类型，先去掉再匹配关键字
        text_kw = _CAPACITY_RE.sub(" ", text)
        hits = []
        for kw_re, ex_re, cid, conf in self.keyword:
            if cid and kw_re.search(text_kw) and not (ex_re and ex_re.search(text_kw)):
                hits.append((cid, conf, kw_re.pattern))
        if hits:
            cid, conf, pat = hits[0]
            others = {c for c, _, _ in hits[1:] if c != cid}
            if others:                                    # 多条规则指向不同分类：不自动改，降到复核/模型仲裁
                conf = min(conf, 0.6)
            out.append(Candidate(cid, conf, "rule:keyword", {"pattern": pat, "conflicts": [self.tax.path(c) for c in others]} if others else {"pattern": pat}))
        return out

    def symbol_consistent(self, p: Part, category_id: str) -> bool | None:
        """器件带符号时，用符号库前缀校验分类；无法判断返回 None。"""
        if not p.symbol:
            return None
        lib = getattr(p, "symbol_lib", "") or ""
        if not lib:
            return None
        for prefix, allowed in self.symbol_lib.items():
            if lib.startswith(prefix):
                if not allowed:
                    return None
                path = self.tax.path(category_id)
                return any(a in path for a in allowed)
        return None


# --------------------------------------------------------------------------- 近邻层
class KnnVoter:
    """字符 n-gram TF-IDF + 余弦近邻。中文描述无需分词，型号前缀也能被 n-gram 捕捉。"""

    def __init__(self, k: int = 7):
        self.k = k
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1, sublinear_tf=True)
        self.nn: NearestNeighbors | None = None
        self.labels: list[str] = []
        self.ids: list[str] = []

    def fit(self, parts: list[Part]):
        parts = [p for p in parts if p.category_id]
        if len(parts) < 2:
            return self
        X = self.vec.fit_transform([p.text for p in parts])
        self._xt = X.T.tocsr()                              # vote_many 用：近邻库矩阵的转置，只算一次
        self.nn = NearestNeighbors(n_neighbors=min(self.k + 1, len(parts)), metric="cosine").fit(X)
        self.labels = [p.category_id for p in parts]
        self.ids = [p.id for p in parts]
        return self

    def vote(self, p: Part) -> Candidate | None:
        return self.vote_many([p])[0]

    def vote_many(self, parts: list[Part], chunk: int = 100) -> list[Candidate | None]:
        """一批物料一起算近邻：一次稀疏矩阵乘法代替逐条 kneighbors。
        逐条调用时每次都要在 9 万条近邻库上做一遍暴力搜索（线上每颗约 0.3 秒，一批 200 颗约 1 分钟）。
        TF-IDF 向量已做 L2 归一化，点积即余弦相似度，与 NearestNeighbors(metric="cosine") 结果相同。"""
        if self.nn is None:
            return [None] * len(parts)
        import numpy as np
        kk = self.nn.n_neighbors
        out: list[Candidate | None] = []
        for s in range(0, len(parts), chunk):
            sub = parts[s:s + chunk]
            sims = (self.vec.transform([p.text for p in sub]) @ self._xt).toarray()
            for row, p in zip(sims, sub):
                top = np.argpartition(-row, kk - 1)[:kk] if len(row) > kk else np.arange(len(row))
                top = top[np.lexsort((top, -row[top]))]       # 相似度降序，同分按库内顺序，和 kneighbors 一致
                out.append(self._tally(p, 1.0 - row[top], top))
        return out

    def _tally(self, p: Part, dist, idx) -> Candidate | None:
        votes, weights = Counter(), Counter()
        n = 0
        for d, i in zip(dist, idx):
            if self.ids[i] == p.id:       # 排除自身
                continue
            w = max(0.0, 1.0 - d)
            votes[self.labels[i]] += 1
            weights[self.labels[i]] += w
            n += 1
            if n >= self.k:
                break
        if not weights:
            return None
        best, wsum = weights.most_common(1)[0]
        total = sum(weights.values())
        conf = (wsum / total) * min(1.0, max(weights.values()) / max(1e-6, n * 0.6))   # 一致性 × 相似度
        return Candidate(best, round(float(conf), 3), "knn", {"votes": votes[best], "k": n, "sim": round(float(wsum / max(votes[best], 1)), 3)})


# --------------------------------------------------------------------------- LLM 仲裁
class LlmArbiter:
    """可插拔。默认用 Anthropic SDK（需 ANTHROPIC_API_KEY）；也可传入自定义 call(prompt)->str。

    每次只给 3~6 个候选（规则/近邻的结果 + 同根下的兄弟叶子），让模型做选择题而不是开放题。
    返回 JSON：{"category_id": ..., "confidence": 0~1, "reason": ...}
    """

    def __init__(self, tax: Taxonomy, call=None, model: str | None = None, provider: str | None = None):
        self.tax = tax
        self.provider = (provider or os.environ.get("EZPLM_LLM_PROVIDER", "anthropic")).lower()
        default_model = {"anthropic": "claude-haiku-4-5", "gemini": "gemini-2.5-flash", "openai": "gpt-4o-mini"}.get(self.provider, "claude-haiku-4-5")
        self.model = model or os.environ.get("EZPLM_LLM_MODEL") or default_model
        prefix = {"anthropic": "claude", "gemini": "gemini", "openai": None}.get(self.provider)
        if prefix and not self.model.lower().startswith(prefix):
            self.model = default_model          # 模型名与提供商不匹配（如 gemini + claude-haiku）→ 用该提供商默认模型
        calls = {"anthropic": self._anthropic_call, "gemini": self._gemini_call, "openai": self._openai_call}
        self.call = call or calls.get(self.provider, self._anthropic_call)
        keys = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}
        self.available = call is not None or bool(os.environ.get(keys.get(self.provider, "ANTHROPIC_API_KEY")))
        self.n_calls = 0
        self.n_errors = 0
        self.last_error = ""

    def _anthropic_call(self, prompt: str) -> str:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(model=self.model, max_tokens=300, messages=[{"role": "user", "content": prompt}])
        return msg.content[0].text

    def _gemini_call(self, prompt: str) -> str:
        """Google Gemini（REST，无需额外 SDK）。模型如 gemini-2.5-flash / gemini-2.5-flash-lite。"""
        import requests
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        r = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]}, timeout=60,
                          json={"contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {"temperature": 0, "maxOutputTokens": 300, "responseMimeType": "application/json"}})
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _openai_call(self, prompt: str) -> str:
        """OpenAI 兼容接口（OpenAI / DeepSeek / 通义千问 / 智谱等），可设 OPENAI_BASE_URL。"""
        import requests
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        r = requests.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, timeout=60,
                          json={"model": self.model, "temperature": 0, "max_tokens": 300, "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _ask(self, prompt: str) -> dict | None:
        self.n_calls += 1
        try:
            txt = self.call(prompt)
            return json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        except Exception as e:  # noqa
            self.n_errors += 1
            self.last_error = str(e)[:200]
            return None

    def _part_block(self, p: Part) -> str:
        return (f"制造商：{p.manufacturer}\n型号：{p.mpn}\n描述：{p.description}\n英文描述：{p.description_en}\n"
                f"现有符号：{p.symbol or '无'}；封装：{p.footprint or '无'}")

    def arbitrate_open(self, p: Part) -> Candidate | None:
        """无候选时的两步开放式分类：先选一级分类，再在其叶子里选。"""
        if not self.available:
            return None
        roots = [n for n in self.tax.nodes.values() if not n.parent]
        opts = "\n".join(f"{i+1}. {n.name}" for i, n in enumerate(roots))
        js = self._ask("你是电子元器件分类专家。根据器件信息选出最合适的一级分类；若无法判断返回 0。\n" + self._part_block(p) +
                       f"\n\n一级分类：\n{opts}\n\n只输出 JSON：{{\"index\": <序号>, \"confidence\": <0到1>}}")
        if not js or not js.get("index"):
            return Candidate("", 0.0, "llm:none", {"step": "root"})
        try:
            root = roots[int(js["index"]) - 1]
        except Exception:
            return Candidate("", 0.0, "llm:none", {"step": "root"})
        leaves = [(cid, path) for cid, path in self.tax.leaf_labels() if cid in self.tax.nodes and self.tax.root_of(cid) == root.id]
        if not leaves:
            return Candidate("", 0.0, "llm:none", {"step": "leaf"})
        opts = "\n".join(f"{i+1}. {path.split(' > ', 1)[-1]}" for i, (cid, path) in enumerate(leaves))
        js2 = self._ask(f"器件已判定属于一级分类「{root.name}」。请在下列子分类中选出最合适的一个；若都不合适返回 0。\n" + self._part_block(p) +
                        f"\n\n子分类：\n{opts}\n\n只输出 JSON：{{\"index\": <序号>, \"confidence\": <0到1>, \"reason\": \"<一句话>\"}}")
        if not js2 or not js2.get("index"):
            return Candidate("", 0.0, "llm:none", {"step": "leaf", "root": root.name})
        try:
            cid = leaves[int(js2["index"]) - 1][0]
        except Exception:
            return Candidate("", 0.0, "llm:none", {"step": "leaf"})
        conf = min(float(js.get("confidence", 0.7)), float(js2.get("confidence", 0.7)))
        return Candidate(cid, round(conf, 3), "llm", {"reason": js2.get("reason", ""), "model": self.model, "mode": "open-2step"})

    def arbitrate(self, p: Part, candidates: list[str]) -> Candidate | None:
        if not self.available:
            return None
        if not candidates:
            return self.arbitrate_open(p)
        cands = list(dict.fromkeys(candidates))
        # 补充同父的兄弟叶子，最多 8 个
        for cid in list(cands):
            parent = self.tax.nodes[cid].parent if cid in self.tax.nodes else ""
            if parent:
                for sib in self.tax.nodes[parent].children:
                    if sib not in cands and self.tax.is_leaf(sib):
                        cands.append(sib)
            if len(cands) >= 8:
                break
        cands = cands[:8]
        options = "\n".join(f"{i+1}. [{cid}] {self.tax.path(cid)}" for i, cid in enumerate(cands))
        prompt = (
            "你是电子元器件分类专家。根据器件信息，从候选分类中选出最合适的一个；若都不合适，返回 none。\n"
            f"制造商：{p.manufacturer}\n型号：{p.mpn}\n描述：{p.description}\n英文描述：{p.description_en}\n"
            f"现有符号：{p.symbol or '无'}；封装：{p.footprint or '无'}\n\n候选分类：\n{options}\n\n"
            '只输出 JSON：{"category_id": "<id 或 none>", "confidence": <0到1>, "reason": "<一句话>"}'
        )
        self.n_calls += 1
        try:
            txt = self.call(prompt)
            js = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        except Exception as e:  # noqa
            self.n_errors += 1
            self.last_error = str(e)[:200]
            return Candidate("", 0.0, "llm:error", {"error": str(e)[:200]})
        cid = js.get("category_id", "")
        if cid in self.tax.nodes:
            return Candidate(cid, float(js.get("confidence", 0.7)), "llm", {"reason": js.get("reason", ""), "model": self.model})
        return Candidate("", 0.0, "llm:none", {"reason": js.get("reason", "")})


# --------------------------------------------------------------------------- 汇总
class Classifier:
    def __init__(self, tax: Taxonomy, rules_path=HERE / "config" / "rules.yaml", policy: Policy | None = None, llm_call=None):
        self.tax = tax
        self.policy = policy or Policy()
        self.rules = RuleEngine(tax, rules_path)
        self.knn = KnnVoter(self.policy.knn_k)
        self.llm = LlmArbiter(tax, call=llm_call) if self.policy.use_llm else None

    def fit(self, trusted: list[Part]):
        self.knn.fit(trusted)
        return self

    def _merge(self, cands: list[Candidate]) -> Candidate | None:
        if not cands:
            return None
        score: dict[str, float] = {}
        for c in cands:
            if c.category_id:
                score[c.category_id] = score.get(c.category_id, 0) + c.conf
        if not score:
            return None
        best = max(score, key=score.get)
        agreeing = [c for c in cands if c.category_id == best]
        conf = max(c.conf for c in agreeing)
        if len(agreeing) >= 2:                      # 多层一致 → 加成
            conf = min(0.99, conf + 0.08 * (len(agreeing) - 1))
        if len(score) > 1:                          # 有分歧 → 扣减
            conf = conf * (score[best] / sum(score.values()))
        return Candidate(best, round(conf, 3), "+".join(sorted({c.method.split(":")[0] for c in agreeing})),
                         {"layers": [{"m": c.method, "cat": self.tax.path(c.category_id), "conf": c.conf} for c in cands]})

    def classify_many(self, parts: list[Part]) -> list[Proposal]:
        """批量分类：近邻一次算完整批，其余逻辑与 classify 相同。"""
        knn = self.knn.vote_many(parts)
        return [self.classify(p, _knn=kv, _knn_done=True) for p, kv in zip(parts, knn)]

    def classify(self, p: Part, _knn=None, _knn_done: bool = False) -> Proposal:
        cands = self.rules.apply(p)
        kv = _knn if _knn_done else self.knn.vote(p)
        if kv:
            cands.append(kv)
        merged = self._merge(cands)
        if merged is not None and merged.method == "knn" and merged.conf > self.policy.knn_only_max:
            merged.evidence["knn_capped_from"] = merged.conf
            merged.conf = self.policy.knn_only_max
        if self.llm and (merged is None or merged.conf < self.policy.auto_fill_min or (p.category_id and merged.category_id != p.category_id)):
            seed = [c.category_id for c in cands if c.category_id] + ([p.category_id] if p.category_id else [])
            lv = self.llm.arbitrate(p, seed)
            if lv and lv.category_id:
                cands.append(lv)
                merged = self._merge(cands)
        if merged is None:
            return Proposal(p.id, p.mpn, "category_id", p.category_id, "", "", 0.0, "review", "none",
                            {"note": "无任何候选；需 LLM 或人工"}, ["待分类"])
        # 符号库交叉验证
        consistent = self.rules.symbol_consistent(p, merged.category_id)
        if consistent is False:
            merged.conf = round(merged.conf * 0.6, 3)
            merged.evidence["symbol_conflict"] = True
        return self._decide(p, merged)

    def _decide(self, p: Part, c: Candidate) -> Proposal:
        pol = self.policy
        label = self.tax.path(c.category_id)
        if not p.category_id:
            if c.conf >= pol.auto_fill_min:
                action, tags = "auto_fill", ["自动分类"]
            elif c.conf >= pol.propose_min:
                action, tags = "review", ["待分类"]
            else:
                action, tags = "skip", ["待分类"]          # 猜测太弱，只打标签不出建议
        elif c.category_id == p.category_id:
            action, tags = "ok", []
        elif c.conf >= pol.flag_min:
            action, tags = "flag_mismatch", ["分类疑似错误"]
            c.evidence["current_path"] = self.tax.path(p.category_id)
        else:
            action, tags = "ok", []                          # 已有分类且无把握推翻 → 不打扰
        return Proposal(p.id, p.mpn, "category_id", p.category_id, c.category_id, label, c.conf, action, c.method, c.evidence, tags)
