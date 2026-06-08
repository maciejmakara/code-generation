import argparse
import csv
import html
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


ARTIFACTS_DIR = Path(__file__).resolve().parent / "artrifacts"
OUTPUT_DIR = Path(__file__).resolve().parent / "analysis_out" / "semantic_errors"
SCORE_RE = re.compile(r"^(GPT|Claude)\s*-\s*(0(?:\.5)?|1)\s*$", re.IGNORECASE)
GOOD_SPEC_RE = re.compile(r"^Good\s+Spec\s*:\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class SemanticRow:
    scenario: str
    file_id: str
    model: str
    score: float
    rationale: str
    has_good_spec: bool
    good_spec_note: str
    included_in_filtered: bool
    source_path: str


def discover_files(artifacts_dir: Path) -> List[Path]:
    return sorted(
        path
        for path in artifacts_dir.glob("*/check_incorrect_semantic/*")
        if path.is_file() and path.name in {"1", "2"}
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def parse_file(path: Path, artifacts_dir: Path) -> List[SemanticRow]:
    rel = path.relative_to(artifacts_dir)
    scenario = rel.parts[0]
    file_id = path.name

    lines = read_text(path).splitlines()
    current_model = None
    current_score = None
    rationale_lines: List[str] = []
    rows_temp: List[Tuple[str, float, str]] = []
    in_good_spec = False
    good_spec_lines: List[str] = []

    def flush() -> None:
        nonlocal current_model, current_score, rationale_lines
        if current_model is None or current_score is None:
            return
        rationale = "\n".join(line for line in rationale_lines if line.strip()).strip()
        rows_temp.append((current_model, current_score, rationale))
        current_model = None
        current_score = None
        rationale_lines = []

    for raw_line in lines:
        line = raw_line.strip()
        score_match = SCORE_RE.match(line)
        if score_match:
            flush()
            in_good_spec = False
            current_model = score_match.group(1).title()
            current_score = float(score_match.group(2))
            continue
        if GOOD_SPEC_RE.match(line):
            flush()
            in_good_spec = True
            continue
        if in_good_spec:
            if line:
                good_spec_lines.append(line)
            continue
        if current_model is not None:
            rationale_lines.append(raw_line.rstrip())

    flush()

    good_spec_note = "\n".join(good_spec_lines).strip()
    has_good_spec = bool(good_spec_note)
    return [
        SemanticRow(
            scenario=scenario,
            file_id=file_id,
            model=model,
            score=score,
            rationale=rationale,
            has_good_spec=has_good_spec,
            good_spec_note=good_spec_note,
            included_in_filtered=not has_good_spec,
            source_path=str(path),
        )
        for model, score, rationale in rows_temp
    ]


def summarize(rows: Sequence[SemanticRow], filtered_only: bool) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    selected = [row for row in rows if row.included_in_filtered] if filtered_only else list(rows)
    file_summary: Dict[Tuple[str, str], List[SemanticRow]] = {}
    overall_summary: Dict[Tuple[str, str], List[SemanticRow]] = {}

    for row in selected:
        file_summary.setdefault((row.scenario, row.file_id), []).append(row)
        overall_summary.setdefault(("scenario", row.scenario), []).append(row)
        overall_summary.setdefault(("model", row.model), []).append(row)

    file_rows: List[Dict[str, object]] = []
    for (scenario, file_id), bucket in sorted(file_summary.items()):
        file_rows.append(
            {
                "dataset": "filtered" if filtered_only else "all",
                "scenario": scenario,
                "file_id": file_id,
                "avg_score": sum(row.score for row in bucket) / len(bucket),
                "count": len(bucket),
                "has_good_spec": int(any(row.has_good_spec for row in bucket)),
            }
        )

    overall_rows: List[Dict[str, object]] = []
    for (kind, name), bucket in sorted(overall_summary.items()):
        overall_rows.append(
            {
                "dataset": "filtered" if filtered_only else "all",
                "kind": kind,
                "name": name,
                "avg_score": sum(row.score for row in bucket) / len(bucket),
                "count": len(bucket),
            }
        )

    return file_rows, overall_rows


def write_rows_csv(rows: Sequence[SemanticRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "scenario",
                "file_id",
                "model",
                "score",
                "has_good_spec",
                "included_in_filtered",
                "rationale",
                "good_spec_note",
                "source_path",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.scenario,
                    row.file_id,
                    row.model,
                    f"{row.score:.3g}",
                    int(row.has_good_spec),
                    int(row.included_in_filtered),
                    row.rationale,
                    row.good_spec_note,
                    row.source_path,
                ]
            )


def write_dict_csv(rows: Sequence[Dict[str, object]], fieldnames: Sequence[str], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def score_color(value: float) -> str:
    value = max(0.0, min(1.0, value))
    red = int(220 - (120 * value))
    green = int(90 + (130 * value))
    blue = 95
    return f"rgb({red}, {green}, {blue})"


def build_heatmap(file_rows: Sequence[Dict[str, object]], dataset: str, title: str) -> str:
    entries = [row for row in file_rows if row["dataset"] == dataset]
    if not entries:
        return f"<h3>{html.escape(title)}</h3><p>No data.</p>"
    
    # Only use scenarios and file_ids that actually have data in this dataset
    scenarios = sorted({str(row["scenario"]) for row in entries})
    file_ids = sorted({str(row["file_id"]) for row in entries})
    score_map = {(str(row["scenario"]), str(row["file_id"])): float(row["avg_score"]) for row in entries}

    cell_w = 120
    cell_h = 46
    left = 110
    top = 60
    width = left + len(file_ids) * cell_w + 20
    height = top + len(scenarios) * cell_h + 20
    parts = [f"<h3>{html.escape(title)}</h3>", f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    for col, file_id in enumerate(file_ids):
        x = left + col * cell_w
        parts.append(
            f'<text x="{x + 55}" y="24" text-anchor="middle" font-size="12" font-family="Segoe UI">file {html.escape(file_id)}</text>'
        )
    for row_index, scenario in enumerate(scenarios):
        y = top + row_index * cell_h
        parts.append(
            f'<text x="98" y="{y + 27}" text-anchor="end" font-size="13" font-family="Segoe UI">{html.escape(scenario)}</text>'
        )
        for col, file_id in enumerate(file_ids):
            x = left + col * cell_w
            score = score_map.get((scenario, file_id))
            if score is None:
                # Skip cells with no data (filtered out)
                parts.append(
                    f'<rect x="{x}" y="{y}" width="108" height="36" rx="6" ry="6" fill="#f5f5f5" stroke="#ddd" stroke-dasharray="3,3" />'
                )
                parts.append(
                    f'<text x="{x + 54}" y="{y + 23}" text-anchor="middle" font-size="11" font-family="Segoe UI" fill="#999">N/A</text>'
                )
            else:
                parts.append(
                    f'<rect x="{x}" y="{y}" width="108" height="36" rx="6" ry="6" fill="{score_color(score)}" stroke="#fff" />'
                )
                parts.append(
                    f'<text x="{x + 54}" y="{y + 23}" text-anchor="middle" font-size="12" font-family="Segoe UI">{score:.2f}</text>'
                )
    parts.append("</svg>")
    return "".join(parts)


def build_cards(overall_rows: Sequence[Dict[str, object]], dataset: str, kind: str) -> str:
    cards = []
    for row in overall_rows:
        if row["dataset"] != dataset or row["kind"] != kind:
            continue
        cards.append(
            "<div class='card'>"
            f"<h3>{html.escape(str(row['name']))}</h3>"
            f"<p>Average score: {float(row['avg_score']):.2f}</p>"
            f"<p>Entries: {row['count']}</p>"
            "</div>"
        )
    return "".join(cards)


def build_good_spec_table(rows: Sequence[SemanticRow]) -> str:
    flagged = []
    seen = set()
    for row in rows:
        key = (row.scenario, row.file_id)
        if row.has_good_spec and key not in seen:
            flagged.append(row)
            seen.add(key)
    if not flagged:
        return "<tr><td colspan='3'>No Good Spec cases.</td></tr>"
    return "".join(
        "<tr>"
        f"<td>{html.escape(row.scenario)}</td>"
        f"<td>{html.escape(row.file_id)}</td>"
        f"<td>{html.escape(row.good_spec_note or '-')}</td>"
        "</tr>"
        for row in flagged
    )


def write_report_html(
    rows: Sequence[SemanticRow],
    file_rows: Sequence[Dict[str, object]],
    overall_rows: Sequence[Dict[str, object]],
    out_path: Path,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Semantic Errors Analysis</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; background: #fafafa; color: #1a1a1a; }}
    .panel {{ background: #fff; border-radius: 12px; padding: 18px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
    .grid {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .card {{ width: 220px; border: 1px solid #ececec; border-radius: 12px; padding: 16px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #e4e4e4; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f3f3; }}
  </style>
</head>
<body>
  <div class="panel">
    <h1>Semantic errors analysis</h1>
    <p>Dataset `all` contains every evaluated row.</p>
    <p>Dataset `filtered` excludes whole files marked with `Good Spec`, for both GPT and Claude.</p>
    <p>Total parsed rows: {len(rows)}</p>
  </div>
  <div class="panel">
    <h2>All cases: by scenario</h2>
    <div class="grid">{build_cards(overall_rows, "all", "scenario")}</div>
  </div>
  <div class="panel">
    <h2>All cases: by model</h2>
    <div class="grid">{build_cards(overall_rows, "all", "model")}</div>
  </div>
  <div class="panel">
    <h2>Filtered cases: by scenario</h2>
    <div class="grid">{build_cards(overall_rows, "filtered", "scenario")}</div>
  </div>
  <div class="panel">
    <h2>Filtered cases: by model</h2>
    <div class="grid">{build_cards(overall_rows, "filtered", "model")}</div>
  </div>
  <div class="panel">
    {build_heatmap(file_rows, "all", "Heatmap: all semantic cases")}
  </div>
  <div class="panel">
    {build_heatmap(file_rows, "filtered", "Heatmap: filtered semantic cases")}
  </div>
  <div class="panel">
    <h2>Good Spec exclusions</h2>
    <table>
      <thead><tr><th>Scenario</th><th>File</th><th>Note</th></tr></thead>
      <tbody>{build_good_spec_table(rows)}</tbody>
    </table>
  </div>
</body>
</html>"""
    out_path.write_text(html_text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze incorrect semantic evaluation files.")
    parser.add_argument("--artifacts-dir", type=Path, default=ARTIFACTS_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    rows: List[SemanticRow] = []
    for path in discover_files(args.artifacts_dir):
        rows.extend(parse_file(path, args.artifacts_dir))

    file_rows_all, overall_rows_all = summarize(rows, filtered_only=False)
    file_rows_filtered, overall_rows_filtered = summarize(rows, filtered_only=True)
    file_rows = file_rows_all + file_rows_filtered
    overall_rows = overall_rows_all + overall_rows_filtered

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_rows_csv(rows, args.output_dir / "semantic_details.csv")
    write_dict_csv(
        file_rows,
        ["dataset", "scenario", "file_id", "avg_score", "count", "has_good_spec"],
        args.output_dir / "semantic_file_summary.csv",
    )
    write_dict_csv(
        overall_rows,
        ["dataset", "kind", "name", "avg_score", "count"],
        args.output_dir / "semantic_overall_summary.csv",
    )
    (args.output_dir / "semantic_report.json").write_text(
        json.dumps(
            {
                "rows": [asdict(row) for row in rows],
                "file_summary": file_rows,
                "overall_summary": overall_rows,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_report_html(rows, file_rows, overall_rows, args.output_dir / "semantic_report.html")

    print(f"Parsed semantic rows: {len(rows)}")
    print(f"Output directory: {args.output_dir}")


if __name__ == "__main__":
    main()
