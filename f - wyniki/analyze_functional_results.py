import argparse
import csv
import html
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple


ARTIFACTS_DIR = Path(__file__).resolve().parent / "artrifacts"
OUTPUT_DIR = Path(__file__).resolve().parent / "analysis_out" / "functional_results"
TEST_RE = re.compile(
    r"^(?P<test_id>T\d+[A-Za-z]?)"
    r"(?:\s*-\s*(?P<label>.*?))?"
    r"\s+(?P<status>PASS|FAIL|OK)\b"
    r"(?P<detail>.*)$"
)


@dataclass(frozen=True)
class TestRow:
    scenario: str
    file_id: str
    view: str
    test_id: str
    label: str
    status: str
    passed: bool
    detail: str
    source_path: str


def discover_files(artifacts_dir: Path) -> List[Path]:
    return sorted(
        path
        for path in artifacts_dir.glob("*/ai_code/gpt/functional_tests/*")
        if path.is_file() and path.name in {"1", "2", "3"}
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def parse_file(path: Path, artifacts_dir: Path) -> Tuple[List[TestRow], List[TestRow]]:
    rel = path.relative_to(artifacts_dir)
    scenario = rel.parts[0]
    file_id = path.name

    normal_map: Dict[str, TestRow] = {}
    strict_override_map: Dict[str, TestRow] = {}
    current_mode = "normal"

    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.rstrip(":").lower() == "strict":
            current_mode = "strict"
            continue

        match = TEST_RE.match(line)
        if not match:
            continue

        status = match.group("status").upper()
        if status == "OK":
            status = "PASS"
        row = TestRow(
            scenario=scenario,
            file_id=file_id,
            view="normal",
            test_id=match.group("test_id"),
            label=(match.group("label") or "").strip(),
            status=status,
            passed=(status == "PASS"),
            detail=match.group("detail").strip(" -,:"),
            source_path=str(path),
        )
        if current_mode == "normal":
            normal_map[row.test_id] = row
        else:
            strict_override_map[row.test_id] = row

    normal_rows = [normal_map[key] for key in sorted(normal_map.keys())]
    strict_rows: List[TestRow] = []
    for test_id in sorted(normal_map.keys()):
        if test_id in strict_override_map:
            override = strict_override_map[test_id]
            strict_rows.append(
                TestRow(
                    scenario=scenario,
                    file_id=file_id,
                    view="strict_final",
                    test_id=test_id,
                    label=override.label or normal_map[test_id].label,
                    status=override.status,
                    passed=override.passed,
                    detail=override.detail,
                    source_path=override.source_path,
                )
            )
        else:
            base = normal_map[test_id]
            strict_rows.append(
                TestRow(
                    scenario=scenario,
                    file_id=file_id,
                    view="strict_final",
                    test_id=base.test_id,
                    label=base.label,
                    status=base.status,
                    passed=base.passed,
                    detail=base.detail,
                    source_path=base.source_path,
                )
            )

    for test_id in sorted(set(strict_override_map.keys()) - set(normal_map.keys())):
        override = strict_override_map[test_id]
        strict_rows.append(
            TestRow(
                scenario=scenario,
                file_id=file_id,
                view="strict_final",
                test_id=override.test_id,
                label=override.label,
                status=override.status,
                passed=override.passed,
                detail=override.detail,
                source_path=override.source_path,
            )
        )

    return normal_rows, strict_rows


def summarize(rows: Sequence[TestRow]) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    file_summary: List[Dict[str, object]] = []
    scenario_summary: List[Dict[str, object]] = []

    grouped_file: Dict[Tuple[str, str, str], List[TestRow]] = {}
    grouped_scenario: Dict[Tuple[str, str], List[TestRow]] = {}

    for row in rows:
        grouped_file.setdefault((row.scenario, row.file_id, row.view), []).append(row)
        grouped_scenario.setdefault((row.scenario, row.view), []).append(row)

    for (scenario, file_id, view), bucket in sorted(grouped_file.items()):
        passed = sum(1 for row in bucket if row.passed)
        failed = len(bucket) - passed
        file_summary.append(
            {
                "scenario": scenario,
                "file_id": file_id,
                "view": view,
                "passed": passed,
                "failed": failed,
                "total": len(bucket),
                "pass_rate": passed / len(bucket) if bucket else 0.0,
                "failed_tests": ",".join(row.test_id for row in bucket if not row.passed),
            }
        )

    for (scenario, view), bucket in sorted(grouped_scenario.items()):
        passed = sum(1 for row in bucket if row.passed)
        failed = len(bucket) - passed
        scenario_summary.append(
            {
                "scenario": scenario,
                "view": view,
                "passed": passed,
                "failed": failed,
                "total": len(bucket),
                "pass_rate": passed / len(bucket) if bucket else 0.0,
            }
        )

    return file_summary, scenario_summary


def write_rows_csv(rows: Sequence[TestRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["scenario", "file_id", "view", "test_id", "label", "status", "passed", "detail", "source_path"]
        )
        for row in rows:
            writer.writerow(
                [
                    row.scenario,
                    row.file_id,
                    row.view,
                    row.test_id,
                    row.label,
                    row.status,
                    int(row.passed),
                    row.detail,
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


def build_heatmap(file_summary: Sequence[Dict[str, object]], view: str, title: str) -> str:
    entries = [row for row in file_summary if row["view"] == view]
    if not entries:
        return f"<h3>{html.escape(title)}</h3><p>No data.</p>"

    scenarios = sorted({str(row["scenario"]) for row in entries})
    file_ids = sorted({str(row["file_id"]) for row in entries})
    value_map = {(str(row["scenario"]), str(row["file_id"])): float(row["pass_rate"]) for row in entries}
    fail_map = {(str(row["scenario"]), str(row["file_id"])): str(row["failed_tests"]) for row in entries}

    cell_w = 120
    cell_h = 46
    left = 110
    top = 60
    width = left + (len(file_ids) * cell_w) + 20
    height = top + (len(scenarios) * cell_h) + 20

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
            value = value_map.get((scenario, file_id), 0.0)
            failed_tests = fail_map.get((scenario, file_id), "")
            label = f"{value * 100:.0f}%"
            if failed_tests:
                label += f" | {failed_tests}"
            parts.append(
                f'<rect x="{x}" y="{y}" width="108" height="36" rx="6" ry="6" fill="{score_color(value)}" stroke="#fff" />'
            )
            parts.append(
                f'<text x="{x + 54}" y="{y + 23}" text-anchor="middle" font-size="11" font-family="Segoe UI">{html.escape(label)}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def build_cards(scenario_summary: Sequence[Dict[str, object]], view: str) -> str:
    cards = []
    for row in scenario_summary:
        if row["view"] != view:
            continue
        rate = float(row["pass_rate"])
        cards.append(
            "<div class='card'>"
            f"<h3>{html.escape(str(row['scenario']))}</h3>"
            f"<p>{row['passed']} / {row['total']} passed</p>"
            f"<div class='bar'><span style='width:{int(rate * 100)}%'></span></div>"
            f"<p>Pass rate: {rate * 100:.1f}%</p>"
            "</div>"
        )
    return "".join(cards)


def build_failures_table(rows: Sequence[TestRow], view: str) -> str:
    filtered = [row for row in rows if row.view == view and not row.passed]
    if not filtered:
        return "<tr><td colspan='6'>No failures.</td></tr>"
    return "".join(
        "<tr>"
        f"<td>{html.escape(row.scenario)}</td>"
        f"<td>{html.escape(row.file_id)}</td>"
        f"<td>{html.escape(row.test_id)}</td>"
        f"<td>{html.escape(row.label or '-')}</td>"
        f"<td>{html.escape(row.status)}</td>"
        f"<td>{html.escape(row.detail or '-')}</td>"
        "</tr>"
        for row in filtered
    )


def write_report_html(
    normal_rows: Sequence[TestRow],
    strict_rows: Sequence[TestRow],
    file_summary: Sequence[Dict[str, object]],
    scenario_summary: Sequence[Dict[str, object]],
    out_path: Path,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    all_rows = list(normal_rows) + list(strict_rows)
    html_text = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Functional Results Analysis</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; background: #fafafa; color: #1a1a1a; }}
    .panel {{ background: #fff; border-radius: 12px; padding: 18px; margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
    .grid {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .card {{ width: 220px; border: 1px solid #ececec; border-radius: 12px; padding: 16px; }}
    .bar {{ height: 12px; background: #ececec; border-radius: 999px; overflow: hidden; }}
    .bar span {{ display: block; height: 100%; background: linear-gradient(90deg, #d46d6d, #78b07a); }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #e4e4e4; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f3f3; }}
  </style>
</head>
<body>
  <div class="panel">
    <h1>Functional tests analysis</h1>
    <p>Normal view = raw normal script results.</p>
    <p>Strict-final view = normal results with only listed Strict tests overridden.</p>
    <p>Total parsed rows: {len(all_rows)}</p>
  </div>
  <div class="panel">
    <h2>Scenario overview: normal</h2>
    <div class="grid">{build_cards(scenario_summary, "normal")}</div>
  </div>
  <div class="panel">
    <h2>Scenario overview: strict-final</h2>
    <div class="grid">{build_cards(scenario_summary, "strict_final")}</div>
  </div>
  <div class="panel">
    {build_heatmap(file_summary, "normal", "Heatmap: normal")}
  </div>
  <div class="panel">
    {build_heatmap(file_summary, "strict_final", "Heatmap: strict-final")}
  </div>
  <div class="panel">
    <h2>Failures: normal</h2>
    <table>
      <thead><tr><th>Scenario</th><th>File</th><th>Test</th><th>Label</th><th>Status</th><th>Detail</th></tr></thead>
      <tbody>{build_failures_table(normal_rows, "normal")}</tbody>
    </table>
  </div>
  <div class="panel">
    <h2>Failures: strict-final</h2>
    <table>
      <thead><tr><th>Scenario</th><th>File</th><th>Test</th><th>Label</th><th>Status</th><th>Detail</th></tr></thead>
      <tbody>{build_failures_table(strict_rows, "strict_final")}</tbody>
    </table>
  </div>
</body>
</html>"""
    out_path.write_text(html_text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze functional test files for GPT artifacts.")
    parser.add_argument("--artifacts-dir", type=Path, default=ARTIFACTS_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    normal_rows: List[TestRow] = []
    strict_rows: List[TestRow] = []
    for path in discover_files(args.artifacts_dir):
        parsed_normal, parsed_strict = parse_file(path, args.artifacts_dir)
        normal_rows.extend(parsed_normal)
        strict_rows.extend(parsed_strict)

    file_summary, scenario_summary = summarize(list(normal_rows) + list(strict_rows))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_rows_csv(normal_rows, args.output_dir / "functional_normal_details.csv")
    write_rows_csv(strict_rows, args.output_dir / "functional_strict_final_details.csv")
    write_dict_csv(
        file_summary,
        ["scenario", "file_id", "view", "passed", "failed", "total", "pass_rate", "failed_tests"],
        args.output_dir / "functional_file_summary.csv",
    )
    write_dict_csv(
        scenario_summary,
        ["scenario", "view", "passed", "failed", "total", "pass_rate"],
        args.output_dir / "functional_scenario_summary.csv",
    )
    (args.output_dir / "functional_report.json").write_text(
        json.dumps(
            {
                "normal_rows": [asdict(row) for row in normal_rows],
                "strict_final_rows": [asdict(row) for row in strict_rows],
                "file_summary": file_summary,
                "scenario_summary": scenario_summary,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_report_html(normal_rows, strict_rows, file_summary, scenario_summary, args.output_dir / "functional_report.html")

    print(f"Parsed normal rows: {len(normal_rows)}")
    print(f"Parsed strict-final rows: {len(strict_rows)}")
    print(f"Output directory: {args.output_dir}")


if __name__ == "__main__":
    main()
