"""Summarize measured durations from JSONL; never substitutes estimates for measurements.

Each line: {"metric":"speech_end_to_first_audible_ms","duration_ms":812.3}
Use actual device timestamps, and record meaningful speech separately from filler.
"""
import argparse
import json
import math
from collections import defaultdict
from statistics import median


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        value = float(row["duration_ms"])
        if not math.isfinite(value) or value < 0:
            raise ValueError("Durations must be finite and nonnegative")
        groups[row["metric"]].append(value)
    return {key: {"n": len(values), "median_ms": median(values),
                  "p95_ms": sorted(values)[math.ceil(len(values) * 0.95) - 1]}
            for key, values in sorted(groups.items())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl")
    args = parser.parse_args()
    with open(args.jsonl) as stream:
        print(json.dumps(summarize(json.loads(line) for line in stream if line.strip()), indent=2))
