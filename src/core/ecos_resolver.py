from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from src.core.http import get_json
from src.core.io import read_json, write_json

API = "https://ecos.bok.or.kr/api"
CACHE_VERSION = 1


def _norm(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").lower())


def _rows(payload: Any, root: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    node = payload.get(root)
    if not isinstance(node, dict):
        # ECOS errors use RESULT at the top level.
        result = payload.get("RESULT")
        if isinstance(result, dict):
            raise RuntimeError(f"ECOS 오류 {result.get('CODE')}: {result.get('MESSAGE')}")
        return []
    result = node.get("RESULT")
    if isinstance(result, dict) and str(result.get("CODE", "")).upper() not in {"INFO-000", ""}:
        raise RuntimeError(f"ECOS 오류 {result.get('CODE')}: {result.get('MESSAGE')}")
    rows = node.get("row", [])
    return rows if isinstance(rows, list) else []


def _url(service: str, key: str, *parts: str) -> str:
    encoded = [quote(str(p).strip("/"), safe="") for p in parts]
    return "/".join([API, service, key, "json", "kr", *encoded])


def _score(text: str, keywords: list[str], excludes: list[str] | None = None) -> int:
    n = _norm(text)
    if excludes and any(_norm(x) in n for x in excludes):
        return -10_000
    score = 0
    for idx, kw in enumerate(keywords):
        k = _norm(kw)
        if k and k in n:
            score += 100 - idx * 5 + min(len(k), 30)
    return score


@dataclass
class EcosResolution:
    stat_code: str
    stat_name: str
    cycle: str
    item_code1: str
    item_name1: str
    item_code2: str = "?"
    item_code3: str = "?"

    def to_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


class EcosResolver:
    def __init__(self, key: str, timeout: int, retries: int, cache_path: Path = Path("cache/ecos_resolved.json")):
        self.key = key
        self.timeout = timeout
        self.retries = retries
        self.cache_path = cache_path
        cache = read_json(cache_path) if cache_path.exists() else {}
        self.cache = cache if cache.get("cache_version") == CACHE_VERSION else {"cache_version": CACHE_VERSION, "series": {}}
        self._tables: list[dict[str, Any]] | None = None

    def save(self) -> None:
        write_json(self.cache_path, self.cache)

    def _table_rows(self) -> list[dict[str, Any]]:
        if self._tables is None:
            payload = get_json(_url("StatisticTableList", self.key, "1", "10000", ""), timeout=self.timeout, retries=self.retries)
            self._tables = _rows(payload, "StatisticTableList")
        return self._tables

    def _item_rows(self, stat_code: str) -> list[dict[str, Any]]:
        payload = get_json(_url("StatisticItemList", self.key, "1", "10000", stat_code), timeout=self.timeout, retries=self.retries)
        return _rows(payload, "StatisticItemList")

    def resolve(self, name: str, cfg: dict[str, Any], force: bool = False) -> EcosResolution:
        cached = self.cache.get("series", {}).get(name)
        if cached and not force:
            return EcosResolution(**cached)

        table_keywords = list(cfg.get("table_keywords", []))
        item_keywords = list(cfg.get("item_keywords", []))
        excludes = list(cfg.get("exclude_keywords", []))
        wanted_cycle = str(cfg.get("frequency", "D"))

        candidates = []
        for row in self._table_rows():
            code = str(row.get("STAT_CODE", "")).strip()
            name_text = str(row.get("STAT_NAME", "")).strip()
            cycle = str(row.get("CYCLE", "")).strip()
            if not code:
                continue
            s = _score(f"{name_text} {code}", table_keywords)
            if cycle == wanted_cycle:
                s += 30
            if s > 0:
                candidates.append((s, row))
        candidates.sort(key=lambda x: x[0], reverse=True)

        tried: list[str] = []
        for _, table in candidates[:20]:
            code = str(table.get("STAT_CODE", "")).strip()
            stat_name = str(table.get("STAT_NAME", "")).strip()
            cycle = str(table.get("CYCLE", wanted_cycle)).strip() or wanted_cycle
            try:
                items = self._item_rows(code)
            except Exception as exc:
                tried.append(f"{code}: {exc}")
                continue
            ranked = []
            for item in items:
                item_name = str(item.get("ITEM_NAME", "")).strip()
                item_code = str(item.get("ITEM_CODE", "")).strip()
                if not item_code:
                    continue
                s = _score(item_name, item_keywords, excludes)
                if s > 0:
                    # Prefer the first item dimension, because it is the measured series.
                    if str(item.get("ITEM_CODE1", "")).strip() == item_code:
                        s += 10
                    ranked.append((s, item))
            ranked.sort(key=lambda x: x[0], reverse=True)
            if not ranked:
                tried.append(f"{code}: matching item 없음")
                continue
            item = ranked[0][1]
            item_code = str(item.get("ITEM_CODE", "")).strip()
            resolved = EcosResolution(
                stat_code=code,
                stat_name=stat_name,
                cycle=cycle,
                item_code1=item_code,
                item_name1=str(item.get("ITEM_NAME", "")).strip(),
            )
            self.cache.setdefault("series", {})[name] = resolved.to_dict()
            return resolved
        raise RuntimeError("적합한 ECOS 통계표/항목을 찾지 못했습니다. " + "; ".join(tried[:8]))
