#!/usr/bin/env python3
"""Repository CLI adapter: isolated runs, audited GT evaluation and measured reports."""

from __future__ import annotations
import argparse
import csv
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
from datetime import datetime, timezone
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.benchmark import evaluate_events, write_table, STAGES
from src.pipeline import EVENT_FIELDS

MODES = ("baseline", "threaded", "optimized")
NAN = float("nan")


def read_csv(path, required=()):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not set(required).issubset(reader.fieldnames or []):
            raise ValueError(f"{path}: required columns {required}")
        return list(reader)


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else NAN
    except (TypeError, ValueError):
        return NAN


def mean(values):
    values = [number(v) for v in values if math.isfinite(number(v))]
    return statistics.mean(values) if values else NAN


def improvement(fps, baseline):
    fps, baseline = number(fps), number(baseline)
    return (fps / baseline - 1) * 100 if baseline > 0 and math.isfinite(fps) else NAN


def save_csv(path, rows, fields=None):
    fields = fields or list(dict.fromkeys(k for row in rows for k in row))
    clean = [
        {
            k: (
                "N/A"
                if row.get(k) is None or isinstance(row.get(k), float) and not math.isfinite(row[k])
                else row[k]
            )
            for k in fields
        }
        for row in rows
    ]
    write_table(path, clean, fields or ["status"])


