"""Appended after CONFIG; standalone, editable and executed only after approval."""
import csv
import hashlib
import html
import importlib.metadata
import math
import statistics
from pathlib import Path


def run_analysis(config):
    try:
        version = importlib.metadata.version("scipy")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("请为所选 Python 安装 scipy==1.15.3，然后重新生成并审批计划。") from exc
    if version != "1.15.3":
        raise RuntimeError("此模板固定 scipy==1.15.3；请核对分析环境后重新审批。")
    from scipy import stats
    source = Path(config["file"])
    if hashlib.sha256(source.read_bytes()).hexdigest() != config["input_sha256"]:
        raise ValueError("Input changed since template generation")
    rows, dropped, total = [], 0, 0
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t" if source.suffix.lower() == ".tsv" else ",")
        for row in reader:
            total += 1
            if total > 100000 or None in row or any(v is None for v in row.values()):
                raise ValueError("Malformed CSV or more than 100000 rows")
            values = [row[c].strip() for c in config["columns"]]
            if any(v == "" for v in values):
                if config["missing"] == "error":
                    raise ValueError(f"Missing value in input row {total + 1}")
                dropped += 1
                continue
            numbers = [float(v) for v in values]
            if not all(math.isfinite(v) for v in numbers):
                raise ValueError("Nonfinite values require manual correction; they are never silently discarded")
            rows.append(numbers)
    if not rows:
        raise ValueError("No complete numeric observations")
    result = {"schema": "rsci.statistics/1", "template": config["template"], "parameters": config,
              "n": len(rows), "input_rows": total, "dropped_missing_rows": dropped,
              "dependencies": {"scipy": version}, "scientific_status": "not_assessed",
              "limitations": ["Units and independence of observations have not been verified.",
                              "Correlation or regression is not evidence of causality."]}
    if config["template"] == "descriptive":
        result["statistics"] = {}
        for i, col in enumerate(config["columns"]):
            nums = [row[i] for row in rows]
            result["statistics"][col] = {"mean": statistics.fmean(nums), "median": statistics.median(nums),
                "sample_std": statistics.stdev(nums) if len(nums) > 1 else None, "min": min(nums), "max": max(nums)}
    else:
        x, y = [r[0] for r in rows], [r[1] for r in rows]
        if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
            raise ValueError("At least three complete observations and two nonconstant columns are required")
        if config["template"] == "correlation":
            pearson, spearman = stats.pearsonr(x, y), stats.spearmanr(x, y)
            result["statistics"] = {"pearson_r": float(pearson.statistic), "pearson_p": float(pearson.pvalue),
                                    "spearman_rho": float(spearman.statistic), "spearman_p": float(spearman.pvalue)}
            result["limitations"].append("Pearson assesses linear association; Spearman assesses monotonic association; p-values depend on sampling assumptions.")
        else:
            fit = stats.linregress(x, y)
            radius = float(stats.t.ppf(.975, len(x) - 2)) * float(fit.stderr)
            result["statistics"] = {"slope": float(fit.slope), "intercept": float(fit.intercept),
                "r_squared": float(fit.rvalue ** 2), "p_value": float(fit.pvalue), "slope_stderr": float(fit.stderr),
                "slope_95_ci": [float(fit.slope - radius), float(fit.slope + radius)]}
            result["limitations"].append("OLS interval assumes independent, homoscedastic errors and an appropriate linear model; assumptions have not been tested.")
    out = Path("results"); out.mkdir(exist_ok=True)
    (out / "statistics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (out / "report.md").write_text("# Analysis results\n\n```json\n" + json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n```\n", encoding="utf-8")
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="480" viewBox="0 0 800 480">', '<rect width="800" height="480" fill="white"/>',
           '<path d="M80 40V400H750" fill="none" stroke="#172334"/>']
    if len(config["columns"]) >= 2:
        x, y = [r[0] for r in rows], [r[1] for r in rows]
        def sx(v): return 90 + (v - min(x)) / (max(x) - min(x) or 1) * 640
        def sy(v): return 390 - (v - min(y)) / (max(y) - min(y) or 1) * 330
        step = max(1, math.ceil(len(rows) / 3000))
        for a, b, *_ in rows[::step]:
            svg.append(f'<circle cx="{sx(a):.3f}" cy="{sy(b):.3f}" r="2" fill="#165bb6"/>')
        if config["template"] == "regression":
            svg.append(f'<path d="M{sx(min(x))} {sy(fit.intercept+fit.slope*min(x))}L{sx(max(x))} {sy(fit.intercept+fit.slope*max(x))}" stroke="#b42328"/>')
        svg += [f'<text x="80" y="430" fill="#172334">X: {html.escape(config["columns"][0])} [{min(x):.5g}, {max(x):.5g}]</text>',
                f'<text x="80" y="455" fill="#172334">Y: {html.escape(config["columns"][1])} [{min(y):.5g}, {max(y):.5g}] · plotted every {step} row(s)</text>']
    else:
        nums = [r[0] for r in rows]
        svg.append(f'<text x="110" y="200" fill="#172334">{html.escape(config["columns"][0])}: n={len(nums)}, mean={statistics.fmean(nums):.6g}</text>')
    svg.append('</svg>')
    (out / "plot.svg").write_text("\n".join(svg), encoding="utf-8")
    return result


if __name__ == "__main__":
    import json
    print(json.dumps(run_analysis(CONFIG), ensure_ascii=False, allow_nan=False))
