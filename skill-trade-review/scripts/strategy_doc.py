"""策略文档契约 — 用户提供的「策略文件」的统一抽象。

支持三种格式:
  - YAML: 结构化字段，直接 load
  - Markdown: 原文整段交给 LLM 抽取，规则层只截取标题与首段
  - Python: 暴露 register_strategy() 函数，返回 StrategyDoc 实例

StrategyDoc 字段（全部可缺，缺则按"未声明"处理）:
  name              策略名称
  description       一句话描述
  applicable_regimes 适用市场环境列表，如 ["uptrend", "low_vol"]
  entry_rules       入场规则文本/列表
  exit_rules        出场规则文本/列表
  risk_rules        风控规则文本/列表
  key_indicators    依赖的关键指标列表，如 ["MA20", "MACD", "ATR"]
  reason_tags       该策略对应的 reason_tag 标签集合（用于和 trades 关联）
  raw_text          原文（最长 8000 字符，给 LLM 做兜底参考）
  source_format     yaml/markdown/python
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class StrategyDoc:
    name: str = "unnamed_strategy"
    description: str = ""
    applicable_regimes: list[str] = field(default_factory=list)
    entry_rules: list[str] | str = field(default_factory=list)
    exit_rules: list[str] | str = field(default_factory=list)
    risk_rules: list[str] | str = field(default_factory=list)
    key_indicators: list[str] = field(default_factory=list)
    reason_tags: list[str] = field(default_factory=list)
    raw_text: str = ""
    source_format: str = "unknown"
    source_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def is_empty(self) -> bool:
        """没有任何有意义内容时为 True。"""
        return not (self.name and self.name != "unnamed_strategy") \
            and not self.description \
            and not self.entry_rules \
            and not self.raw_text


# ---------- YAML / TOML ----------

def _parse_yaml(path: Path) -> StrategyDoc:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        data = yaml.safe_load(text) or {}
    except ImportError:
        # 没装 yaml 时退回为原文兜底
        return StrategyDoc(
            name=path.stem, description="(未安装 pyyaml，仅保留原文)",
            raw_text=text[:8000], source_format="yaml_raw",
            source_path=str(path),
        )
    if not isinstance(data, dict):
        return StrategyDoc(
            name=path.stem, raw_text=text[:8000],
            source_format="yaml_invalid", source_path=str(path),
        )
    return StrategyDoc(
        name=str(data.get("name") or path.stem),
        description=str(data.get("description") or ""),
        applicable_regimes=_to_list(data.get("applicable_regimes")),
        entry_rules=_to_list_or_str(data.get("entry_rules")),
        exit_rules=_to_list_or_str(data.get("exit_rules")),
        risk_rules=_to_list_or_str(data.get("risk_rules")),
        key_indicators=_to_list(data.get("key_indicators")),
        reason_tags=_to_list(data.get("reason_tags")),
        raw_text=text[:8000],
        source_format="yaml",
        source_path=str(path),
    )


# ---------- Markdown ----------

def _parse_markdown(path: Path) -> StrategyDoc:
    """Markdown: 仅做最轻量的标题/段落识别；语义解析交给 LLM。

    识别规则:
      - 第一个 H1 作为 name
      - 第一个非空段落作为 description
      - "## 入场规则 / 入场 / Entry"  → entry_rules
      - "## 出场规则 / 出场 / Exit"   → exit_rules
      - "## 风控 / 风险 / Risk"        → risk_rules
      - "## 适用环境 / 适用 / Regime"  → applicable_regimes (空格/逗号分割)
      - "## 关键指标 / 指标"           → key_indicators
      - "## reason_tag / 标签"         → reason_tags
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    name = path.stem
    description = ""
    sections: dict[str, list[str]] = {}
    cur: str | None = None

    h2_map = {
        "入场规则": "entry_rules", "入场": "entry_rules", "entry": "entry_rules", "entry rules": "entry_rules",
        "出场规则": "exit_rules", "出场": "exit_rules", "exit": "exit_rules", "exit rules": "exit_rules",
        "风控": "risk_rules", "风险": "risk_rules", "风险控制": "risk_rules", "risk": "risk_rules",
        "适用环境": "applicable_regimes", "适用": "applicable_regimes", "regime": "applicable_regimes",
        "关键指标": "key_indicators", "指标": "key_indicators", "indicators": "key_indicators",
        "reason_tag": "reason_tags", "标签": "reason_tags", "tag": "reason_tags",
    }

    found_first_para = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cur = None
            continue
        if stripped.startswith("# ") and name == path.stem:
            name = stripped[2:].strip() or path.stem
            continue
        if stripped.startswith("## "):
            head = stripped[3:].strip().lower()
            cur = h2_map.get(head)
            continue
        if cur:
            sections.setdefault(cur, []).append(stripped)
        elif not found_first_para and not stripped.startswith("#"):
            description = stripped
            found_first_para = True

    def _normalize(items: list[str] | None) -> list[str]:
        if not items:
            return []
        out = []
        for ln in items:
            ln = ln.lstrip("-*0123456789.) ").strip()
            if ln:
                out.append(ln)
        return out

    def _normalize_csv(items: list[str] | None) -> list[str]:
        """逗号分隔型字段：再按 , / 、 拆开。"""
        flat: list[str] = []
        for s in _normalize(items):
            for piece in s.replace("，", ",").replace("、", ",").split(","):
                p = piece.strip()
                if p:
                    flat.append(p)
        return flat

    return StrategyDoc(
        name=name,
        description=description,
        applicable_regimes=_normalize_csv(sections.get("applicable_regimes")),
        entry_rules=_normalize(sections.get("entry_rules")),
        exit_rules=_normalize(sections.get("exit_rules")),
        risk_rules=_normalize(sections.get("risk_rules")),
        key_indicators=_normalize_csv(sections.get("key_indicators")),
        reason_tags=_normalize_csv(sections.get("reason_tags")),
        raw_text=text[:8000],
        source_format="markdown",
        source_path=str(path),
    )


