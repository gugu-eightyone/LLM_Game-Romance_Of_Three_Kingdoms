"""도시 관계망 다이어그램 생성기 → docs/city_network.png.

scenario.json의 distances를 읽어 그린다(지도 튜닝하면 다시 실행).
의존성(viz 전용, 게임 런타임 아님): matplotlib. 실행: `python tools/draw_map.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.family"] = "Malgun Gothic"   # Windows 한글 폰트
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = ROOT / "data" / "scenario.json"
OUT = ROOT / "docs" / "city_network.png"

# 손 배치 좌표(지리 대략): 위=북(위쪽), 촉=서(좌), 오=동남(우), 형주=중앙
POS = {
    "업": (5.0, 6.0), "허창": (4.0, 5.3), "낙양": (3.0, 5.0), "장안": (1.5, 5.0), "완": (3.3, 4.0),
    "한중": (1.0, 3.4), "성도": (0.5, 1.4), "강주": (2.3, 1.9),
    "형주": (4.0, 2.6),
    "강하": (5.5, 2.2), "시상": (6.8, 2.9), "건업": (7.8, 3.6),
}
FACTION_COLOR = {"위": "#d1495b", "촉": "#2a9d8f", "오": "#3d6cb9", "중립": "#8d99ae"}


def main() -> None:
    raw = json.loads(SCENARIO.read_text(encoding="utf-8"))
    cities, dists = raw["cities"], raw["distances"]

    fig, ax = plt.subplots(figsize=(11, 7.5))

    # 간선(중복 제거: 정렬 쌍) + 거리 라벨
    seen = set()
    for a, nbrs in dists.items():
        for b, d in nbrs.items():
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            (x1, y1), (x2, y2) = POS[a], POS[b]
            ax.plot([x1, x2], [y1, y2], color="#adb5bd", lw=1.4, zorder=1)
            ax.text((x1 + x2) / 2, (y1 + y2) / 2, str(d), fontsize=9, color="#495057",
                    ha="center", va="center", zorder=2,
                    bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.85))

    # 노드(세력색) + 이름
    for name, (x, y) in POS.items():
        owner = cities[name]["owner"]
        ax.scatter(x, y, s=1500, color=FACTION_COLOR[owner], edgecolors="white",
                   linewidths=2, zorder=3)
        ax.text(x, y, name, fontsize=11, fontweight="bold", color="white",
                ha="center", va="center", zorder=4)

    # 범례
    handles = [plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=c,
                          markersize=13, label=f) for f, c in FACTION_COLOR.items()]
    ax.legend(handles=handles, loc="lower right", fontsize=10, title="세력")

    ax.set_title("삼국정립 도시 관계망  (숫자 = 거리, 개월)", fontsize=15, fontweight="bold")
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(OUT, dpi=150, bbox_inches="tight")
    print(f"저장: {OUT}")


if __name__ == "__main__":
    main()
