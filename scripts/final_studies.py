"""Measured resolution/optimization comparisons through the official experiment adapter.

No synthetic input, GT edits or detector replacements. Each case uses fresh isolated
CLI execution and full-source EOF. Recording and frame_interval=1 remain enabled.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from scripts import run_experiments as exp


def study_cases():
    cases = []
    for size in ((192, 144), (288, 216), (384, 288)):
        for mode in exp.MODES:
            cases.append(
                dict(
                    study="resolution",
                    technique="input_resolution",
                    setting=f"{size[0]}x{size[1]}",
                    mode=mode,
                    config={"input_resolution": list(size)},
                )
            )
    for technique, off, on in (
        ("input_thread", ("baseline", {}, None), ("threaded", {}, None)),
        (
            "downscale",
            ("optimized", {"resize_enabled": False}, None),
            ("optimized", {"resize_enabled": True, "analysis_resolution": [192, 144]}, None),
        ),
        (
            "roi",
            ("optimized", {"roi_enabled": False}, None),
            ("optimized", {"roi_enabled": True}, None),
        ),
        (
            "queue_capacity",
            ("threaded", {"queue_max_size": 8}, None),
            ("threaded", {"queue_max_size": 2}, None),
        ),
        ("opencv_threads", ("optimized", {}, None), ("optimized", {}, 1)),
    ):
        for setting, (mode, config, threads) in (("OFF", off), ("ON", on)):
            cases.append(
                dict(
                    study="optimization",
                    technique=technique,
                    setting=setting,
                    mode=mode,
                    config=config,
                    opencv_threads=threads,
                )
            )
    return cases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("positive repeats required")
    options = json.loads(exp.absolute("scripts/experiments.json").read_text())
    base = json.loads(exp.absolute(options["base_config"]).read_text())
    # Representative short videos use both supplied zone layouts. Scope is explicit.
    specs = [s for s in options["videos"] if Path(s["video"]).name in ("Browse.mp4", "Danger.mp4")]
    batch = exp.absolute("outputs/experiments") / (
        "study-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    batch.mkdir(parents=True)
    cases = study_cases()
    manifest = dict(
        options=options,
        cases=cases,
        videos=specs,
        repeats=args.repeats,
        gt_sha256=hashlib.sha256(exp.absolute(options["ground_truth"]).read_bytes()).hexdigest(),
        resolution_basis="input frame resize in memory; native recording preserved",
        order="reverse case order on even repeats",
        scope="Browse.mp4 and Danger.mp4",
    )
    (batch / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    rows, details, stages = [], [], []
    for repeat in range(1, args.repeats + 1):
        ordered = cases if repeat % 2 else list(reversed(cases))
        for case in ordered:
            case_root = batch / case["study"] / case["technique"] / case["setting"]
            for spec in specs:
                print(
                    f"[study] {repeat} {case['technique']} {case['setting']} {case['mode']} {Path(spec['video']).name}",
                    flush=True,
                )
                result = exp.execute_case(
                    spec,
                    case["mode"],
                    repeat,
                    case_root,
                    {**base, **case["config"]},
                    {**options, "opencv_threads": case.get("opencv_threads")},
                )
                row, _, matches, _, timing, _ = result
                tags = {k: case[k] for k in ("study", "technique", "setting")}
                rows.append({**row, **tags})
                details.extend(
                    {**m, **tags, "mode": case["mode"], "repeat": repeat} for m in matches
                )
                stages.extend({**t, **tags} for t in timing)
                exp.save_csv(batch / "study_results.csv", rows)
    exp.save_csv(batch / "event_matches.csv", details)
    exp.save_csv(batch / "stage_timings.csv", stages)
    print(f"[study] Results: {batch}", flush=True)
    exp.absolute("outputs/experiments/latest_study.json").write_text(
        json.dumps({"batch": str(batch)}, indent=2)
    )
    return int(
        any(r["status"] != "COMPLETED" or r["evaluation_status"] != "EVALUATED" for r in rows)
    )


if __name__ == "__main__":
    raise SystemExit(main())
