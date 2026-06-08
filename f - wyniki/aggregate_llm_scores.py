import argparse
import csv
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ScoreRow:
    scenario: str
    generator: str
    evaluator: str
    category: str  # structural|semantic|specification
    score: float


@dataclass(frozen=True)
class BreakdownRow:
    scenario: str
    generator: str
    evaluator: str
    category: str
    criterion: str
    score: float


CATEGORIES: Tuple[str, ...] = ("structural", "semantic", "specification")
PRIMARY_CATEGORIES: Tuple[str, ...] = ("structural", "semantic")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def discover_llm_score_files(artifacts_dir: Path) -> List[Path]:
    # Layout: final_tests/artrifacts/<scenario>/ai_code/<generator>/llm_score/<evaluator>.json
    return sorted(artifacts_dir.glob("**/ai_code/*/llm_score/*.json"))


def parse_path_metadata(artifacts_dir: Path, score_path: Path) -> Tuple[str, str, str]:
    rel = score_path.relative_to(artifacts_dir)
    parts = rel.parts

    # Expected at least: scenario / ai_code / generator / llm_score / file.json
    # Example: fork/ai_code/gpt/llm_score/swe.json
    if len(parts) < 5:
        raise ValueError(f"Unexpected score path (too short): {rel}")

    scenario = parts[0]

    # Find 'ai_code' index to be robust
    try:
        i_ai = parts.index("ai_code")
        generator = parts[i_ai + 1]
    except (ValueError, IndexError):
        raise ValueError(f"Unexpected score path (missing ai_code/<generator>): {rel}")

    evaluator = score_path.stem
    return scenario, generator, evaluator


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def extract_rows(
    artifacts_dir: Path,
    score_path: Path,
    payload: Dict[str, Any],
) -> Tuple[List[ScoreRow], List[BreakdownRow]]:
    scenario, generator, evaluator = parse_path_metadata(artifacts_dir, score_path)

    score_rows: List[ScoreRow] = []
    breakdown_rows: List[BreakdownRow] = []

    for category in CATEGORIES:
        node = payload.get(category)
        if not isinstance(node, dict):
            continue

        s = _safe_float(node.get("score"))
        if s is not None and not math.isnan(s):
            score_rows.append(
                ScoreRow(
                    scenario=scenario,
                    generator=generator,
                    evaluator=evaluator,
                    category=category,
                    score=s,
                )
            )

        breakdown = node.get("breakdown")
        if isinstance(breakdown, dict):
            for crit, val in breakdown.items():
                fv = _safe_float(val)
                if fv is None or math.isnan(fv):
                    continue
                breakdown_rows.append(
                    BreakdownRow(
                        scenario=scenario,
                        generator=generator,
                        evaluator=evaluator,
                        category=category,
                        criterion=str(crit),
                        score=fv,
                    )
                )

    return score_rows, breakdown_rows


def write_scores_csv(rows: Sequence[ScoreRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "generator", "evaluator", "category", "score"])
        for r in rows:
            w.writerow([r.scenario, r.generator, r.evaluator, r.category, f"{r.score:.6g}"])


def write_breakdown_csv(rows: Sequence[BreakdownRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "generator", "evaluator", "category", "criterion", "score"])
        for r in rows:
            w.writerow(
                [r.scenario, r.generator, r.evaluator, r.category, r.criterion, f"{r.score:.6g}"]
            )


def _group_stats(values: Sequence[float]) -> Tuple[int, Optional[float], Optional[float]]:
    if not values:
        return 0, None, None
    if len(values) == 1:
        return 1, float(values[0]), 0.0
    return len(values), mean(values), pstdev(values)


