#!/usr/bin/env python3
"""project-meme x PySR experiment: discover readable equations from Meme's records.

PySR (symbolic regression) searches for *equations* that fit the data, instead of
a black-box model. In project-meme terms, a discovered equation is a candidate
"hypothesis" you can register in the dictionary and check against new evidence.

Data sources (pick one):
  --demo                 synthetic data with a known ground-truth rule (default
                         when no other source is given). Use this to see what a
                         successful discovery looks like.
  --records records.json the JSON inside the ZIP produced by project-meme's own
                         export feature (tables: health_entries, observations).
  --csv health.csv       the health CSV produced by project-meme's CSV export.

Targets:
  --target activity      hourly count of observations (a proxy for how active /
                         vocal Meme was), from: hour of day, hours since the
                         last food event, food/water/toilet events so far today,
                         weekend flag.
  --target vomit         daily vomit count, from: that day's food/water/toilet
                         event counts and the mean gap between food events.
                         (meal_interval_minutes on vomit rows is NOT used: it is
                         recorded at vomit time, so it would leak the answer.)

Sample rows (is_sample = true) are excluded by default; pass --include-sample
to keep them.

Examples:
  python pysr_experiment.py --demo
  python pysr_experiment.py --records records.json --target activity
  python pysr_experiment.py --records records.json --target vomit --niterations 100
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

JST = "Asia/Tokyo"


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def load_records_json(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (health_entries, observations) DataFrames from an export records.json."""
    data = json.loads(path.read_text(encoding="utf-8"))
    tables = data["tables"]
    health = pd.DataFrame(tables.get("health_entries", []))
    observations = pd.DataFrame(tables.get("observations", []))
    return health, observations


def load_health_csv(path: Path) -> pd.DataFrame:
    """Map project-meme's Japanese health CSV onto the health_entries schema."""
    raw = pd.read_csv(path, encoding="utf-8-sig")
    label_to_type = {
        "嘔吐": "vomit", "食事": "food", "水分": "water",
        "トイレ": "toilet", "体重": "weight", "元気": "energy", "その他": "other",
    }
    health = pd.DataFrame()
    health["occurred_at"] = raw["日時（日本時間）"]
    health["event_type"] = raw["種類"].map(label_to_type).fillna("other")
    health["count"] = raw["回数"]
    return health


def drop_samples(df: pd.DataFrame, include_sample: bool) -> pd.DataFrame:
    if include_sample or "is_sample" not in df.columns:
        return df
    return df[~df["is_sample"].astype(bool)].copy()


# --------------------------------------------------------------------------
# Feature engineering
# --------------------------------------------------------------------------

def build_activity_frame(health: pd.DataFrame, observations: pd.DataFrame) -> pd.DataFrame:
    """One row per hour: how many observations were logged, and what led up to it."""
    if observations.empty:
        raise SystemExit("observations が空です。--target vomit を試すか、エクスポートを確認してください。")
    obs = observations.copy()
    obs["occurred_at"] = pd.to_datetime(obs["occurred_at"], utc=True).dt.tz_convert(JST)
    obs["hour_bin"] = obs["occurred_at"].dt.floor("h")
    counts = obs.groupby("hour_bin").size().rename("obs_count")

    start = obs["hour_bin"].min()
    end = obs["hour_bin"].max()
    hours = pd.date_range(start, end, freq="h", tz=JST)
    frame = pd.DataFrame(index=hours)
    frame["obs_count"] = counts.reindex(hours).fillna(0).astype(float)

    health = health.copy()
    if not health.empty:
        health["occurred_at"] = pd.to_datetime(health["occurred_at"], utc=True).dt.tz_convert(JST)
        food_times = np.sort(
            health.loc[health["event_type"] == "food", "occurred_at"].astype("int64").to_numpy()
        )
        for kind in ("food", "water", "toilet"):
            times = health.loc[health["event_type"] == kind, "occurred_at"]
            per_hour = times.dt.floor("h").value_counts().sort_index()
            frame[f"{kind}_this_hour"] = per_hour.reindex(hours).fillna(0).astype(float)
    else:
        food_times = np.array([], dtype="int64")
        for kind in ("food", "water", "toilet"):
            frame[f"{kind}_this_hour"] = 0.0

    hour_int64 = hours.astype("int64").to_numpy()
    if len(food_times):
        pos = np.searchsorted(food_times, hour_int64, side="right") - 1
        last_food = np.where(pos >= 0, food_times[np.maximum(pos, 0)], hour_int64[0])
        frame["hours_since_food"] = np.where(
            pos >= 0, (hour_int64 - last_food) / 3.6e12, 24.0
        )
    else:
        frame["hours_since_food"] = 24.0

    frame["hour"] = hours.hour.astype(float)
    frame["is_weekend"] = (hours.dayofweek >= 5).astype(float)
    frame = frame.reset_index(drop=True)
    feature_cols = ["hour", "hours_since_food", "food_this_hour",
                    "water_this_hour", "toilet_this_hour", "is_weekend"]
    return frame[feature_cols + ["obs_count"]].rename(columns={"obs_count": "y"})