def absolute(path):
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def load_truth(path, video, zones_path, options):
    """Normalize only explicit mappings; unresolved labels invalidate the video evaluation.

    Legacy /data paths are matched by unique configured basename, never opened as detections.
    Overlapping zone events require an explicit union/raw decision in the manifest.
    """
    rows = read_csv(path, ("video_name", "zone_name", "start_time", "end_time", "event_type"))
    zone_data = json.loads(Path(zones_path).read_text(encoding="utf-8"))
    names = [z.get("name") or z.get("id") for z in zone_data["zones"]]
    selected = [(i + 2, r) for i, r in enumerate(rows) if Path(r["video_name"]).name == video]
    truth, audit, issues = [], [], []
    for line, original in selected:
        row = dict(original)
        label = options["event_types"].get(row["event_type"])
        zone = row["zone_name"]
        if zone not in names:
            if Path(zone).name == Path(zones_path).name and len(names) == 1:
                zone = names[0]
            else:
                issues.append(f"GT line {line}: ambiguous zone {zone}")
        audit.append(
            dict(
                line=line,
                video=video,
                original_zone=row["zone_name"],
                zone=zone,
                original_type=row["event_type"],
                normalized_type=label,
                start_time=row["start_time"],
                end_time=row["end_time"],
            )
        )
        if label not in ("intrusion", "normal"):
            issues.append(f"GT line {line}: unresolved event_type={row['event_type']}")
            continue
        # Explicit whole-video negative marker in the supplied GT.
        if label == "normal" and row["start_time"].lower() == row["end_time"].lower() == "none":
            continue
        start, end = number(row["start_time"]), number(row["end_time"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            issues.append(f"GT line {line}: invalid interval")
            continue
        truth.append(
            dict(video_name=video, zone_name=zone, start_time=start, end_time=end, event_type=label)
        )
    positives = sorted(
        (r for r in truth if r["event_type"] == "intrusion"),
        key=lambda r: (r["zone_name"], r["start_time"], r["end_time"]),
    )
    merged = []
    overlap = False
    for row in positives:
        if (
            merged
            and merged[-1]["zone_name"] == row["zone_name"]
            and row["start_time"] <= merged[-1]["end_time"]
        ):
            overlap = True
            merged[-1]["end_time"] = max(merged[-1]["end_time"], row["end_time"])
        else:
            merged.append(dict(row))
    if overlap and options.get("merge_zone_intervals") is None:
        issues.append(
            "Overlapping GT: set merge_zone_intervals true (zone union) or false (raw events)"
        )
    if options.get("merge_zone_intervals") is True:
        truth = merged + [r for r in truth if r["event_type"] == "normal"]
    return truth, audit, issues


def load_detections(path):
    rows = read_csv(
        path,
        (
            "run_id",
            "video_name",
            "zone_name",
            "start_time",
            "end_time",
            "alert_time",
            "event_id",
            "status",
            "mock",
        ),
    )
    if any(r["mock"].lower() in ("true", "1") for r in rows):
        raise ValueError("Mock detections cannot be evaluated")
    return rows


def video_metadata(path):
    """Read source metadata; never substitute processing throughput for source FPS."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ValueError(f"Cannot open source metadata: {path}")
        count = number(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = number(cap.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(count) or count <= 0 or not math.isfinite(fps) or fps <= 0:
            raise ValueError("Invalid source frame_count/source_fps")
        return dict(frame_count=count, source_fps=fps, video_eof_time=count / fps)
    finally:
        cap.release()


def evaluation_intervals(detections, video_eof_time=None):
    """Create private matcher input; measured events and their status remain untouched."""
    adapted = []
    for original in detections:
        row = dict(original)
        if row.get("status") == "open_at_eof":
            start, alert = number(row.get("start_time")), number(row.get("alert_time"))
            end = (
                number(video_eof_time)
                if row.get("end_time") in (None, "")
                else number(row["end_time"])
            )
            if (
                not all(math.isfinite(x) for x in (start, alert, end))
                or not 0 <= start <= alert <= end
            ):
                raise ValueError(
                    "Invalid open_at_eof interval/alert or unavailable source EOF; review required"
                )
            row.update(end_time=end, status="closed")
        adapted.append(row)
    return adapted


def evaluate(truth, detections, video, tolerance, video_eof_time=None):
    """Use existing maximum-cardinality 1:1 overlap matching without changing detection."""
    adapted = evaluation_intervals(detections, video_eof_time)
    result = evaluate_events(
        truth, adapted, video_name=video, tolerance_s=tolerance, include_details=True
    )
    tp, fp, fn = result["detected_events"], result["false_alarms"], result["missed_events"]
    metrics = dict(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=tp / (tp + fp) if tp + fp else NAN,
        recall=tp / (tp + fn) if tp + fn else NAN,
        f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else NAN,
        false_positive_count=fp,
        false_discovery_fraction=fp / (tp + fp) if tp + fp else NAN,
        fpr=NAN,
        alert_delay=result["mean_alert_delay_s"],
        alert_delay_min=result["min_alert_delay_s"],
        alert_delay_max=result["max_alert_delay_s"],
        alert_delay_count=len(result["alert_delays_s"]),
        alert_delay_sum=sum(result["alert_delays_s"]),
    )
    gt = [r for r in truth if r["event_type"] == "intrusion" and r["video_name"] == video]
    matched_d = {m["detection_index"]: m["truth_index"] for m in result["matches"]}
    matched_g = set(matched_d.values())
    details = []
    for i, row in enumerate(detections):
        j = matched_d.get(i)
        details.append(
            dict(
                video=video,
                outcome="TP" if j is not None else "FP",
                event_id=row["event_id"],
                zone=row["zone_name"],
                truth_index=j,
                detected_start=row["start_time"],
                detected_end=row["end_time"],
                status=row.get("status", "closed"),
                evaluation_end_time=adapted[i]["end_time"],
                truth_start=gt[j]["start_time"] if j is not None else None,
                truth_end=gt[j]["end_time"] if j is not None else None,
                alert_delay=number(row["alert_time"]) - gt[j]["start_time"]
                if j is not None
                else NAN,
            )
        )
    for j, row in enumerate(gt):
        if j not in matched_g:
            details.append(
                dict(
                    video=video,
                    outcome="FN",
                    truth_index=j,
                    zone=row["zone_name"],
                    truth_start=row["start_time"],
                    truth_end=row["end_time"],
                )
            )
    return metrics, details


def execute_case(spec, mode, repeat, batch, base_config, options):
    video = absolute(spec["video"])
    root = batch / mode / f"{video.stem}-r{repeat}"
    root.mkdir(parents=True)
    row = dict(
        video=video.name,
        mode=mode,
        repeat=repeat,
        status="FAILED",
        evaluation_status="N/A",
        error="",
        evaluation_note="",
        output_dir=str(root),
    )
    row.update(
        dict.fromkeys(
            (
                "processed_frames",
                "measured_frames",
                "elapsed_time",
                "fps",
                "tp",
                "fp",
                "fn",
                "precision",
                "recall",
                "f1",
                "alert_delay",
                "detected_events",
                "recorded_events",
            ),
            NAN,
        )
    )
    events, details, audit, stages, normalized = [], [], [], [], []
    try:
        if not video.is_file():
            raise FileNotFoundError(video)
        zones = absolute(spec["zones"])
        config = {
            **base_config,
            "no_display": True,
            "mock_detection": False,
            "measure_fps": True,
            "recording_enabled": True,
            "loop_video": False,
            "duration_seconds": 0,
            "experiment_kind": "throughput",
            "queue_policy": "block",
            "frame_interval": 1,
            "edit_zones": False,
            "output_dir": str(root),
            "zones_path": str(zones),
            "repeat": repeat,
        }
        config_path = root / "requested_config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "scripts.experiment_worker",
            "--source",
            str(video),
            "--mode",
            mode,
            "--config",
            str(config_path),
            "--no-display",
        ]
        if options.get("opencv_threads") is not None:
            command += ["--opencv-threads", str(options["opencv_threads"])]
        (root / "command.json").write_text(json.dumps(command), encoding="utf-8")
        with (root / "run.log").open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=options["timeout_seconds"],
                check=False,
            )
        if completed.returncode:
            raise RuntimeError(f"CLI exit {completed.returncode}; see {root / 'run.log'}")
        runs = list((root / "runs").iterdir())
        if len(runs) != 1:
            raise ValueError("Expected exactly one fresh run")
        run = runs[0]
        status = json.loads((run / "status.json").read_text())
        if status["status"] != "completed" or status["pending_at_stop"]:
            raise ValueError(f"Incomplete pipeline: {status}")
        summary = read_csv(run / "benchmark_summary.csv")[0]
        row.update(
            status="COMPLETED",
            run_dir=str(run),
            processed_frames=status["processed_frames"],
            measured_frames=int(summary["analyzed_frames"]),
            elapsed_time=number(summary["elapsed_seconds"]),
            fps=number(summary["throughput_fps"]),
            dropped_frames=status["analyzed_queue_drops"],
            pending_at_stop=status["pending_at_stop"],
            queue_wait_ms=number(summary["queue_wait_ms_mean"]),
            total_frame_ms=number(summary["total_ms_mean"]),
            source_fps=number(summary["source_fps"]),
        )
        events = load_detections(run / "events.csv")
        row.update(video_metadata(video))
        row["open_at_eof_count"] = sum(e["status"] == "open_at_eof" for e in events)
        clips = read_csv(run / "clips.csv")
        verified = set()
        import cv2

        for clip in clips:
            path = Path(clip["path"])
            cap = cv2.VideoCapture(str(path))
            ok, _ = cap.read()
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()
            if ok and count == int(clip["frames_written"]):
                verified.add(str(path))
        row.update(
            detected_events=len(events),
            recorded_events=sum(e["video_path"] in verified for e in events),
            recorded_clips=len(clips),
            verified_clips=len(verified),
            truncated_clips=sum(c["truncated"].lower() == "true" for c in clips),
            recording_status="PASS"
            if len(verified) == len(clips) and all(e["video_path"] in verified for e in events)
            else "CHECK",
        )
        for stage in STAGES:
            stages.append(
                dict(
                    video=video.name,
                    mode=mode,
                    repeat=repeat,
                    stage=stage,
                    mean_ms=number(summary[stage + "_ms_mean"]),
                    p95_ms=number(summary[stage + "_ms_p95"]),
                )
            )
        try:
            truth, audit, issues = load_truth(
                absolute(options["ground_truth"]), video.name, zones, options
            )
            normalized = truth
            if not spec.get("truth_complete", False):
                issues.append("truth_complete is not confirmed for this video")
            if issues:
                raise ValueError("; ".join(issues))
            metrics, details = evaluate(
                truth, events, video.name, options["tolerance_s"], row["video_eof_time"]
            )
            row.update(metrics, evaluation_status="EVALUATED")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            row["evaluation_note"] = str(exc)
    except Exception as exc:
        row.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
    return row, events, details, audit, stages, normalized


def mode_summary(rows):
    # Balanced performance and accuracy cohorts prevent survivor-biased comparisons.
    selected_modes = sorted({r["mode"] for r in rows})

    def cohort(key):
        groups = {}
        for row in rows:
            if key(row):
                groups.setdefault((row["video"], row["repeat"]), set()).add(row["mode"])
        return {key for key, modes in groups.items() if len(modes) == len(selected_modes)}

    performance = cohort(
        lambda r: r["status"] == "COMPLETED" and math.isfinite(number(r.get("fps")))
    )
    accuracy = cohort(lambda r: r["evaluation_status"] == "EVALUATED")
    output = []
    for mode in selected_modes:
        all_rows = [r for r in rows if r["mode"] == mode]
        perf = [r for r in all_rows if (r["video"], r["repeat"]) in performance]
        acc = [r for r in all_rows if (r["video"], r["repeat"]) in accuracy]
        tp, fp, fn = (sum(r[k] for r in acc) for k in ("tp", "fp", "fn"))
        delay_count = sum(r["alert_delay_count"] for r in acc)
        output.append(
            dict(
                mode=mode,
                attempted=len(all_rows),
                completed=sum(r["status"] == "COMPLETED" for r in all_rows),
                paired_performance_runs=len(perf),
                evaluated_runs=len(acc),
                fps=mean(r["fps"] for r in perf),
                pooled_fps=sum(r["measured_frames"] for r in perf)
                / sum(r["elapsed_time"] for r in perf)
                if perf and sum(r["elapsed_time"] for r in perf) > 0
                else NAN,
                fps_std=statistics.stdev([r["fps"] for r in perf]) if len(perf) > 1 else NAN,
                tp=tp if acc else NAN,
                fp=fp if acc else NAN,
                fn=fn if acc else NAN,
                precision=tp / (tp + fp) if tp + fp else NAN,
                recall=tp / (tp + fn) if tp + fn else NAN,
                f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else NAN,
                false_alarms_per_video=fp / len(acc) if acc else NAN,
                alert_delay=sum(r["alert_delay_sum"] for r in acc) / delay_count
                if delay_count
                else NAN,
                alert_delay_min=min(
                    (r["alert_delay_min"] for r in acc if r["alert_delay_count"]), default=NAN
                ),
                alert_delay_max=max(
                    (r["alert_delay_max"] for r in acc if r["alert_delay_count"]), default=NAN
                ),
            )
        )
    baseline = next((r["fps"] for r in output if r["mode"] == "baseline"), NAN)
    for row in output:
        row["improvement_percent"] = improvement(row["fps"], baseline)
    return output


def plots(summary, rows, modes):
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/safety-monitor-matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figures = summary / "figures"
    figures.mkdir()
    made = []
    for metric, filename, label in [
        ("fps", "fps_comparison", "Throughput FPS (recording enabled)"),
        ("precision", "precision_comparison", "Precision"),
        ("recall", "recall_comparison", "Recall"),
        ("f1", "f1_comparison", "F1"),
        ("alert_delay", "alert_delay_comparison", "Matched media-time alert delay (s)"),
    ]:
        valid = [r for r in modes if math.isfinite(number(r[metric]))]
        if not valid:
            continue
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar([r["mode"] for r in valid], [r[metric] for r in valid])
        ax.set_ylabel(label)
        ax.set_title("Paired runs; accuracy uses evaluable videos only")
        fig.tight_layout()
        fig.savefig(figures / f"{filename}.png", dpi=150)
        plt.close(fig)
        made.append(f"{filename}.png")
    valid = [r for r in rows if r["status"] == "COMPLETED" and math.isfinite(number(r.get("fps")))]
    if valid:
        import pandas as pd

        table = pd.DataFrame(valid).pivot_table(
            index="video", columns="mode", values="fps", aggfunc="mean"
        )
        ax = table.plot.bar(figsize=(11, 5), ylabel="Throughput FPS", rot=30)
        ax.figure.tight_layout()
        ax.figure.savefig(figures / "fps_by_video.png", dpi=150)
        plt.close(ax.figure)
        made.append("fps_by_video.png")
    return made


def markdown_table(rows, columns):
    def fmt(value):
        if isinstance(value, float):
            return f"{value:.4f}" if math.isfinite(value) else "N/A"
        return str(value if value is not None else "N/A").replace("|", "/")

    return "\n".join(
        ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
        + ["| " + " | ".join(fmt(r.get(k)) for k in columns) + " |" for r in rows]
    )


def report(summary, rows, modes, stages, options, figures):
    baseline = [
        r
        for r in stages
        if r["mode"] == "baseline" and r["stage"] not in ("total", "queue_wait", "display")
    ]
    ranked = sorted(
        [
            dict(stage=s, mean_ms=mean(r["mean_ms"] for r in baseline if r["stage"] == s))
            for s in STAGES
            if s not in ("total", "queue_wait", "display")
        ],
        key=lambda r: number(r["mean_ms"]) if math.isfinite(number(r["mean_ms"])) else -1,
        reverse=True,
    )
    checks = [
        (
            "PASS",
            "동일 감지 경로",
            "src.main → run_pipeline → RealBackend, Mock 금지; 감지 구현 변경 없음",
        ),
        (
            "PASS",
            "입력/큐 구조",
            "baseline 순차, threaded/optimized 입력 producer + Queue; block, frame_interval=1",
        ),
        (
            "PASS",
            "기존 녹화 재사용",
            "create_recorder/record_frame/close_recorder, pre/post 원 설정 유지",
        ),
        (
            "CHECK",
            "frame copy / resize",
            "preprocess는 항상 cv2.resize 호출; 버퍼 원본 복사 비용은 record에 포함, 별도 제거 실험 없음",
        ),
        (
            "PASS" if baseline else "N/A",
            "단계 시간",
            "read/preprocess/detect/intrusion/state/record 원 계측; stage_timings.csv",
        ),
        ("CHECK", "MOG2/morphology", "detect_ms 안에 함께 포함; 개별 기여도는 측정하지 않음"),
        ("N/A", "GUI/CSV I/O", "no-display; CSV는 루프 종료 후 저장, 별도 시간 계측 없음"),
        (
            "PASS" if all(r["status"] == "COMPLETED" for r in rows) else "CHECK",
            "모드 실행",
            "experiment_summary.csv 상태 확인",
        ),
        (
            "PASS" if all(r.get("recording_status") == "PASS" for r in rows) else "CHECK",
            "녹화 파일",
            "첫 프레임 decode 및 선언 프레임 수 확인; 전체 영상 육안 검증 아님",
        ),
        (
            "PASS" if all(r["evaluation_status"] == "EVALUATED" for r in rows) else "CHECK",
            "FP/FN/경고 지연",
            "미확정 GT 또는 손상 검출은 영상 단위 N/A; open_at_eof는 source EOF로 평가; 평가 커버리지 확인",
        ),
        (
            "CHECK",
            "최적화 효과",
            "현재 원본 384×288, 분석 상한 640×360, ROI off이면 축소가 적용되지 않음",
        ),
        (
            "CHECK",
            "반복 측정",
            f"영상 전체 EOF 실행 {options['repeats']}회; 공식 60초×3 benchmark suite와 구분",
        ),
    ]
    (summary / "optimization_checklist.md").write_text(
        "# 최적화 체크리스트\n\n"
        + markdown_table(
            [dict(status=a, item=b, evidence=c) for a, b, c in checks],
            ["status", "item", "evidence"],
        )
        + "\n",
        encoding="utf-8",
    )
    comparison = markdown_table(
        modes,
        [
            "mode",
            "completed",
            "paired_performance_runs",
            "evaluated_runs",
            "fps",
            "improvement_percent",
            "tp",
            "fp",
            "fn",
            "precision",
            "recall",
            "f1",
            "alert_delay",
        ],
    )
    issues = [
        dict(
            video=r["video"],
            mode=r["mode"],
            repeat=r["repeat"],
            reason=r["error"] or r["evaluation_note"],
        )
        for r in rows
        if r["error"] or r["evaluation_note"]
    ]
    lines = [
        "# 최종 성능 분석 보고서",
        "",
        "## 1. 시스템 구현 결과",
        "schema_version=1 다각형을 원본 해상도로 로딩한다. BGR resize/blur → MOG2 → 그림자 제외 threshold → opening/closing → contour 면적 필터 → 박스 하단 중앙점의 polygon 내부 판정을 수행한다. 연속 감지/해제 프레임으로 ALERT/CLEARED 전이 후 CSV와 원본 사건 클립을 저장한다. 기존 pre/post 설정과 EOF 절단 표시를 유지한다.",
        "baseline은 순차 입력, threaded/optimized는 입력 스레드와 제한 Queue를 사용한다. 분석·상태·녹화는 소비 경로에서 처리한다. optimized만 기존 ROI/분석 축소 옵션을 적용한다.",
        "",
        "## 2. 실험 환경 및 방법",
        f"배치: `{summary.parent}`. 설정: `manifest.json`, 실행별 `effective_config.json`에 환경·소스 SHA256·실제 구역·해상도를 보존했다. 입력 영상 전체를 EOF까지 {options['repeats']}회, no-display, throughput, 실제 감지, 녹화 활성 상태로 처리했다. 실행 순서는 반복별 회전한다.",
        "FPS = 기존 benchmark의 warmup 제외 analyzed_frames / elapsed_seconds. 평균 FPS는 공통 성공 영상/반복 집합의 산술평균이며 pooled_fps는 합산 프레임/합산 시간이다. fps_std는 영상/반복 전체의 표준편차이므로 신뢰구간이 아니다. 초기화·해시·최종 CSV/Writer 종료 비용은 이 처리 구간에 포함되지 않는다.",
        f"GT와 Detection은 별도 입력이다. GT 원본을 보존하고 파일 basename과 명시적 JSON→단일 구역명을 정규화한다. 같은 영상·구역에서 닫힌 시간 구간이 겹치면 후보가 된다(끝점 접촉 포함, tolerance={options['tolerance_s']}초). 기존 최대 cardinality 1:1 매칭을 사용한다. 동률은 CSV 순서에 따른 결정적 매칭이며 지연 최소화를 보장하지 않는다.",
        f"GT 중첩 구간 병합 설정: `{options.get('merge_zone_intervals')}`. null은 중첩 영상 평가 보류, true는 구역별 합집합, false는 원본 사건 수 유지다. 분류 미확정 라벨이나 손상된 필수 시각은 N/A 처리한다. open_at_eof의 빈 종료 시각은 평가용 복사본에만 frame_count/source_fps를 적용하며 원본 사건은 보존한다.",
        "FP는 매칭되지 않은 경보 사건 수다. false_discovery_fraction=FP/(TP+FP), false_alarms_per_video=평가된 실행당 FP다. TN을 정의하지 않으므로 일반 FPR은 N/A다. 정상 구간 행을 TN으로 세지 않는다. 분모가 0이면 해당 Precision/Recall/F1은 N/A다.",
        "Alert delay는 매칭된 검출 alert_time − GT start_time (영상 초)이며 음수도 보존한다. 시스템/스케줄 지연과 구별하며 실제 실시간 벽시계 응답성 실험으로 해석하지 않는다. 정확도 집계는 모든 선택 모드에서 평가 가능한 공통 영상/반복 집합의 micro 합산이며, 평가 누락 영상을 포함한 전체 정확도로 일반화할 수 없다.",
        "",
        "### 영상별 실행 결과",
        markdown_table(
            rows,
            [
                "video",
                "mode",
                "repeat",
                "status",
                "evaluation_status",
                "fps",
                "detected_events",
                "recorded_events",
            ],
        ),
        "",
        "## 3. 최적화 전 병목 분석",
        markdown_table(ranked, ["stage", "mean_ms"]),
        "위 표는 baseline 실행별 stage 평균을 평균한 측정 순위다. 가장 큰 직접 처리 단계부터 개선 후보를 검토한다. 입력 스레드와 소비 작업은 중첩될 수 있어 stage 합계를 wall time으로 해석하지 않는다. queue_wait는 큐 체류/백프레셔를 포함하며 CPU 작업량이 아니다. total은 read+queue+소비 지연이다. detect는 MOG2·threshold·morphology·contour를 함께 측정하므로 각각의 병목을 단정하지 않는다. GUI는 비활성, CSV I/O는 미계측이다.",
        "",
        "## 4. 기법별 실험 결과",
        comparison,
        "",
        "## 5. 최적화 전후 FPS 비교",
        "개선율=(mode FPS / baseline FPS − 1)×100. baseline 미실행 또는 유효하지 않으면 N/A. 음수 개선율은 성능 저하다. 현재 영상은 384×288로 기본 분석 상한 640×360보다 작고 ROI가 꺼져 있으므로 optimized의 실제 공간 축소 효과를 검증하는 데이터가 아니다.",
        "",
        "## 6. 감지 정확도 변화",
        "위 공통 평가 집합 표와 detection_comparison.csv의 TP/FP/FN, Precision/Recall/F1, Alert Delay를 함께 비교한다. 정확도가 같아도 평가가 보류된 영상까지 동일하다고 결론내릴 수 없다. event_matches.csv는 TP/FP/FN 근거를, ground_truth_audit.csv는 GT 변환 내역을 제공한다.",
        "",
        "## 7. 개선 효과",
    ]
    for row in modes:
        lines.append(
            f"- {row['mode']}: FPS={row['fps']:.3f}, baseline 대비 {row['improvement_percent']:.2f}%, 공통 평가 실행 {row['evaluated_runs']}건, F1={row['f1']:.4f}, FP={row['fp']}, FN={row['fn']}."
        )
    lines += [
        "수치는 이번 환경에서 측정된 결과이며 반복 수·입력 크기·GT 커버리지의 제약을 함께 고려해야 한다. 기록 속도를 유지하면서 정확도가 보존되었는지는 평가 가능한 부분에서만 판단한다.",
        "",
        "## 8. 프로젝트 기대효과",
        "감지에서 사건 클립 저장까지 연결한 파이프라인에 재현 가능한 실험과 정답 검증 절차를 추가했다. 이를 통해 단순 동작 확인을 넘어 입력 스레드 구조의 처리량과 경보 품질을 함께 검토할 수 있다. 산업안전 CCTV나 스마트팩토리 적용 전에는 더 큰 해상도·실시간 투입 조건, 완전한 정답 라벨, 반복 측정을 확장해야 한다.",
        "",
        "## 평가 제한 및 실패 내역",
        markdown_table(issues, ["video", "mode", "repeat", "reason"]) if issues else "없음",
        "",
        "## 그래프",
    ]
    lines += [f"![{name}](figures/{name})" for name in figures]
    (summary / "final_performance_report.md").write_text(
        "\n\n".join(lines).replace("nan", "N/A"), encoding="utf-8"
    )


def run(options, modes=MODES, executor=execute_case):
    base = json.loads(absolute(options["base_config"]).read_text(encoding="utf-8"))
    output = absolute(options["output_dir"])
    batch = output / (
        "batch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    summary = batch / "summary"
    summary.mkdir(parents=True)
    (batch / "manifest.json").write_text(
        json.dumps(options, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    gt_path = absolute(options["ground_truth"])
    if gt_path.is_file():
        (summary / "ground_truth_original.csv").write_bytes(gt_path.read_bytes())
    specs = list(options["videos"])
    configured = {absolute(s["video"]).resolve() for s in specs}
    for path in sorted(ROOT.glob(options.get("video_glob", "data/samples/*.mp4"))):
        if path.resolve() not in configured:
            specs.append(dict(video=str(path), zones="", truth_complete=False))
    names = [Path(s["video"]).name for s in specs]
    if len(names) != len(set(names)):
        raise ValueError("Video basenames must be unique for GT association")
    rows, events, details, audits, stages, truths = [], [], [], [], [], []
    for repeat in range(1, options["repeats"] + 1):
        order = list(modes)
        offset = (repeat - 1) % len(order)
        order = order[offset:] + order[:offset]
        for spec in specs:
            for mode in order:
                print(f"[experiment] {Path(spec['video']).name} {mode} repeat={repeat}", flush=True)
                row, ev, matches, audit, timing, truth = executor(
                    spec, mode, repeat, batch, base, options
                )
                rows.append(row)
                events.extend(ev)
                details.extend(dict(m, mode=mode, repeat=repeat) for m in matches)
                audits.extend(dict(a, mode=mode, repeat=repeat) for a in audit)
                stages.extend(timing)
                truths.extend(dict(t, mode=mode, repeat=repeat) for t in truth)
                save_csv(summary / "experiment_summary.csv", rows)
    grouped = mode_summary(rows)
    fps_rows = []
    for row in rows:
        baseline = next(
            (
                r.get("fps")
                for r in rows
                if r["video"] == row["video"]
                and r["repeat"] == row["repeat"]
                and r["mode"] == "baseline"
                and r["status"] == "COMPLETED"
            ),
            NAN,
        )
        fps_rows.append(
            dict(
                video=row["video"],
                mode=row["mode"],
                repeat=row["repeat"],
                fps=row.get("fps"),
                improvement_percent=improvement(row.get("fps"), baseline),
                status=row["status"],
            )
        )
    save_csv(summary / "mode_summary.csv", grouped)
    save_csv(summary / "fps_comparison.csv", fps_rows)
    consistency = []

    def signature(run_id):
        return sorted(
            tuple(e[k] for k in ("zone_name", "start_time", "end_time", "alert_time", "status"))
            for e in events
            if e["run_id"] == run_id
        )

    for row in rows:
        baseline = next(
            (
                r
                for r in rows
                if r["video"] == row["video"]
                and r["repeat"] == row["repeat"]
                and r["mode"] == "baseline"
                and r["status"] == "COMPLETED"
            ),
            None,
        )
        equal = None
        if baseline and row["status"] == "COMPLETED":
            equal = signature(Path(row["run_dir"]).name) == signature(
                Path(baseline["run_dir"]).name
            )
        consistency.append(
            dict(
                video=row["video"],
                mode=row["mode"],
                repeat=row["repeat"],
                exact_event_intervals_equal_baseline=equal,
            )
        )
    save_csv(summary / "event_consistency.csv", consistency)
    for name, fields in [
        (
            "detection_comparison",
            [
                "video",
                "mode",
                "repeat",
                "evaluation_status",
                "tp",
                "fp",
                "fn",
                "precision",
                "recall",
                "f1",
                "evaluation_note",
            ],
        ),
        (
            "false_alarm_analysis",
            [
                "video",
                "mode",
                "repeat",
                "evaluation_status",
                "false_positive_count",
                "false_discovery_fraction",
                "fpr",
            ],
        ),
        (
            "alert_delay",
            [
                "video",
                "mode",
                "repeat",
                "evaluation_status",
                "alert_delay",
                "alert_delay_min",
                "alert_delay_max",
                "alert_delay_count",
            ],
        ),
    ]:
        save_csv(summary / f"{name}.csv", [{k: r.get(k) for k in fields} for r in rows], fields)
    for name, data, fields in [
        ("events", events, EVENT_FIELDS),
        ("event_matches", details, None),
        ("ground_truth_audit", audits, None),
        ("ground_truth_normalized", truths, None),
        ("stage_timings", stages, None),
    ]:
        save_csv(summary / f"{name}.csv", data, fields)
    figures = plots(summary, rows, grouped)
    report(summary, rows, grouped, stages, options, figures)
    # Relative pointer files, not destructive replacement of previous result directories.
    (output / "latest.json").write_text(
        json.dumps({"batch": str(batch), "summary": str(summary)}, indent=2), encoding="utf-8"
    )
    print(f"[experiment] Results: {summary}", flush=True)
    return rows, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "scripts/experiments.json"))
    parser.add_argument("--mode", choices=("all",) + MODES, default="all")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--tolerance-s", type=float)
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)
    options = json.loads(absolute(args.config).read_text(encoding="utf-8"))
    for key in ("repeats", "tolerance_s", "output_dir"):
        if getattr(args, key) is not None:
            options[key] = getattr(args, key)
    if type(options["repeats"]) is not int or options["repeats"] < 1:
        parser.error("repeats must be a positive integer")
    if not math.isfinite(options["tolerance_s"]) or options["tolerance_s"] < 0:
        parser.error("tolerance must be finite and nonnegative")
    rows, _ = run(options, MODES if args.mode == "all" else (args.mode,))
    return 1 if any(r["status"] == "FAILED" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