def summarize_scores(
    score_rows: Sequence[ScoreRow],
) -> Dict[str, List[Dict[str, Any]]]:
    # Long-form -> multiple summary tables
    by_gen_cat: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    by_gen_eval_cat: Dict[Tuple[str, str, str], List[float]] = defaultdict(list)
    by_eval_cat: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    by_item_cat: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(dict)
    by_scenario_gen_cat: Dict[Tuple[str, str, str], List[float]] = defaultdict(list)  # (scenario, generator, category) -> scores

    for r in score_rows:
        by_gen_cat[(r.generator, r.category)].append(r.score)
        by_gen_eval_cat[(r.generator, r.evaluator, r.category)].append(r.score)
        by_eval_cat[(r.evaluator, r.category)].append(r.score)
        # For agreement: (scenario,generator,category) -> evaluator->score
        by_item_cat[(r.scenario, r.generator, r.category)][r.evaluator] = r.score
        # For scenario ranking: (scenario,generator,category) -> scores
        by_scenario_gen_cat[(r.scenario, r.generator, r.category)].append(r.score)

    summary_generator: List[Dict[str, Any]] = []
    for (gen, cat), vals in sorted(by_gen_cat.items()):
        n, m, sd = _group_stats(vals)
        summary_generator.append(
            {"generator": gen, "category": cat, "n": n, "mean": m, "std": sd}
        )

    summary_generator_evaluator: List[Dict[str, Any]] = []
    for (gen, ev, cat), vals in sorted(by_gen_eval_cat.items()):
        n, m, sd = _group_stats(vals)
        summary_generator_evaluator.append(
            {"generator": gen, "evaluator": ev, "category": cat, "n": n, "mean": m, "std": sd}
        )

    summary_evaluator: List[Dict[str, Any]] = []
    for (ev, cat), vals in sorted(by_eval_cat.items()):
        n, m, sd = _group_stats(vals)
        summary_evaluator.append({"evaluator": ev, "category": cat, "n": n, "mean": m, "std": sd})

    # Agreement: pairwise correlation across evaluators for same (scenario,generator,category)
    evaluators = sorted({r.evaluator for r in score_rows})
    corr_tables: Dict[str, List[Dict[str, Any]]] = {}

    def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
        if len(xs) != len(ys) or len(xs) < 2:
            return None
        mx = mean(xs)
        my = mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        deny = math.sqrt(sum((y - my) ** 2 for y in ys))
        if denx == 0 or deny == 0:
            return None
        return num / (denx * deny)

    for cat in CATEGORIES:
        rows: List[Dict[str, Any]] = []
        # Build evaluator->list aligned on items present for both
        items = [k for k in by_item_cat.keys() if k[2] == cat]
        for i, e1 in enumerate(evaluators):
            for e2 in evaluators[i + 1 :]:
                xs: List[float] = []
                ys: List[float] = []
                for item in items:
                    scores = by_item_cat[item]
                    if e1 in scores and e2 in scores:
                        xs.append(scores[e1])
                        ys.append(scores[e2])
                c = pearson(xs, ys)
                rows.append(
                    {
                        "category": cat,
                        "evaluator_a": e1,
                        "evaluator_b": e2,
                        "n_pairs": len(xs),
                        "pearson": c,
                    }
                )
        corr_tables[cat] = rows

    def build_scenario_summary(categories: Sequence[str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for scenario in sorted({r.scenario for r in score_rows}):
            scenario_scores: List[float] = []
            for (sc, gen, cat), vals in by_scenario_gen_cat.items():
                if sc == scenario and cat in categories:
                    scenario_scores.extend(vals)

            n, m, sd = _group_stats(scenario_scores)
            difficulty_score = (m - sd) if (m is not None and sd is not None) else float("nan")
            rows.append(
                {
                    "scenario": scenario,
                    "n": n,
                    "mean": m,
                    "std": sd,
                    "difficulty_score": difficulty_score,
                    "categories_used": "+".join(categories),
                }
            )

        rows.sort(
            key=lambda x: x["difficulty_score"]
            if not math.isnan(x["difficulty_score"])
            else float("inf")
        )
        return rows

    # Main scenario difficulty ranking should reflect generated code quality,
    # so it intentionally excludes the auxiliary specification score.
    summary_scenario = build_scenario_summary(PRIMARY_CATEGORIES)
    # Secondary view: whole-task difficulty, including specification quality.
    summary_scenario_with_spec = build_scenario_summary(CATEGORIES)

    return {
        "summary_generator": summary_generator,
        "summary_generator_evaluator": summary_generator_evaluator,
        "summary_evaluator": summary_evaluator,
        "summary_scenario": summary_scenario,
        "summary_scenario_with_spec": summary_scenario_with_spec,
        "agreement": [r for cat in CATEGORIES for r in corr_tables[cat]],
    }


def write_dicts_csv(rows: Sequence[Dict[str, Any]], out_path: Path, fieldnames: Sequence[str]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fieldnames))
        w.writeheader()
        for r in rows:
            w.writerow(r)


def plot_structural_vs_semantic_comparison(score_rows: Sequence[ScoreRow], out_dir: Path) -> str:
    """Create bar chart comparing structural and semantic scores across generators with error bars for semantic scores."""
    import matplotlib.pyplot as plt
    import numpy as np
    
    # Build data structure for generator stats
    by_gen_cat: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for r in score_rows:
        if r.category in ["structural", "semantic"]:
            by_gen_cat[(r.generator, r.category)].append(r.score)
    
    generators = sorted({r.generator for r in score_rows})
    
    # Prepare data for plotting
    structural_means = []
    structural_stds = []
    semantic_means = []
    semantic_stds = []
    
    for gen in generators:
        # Structural data
        struct_vals = by_gen_cat.get((gen, "structural"), [])
        _, struct_mean, struct_std = _group_stats(struct_vals)
        structural_means.append(struct_mean if struct_mean is not None else 0)
        structural_stds.append(struct_std if struct_std is not None else 0)
        
        # Semantic data
        sem_vals = by_gen_cat.get((gen, "semantic"), [])
        _, sem_mean, sem_std = _group_stats(sem_vals)
        semantic_means.append(sem_mean if sem_mean is not None else 0)
        semantic_stds.append(sem_std if sem_std is not None else 0)
    
    # Create figure
    fig, ax = plt.subplots(figsize=(14, 8))
    
    # Position setup for 6 bars
    x_pos = np.arange(len(generators))
    width = 0.35
    
    # Plot structural bars (no error bars)
    bars1 = ax.bar(x_pos - width/2, structural_means, width, 
                   label='Structural Score', alpha=0.8, color='steelblue')
    
    # Plot semantic bars (with error bars)
    bars2 = ax.bar(x_pos + width/2, semantic_means, width, yerr=semantic_stds,
                   label='Semantic Score ± std', alpha=0.8, color='coral', 
                   capsize=5, ecolor='darkred')
    
    # Customize plot
    ax.set_xlabel('Generator Model', fontsize=12, fontweight='bold')
    ax.set_ylabel('Score', fontsize=12, fontweight='bold')
    ax.set_title('Structural vs Semantic Score Comparison\n(Error bars show semantic score standard deviation)', 
                fontsize=14, fontweight='bold')
    ax.set_xticks(x_pos)
    ax.set_xticklabels([gen.upper() for gen in generators])
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(0.5, 1.0)
    
    # Add value labels on bars
    def add_value_labels(bars, values, stds=None):
        for bar, value, std in zip(bars, values, stds or [0]*len(bars)):
            height = bar.get_height()
            if std > 0:
                label = f'{value:.3f}±{std:.3f}'
            else:
                label = f'{value:.3f}'
            ax.text(bar.get_x() + bar.get_width()/2., height + 0.01,
                   label, ha='center', va='bottom', fontsize=9)
    
    add_value_labels(bars1, structural_means)
    add_value_labels(bars2, semantic_means, semantic_stds)
    
    # Save plot
    output_path = out_dir / "structural_vs_semantic_comparison.png"
    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    
    return str(output_path)


def try_make_plots(score_rows: Sequence[ScoreRow], out_dir: Path) -> List[str]:
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    generated_files: List[str] = []

    # Helper: build nested dict for means/stds
    by_gen_cat: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    by_eval_cat: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    by_gen_eval_cat: Dict[Tuple[str, str, str], List[float]] = defaultdict(list)
    by_scenario_gen_eval: Dict[Tuple[str, str, str], List[float]] = defaultdict(list)  # (scenario, generator, evaluator) -> structural+semantic scores

    for r in score_rows:
        by_gen_cat[(r.generator, r.category)].append(r.score)
        by_eval_cat[(r.evaluator, r.category)].append(r.score)
        by_gen_eval_cat[(r.generator, r.evaluator, r.category)].append(r.score)
        # Scenario-level diagrams should reflect generator output quality rather
        # than the auxiliary specification score.
        if r.category in PRIMARY_CATEGORIES:
            by_scenario_gen_eval[(r.scenario, r.generator, r.evaluator)].append(r.score)

    generators = sorted({r.generator for r in score_rows})
    evaluators = sorted({r.evaluator for r in score_rows})

    # Helper function to calculate y-axis limits (fixed 0.5-1.0 range)
    def get_y_limits(values: List[float]) -> Tuple[float, float]:
        return 0.5, 1.0

    # Create a comprehensive multi-subplot figure
    fig = plt.figure(figsize=(20, 16))

    # 1) Generator performance bar plots (3 subplots in a row)
    for i, cat in enumerate(CATEGORIES):
        ax = fig.add_subplot(4, 3, i + 1)  # First row, 3 columns

        xs = generators
        means = []
        stds = []
        for g in xs:
            vals = by_gen_cat.get((g, cat), [])
            n, m, sd = _group_stats(vals)
            means.append(m if m is not None else float("nan"))
            stds.append(sd if sd is not None else float("nan"))

        y_min, y_max = get_y_limits(means)
        ax.bar(xs, means, yerr=stds, capsize=4)
        ax.set_ylim(y_min, y_max)
        ax.set_title(f"Generator comparison — {cat}")
        ax.set_ylabel("score")
        ax.set_xlabel("generator")
        ax.tick_params(axis='x', rotation=45)

    # 2) Heatmaps (3 subplots in a row)
    for i, cat in enumerate(CATEGORIES):
        ax = fig.add_subplot(4, 3, i + 4)  # Second row, 3 columns

        matrix: List[List[float]] = []
        for g in generators:
            row = []
            for e in evaluators:
                vals = by_gen_eval_cat.get((g, e, cat), [])
                _, m, _ = _group_stats(vals)
                row.append(m if m is not None else float("nan"))
            matrix.append(row)

        im = ax.imshow(matrix, vmin=0.5, vmax=1.0, aspect="auto", cmap="Greens")
        ax.set_title(f"Heatmap — {cat}")
        ax.set_xlabel("evaluator")
        ax.set_ylabel("generator")
        ax.set_xticks(list(range(len(evaluators))), labels=evaluators, rotation=45, ha="right")
        ax.set_yticks(list(range(len(generators))), labels=generators)

        # annotate values
        for i_gen, g in enumerate(generators):
            for j_eval, e in enumerate(evaluators):
                val = matrix[i_gen][j_eval]
                if not math.isnan(val):
                    ax.text(j_eval, i_gen, f"{val:.2f}", ha="center", va="center", fontsize=8, color="black")

    # 3) Evaluator strictness bar plots (3 subplots in a row)
    for i, cat in enumerate(CATEGORIES):
        ax = fig.add_subplot(4, 3, i + 7)  # Third row, 3 columns

        xs = evaluators
        means = []
        stds = []
        for e in xs:
            vals = by_eval_cat.get((e, cat), [])
            n, m, sd = _group_stats(vals)
            means.append(m if m is not None else float("nan"))
            stds.append(sd if sd is not None else float("nan"))

        y_min, y_max = get_y_limits(means)
        ax.bar(xs, means, yerr=stds, capsize=4, color="#8888cc")
        ax.set_ylim(y_min, y_max)
        ax.set_title(f"Evaluator strictness — {cat}")
        ax.set_ylabel("score")
        ax.set_xlabel("evaluator")
        ax.tick_params(axis='x', rotation=45)

    # 4) Composite ranking plot (spans 3 columns)
    cats_for_rank = ("structural", "semantic")
    gen_rank_data: List[Tuple[str, float, float, float]] = []  # generator, mean, std, composite
    for g in generators:
        vals: List[float] = []
        for cat in cats_for_rank:
            vals.extend(by_gen_cat.get((g, cat), []))
        n, m, sd = _group_stats(vals)
        comp = (m - sd) if (m is not None and sd is not None) else float("nan")
        gen_rank_data.append((g, m if m is not None else float("nan"), sd if sd is not None else float("nan"), comp))

    gen_rank_data.sort(key=lambda x: x[3], reverse=True)  # sort by composite descending

    xs = [x[0] for x in gen_rank_data]
    means = [x[1] for x in gen_rank_data]
    stds = [x[2] for x in gen_rank_data]
    comps = [x[3] for x in gen_rank_data]

    comp_min, comp_max = get_y_limits(comps)

    ax = fig.add_subplot(4, 1, 4)  # Fourth row, spans full width
    bars = ax.bar(xs, comps, color="#6a994e")
    ax.set_ylim(comp_min, comp_max)
    ax.set_title("Generator ranking (structural + semantic only)\nComposite = mean - std (higher is better)")
    ax.set_ylabel("Composite score")
    ax.set_xlabel("generator")

    # Annotate each bar with mean±std
    for i, (bar, m, sd) in enumerate(zip(bars, means, stds)):
        height = bar.get_height()
        if not math.isnan(m) and not math.isnan(sd):
            # Fixed text offset for 0.5-1.0 range
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                height + 0.01,
                f"{m:.3f}±{sd:.3f}",
                ha="center",
                va="bottom",
                fontsize=9,
                color="black",
                )

    # Add a colorbar for the heatmaps
    cbar_ax = fig.add_axes([0.92, 0.35, 0.02, 0.3])  # Position the colorbar on the right
    sm = plt.cm.ScalarMappable(cmap="Greens", norm=plt.Normalize(vmin=0.5, vmax=1.0))
    sm.set_array([])
    cbar = fig.colorbar(sm, cax=cbar_ax)
    cbar.set_label('Score', rotation=270, labelpad=15)

    plt.tight_layout(rect=[0, 0, 0.9, 1])  # Make room for colorbar
    fig.savefig(out_dir / "comprehensive_analysis.png", dpi=150, bbox_inches='tight')
    generated_files.append("comprehensive_analysis.png")
    plt.close(fig)

    # NEW: Scenario-specific analysis diagrams
    scenarios = sorted({r.scenario for r in score_rows})
    
    # 1) Scenario difficulty heatmap (scenarios vs generators)
    fig2, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))
    
    # Scenario vs Generator heatmap
    scenario_gen_matrix: List[List[float]] = []
    for scenario in scenarios:
        row = []
        for generator in generators:
            vals = []
            for (sc, gen, eval_), scores in by_scenario_gen_eval.items():
                if sc == scenario and gen == generator:
                    vals.extend(scores)
            
            _, m, _ = _group_stats(vals)
            row.append(m if m is not None else float("nan"))
        scenario_gen_matrix.append(row)
    
    im1 = ax1.imshow(scenario_gen_matrix, vmin=0.5, vmax=1.0, aspect="auto", cmap="RdYlGn")
    ax1.set_title("Scenario Performance by Generator\n(Green = High scores, Red = Low scores)")
    ax1.set_xlabel("Generator")
    ax1.set_ylabel("Scenario")
    ax1.set_xticks(list(range(len(generators))), labels=generators, rotation=45, ha="right")
    ax1.set_yticks(list(range(len(scenarios))), labels=scenarios)
    
    # Add values to heatmap
    for i, scenario in enumerate(scenarios):
        for j, generator in enumerate(generators):
            val = scenario_gen_matrix[i][j]
            if not math.isnan(val):
                ax1.text(j, i, f"{val:.2f}", ha="center", va="center", fontsize=8, color="black")
    
    # Scenario vs Evaluator heatmap
    scenario_eval_matrix: List[List[float]] = []
    for scenario in scenarios:
        row = []
        for evaluator in evaluators:
            vals = []
            for (sc, gen, eval_), scores in by_scenario_gen_eval.items():
                if sc == scenario and eval_ == evaluator:
                    vals.extend(scores)
            
            _, m, _ = _group_stats(vals)
            row.append(m if m is not None else float("nan"))
        scenario_eval_matrix.append(row)
    
    im2 = ax2.imshow(scenario_eval_matrix, vmin=0.5, vmax=1.0, aspect="auto", cmap="RdYlGn")
    ax2.set_title("Scenario Performance by Evaluator\n(Green = High scores, Red = Low scores)")
    ax2.set_xlabel("Evaluator")
    ax2.set_ylabel("Scenario")
    ax2.set_xticks(list(range(len(evaluators))), labels=evaluators, rotation=45, ha="right")
    ax2.set_yticks(list(range(len(scenarios))), labels=scenarios)
    
    # Add values to heatmap
    for i, scenario in enumerate(scenarios):
        for j, evaluator in enumerate(evaluators):
            val = scenario_eval_matrix[i][j]
            if not math.isnan(val):
                ax2.text(j, i, f"{val:.2f}", ha="center", va="center", fontsize=8, color="black")
    
    # Add colorbars
    plt.colorbar(im1, ax=ax1, label='Score')
    plt.colorbar(im2, ax=ax2, label='Score')
    
    plt.tight_layout()
    fig2.savefig(out_dir / "scenario_analysis_heatmaps.png", dpi=150, bbox_inches='tight')
    generated_files.append("scenario_analysis_heatmaps.png")
    plt.close(fig2)

    # 2) Scenario difficulty ranking bar chart
    fig3, ax = plt.subplots(figsize=(12, 8))
    
    scenario_difficulty: List[Tuple[str, float]] = []
    for scenario in scenarios:
        scenario_scores = []
        for (sc, gen, eval_), scores in by_scenario_gen_eval.items():
            if sc == scenario:
                scenario_scores.extend(scores)
        
        _, m, sd = _group_stats(scenario_scores)
        difficulty = (m - sd) if (m is not None and sd is not None) else float("nan")
        if not math.isnan(difficulty):
            scenario_difficulty.append((scenario, difficulty))
    
    scenario_difficulty.sort(key=lambda x: x[1])  # Sort by difficulty (ascending = harder)
    
    scenarios_sorted = [x[0] for x in scenario_difficulty]
    difficulties = [x[1] for x in scenario_difficulty]
    
    bars = ax.barh(scenarios_sorted, difficulties, color='coral')
    ax.set_xlabel('Difficulty Score (lower = more difficult)')
    ax.set_title('Scenario Difficulty Ranking\n(Difficulty = mean - std, lower values indicate harder scenarios)')
    ax.grid(axis='x', alpha=0.3)
    ax.set_xlim(0.7, 0.85)  # Set x-axis limits for better precision
    
    # Add values to bars
    for i, (bar, diff) in enumerate(zip(bars, difficulties)):
        width = bar.get_width()
        ax.text(width + 0.01, bar.get_y() + bar.get_height()/2, 
                f'{diff:.3f}', ha='left', va='center', fontsize=9)
    
    plt.tight_layout()
    fig3.savefig(out_dir / "scenario_difficulty_ranking.png", dpi=150, bbox_inches='tight')
    generated_files.append("scenario_difficulty_ranking.png")
    plt.close(fig3)

    # 3) Generator-Evaluator performance network diagram
    if len(generators) <= 5 and len(evaluators) <= 5:  # Only for smaller datasets
        fig4, ax = plt.subplots(figsize=(10, 8))
        
        # Calculate average code-quality performance for each generator-evaluator pair
        gen_eval_performance: Dict[Tuple[str, str], float] = {}
        for (gen, eval_, cat), scores in by_gen_eval_cat.items():
            if cat not in PRIMARY_CATEGORIES:
                continue
            key = (gen, eval_)
            if key not in gen_eval_performance:
                gen_eval_performance[key] = []
            gen_eval_performance[key].extend(scores)
        
        # Average across categories
        for key in list(gen_eval_performance.keys()):
            _, m, _ = _group_stats(gen_eval_performance[key])
            gen_eval_performance[key] = m if m is not None else float("nan")
        
        # Create network layout
        gen_positions = {gen: (i, 1) for i, gen in enumerate(generators)}
        eval_positions = {eval_: (i, 0) for i, eval_ in enumerate(evaluators)}
        
        # Draw nodes
        for gen, (x, y) in gen_positions.items():
            ax.scatter(x, y, s=500, c='lightblue', edgecolors='navy', linewidth=2)
            ax.text(x, y + 0.15, gen, ha='center', va='bottom', fontweight='bold')
        
        for eval_, (x, y) in eval_positions.items():
            ax.scatter(x, y, s=500, c='lightgreen', edgecolors='darkgreen', linewidth=2)
            ax.text(x, y - 0.15, eval_, ha='center', va='top', fontweight='bold')
        
        # Draw edges with thickness based on performance
        for (gen, eval_), perf in gen_eval_performance.items():
            if not math.isnan(perf):
                x1, y1 = gen_positions[gen]
                x2, y2 = eval_positions[eval_]
                
                # Edge thickness and color based on performance (more intuitive scaling)
                thickness = 1 + (perf - 0.5) * 8  # Base thickness 1, max 5
                alpha = 0.3 + (perf - 0.5) * 1.4  # Base alpha 0.3, max 1.0
                
                ax.plot([x1, x2], [y1, y2], 'gray', linewidth=thickness, alpha=alpha)
                
                # Add performance value on edge
                mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
                ax.text(mid_x, mid_y, f'{perf:.2f}', ha='center', va='center', 
                       bbox=dict(boxstyle='round,pad=0.3', facecolor='white', alpha=0.8))
        
        ax.set_xlim(-0.5, max(len(generators), len(evaluators)) - 0.5)
        ax.set_ylim(-0.5, 1.5)
        ax.set_aspect('equal')
        ax.axis('off')
        ax.set_title('Generator-Evaluator Performance Network\n(Thicker edges = better performance)')
        
        plt.tight_layout()
        fig4.savefig(out_dir / "generator_evaluator_network.png", dpi=150, bbox_inches='tight')
        generated_files.append("generator_evaluator_network.png")
        plt.close(fig4)

    return generated_files


