"""통합 제안 지도 렌더 → docs/map_proposal.png.

시대 ~219(관우 존명): 형주 삼분 접점 — 양양(위)·강릉(촉)·강하(오).
지도 확정 전 검토용 일회성. 확정되면 scenario.json 갱신 후 이 파일은 지워도 됨.
의존성: matplotlib. 실행: `python tools/draw_compare.py`.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parent.parent
FCOLOR = {"위": "#d1495b", "촉": "#2a9d8f", "오": "#3d6cb9"}

POS = {
    "업": (5.0, 6.0), "허창": (4.0, 5.3), "낙양": (3.0, 5.0), "장안": (1.5, 5.0),
    "완": (3.3, 4.0), "양양": (3.6, 2.7), "수춘": (6.2, 4.8), "하비": (7.8, 5.3),
    "여강": (7.0, 3.7),
    "한중": (1.0, 3.4), "성도": (0.5, 1.4), "강주": (2.3, 1.9), "강릉": (4.3, 1.9),
    "강하": (5.7, 2.5), "시상": (7.1, 2.5), "건업": (8.4, 3.5),
}
OWNER = {
    "업": "위", "허창": "위", "낙양": "위", "장안": "위", "완": "위",
    "양양": "위", "수춘": "위", "하비": "위",
    "성도": "촉", "한중": "촉", "강주": "촉", "강릉": "촉",
    "건업": "오", "시상": "오", "강하": "오", "여강": "오",
}
EDGES = [
    ("업", "허창", 1), ("허창", "낙양", 1), ("허창", "완", 1), ("낙양", "완", 1),
    ("낙양", "장안", 2), ("허창", "수춘", 2),
    ("수춘", "하비", 2),          # 위 동북 종심(서주 방면)
    ("완", "양양", 2),            # 거리 늘림(1→2)
    ("양양", "강릉", 2),          # 위-촉 형주 전선(관우 북벌)
    ("양양", "강하", 2),          # 위-오 형주 접점 (양양=3방 접점)
    ("장안", "한중", 2),          # 위-촉 서부
    ("성도", "한중", 2), ("성도", "강주", 1), ("강주", "강릉", 3),
    ("강릉", "강하", 2),          # 촉-오
    ("강하", "시상", 1), ("시상", "건업", 1),
    ("수춘", "여강", 1),          # 위-오 서부 전선(회남)
    ("여강", "건업", 1), ("여강", "강하", 2),   # 여강=오 4번째, 종심 고리
    ("하비", "건업", 2),          # 위-오 동부 축(광릉 회랑)
]
# 강 구간(도하 지연·수전 보정) — scenario.json river_edges와 동일. 파랑으로 표시.
RIVERS = {
    frozenset(e) for e in [
        ("업", "허창"), ("양양", "강하"), ("강릉", "강하"), ("강하", "시상"),
        ("시상", "건업"), ("여강", "건업"), ("여강", "강하"), ("하비", "건업"),
    ]
}


def main() -> None:
    out = ROOT / "docs" / "map_proposal.png"
    fig, ax = plt.subplots(figsize=(11.5, 7.5))
    for a, b, d in EDGES:
        (x1, y1), (x2, y2) = POS[a], POS[b]
        river = frozenset((a, b)) in RIVERS
        ax.plot([x1, x2], [y1, y2], color="#4a90d9" if river else "#adb5bd",
                lw=2.6 if river else 1.4, zorder=1)
        ax.text((x1 + x2) / 2, (y1 + y2) / 2, str(d), fontsize=9, color="#495057",
                ha="center", va="center", zorder=2,
                bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.85))
    for name, (x, y) in POS.items():
        ax.scatter(x, y, s=1500, color=FCOLOR[OWNER[name]], edgecolors="white",
                   linewidths=2, zorder=3)
        ax.text(x, y, name, fontsize=11, fontweight="bold", color="white",
                ha="center", va="center", zorder=4)
    handles = [plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=c,
                          markersize=13, label=f) for f, c in FCOLOR.items()]
    ax.legend(handles=handles, loc="lower right", fontsize=10, title="세력")
    ax.set_title("확정 지도 (~219 형주 삼분)  위 8·촉 4·오 4 (총16)   숫자=거리(개월)  파란선=강",
                 fontsize=14, fontweight="bold")
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
