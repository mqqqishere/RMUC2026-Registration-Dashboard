#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻流（qingflow）分享看板抓取器

逆向自官方前端 bundle (cdn-prod.qingflow.com/pc/main.*.js) —— 该页面是一个
纯前端 SPA，通过以下接口读取只读分享视图数据：

    GET  /api/view/{viewKey}/viewConfig/baseInfo
         → 返回 viewgraphQuestions（问题/列定义）
    GET  /api/view/{viewKey}/lane/baseInfo?pageNum=1&pageSize=50
         → 返回 laneBaseInfoList（看板各"泳道"信息）
    POST /api/view/{viewKey}/lane/{laneId}/boardViewFilter
         → body {"filter":{"queryKey":null,"pageSize":100,"pageNum":1,"type":8}}
         → 返回 {"list":[...],"total":N,"pageNum":1,"pageSize":100}

本模块提供：
    fetch_view(view_key)  —— 自动完成 schema + 分页抓取，返回结构化 rows

使用：
    python3 qingflow.py e3bol20d1c02          # 直接打印抓取结果
    python3 qingflow.py --url <shareView URL>  # 从完整 URL 抽取 viewKey

也可被 allocator.py 导入：
    from qingflow import fetch_view, parse_share_url
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

API_ROOT = "https://qingflow.com/api"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP 工具
# ──────────────────────────────────────────────────────────────────────────────

class QingflowError(RuntimeError):
    pass


def _request(method: str, path: str, body: Optional[dict] = None,
             timeout: int = 20) -> dict:
    url = f"{API_ROOT}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://qingflow.com")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise QingflowError(f"HTTP {e.code} on {path}: {e.read()[:200]!r}") from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise QingflowError(f"Bad JSON from {path}: {raw[:200]!r}") from e
    code = payload.get("code", payload.get("errCode"))
    if code not in (0, None):
        msg = payload.get("message") or payload.get("errMsg")
        raise QingflowError(f"API error {code} on {path}: {msg}")
    return payload.get("data") or payload.get("result") or {}


def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params:
        path = f"{path}?{urllib.parse.urlencode(params)}"
    return _request("GET", path)


def api_post(path: str, body: dict) -> dict:
    return _request("POST", path, body=body)


# ──────────────────────────────────────────────────────────────────────────────
# URL 解析
# ──────────────────────────────────────────────────────────────────────────────

SHARE_URL_RE = re.compile(
    r"qingflow\.com/appView/([a-zA-Z0-9]+)/shareView/([a-zA-Z0-9]+)"
)


def parse_share_url(url: str) -> Tuple[str, str]:
    """把完整分享 URL 拆成 (appKey, viewKey)。"""
    m = SHARE_URL_RE.search(url)
    if not m:
        raise ValueError(f"不是合法的 qingflow shareView URL: {url}")
    return m.group(1), m.group(2)


# ──────────────────────────────────────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Question:
    que_id: int
    que_type: int
    title: str


@dataclass
class Lane:
    id: int              # 内部记录 id（用于 laneValue 关联）
    lane_id: int         # 选项 id（用于 POST body）
    name: str
    ordinal: int
    current_apply: int


@dataclass
class ApplyRecord:
    apply_id: Optional[int]
    lane_id: int
    raw: dict                           # 原始 apply 对象
    answers: Dict[int, Any] = field(default_factory=dict)  # queId → 文本

    def get(self, que_id: int, default: str = "") -> str:
        return self.answers.get(que_id, default)


# ──────────────────────────────────────────────────────────────────────────────
# 答案解析：轻流的 answers 字段结构多变，统一抽取为字符串
# ──────────────────────────────────────────────────────────────────────────────

def _stringify_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, dict):
        for k in ("value", "optionValue", "name", "memberName",
                  "displayValue", "text", "label"):
            if k in v and v[k] not in (None, ""):
                return _stringify_value(v[k])
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return " / ".join(_stringify_value(x) for x in v if x not in (None, ""))
    return str(v)


def _extract_answers(apply_obj: dict) -> Dict[int, str]:
    """
    从 apply 对象里提取 queId → 显示文本。兼容以下几种常见结构：
      1) {"answers":[{"queId":X,"values":[...]}, ...]}
      2) {"queAnswers":[{"queId":X, ...}]}
      3) {"answers": {"X":[...], "Y":[...]}}  (字典形式)
    """
    out: Dict[int, str] = {}
    for key in ("answers", "queAnswers", "answerList"):
        ans = apply_obj.get(key)
        if ans is None:
            continue
        if isinstance(ans, list):
            for a in ans:
                qid = a.get("queId") or a.get("queID") or a.get("id")
                if qid is None:
                    continue
                # 值可能在 values / answer / value / optionValues
                val = (a.get("values") or a.get("answer") or
                       a.get("value") or a.get("optionValues"))
                out[int(qid)] = _stringify_value(val if val is not None else a)
        elif isinstance(ans, dict):
            for qid, val in ans.items():
                try:
                    out[int(qid)] = _stringify_value(val)
                except (TypeError, ValueError):
                    pass
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 抓取主流程
# ──────────────────────────────────────────────────────────────────────────────

