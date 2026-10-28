#!/usr/bin/env python3
"""Synthetic PySR demo. Real records are not suitable without observation coverage.

Examples:
  python pysr_experiment.py --demo
"""

from __future__ import annotations

import argparse
import tempfile

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Demo data: a known rule for activity, so we can check PySR rediscovers it.
#   y = 1.2 + 0.9 * sin(2*pi*(hour - 6)/24) + 3.0 / (1 + hours_since_food) + noise
# --------------------------------------------------------------------------

def build_demo_frame(n_days: int = 60, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    last_food_time = -rng.uniform(0, 3)
    for day in range(n_days):
        weekend = 1.0 if day % 7 in (5, 6) else 0.0
        for hour in range(24):
            # A meal resets the clock at the beginning of this synthetic hour.
            food_this_hour = float(hour in (7, 18) and rng.random() < 0.8)
            current_time = day * 24 + hour
            if food_this_hour:
                last_food_time = current_time
            hours_since_food = current_time - last_food_time
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

def run_pysr(frame: pd.DataFrame, niterations: int, seed: int, output_directory: str) -> None:
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
        output_directory=output_directory,
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
    print(f"R^2 on training data only: {r2:.3f}")
    print("\n合成データの学習結果です。実記録の傾向や予測精度は示していません。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="use synthetic demo data")
    parser.add_argument("--niterations", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.niterations < 1:
        parser.error("--niterations must be positive")
    print("source: synthetic demo; training R^2 is not a validation score")
    # PySR persists equations and model state by default. Keep the demo out of the repo.
    with tempfile.TemporaryDirectory(prefix="project-meme-pysr-") as output_directory:
        run_pysr(build_demo_frame(), args.niterations, args.seed, output_directory)


if __name__ == "__main__":
    main()