# ---------- Python ----------

def _parse_python(path: Path) -> StrategyDoc:
    """加载 .py 文件，调用其 register_strategy() 函数。

    安全策略：
      - 只信任用户显式给出的路径，不做远程加载
      - register_strategy() 必须返回 dict / StrategyDoc 实例
      - 无 register_strategy 时退回为「以源码作为 raw_text」的兜底
    """
    spec = importlib.util.spec_from_file_location(f"_strategy_{path.stem}", str(path))
    if spec is None or spec.loader is None:
        return StrategyDoc(name=path.stem, source_format="python_load_failed",
                           source_path=str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        return StrategyDoc(name=path.stem,
                           description=f"(加载失败: {exc})",
                           source_format="python_exec_failed",
                           source_path=str(path),
                           raw_text=path.read_text(encoding="utf-8")[:8000])

    fn = getattr(module, "register_strategy", None)
    src = path.read_text(encoding="utf-8")[:8000]
    if not callable(fn):
        return StrategyDoc(
            name=getattr(module, "STRATEGY_NAME", path.stem),
            description=getattr(module, "STRATEGY_DESCRIPTION", "") or
                        "(未提供 register_strategy()，仅保留源码摘要)",
            raw_text=src,
            source_format="python_raw",
            source_path=str(path),
        )

    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001
        return StrategyDoc(name=path.stem, description=f"(register_strategy 抛异常: {exc})",
                           source_format="python_call_failed",
                           source_path=str(path), raw_text=src)

    if isinstance(result, StrategyDoc):
        result.source_format = "python"
        result.source_path = str(path)
        if not result.raw_text:
            result.raw_text = src
        return result
    if isinstance(result, dict):
        return StrategyDoc(
            name=str(result.get("name") or path.stem),
            description=str(result.get("description") or ""),
            applicable_regimes=_to_list(result.get("applicable_regimes")),
            entry_rules=_to_list_or_str(result.get("entry_rules")),
            exit_rules=_to_list_or_str(result.get("exit_rules")),
            risk_rules=_to_list_or_str(result.get("risk_rules")),
            key_indicators=_to_list(result.get("key_indicators")),
            reason_tags=_to_list(result.get("reason_tags")),
            raw_text=src,
            source_format="python",
            source_path=str(path),
        )
    return StrategyDoc(name=path.stem, description="(register_strategy 返回值类型不识别)",
                       raw_text=src, source_format="python_unknown",
                       source_path=str(path))


# ---------- 工具 ----------

def _to_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.replace("，", ",").split(",") if s.strip()]
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def _to_list_or_str(v: Any) -> list[str] | str:
    if v is None:
        return []
    if isinstance(v, str):
        return v.strip()
    return _to_list(v)


# ---------- 主入口 ----------

def load_strategy(path: str | Path) -> StrategyDoc:
    """根据扩展名分发到对应解析器。无法解析时返回空 StrategyDoc。"""
    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"strategy file not found: {p}")
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return _parse_yaml(p)
    if suffix in (".md", ".markdown", ".txt"):
        return _parse_markdown(p)
    if suffix == ".py":
        return _parse_python(p)
    # 未知扩展名：尝试当 markdown 处理
    return _parse_markdown(p)