def fetch_view_config(view_key: str) -> List[Question]:
    data = api_get(f"/view/{view_key}/viewConfig/baseInfo")
    questions = []
    for q in data.get("viewgraphQuestions") or []:
        questions.append(Question(
            que_id=int(q.get("queId", 0)),
            que_type=int(q.get("queType", 0)),
            title=str(q.get("queTitle", "")),
        ))
    return questions


def fetch_lanes(view_key: str) -> List[Lane]:
    data = api_get(f"/view/{view_key}/lane/baseInfo",
                   {"pageNum": 1, "pageSize": 50})
    lanes: List[Lane] = []
    for ln in data.get("laneBaseInfoList") or []:
        lanes.append(Lane(
            id=int(ln["id"]),
            lane_id=int(ln["laneId"]),
            name=str(ln.get("laneName") or ""),
            ordinal=int(ln.get("laneOrdinal") or 0),
            current_apply=int(ln.get("optionCurrentApply") or 0),
        ))
    lanes.sort(key=lambda x: x.ordinal)
    return lanes


def fetch_lane_records(view_key: str, lane: Lane,
                       page_size: int = 100) -> List[ApplyRecord]:
    page = 1
    out: List[ApplyRecord] = []
    while True:
        data = api_post(
            f"/view/{view_key}/lane/{lane.lane_id}/boardViewFilter",
            {"filter": {"queryKey": None, "pageSize": page_size,
                        "pageNum": page, "type": 8}},
        )
        items = data.get("list") or []
        for item in items:
            rec = ApplyRecord(
                apply_id=item.get("applyId") or item.get("id"),
                lane_id=lane.lane_id,
                raw=item,
                answers=_extract_answers(item),
            )
            out.append(rec)
        total = data.get("total") or 0
        if page * page_size >= total or not items:
            break
        page += 1
    return out


def fetch_view(view_key: str, verbose: bool = False) -> dict:
    """
    一站式抓取分享看板。返回：
    {
        "view_key": ...,
        "questions": [Question, ...],
        "lanes":     [Lane, ...],
        "records":   [ApplyRecord, ...],   # 所有泳道合并
    }
    """
    questions = fetch_view_config(view_key)
    lanes = fetch_lanes(view_key)
    if verbose:
        print(f"[qingflow] view={view_key}")
        print(f"[qingflow] 列定义 ({len(questions)}):")
        for q in questions:
            print(f"          qid={q.que_id:>12} type={q.que_type:<3} {q.title}")
        print(f"[qingflow] 泳道 ({len(lanes)}):")
        for ln in lanes:
            print(f"          laneId={ln.lane_id} {ln.name} (当前 {ln.current_apply})")

    records: List[ApplyRecord] = []
    for ln in lanes:
        recs = fetch_lane_records(view_key, ln)
        if verbose:
            print(f"[qingflow] {ln.name}: 抓到 {len(recs)} 条")
        records.extend(recs)

    return {
        "view_key": view_key,
        "questions": questions,
        "lanes": lanes,
        "records": records,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 字段语义识别（按 title 猜 queId）
# ──────────────────────────────────────────────────────────────────────────────

TITLE_HINTS = {
    "school":    ("申请学校", "学校", "院校", "参赛学校"),
    "volunteer": ("赛区选择", "赛区", "志愿赛区", "意向赛区"),
    "team":      ("队伍", "战队", "队名"),
    "city":      ("城市", "所在城市"),
    "contact":   ("填写人", "联系人"),
    "phone":     ("电话", "手机"),
    "position":  ("职位", "战队职位"),
}


def guess_field_map(questions: List[Question]) -> Dict[str, int]:
    """根据列标题猜出 {role: queId}。"""
    result: Dict[str, int] = {}
    for role, hints in TITLE_HINTS.items():
        for q in questions:
            if any(h in q.title for h in hints):
                result[role] = q.que_id
                break
    return result


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="qingflow 分享看板抓取器")
    ap.add_argument("target", help="viewKey 或完整 shareView URL")
    ap.add_argument("--raw", action="store_true", help="打印完整原始 JSON")
    ap.add_argument("--json", action="store_true", help="输出为 JSON")
    args = ap.parse_args(argv)

    if args.target.startswith("http"):
        _, view_key = parse_share_url(args.target)
    else:
        view_key = args.target

    out = fetch_view(view_key, verbose=not args.json)
    questions = out["questions"]
    records = out["records"]
    field_map = guess_field_map(questions)

    if args.json:
        json.dump({
            "view_key": view_key,
            "questions": [q.__dict__ for q in questions],
            "lanes":     [ln.__dict__ for ln in out["lanes"]],
            "field_map": field_map,
            "records": [{
                "apply_id": r.apply_id,
                "lane_id":  r.lane_id,
                "answers":  r.answers,
                **({"raw": r.raw} if args.raw else {}),
            } for r in records],
        }, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    print("\n[猜测字段映射]")
    for k, v in field_map.items():
        q = next((q for q in questions if q.que_id == v), None)
        print(f"  {k:<10} → qid={v} ({q.title if q else '?'})")

    print(f"\n[抓取记录] 共 {len(records)} 条")
    for r in records[:40]:
        school = r.get(field_map.get("school", 0))
        vol = r.get(field_map.get("volunteer", 0))
        print(f"  #{r.apply_id}  lane={r.lane_id}  学校={school!r:<24}  志愿={vol!r}")
    if args.raw and records:
        print("\n[原始样本记录]")
        print(json.dumps(records[0].raw, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
