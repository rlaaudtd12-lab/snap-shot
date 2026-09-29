"""Regenerate evaluation in a new directory from immutable measured run artifacts."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from scripts import run_experiments as exp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    args = parser.parse_args(argv)
    batch = args.batch.resolve()
    options = json.loads((batch / "manifest.json").read_text())
    output = batch / ("reevaluation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
    output.mkdir()
    rows = exp.read_csv(batch / "summary/experiment_summary.csv")
    details = []
    for row in rows:
        for key in ("repeat", "processed_frames", "measured_frames", "elapsed_time", "fps"):
            row[key] = exp.number(row.get(key))
        if row["status"] != "COMPLETED":
            continue
        spec = next(s for s in options["videos"] if Path(s["video"]).name == row["video"])
        events = exp.load_detections(Path(row["run_dir"]) / "events.csv")
        row.update(exp.video_metadata(exp.absolute(spec["video"])))
        row["open_at_eof_count"] = sum(e["status"] == "open_at_eof" for e in events)
        truth, _, issues = exp.load_truth(
            exp.absolute(options["ground_truth"]),
            row["video"],
            exp.absolute(spec["zones"]),
            options,
        )
        try:
            if issues or not spec.get("truth_complete"):
                raise ValueError(str(issues) or "Unconfirmed truth")
            metrics, matches = exp.evaluate(
                truth, events, row["video"], options["tolerance_s"], row["video_eof_time"]
            )
            row.update(metrics, evaluation_status="EVALUATED", evaluation_note="")
            details.extend({**m, "mode": row["mode"], "repeat": row["repeat"]} for m in matches)
        except (ValueError, TypeError, KeyError) as exc:
            row.update(evaluation_status="N/A", evaluation_note=str(exc))
    modes = exp.mode_summary(rows)
    stages = exp.read_csv(batch / "summary/stage_timings.csv")
    exp.save_csv(output / "experiment_summary.csv", rows)
    exp.save_csv(output / "mode_summary.csv", modes)
    exp.save_csv(output / "event_matches.csv", details)
    exp.report(output, rows, modes, stages, options, exp.plots(output, rows, modes))
    print(output)
    for row in rows:
        if row["video"] == "Browse.mp4":
            print(
                {
                    k: row.get(k)
                    for k in (
                        "video",
                        "mode",
                        "evaluation_status",
                        "tp",
                        "fp",
                        "fn",
                        "open_at_eof_count",
                    )
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