def build_vomit_frame(health: pd.DataFrame) -> pd.DataFrame:
    """One row per day: did the day's feeding pattern line up with vomiting?"""
    if health.empty:
        raise SystemExit("health_entries が空です。")
    h = health.copy()
    h["occurred_at"] = pd.to_datetime(h["occurred_at"], utc=True).dt.tz_convert(JST)
    h["day"] = h["occurred_at"].dt.floor("D")
    days = pd.date_range(h["day"].min(), h["day"].max(), freq="D", tz=JST)
    frame = pd.DataFrame(index=days)

    for kind in ("food", "water", "toilet"):
        per_day = h.loc[h["event_type"] == kind].groupby("day").size()
        frame[f"{kind}_count"] = per_day.reindex(days).fillna(0).astype(float)

    vomit = h.loc[h["event_type"] == "vomit"].copy()
    if not vomit.empty:
        vomit["count"] = vomit["count"].fillna(1)
        frame["y"] = vomit.groupby("day")["count"].sum().reindex(days).fillna(0).astype(float)
    else:
        raise SystemExit("嘔吐の記録がありません。--target activity を試してください。")

    gaps = (
        h.loc[h["event_type"] == "food"]
        .sort_values("occurred_at")
        .groupby("day")["occurred_at"]
        .apply(lambda s: s.diff().dt.total_seconds().div(3600).mean())
    )
    frame["meal_gap_mean_h"] = gaps.reindex(days)
    frame["meal_gap_mean_h"] = frame["meal_gap_mean_h"].fillna(frame["meal_gap_mean_h"].median())
    frame["dayofweek"] = days.dayofweek.astype(float)
    frame = frame.reset_index(drop=True)
    cols = ["food_count", "water_count", "toilet_count", "meal_gap_mean_h", "dayofweek"]
    return frame[cols + ["y"]]


# --------------------------------------------------------------------------
# Demo data: a known rule for activity, so we can check PySR rediscovers it.
#   y = 1.2 + 0.9 * sin(2*pi*(hour - 6)/24) + 3.0 / (1 + hours_since_food) + noise
# --------------------------------------------------------------------------

def build_demo_frame(n_days: int = 60, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for day in range(n_days):
        weekend = 1.0 if day % 7 in (5, 6) else 0.0
        last_food_hour = -rng.uniform(0, 3)
        for hour in range(24):
            # meals at roughly 7:00 and 18:00, jittered
            if abs(hour - 7) < 1 or abs(hour - 18) < 1:
                if rng.random() < 0.8:
                    last_food_hour = hour - rng.uniform(0, 0.5)
            hours_since_food = hour - last_food_hour
            food_this_hour = 1.0 if hour in (7, 18) and rng.random() < 0.8 else 0.0
            signal = (
                1.2
                + 0.9 * np.sin(2 * np.pi * (hour - 6) / 24)
                + 3.0 / (1 + hours_since_food)
                + 0.3 * weekend
            )
            y = max(0.0, signal + rng.normal(0, 0.25))
            rows.append({
                "hour": float(hour),
                "hours_since_food": float(hours_since_food),
                "food_this_hour": food_this_hour,
                "water_this_hour": float(rng.integers(0, 2)),
                "toilet_this_hour": float(rng.integers(0, 2)),
                "is_weekend": weekend,
                "y": y,
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Symbolic regression
# --------------------------------------------------------------------------

def run_pysr(frame: pd.DataFrame, niterations: int, seed: int) -> None:
    from pysr import PySRRegressor

    feature_names = [c for c in frame.columns if c != "y"]
    X = frame[feature_names].to_numpy(dtype=float)
    y = frame["y"].to_numpy(dtype=float)
    print(f"rows={len(frame)}  features={feature_names}  target=y")
    print(f"y: mean={y.mean():.3f}  std={y.std():.3f}  zeros={(y == 0).mean():.0%}")

    model = PySRRegressor(
        niterations=niterations,
        maxsize=20,
        binary_operators=["+", "-", "*", "/"],
        unary_operators=["sin", "cos"],
        model_selection="best",
        random_state=seed,
        deterministic=True,
        parallelism="serial",
        progress=True,
        verbosity=0,
    )
    model.fit(X, y, variable_names=feature_names)

    equations = model.equations_
    print("\n=== discovered equations (simple -> complex) ===")
    cols = [c for c in ("complexity", "loss", "score", "equation") if c in equations.columns]
    print(equations[cols].to_string(index=False))

    pred = model.predict(X)
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    print(f"\nselected equation: {model.sympy()}")
    print(f"R^2 on all data: {r2:.3f}")
    print("\nこの式は「仮説」の候補です。project-meme の辞書に仮説として登録し、"
          "新しい観察で支持/反証を積み上げるのが次のステップになります。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="use synthetic demo data")
    parser.add_argument("--records", type=Path, help="records.json from project-meme export ZIP")
    parser.add_argument("--csv", type=Path, help="health CSV from project-meme CSV export (vomit target only)")
    parser.add_argument("--target", choices=["activity", "vomit"], default="activity")
    parser.add_argument("--niterations", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--include-sample", action="store_true")
    args = parser.parse_args()

    if args.records:
        health, observations = load_records_json(args.records)
        health = drop_samples(health, args.include_sample)
        observations = drop_samples(observations, args.include_sample)
        frame = (build_activity_frame(health, observations) if args.target == "activity"
                 else build_vomit_frame(health))
        source = f"records.json ({args.records})"
    elif args.csv:
        if args.target != "vomit":
            raise SystemExit("CSV には健康記録しか含まれないため --target vomit 専用です。")
        frame = build_vomit_frame(load_health_csv(args.csv))
        source = f"CSV ({args.csv})"
    else:
        if args.target != "activity":
            raise SystemExit("デモデータは --target activity 専用です。")
        frame = build_demo_frame()
        source = "demo (ground truth: 1.2 + 0.9*sin(2*pi*(hour-6)/24) + 3.0/(1+hours_since_food) + 0.3*is_weekend)"

    print(f"source: {source}  target: {args.target}")
    run_pysr(frame, args.niterations, args.seed)


if __name__ == "__main__":
    main()
