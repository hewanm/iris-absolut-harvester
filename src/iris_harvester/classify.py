"""Domain + geographic relevance filter driven by ``taxonomy.yaml``."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Classification:
    topics: list[str]
    matched_terms: dict[str, list[str]]
    score: float
    geo_scope: str
    keep: bool
    reject_reason: str | None = None
    flags: list[str] = field(default_factory=list)


@dataclass
class _Topic:
    name: str
    weight: float
    patterns: list[re.Pattern]


class Taxonomy:
    def __init__(self, config: dict):
        self.title_weight = float(config.get("title_weight", 1.5))
        self.max_terms = int(config.get("max_terms_per_topic", 3))
        rule = config.get("keep_rule", {})
        self.min_score = float(rule.get("min_score", 6.0))
        self.required_any = set(rule.get("required_any", []))
        self.allowed_scopes = set(rule.get("allowed_geo_scopes", ["DE", "DACH", "UNKNOWN"]))
        self.topics = [
            _Topic(name, float(spec.get("weight", 1.0)), [re.compile(p, re.I) for p in spec["patterns"]])
            for name, spec in config["topics"].items()
        ]
        self.geo_patterns = {
            scope: [re.compile(p, re.I) for p in pats] for scope, pats in config.get("geo_scope", {}).items()
        }
        unknown = self.required_any - {t.name for t in self.topics}
        if unknown:
            raise ValueError(f"keep_rule.required_any references unknown topics: {sorted(unknown)}")

    @classmethod
    def load(cls, path: Path) -> "Taxonomy":
        with open(path, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    @staticmethod
    def _terms(patterns: list[re.Pattern], text: str) -> list[str]:
        found: list[str] = []
        for pat in patterns:
            for m in pat.finditer(text):
                term = m.group(0).lower()
                if term not in found:
                    found.append(term)
        return found

    def geo_scope(self, text: str, has_german_place: bool) -> str:
        hits = {scope for scope, pats in self.geo_patterns.items() if any(p.search(text) for p in pats)}
        if has_german_place:
            hits.add("DE")
        foreign = hits & {"AT", "CH"}
        if "DE" in hits:
            return "DACH" if foreign else "DE"
        if len(foreign) == 1:
            return foreign.pop()
        if foreign:
            return "DACH_EXCL_DE"
        return "UNKNOWN"

    def classify(self, title: str, body: str, has_german_place: bool = False) -> Classification:
        full = f"{title} {body}"
        matched: dict[str, list[str]] = {}
        score = 0.0
        for topic in self.topics:
            terms = self._terms(topic.patterns, full)
            if not terms:
                continue
            matched[topic.name] = terms
            in_title = bool(self._terms(topic.patterns, title))
            score += topic.weight * min(len(terms), self.max_terms) * (self.title_weight if in_title else 1.0)

        scope = self.geo_scope(full, has_german_place)
        topics = list(matched)
        flags = ["geo_scope_unknown"] if scope == "UNKNOWN" else []

        reason = None
        if not (self.required_any & set(topics)):
            reason = "no_objective_topic"
        elif scope not in self.allowed_scopes:
            reason = f"out_of_scope_geo:{scope}"
        elif score < self.min_score:
            reason = "below_min_score"

        return Classification(
            topics=topics,
            matched_terms=matched,
            score=round(score, 2),
            geo_scope=scope,
            keep=reason is None,
            reject_reason=reason,
            flags=flags,
        )
