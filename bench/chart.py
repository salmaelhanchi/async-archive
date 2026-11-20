"""Draw the benchmark chart from ``docs/benchmark/results.json`` as a plain SVG.

    uv run python -m bench.chart

Horizontal bars, one per run under the politeness cap, files per second. A dashed line
marks the most the cap allows. No plotting library: the SVG is small, diffable, and
follows the reader's light or dark theme on GitHub.

Colors: categorical slots 1 and 2 of the reference palette, validated for color vision
deficiency and contrast against both surfaces (blue = this project, orange = baseline).
"""

import json
from html import escape
from pathlib import Path

from bench.benchmark import RESULTS, SPEC
from bench.tree import Archive

CHART = RESULTS.with_name("throughput.svg")

WIDTH = 760
LEFT = 210  # label column
RIGHT = 130  # value column, clear of the bars and the cap line
TOP = 92
ROW = 36
BAR = 18

STYLE = """
  .surface { fill: #fcfcfb; }
  .ink { fill: #0b0b0b; }
  .ink-2 { fill: #52514e; }
  .grid { stroke: #e4e3df; stroke-width: 1; }
  .axis { stroke: #a3a29b; stroke-width: 1; }
  .cap { stroke: #52514e; stroke-width: 1.5; stroke-dasharray: 5 4; }
  .v1 { fill: #eb6834; }
  .v2 { fill: #2a78d6; }
  text { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; }
  @media (prefers-color-scheme: dark) {
    .surface { fill: #1a1a19; }
    .ink { fill: #f0efec; }
    .ink-2 { fill: #c3c2b7; }
    .grid { stroke: #2e2e2c; }
    .axis { stroke: #6b6a64; }
    .cap { stroke: #c3c2b7; }
    .v1 { fill: #d95926; }
    .v2 { fill: #3987e5; }
  }
"""


def _bar(x: float, y: float, width: float, height: float, radius: float = 4) -> str:
    """A bar anchored at ``x`` (square end) with a rounded data end on the right."""
    r = min(radius, width, height / 2)
    return (
        f"M{x:.1f},{y:.1f} h{width - r:.1f} a{r},{r} 0 0 1 {r},{r}"
        f" v{height - 2 * r:.1f} a{r},{r} 0 0 1 {-r},{r} h{-(width - r):.1f} z"
    )


def _nice_max(value: float) -> float:
    for step in (0.5, 1, 2, 2.5, 5, 10):
        top = step * -(-value // step)
        if top / step <= 6:
            return top
    return value


def render(results: dict) -> str:
    setup = results["setup"]
    capped = [c for c in results["configs"] if c["rate"] is not None]
    medians = results["median"]
    baseline = medians[capped[0]["label"]]["seconds"]
    requests = setup["files"] + len(Archive(SPEC).tree) + 1  # files, listings, robots.txt
    ceiling = setup["cap_requests_per_s"] * setup["files"] / requests
    top = _nice_max(max(ceiling, *(medians[c["label"]]["files_per_s"] for c in capped)) * 1.05)
    plot = WIDTH - LEFT - RIGHT
    height = TOP + ROW * len(capped) + 58

    def x_of(value: float) -> float:
        return LEFT + plot * value / top

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}"'
        f' width="{WIDTH}" height="{height}" role="img"'
        ' aria-labelledby="title desc">',
        '<title id="title">Files per second under the same politeness cap</title>',
        f'<desc id="desc">Sequential baseline against the asyncio mirror at 1, 4, 8 and 16'
        f" workers, {setup['files']} files, cap {setup['cap_requests_per_s']:g} requests"
        " per second. Values are in the table in the README.</desc>",
        f"<style>{STYLE}</style>",
        f'<rect class="surface" width="{WIDTH}" height="{height}" rx="8"/>',
        '<text class="ink" x="24" y="34" font-size="17" font-weight="600">'
        "Files per second, same politeness cap"
        f" ({setup['cap_requests_per_s']:g} requests/s per host)</text>",
        f'<text class="ink-2" x="24" y="56" font-size="12.5">{setup["files"]} files,'
        f" {setup['bytes'] / 1e6:.0f} MB, {setup['latency_ms']} ms latency,"
        f" {setup['body_rate_bytes_per_s'] / 1e6:g} MB/s per connection;"
        f" median of {setup['rounds']} runs</text>",
        # legend: two identities, also named on every row
        '<rect class="v1" x="24" y="68" width="10" height="10" rx="2"/>',
        '<text class="ink-2" x="40" y="77" font-size="12">baseline: sequential, requests</text>',
        '<rect class="v2" x="236" y="68" width="10" height="10" rx="2"/>',
        '<text class="ink-2" x="252" y="77" font-size="12">this project: asyncio</text>',
    ]
    axis_y = TOP + ROW * len(capped)
    ticks = 5
    for i in range(ticks + 1):
        value = top * i / ticks
        x = x_of(value)
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{TOP - 6}" x2="{x:.1f}" y2="{axis_y}"/>')
        parts.append(
            f'<text class="ink-2" x="{x:.1f}" y="{axis_y + 18}" font-size="11.5"'
            f' text-anchor="middle">{value:g}</text>'
        )
    parts.append(f'<line class="axis" x1="{LEFT}" y1="{TOP - 6}" x2="{LEFT}" y2="{axis_y}"/>')
    for index, config in enumerate(capped):
        m = medians[config["label"]]
        y = TOP + ROW * index + (ROW - BAR) / 2
        width = max(x_of(m["files_per_s"]) - LEFT, 2)
        kind = config["kind"]
        speedup = baseline / m["seconds"]
        value = f"{m['files_per_s']:.2f}" + ("" if kind == "v1" else f"  ({speedup:.1f}x)")
        parts += [
            f'<text class="ink" x="{LEFT - 12}" y="{y + BAR / 2 + 4:.1f}" font-size="13"'
            f' text-anchor="end">{escape(config["label"])}</text>',
            f'<path class="{kind}" d="{_bar(LEFT, y, width, BAR)}"/>',
            f'<text class="ink" x="{LEFT + plot + 14:.1f}" y="{y + BAR / 2 + 4:.1f}"'
            f' font-size="12.5">{value}</text>',
        ]
    cap_x = x_of(ceiling)
    parts += [
        f'<line class="cap" x1="{cap_x:.1f}" y1="{TOP - 10}" x2="{cap_x:.1f}" y2="{axis_y}"/>',
        f'<text class="ink-2" x="{cap_x:.1f}" y="{TOP - 14}" font-size="11.5"'
        f' text-anchor="middle">most the cap allows: {ceiling:.2f}</text>',
        f'<text class="ink-2" x="{LEFT + plot / 2:.1f}" y="{axis_y + 40}" font-size="12"'
        ' text-anchor="middle">files per second (higher is better)</text>',
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


def main() -> None:
    results = json.loads(Path(RESULTS).read_text(encoding="utf-8"))
    CHART.write_text(render(results), encoding="utf-8", newline="\n")
    print(f"wrote {CHART}")


if __name__ == "__main__":
    main()
