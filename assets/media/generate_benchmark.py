"""Render exact, bilingual SVG benchmark figures from a committed data snapshot.

The published AACR result is checked against benchmark-data.json when present.
Its SHA and summary must match before rendering; the pinned, reviewable snapshot
also allows reproduction without the full evaluation artifacts.
"""

from __future__ import annotations

import hashlib
import json
from html import escape
from pathlib import Path


MEDIA = Path(__file__).resolve().parent
ROOT = MEDIA.parents[1]
DATA = json.loads((MEDIA / "benchmark-data.json").read_text(encoding="utf-8"))
CODE = DATA["codesage"]
OCR = DATA["ocr_reference"]


def verify() -> None:
    source = ROOT / CODE["source_path"]
    if source.exists():
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != CODE["source_sha256"]:
            raise ValueError("AACR source hash changed; update the reviewed benchmark snapshot")
        summary = json.loads(raw)["summary"]
        fields = {
            "semantic_match_rate": "precision",
            "semantic_recall_rate": "recall",
            "semantic_f1": "f1",
        }
        for field, key in fields.items():
            if abs(summary[field] * 100 - CODE[key]) > 0.051:
                raise ValueError(f"AACR metric mismatch: {field}")
        for field, key in {
            "expected_notes": "expected_notes",
            "generated_notes": "generated_notes",
            "matched_semantic_notes": "semantic_matches",
        }.items():
            if summary[field] != CODE[key]:
                raise ValueError(f"AACR count mismatch: {field}")


def txt(x: int, y: int, value: str, *, size: int = 24, color: str = "#F2F2E8", weight: int = 400,
        family: str = "ui", spacing: float = 0) -> str:
    font = ("Bahnschrift, 'Arial Narrow', Arial, sans-serif" if family == "display"
            else "Consolas, 'Microsoft YaHei', monospace" if family == "mono"
            else "'Microsoft YaHei', 'Noto Sans CJK SC', Arial, sans-serif")
    return (f'<text x="{x}" y="{y}" fill="{color}" font-family="{font}" '
            f'font-size="{size}" font-weight="{weight}" letter-spacing="{spacing}">{escape(value)}</text>')


