"""Price vs condition analysis.

Fits an ordinary least squares model on log(price):

    log(price) = b0 + b1*age + b2*mileage_10k_mi + b3*damage + b4*manual + generation dummies (+ photo severity)

Coefficients on log(price) read as approximate % change: b=-0.05 → about -5% per unit.
Writes reports/report.md (+ PNG charts when matplotlib is installed) and
reports/residuals.csv (lots sold cheapest / most expensive relative to the model).
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


def load(data_dir: str | Path = "data") -> pd.DataFrame:
    path = Path(data_dir) / "lots.json"
    lots = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    rows = []
    for lot in lots:
        pdmg = lot.get("photo_damage") or {}
        rows.append(
            {
                "lot_id": lot.get("lot_id"),
                "url": lot.get("url"),
                "title": lot.get("title"),
                "sale_date": lot.get("sale_date_iso"),
                "sale_status": lot.get("sale_status"),
                "year": lot.get("year"),
                "generation": lot.get("generation"),
                "price": lot.get("price_usd"),
                "mileage_mi": lot.get("mileage_mi"),
                "transmission": lot.get("transmission_norm"),
                "primary_damage": lot.get("primary_damage"),
                "damage_score": lot.get("text_damage_score"),
                "photo_severity": pdmg.get("severity"),
                "repair_cost": lot.get("repair_cost_usd"),
            }
        )
    df = pd.DataFrame(rows, columns=["lot_id", "url", "title", "sale_date", "sale_status", "year", "generation", "price",
                                     "mileage_mi", "transmission", "primary_damage", "damage_score", "photo_severity", "repair_cost"])
    for c in ("year", "price", "mileage_mi", "damage_score", "photo_severity", "repair_cost"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def ols(X: pd.DataFrame, y: pd.Series) -> dict:
    Xm = np.column_stack([np.ones(len(X)), X.to_numpy(float)])
    yv = y.to_numpy(float)
    beta, *_ = np.linalg.lstsq(Xm, yv, rcond=None)
    resid = yv - Xm @ beta
    n, k = Xm.shape
    dof = max(n - k, 1)
    sigma2 = resid @ resid / dof
    try:
        se = np.sqrt(np.diag(sigma2 * np.linalg.pinv(Xm.T @ Xm)))
    except np.linalg.LinAlgError:
        se = np.full(k, np.nan)
    ss_tot = ((yv - yv.mean()) ** 2).sum()
    r2 = 1 - (resid @ resid) / ss_tot if ss_tot else float("nan")
    names = ["intercept", *X.columns]
    return {"coef": dict(zip(names, beta)), "se": dict(zip(names, se)), "r2": r2, "n": n, "resid": resid, "fitted": Xm @ beta}


def build_design(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    d = df.dropna(subset=["price", "year"]).copy()
    d = d[d["price"] > 100]
    d["age"] = date.today().year - d["year"]
    d["mileage_10k"] = d["mileage_mi"].fillna(d["mileage_mi"].median()) / 10_000
    d["damage"] = d["damage_score"].fillna(d["damage_score"].median() if d["damage_score"].notna().any() else 0.4)
    d["manual"] = (d["transmission"] == "manual").astype(float)
    cols = ["age", "mileage_10k", "damage", "manual"]
    if d["photo_severity"].notna().mean() >= 0.5:
        d["photo_sev"] = d["photo_severity"].fillna(d["photo_severity"].median())
        cols.append("photo_sev")
    gens = sorted(g for g in d["generation"].dropna().unique())
    for g in gens[1:]:  # first generation is the baseline
        d[f"gen_{g}"] = (d["generation"] == g).astype(float)
        cols.append(f"gen_{g}")
    X = d[cols]
    X = X.loc[:, X.std() > 0]  # drop constant columns (e.g. only one transmission scraped)
    return d, X, np.log(d["price"])


EXPLAIN = {
    "age": "кожен рік віку",
    "mileage_10k": "кожні +10 000 миль пробігу",
    "damage": "damage score 0→1 (з кодів пошкоджень аукціону)",
    "manual": "механіка vs автомат",
    "photo_sev": "+1 бал серйозності за фото (0-10)",
}


def report(data_dir: str | Path = "data", out_dir: str | Path = "reports") -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    df = load(data_dir)
    lines = ["# Mazda MX-5: ціна vs стан (bidfax.info)", "", f"Згенеровано: {date.today().isoformat()}", ""]
    lines += [f"Лотів у базі: **{len(df)}**, з ціною: **{df['price'].notna().sum()}**", ""]

    if df["price"].notna().sum() == 0:
        lines.append("_Даних із ціною ще немає — спершу запустіть `python -m carsearch scrape`._")
        text = "\n".join(lines)
        (out / "report.md").write_text(text, encoding="utf-8")
        return text

    # descriptive tables
    def table(group: str) -> list[str]:
        g = df.dropna(subset=["price"]).groupby(group)["price"].agg(["count", "median", "mean", "min", "max"])
        rows = [f"| {group} | n | медіана $ | середня $ | min $ | max $ |", "|---|---|---|---|---|---|"]
        for idx, r in g.iterrows():
            rows.append(f"| {idx} | {int(r['count'])} | {r['median']:.0f} | {r['mean']:.0f} | {r['min']:.0f} | {r['max']:.0f} |")
        return rows

    for grp, title in (("generation", "За поколінням"), ("transmission", "За коробкою"), ("year", "За роком"), ("primary_damage", "За основним пошкодженням")):
        if df[grp].notna().any():
            lines += [f"## {title}", "", *table(grp), ""]

    d, X, y = build_design(df)
    if len(d) >= X.shape[1] + 5:
        m = ols(X, y)
        lines += ["## Регресія log(ціна)", "", f"n = {m['n']}, R² = {m['r2']:.3f}", "",
                  "| фактор | коеф. | ± SE | ефект на ціну | пояснення |", "|---|---|---|---|---|"]
        for name, b in m["coef"].items():
            if name == "intercept":
                continue
            eff = (math.exp(b) - 1) * 100
            lines.append(f"| {name} | {b:+.4f} | {m['se'][name]:.4f} | {eff:+.1f}% | {EXPLAIN.get(name, 'покоління vs базове')} |")
        lines += ["", "_Ефект = зміна ціни при зміні фактора на 1 одиницю за інших рівних._", ""]

        d = d.assign(fitted_price=np.exp(m["fitted"]), resid=m["resid"])
        d["vs_model_pct"] = (np.exp(d["resid"]) - 1) * 100
        cols = ["lot_id", "year", "transmission", "mileage_mi", "primary_damage", "damage_score", "photo_severity", "price", "fitted_price", "vs_model_pct", "url"]
        d.sort_values("resid")[cols].to_csv(out / "residuals.csv", index=False)
        lines += ["## Найдешевші відносно моделі (потенційно вигідні)", "", "| лот | рік | КПП | пробіг mi | пошкодження | ціна $ | модель $ | Δ |", "|---|---|---|---|---|---|---|---|"]
        for _, r in d.sort_values("resid").head(10).iterrows():
            lines.append(
                f"| [{r['lot_id']}]({r['url']}) | {r['year']:.0f} | {r['transmission'] or '?'} | "
                f"{'' if pd.isna(r['mileage_mi']) else f'{r.mileage_mi:.0f}'} | {r['primary_damage'] or ''} | "
                f"{r['price']:.0f} | {r['fitted_price']:.0f} | {r['vs_model_pct']:+.0f}% |"
            )
        lines.append("")
    else:
        lines += ["_Замало даних для регресії (потрібно хоча б ~15 лотів з ціною)._", ""]

    _charts(d, out, lines)
    text = "\n".join(lines)
    (out / "report.md").write_text(text, encoding="utf-8")
    return text


def _charts(d: pd.DataFrame, out: Path, lines: list[str]):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    for ax, (col, label) in zip(axes, [("year", "Рік"), ("mileage_mi", "Пробіг, миль"), ("damage_score", "Damage score")]):
        for tr, marker in (("manual", "o"), ("automatic", "s")):
            s = d[d["transmission"] == tr]
            ax.scatter(s[col], s["price"], alpha=0.6, marker=marker, label=tr, s=18)
        ax.set_xlabel(label)
        ax.set_ylabel("Ціна, $")
        ax.grid(alpha=0.3)
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(out / "price_scatter.png", dpi=110)
    plt.close(fig)
    lines += ["## Графіки", "", "![price scatter](price_scatter.png)", ""]