def print_console_summary(score_rows: Sequence[ScoreRow]) -> None:
    # Build dicts for quick stats
    by_gen_cat: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    by_eval_cat: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    by_scenario_gen_cat: Dict[Tuple[str, str, str], List[float]] = defaultdict(list)
    by_gen_eval_cat: Dict[Tuple[str, str, str], List[float]] = defaultdict(list)
    
    for r in score_rows:
        by_gen_cat[(r.generator, r.category)].append(r.score)
        by_eval_cat[(r.evaluator, r.category)].append(r.score)
        by_scenario_gen_cat[(r.scenario, r.generator, r.category)].append(r.score)
        by_gen_eval_cat[(r.generator, r.evaluator, r.category)].append(r.score)

    generators = sorted({r.generator for r in score_rows})
    evaluators = sorted({r.evaluator for r in score_rows})

    # 1) Generator performance table (structural + semantic)
    print("\n=== Generator performance (mean ± std) ===")
    print(f"{'Generator':<10} {'Structural':<12} {'Semantic':<12} {'Composite':<12}")
    print("-" * 48)
    gen_rank: List[Tuple[str, float, float, float]] = []  # generator, mean_struct, mean_sem, composite
    for g in generators:
        s_vals = by_gen_cat.get((g, "structural"), [])
        sem_vals = by_gen_cat.get((g, "semantic"), [])
        _, m_s, sd_s = _group_stats(s_vals)
        _, m_sem, sd_sem = _group_stats(sem_vals)
        comp = None
        if (m_s is not None and sd_s is not None) and (m_sem is not None and sd_sem is not None):
            # composite = mean - std (higher better)
            comp = ((m_s - sd_s) + (m_sem - sd_sem)) / 2.0
        gen_rank.append((g, m_s if m_s is not None else float("nan"), m_sem if m_sem is not None else float("nan"), comp if comp is not None else float("nan")))

        s_str = f"{m_s:.3f}±{sd_s:.3f}" if (m_s is not None and sd_s is not None) else "N/A"
        sem_str = f"{m_sem:.3f}±{sd_sem:.3f}" if (m_sem is not None and sd_sem is not None) else "N/A"
        comp_str = f"{comp:.3f}" if comp is not None else "N/A"
        print(f"{g:<10} {s_str:<12} {sem_str:<12} {comp_str:<12}")

    # 2) Evaluator strictness (mean across categories)
    print("\n=== Evaluator strictness (mean ± std) ===")
    print(f"{'Evaluator':<10} {'Structural':<12} {'Semantic':<12} {'Specification':<14}")
    print("-" * 50)
    for e in evaluators:
        s_vals = by_eval_cat.get((e, "structural"), [])
        sem_vals = by_eval_cat.get((e, "semantic"), [])
        spec_vals = by_eval_cat.get((e, "specification"), [])
        _, m_s, sd_s = _group_stats(s_vals)
        _, m_sem, sd_sem = _group_stats(sem_vals)
        _, m_spec, sd_spec = _group_stats(spec_vals)

        s_str = f"{m_s:.3f}±{sd_s:.3f}" if (m_s is not None and sd_s is not None) else "N/A"
        sem_str = f"{m_sem:.3f}±{sd_sem:.3f}" if (m_sem is not None and sd_sem is not None) else "N/A"
        spec_str = f"{m_spec:.3f}±{sd_spec:.3f}" if (m_spec is not None and sd_spec is not None) else "N/A"
        print(f"{e:<10} {s_str:<12} {sem_str:<12} {spec_str:<14}")

    # 3) Recommendation: highest composite (structural+semantic)
    gen_rank.sort(key=lambda x: x[3], reverse=True)
    best = gen_rank[0]
    print("\n=== Recommendation (structural + semantic only) ===")
    print(f"Best generator: {best[0]} (composite={best[3]:.3f})")
    print(f"  Structural: {best[1]:.3f}, Semantic: {best[2]:.3f}")
    print("\nInterpretation: Composite = mean - std (higher is better).")
    print("A high composite means consistently high scores with low variability.")
    print("If you value deterministic, high-quality code, prioritize the top generator.")
    print("\nEvaluator notes:")
    print("- Lower evaluator scores indicate stricter grading.")
    print("- Consider the evaluator that best matches your quality standards.")

    # 4) Scenario difficulty ranking
    print("\n=== Scenario difficulty ranking (most difficult first) ===")
    print(f"{'Scenario':<12} {'Mean':<8} {'Std':<8} {'Difficulty':<12}")
    print("-" * 42)
    
    scenario_rank: List[Tuple[str, float, float, float]] = []  # scenario, mean, std, difficulty_score
    for scenario in sorted({r.scenario for r in score_rows}):
        scenario_scores: List[float] = []
        for (sc, gen, cat), vals in by_scenario_gen_cat.items():
            if sc == scenario and cat in PRIMARY_CATEGORIES:
                scenario_scores.extend(vals)
        
        _, m, sd = _group_stats(scenario_scores)
        difficulty_score = (m - sd) if (m is not None and sd is not None) else float("nan")
        scenario_rank.append((scenario, m if m is not None else float("nan"), sd if sd is not None else float("nan"), difficulty_score if not math.isnan(difficulty_score) else float("inf")))
    
    scenario_rank.sort(key=lambda x: x[3])  # Sort by difficulty_score ascending (lower = more difficult)
    
    for scenario, m, sd, diff in scenario_rank[:10]:  # Show top 10 most difficult
        if math.isnan(m) or math.isnan(sd) or math.isnan(diff):
            continue
        mean_str = f"{m:.3f}"
        std_str = f"{sd:.3f}"
        diff_str = f"{diff:.3f}"
        print(f"{scenario:<12} {mean_str:<8} {std_str:<8} {diff_str:<12}")
    
    if len(scenario_rank) > 10:
        print(f"... and {len(scenario_rank) - 10} more scenarios")
    
    print("\nInterpretation: Lower difficulty scores indicate more challenging scenarios.")
    print("Difficulty score = mean - std (lower means harder for generators).")
    print("\nAlternative whole-task difficulty (including specification score):")
    scenario_rank_with_spec: List[Tuple[str, float, float, float]] = []
    for scenario in sorted({r.scenario for r in score_rows}):
        scenario_scores: List[float] = []
        for (sc, gen, cat), vals in by_scenario_gen_cat.items():
            if sc == scenario and cat in CATEGORIES:
                scenario_scores.extend(vals)

        _, m, sd = _group_stats(scenario_scores)
        difficulty_score = (m - sd) if (m is not None and sd is not None) else float("nan")
        scenario_rank_with_spec.append(
            (
                scenario,
                m if m is not None else float("nan"),
                sd if sd is not None else float("nan"),
                difficulty_score if not math.isnan(difficulty_score) else float("inf"),
            )
        )

    scenario_rank_with_spec.sort(key=lambda x: x[3])
    for scenario, m, sd, diff in scenario_rank_with_spec[:10]:
        if math.isnan(m) or math.isnan(sd) or math.isnan(diff):
            continue
        print(f"  {scenario:<10} mean={m:.3f} std={sd:.3f} difficulty={diff:.3f}")
    print("This variant mixes generator quality with specification quality.")

    # 5) Self-favoritism analysis
    print("\n=== Self-favoritism analysis ===")
    gen_eval_means: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    
    for (gen, eval_, cat), scores in by_gen_eval_cat.items():
        if cat not in PRIMARY_CATEGORIES:
            continue
        _, m, _ = _group_stats(scores)
        gen_eval_means[(gen, eval_)].append(m if m is not None else float("nan"))
    
    # Average across categories for each generator-evaluator pair
    for key in list(gen_eval_means.keys()):
        values = gen_eval_means[key]
        gen_eval_means[key] = mean(values) if values else float("nan")
    
    print(f"{'Generator':<10} {'Evaluator':<10} {'Score':<8} {'Self?':<6}")
    print("-" * 36)
    
    self_favoritism: List[Tuple[str, str, float, bool]] = []
    for (gen, eval_), mean_score in gen_eval_means.items():
        is_self = gen == eval_
        self_favoritism.append((gen, eval_, mean_score, is_self))
        status = "YES" if is_self else "NO"
        score_str = f"{mean_score:.3f}" if not math.isnan(mean_score) else "N/A"
        print(f"{gen:<10} {eval_:<10} {score_str:<8} {status:<6}")
    
    # Statistical analysis of self-favoritism
    self_scores = [score for gen, eval_, score, is_self in self_favoritism if is_self and not math.isnan(score)]
    other_scores = [score for gen, eval_, score, is_self in self_favoritism if not is_self and not math.isnan(score)]
    
    if self_scores and other_scores:
        _, self_mean, self_std = _group_stats(self_scores)
        _, other_mean, other_std = _group_stats(other_scores)
        
        print(f"\nSelf-evaluation scores:  {self_mean:.3f} ± {self_std:.3f} (n={len(self_scores)})")
        print(f"Cross-evaluation scores: {other_mean:.3f} ± {other_std:.3f} (n={len(other_scores)})")
        
        if self_mean > other_mean:
            diff = self_mean - other_mean
            print(f"Potential self-favoritism detected: +{diff:.3f} points higher for self-evaluation")
        elif self_mean < other_mean:
            diff = other_mean - self_mean
            print(f"Models are harsher on themselves: -{diff:.3f} points lower for self-evaluation")
        else:
            print("No significant difference between self and cross evaluation")
    
    # Performance metrics explanation
    print("\n=== Performance metrics explanation ===")
    print("• Scores range from 0.5 to 1.0 (higher = better)")
    print("• Structural: Code structure, syntax, organization")
    print("• Semantic: Logic correctness, functionality, requirements")
    print("• Specification: Adherence to requirements, completeness")
    print("• Composite score = mean - std (rewards consistency)")
    print("• Difficulty score = mean - std (lower = harder scenarios)")
    print("\nEvaluator behavior:")
    print("• Strict evaluators give lower scores across all generators")
    print("• Lenient evaluators give higher scores across all generators")
    print("• Consistent evaluators have low standard deviation")


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate llm_score JSON files from final_tests/artrifacts")
    parser.add_argument(
        "--artifacts-dir",
        default=str(Path(__file__).resolve().parent / "artrifacts"),
        help="Path to final_tests/artrifacts",
    )
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parent / "analysis_out" / "llm_score_aggregate"),
        help="Output directory for CSV/plots",
    )
    parser.add_argument(
        "--exclude-scenarios",
        nargs="*",
        default=[],
        help="Scenarios to exclude from analysis (e.g., trycatch)",
    )
    args = parser.parse_args()

    artifacts_dir = Path(args.artifacts_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    score_files = discover_llm_score_files(artifacts_dir)
    
    # Filter out excluded scenarios
    if args.exclude_scenarios:
        original_count = len(score_files)
        score_files = [
            f for f in score_files 
            if parse_path_metadata(artifacts_dir, f)[0] not in args.exclude_scenarios
        ]
        print(f"Excluded {original_count - len(score_files)} files from scenarios: {args.exclude_scenarios}")
    
    if not score_files:
        print(f"No llm_score json files found under: {artifacts_dir}")
        return 2

    all_scores: List[ScoreRow] = []
    all_breakdowns: List[BreakdownRow] = []
    errors: List[Tuple[str, str]] = []

    for p in score_files:
        try:
            payload = load_json(p)
            srows, brows = extract_rows(artifacts_dir, p, payload)
            all_scores.extend(srows)
            all_breakdowns.extend(brows)
        except Exception as e:
            errors.append((str(p), repr(e)))

    write_scores_csv(all_scores, out_dir / "scores_long.csv")
    write_breakdown_csv(all_breakdowns, out_dir / "breakdown_long.csv")

    summaries = summarize_scores(all_scores)
    write_dicts_csv(
        summaries["summary_generator"],
        out_dir / "summary_generator.csv",
        fieldnames=["generator", "category", "n", "mean", "std"],
        )
    write_dicts_csv(
        summaries["summary_generator_evaluator"],
        out_dir / "summary_generator_evaluator.csv",
        fieldnames=["generator", "evaluator", "category", "n", "mean", "std"],
        )
    write_dicts_csv(
        summaries["summary_evaluator"],
        out_dir / "summary_evaluator.csv",
        fieldnames=["evaluator", "category", "n", "mean", "std"],
        )
    write_dicts_csv(
        summaries["summary_scenario"],
        out_dir / "summary_scenario.csv",
        fieldnames=["scenario", "n", "mean", "std", "difficulty_score", "categories_used"],
        )
    write_dicts_csv(
        summaries["summary_scenario_with_spec"],
        out_dir / "summary_scenario_with_spec.csv",
        fieldnames=["scenario", "n", "mean", "std", "difficulty_score", "categories_used"],
        )
    
    # Self-favoritism analysis CSV
    generators = sorted({r.generator for r in all_scores})
    evaluators = sorted({r.evaluator for r in all_scores})
    gen_eval_means: Dict[Tuple[str, str], float] = {}
    
    # Build generator-evaluator pairs from all_scores
    by_gen_eval_cat_main: Dict[Tuple[str, str, str], List[float]] = defaultdict(list)
    for r in all_scores:
        by_gen_eval_cat_main[(r.generator, r.evaluator, r.category)].append(r.score)
    
    for (gen, eval_, cat), scores in by_gen_eval_cat_main.items():
        if cat not in PRIMARY_CATEGORIES:
            continue
        _, m, _ = _group_stats(scores)
        key = (gen, eval_)
        if key not in gen_eval_means:
            gen_eval_means[key] = []
        gen_eval_means[key].append(m if m is not None else float("nan"))
    
    # Average across categories for each generator-evaluator pair
    for key in list(gen_eval_means.keys()):
        values = gen_eval_means[key]
        gen_eval_means[key] = mean(values) if values else float("nan")
    
    self_favoritism_data = []
    for (gen, eval_), mean_score in gen_eval_means.items():
        self_favoritism_data.append({
            "generator": gen,
            "evaluator": eval_,
            "mean_score": mean_score,
            "is_self_evaluation": gen == eval_,
            "categories_used": "+".join(PRIMARY_CATEGORIES),
        })
    
    write_dicts_csv(
        self_favoritism_data,
        out_dir / "self_favoritism_analysis.csv",
        fieldnames=["generator", "evaluator", "mean_score", "is_self_evaluation", "categories_used"],
        )
    
    write_dicts_csv(
        summaries["agreement"],
        out_dir / "evaluator_agreement_pearson.csv",
        fieldnames=["category", "evaluator_a", "evaluator_b", "n_pairs", "pearson"],
        )

    # Save errors (e.g., malformed json)
    if errors:
        with (out_dir / "errors.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["path", "error"])
            for path, err in errors:
                w.writerow([path, err])

    generated_plots = try_make_plots(all_scores, out_dir / "plots")
    
    # Generate the structural vs semantic comparison plot
    try:
        comparison_plot = plot_structural_vs_semantic_comparison(all_scores, out_dir / "plots")
        generated_plots.append("structural_vs_semantic_comparison.png")
    except Exception as e:
        print(f"Warning: Could not generate structural vs semantic comparison plot: {e}")

    # Console summary
    print_console_summary(all_scores)

    generators = sorted({r.generator for r in all_scores})
    evaluators = sorted({r.evaluator for r in all_scores})

    print(f"\nAggregated {len(score_files)} files")
    print(f"Score rows: {len(all_scores)}")
    print(f"Breakdown rows: {len(all_breakdowns)}")
    print(f"Output: {out_dir}")
    if generated_plots:
        print(f"Generated diagrams:")
        plot_descriptions = {
            "comprehensive_analysis.png": "generator/evaluator comparison",
            "scenario_analysis_heatmaps.png": "scenario vs generator/evaluator heatmaps",
            "scenario_difficulty_ranking.png": "hardest scenarios ranking",
            "generator_evaluator_network.png": "performance network diagram",
            "structural_vs_semantic_comparison.png": "structural vs semantic scores with error bars",
        }
        for plot_name in generated_plots:
            description = plot_descriptions.get(plot_name, "plot")
            print(f"  - {plot_name} ({description})")
    else:
        print("Generated diagrams: skipped (matplotlib not available in the current runtime)")
    if errors:
        print(f"WARN: {len(errors)} files failed to parse. See errors.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