def render(lang: str) -> str:
    zh = lang == "zh"
    labels = {
        "eyebrow": "AACR-BENCH / 语义匹配" if zh else "AACR-BENCH / SEMANTIC MATCHING",
        "title": "审查质量，公开口径。" if zh else "REVIEW QUALITY, IN CONTEXT.",
        "subtitle": "CodeSage Deep · GLM-5.2 · 2026.09.28" if zh else "CodeSage Deep · GLM-5.2 · 2026.09.28",
        "f1": "F1 综合分" if zh else "F1 SCORE",
        "precision": "精确率 / PRECISION" if zh else "PRECISION",
        "recall": "召回率 / RECALL" if zh else "RECALL",
        "scope": "本次报告：100 个评测实例* · 802 条预期评论" if zh else "This run: 100 evaluated instances* · 802 expected notes",
        "reference": "参考：OCR 官方公开结果 · GLM-5.2 · v1.3.1" if zh else "REFERENCE: OCR PUBLISHED RESULT · GLM-5.2 · v1.3.1",
        "ref_scope": "OCR 公布范围：200 个 PR · 1,505 条预期评论" if zh else "OCR published scope: 200 PRs · 1,505 expected notes",
        "caution": "两组样本与运行条件不同；此图仅展示各自结果，不构成同批次优劣比较。" if zh else "Different case sets and run conditions. These snapshots are not a controlled head-to-head comparison.",
        "coverage": "* CodeSage 原始文件的 summary 与 missing_instance_ids 存在矛盾；完成覆盖率仍待核实。" if zh else "* CodeSage source summary conflicts with its missing_instance_ids; completion coverage needs reconciliation.",
        "source": "来源：CodeSage 本地 AACR 结果快照 / OCR 官方基准图" if zh else "Sources: CodeSage local AACR result snapshot / OCR published benchmark figure",
    }
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1000" viewBox="0 0 1600 1000" role="img" aria-labelledby="title desc">',
         f'<title id="title">{escape(labels["title"])}</title>',
         f'<desc id="desc">CodeSage F1 {CODE["f1"]}%, precision {CODE["precision"]}%, recall {CODE["recall"]}%. OCR reference F1 {OCR["f1"]}%, precision {OCR["precision"]}%, recall {OCR["recall"]}%. Different samples; not a head-to-head ranking.</desc>',
         '<rect width="1600" height="1000" fill="#101510"/>',
         '<circle cx="1335" cy="315" r="365" fill="#17231A"/>',
         '<path d="M70 112H1530 M70 925H1530 M70 645H1530" stroke="#354431" stroke-width="1"/>',
         '<path d="M70 0V1000 M800 0V1000 M1530 0V1000" stroke="#354431" stroke-opacity=".23" stroke-dasharray="2 16"/>',
         '<path d="M79 61 62 80l17 19 M94 61l17 19-17 19 M92 67 82 93" stroke="#D3F86A" stroke-width="5" fill="none"/>',
         txt(137, 87, "CodeSage", size=32, weight=700, family="display"),
         txt(70, 166, labels["eyebrow"], size=23, color="#D3F86A", family="mono", spacing=2.5),
         txt(70, 248, labels["title"], size=69 if zh else 72, color="#F2F2E8", weight=700, family="display"),
         txt(73, 296, labels["subtitle"], size=23, color="#9BA993", family="mono"),
         '<rect x="70" y="339" width="1460" height="279" rx="13" fill="#182019" stroke="#476035"/>',
         '<rect x="70" y="339" width="8" height="279" fill="#D3F86A"/>']
    card_x = [116, 604, 1092]
    for x, key in zip(card_x, ["f1", "precision", "recall"]):
        value = CODE[key]
        s.extend([txt(x, 400, labels[key], size=24, color="#A5B59C", family="mono"),
                  txt(x, 512, f"{value:.1f}%", size=96, color="#D3F86A" if key == "f1" else "#F2F2E8", weight=700, family="display"),
                  f'<rect x="{x}" y="556" width="385" height="9" rx="4" fill="#354431"/>',
                  f'<rect x="{x}" y="556" width="{385*value/100:.1f}" height="9" rx="4" fill="#D3F86A"/>'])
    s.append(txt(72, 675, labels["scope"], size=23, color="#D3F86A", family="ui"))
    s.append(txt(70, 744, labels["reference"], size=24, color="#9BA993", family="mono"))
    s.append('<rect x="70" y="771" width="1460" height="89" rx="10" fill="#171E18" stroke="#354431"/>')
    s.append(txt(96, 825, "OCR", size=40, weight=700, family="display"))
    for x, key in zip([480, 815, 1170], ["f1", "precision", "recall"]):
        s.append(txt(x, 825, f"{key.upper()}  {OCR[key]:.1f}%", size=29, color="#D4DAD0", family="mono"))
    s.extend([txt(73, 897, labels["ref_scope"], size=21, color="#9BA993"),
              txt(73, 942, labels["caution"], size=21, color="#F2F2E8"),
              txt(73, 973, labels["coverage"], size=18, color="#9BA993"),
              txt(1040, 86, "SOURCE-BOUND / 2026", size=20, color="#9BA993", family="mono"),
              '</svg>'])
    return "\n".join(s)


if __name__ == "__main__":
    verify()
    for locale in ("zh", "en"):
        target = MEDIA / f"benchmark-{locale}.svg"
        graphic = render(locale)
        target.write_text(graphic, encoding="utf-8")
        (MEDIA / "codesage-showreel" / "public" / target.name).write_text(graphic, encoding="utf-8")
        print(target)
