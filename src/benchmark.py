"""B·C 성능 계측과 실험 자동화 (담당 C).
입력: perf_counter 기반 프레임 표본/실제 영상 경로. 출력: CSV, 통계, 그래프.
의존: numpy, pandas, matplotlib; 실행 시 pipeline을 지연 import한다.
Mock 표본은 실제 결과와 분리하며 정답 평가는 Mock을 거부한다.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from pathlib import Path
import time
import uuid
from typing import Any

import numpy as np

STAGES = (
    "read",
    "queue_wait",
    "preprocess",
    "detect",
    "intrusion",
    "state",
    "record",
    "display",
    "total",
)
RAW_FIELDS = [
    "run_id",
    "source",
    "pipeline_mode",
    "resolution",
    "variant",
    "repeat",
    "experiment_kind",
    "mock",
    "frame_index",
    "cycle",
    "media_time_s",
    "source_fps",
    "captured_at",
    "scheduled_at",
    "processing_started_at",
    "completed_at",
    "measurement_start_at",
    "measurement_end_at",
    "analyzed",
    "dropped",
    "warmup",
    "observed_duration_s",
    "alert_delay_s",
    "system_alert_delay_ms",
    "scheduled_alert_delay_ms",
] + [s + "_ms" for s in STAGES]


@contextmanager
def measure(row: dict, stage: str):
    """row에 stage + _ms 키로 블록의 perf_counter 경과 시간을 밀리초 단위로 기록한다.
    컨텍스트 안에서 예외가 나도 소요 시간을 남기고 원래 예외를 전파한다. 별도 값을 반환하지 않는다."""
    start = time.perf_counter()
    try:
        yield
    finally:
        row[stage + "_ms"] = (time.perf_counter() - start) * 1000


def _stats(values) -> dict[str, float]:
    """values에서 유한한 수만 골라 평균·중앙값·모표준편차(ddof=0)·95백분위수 딕셔너리를 반환한다.
    None·빈 문자열·비유한 값은 제외하며 표본이 없으면 NaN을 사용한다. 숫자 변환 오류는 전파한다."""
    a = np.asarray([float(x) for x in values if x is not None and x != ""], dtype=float)
    a = a[np.isfinite(a)]
    if not len(a):
        return dict.fromkeys(("mean", "median", "std", "p95"), float("nan"))
    return dict(
        mean=float(a.mean()),
        median=float(np.median(a)),
        std=float(a.std(ddof=0)),
        p95=float(np.percentile(a, 95)),
    )


def summarize_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    """프레임 rows의 준비 구간(warmup)을 제외하고 실제 측정 표본의 통계 딕셔너리를 반환한다.
    플래그는 Python bool, 완료 시각과 측정 구간은 perf_counter 초 단위를 기대한다.
    throughput_fps는 분석 완료 수를 전체 측정 경과 시간으로 나눈 처리량이다.
    입력·큐·표시·저장 비용을 포함하며 원본 영상의 FPS와는 다르다. mean_fps 등은
    연속 분석 완료 간격의 역수 표본 통계이므로 throughput_fps와 일반적으로 일치하지 않는다.
    생략과 드롭은 따로 집계하고 단계별 시간·경보 지연은 존재하는 유한 표본만 사용한다.
    alert_delay_s는 영상 시간의 관측→경보 지연, system_alert_delay_ms는 실제 투입→경보,
    scheduled_alert_delay_ms는 예정 투입→경보의 벽시계 지연이다.
    완료 시각 역행·중복 또는 양수가 아닌 측정 구간은 ValueError이며 빈 통계는 NaN이다."""
    measured = [r for r in rows if not r.get("warmup", False)]
    analyzed = [r for r in measured if r.get("analyzed", True) and not r.get("dropped", False)]
    ends = np.asarray([r["completed_at"] for r in analyzed], dtype=float)
    diffs = np.diff(ends)
    if np.any(diffs <= 0):
        raise ValueError("Completion times must strictly increase")
    fps = _stats(1 / diffs)
    elapsed = 0.0
    if measured:
        elapsed = max(r["measurement_end_at"] for r in measured) - min(
            r["measurement_start_at"] for r in measured
        )
        if elapsed <= 0:
            raise ValueError("Measurement interval must be positive")
    result = {
        "throughput_fps": len(analyzed) / elapsed if elapsed else float("nan"),
        "mean_fps": fps["mean"],
        "median_fps": fps["median"],
        "std_fps": fps["std"],
        "p95_fps": fps["p95"],
        "elapsed_seconds": elapsed,
        "input_frames": len(measured),
        "analyzed_frames": len(analyzed),
        "drop_rate": sum(bool(r.get("dropped")) for r in measured) / len(measured)
        if measured
        else float("nan"),
        "skip_rate": sum(
            not r.get("analyzed", True) and not r.get("dropped", False) for r in measured
        )
        / len(measured)
        if measured
        else float("nan"),
    }
    for key in [s + "_ms" for s in STAGES] + [
        "alert_delay_s",
        "system_alert_delay_ms",
        "scheduled_alert_delay_ms",
    ]:
        for stat, value in _stats(r.get(key) for r in measured).items():
            result[f"{key}_{stat}"] = value
    return result


def write_table(path: str | Path, rows: list[dict], fields: list[str], *, append=False) -> None:
    """rows를 fields 순서의 UTF-8 CSV로 path에 저장하며 반환값은 없다.
    추가 키는 무시한다. append=True이면 기존 헤더가 일치해야 하며 불일치는 ValueError다.
    프레임마다 디스크를 쓰지 않고 모은 행을 저장하기 위한 함수다. 동시 쓰기 직렬화는 호출자 책임이며
    파일 시스템 오류는 전파한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    if append and exists:
        with path.open(encoding="utf-8", newline="") as f:
            if next(csv.reader(f)) != fields:
                raise ValueError(f"CSV schema mismatch: {path}")
    with path.open("a" if append else "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not append or not exists:
            writer.writeheader()
        writer.writerows(rows)


def write_metrics(path: str, rows: list[dict[str, Any]]) -> None:
    """프레임 rows를 path에 고정 RAW_FIELDS 헤더로 저장한다. 반환값은 없다.
    빈 입력도 헤더를 남겨 스키마를 유지하며 파일 오류는 write_table에서 전파한다."""
    write_table(path, rows, RAW_FIELDS)


def save_run_metrics(root: Path, run_dir: Path, rows: list[dict], metadata: dict) -> dict:
    """실행 후 rows 통계와 metadata를 합친 요약 딕셔너리를 반환하고 CSV를 저장한다.
    run_dir에는 해당 실행 자료를 저장하고 root에는 누적 자료를 추가한다.
    호출자는 실행마다 고유한 run_dir을 제공해야 한다. 집계·헤더 불일치·파일 오류는 전파한다."""
    summary = {**metadata, **summarize_metrics(rows)}
    write_metrics(str(run_dir / "benchmark_raw.csv"), rows)
    write_table(root / "benchmark_raw.csv", rows, RAW_FIELDS, append=True)
    fields = list(summary)
    write_table(run_dir / "benchmark_summary.csv", [summary], fields)
    write_table(root / "benchmark_summary.csv", [summary], fields, append=True)
    return summary


def generate_plots(summary_path: str | Path, output_dir: str | Path) -> list[Path]:
    """summary_path의 완료된 실행 자료로 그래프를 만들고 생성한 Path 목록을 반환한다.
    output_dir에 저장하며 자료가 없거나 유효 처리량이 없으면 안내 후 빈 목록을 반환한다.
    source·실험 종류·Mock 여부가 섞이면 비교 해석을 막기 위해 ValueError를 낸다.
    Mock 그래프에는 식별 문구를 표시하고 Agg를 사용하여 GUI 없이 그린다.
    필수 컬럼 누락이나 파일·라이브러리 오류는 호출자에게 전달한다."""
    import pandas as pd

    path = Path(summary_path)
    if not path.exists() or path.stat().st_size == 0:
        print("[benchmark] 데이터 없음: 그래프를 생성하지 않습니다.")
        return []
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        print("[benchmark] 데이터 없음: 그래프를 생성하지 않습니다.")
        return []
    if "run_status" in df:
        df = df[df.run_status.eq("completed")]
    if df.empty or "throughput_fps" not in df or not np.isfinite(df["throughput_fps"]).any():
        print("[benchmark] 유효한 측정 데이터 없음.")
        return []
    if df["source"].nunique() > 1:
        raise ValueError("Select one source video before plotting")
    if df["experiment_kind"].nunique() > 1 or df["mock"].nunique() > 1:
        raise ValueError("Separate experiment kinds and mock/real before plotting")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    mock = str(df["mock"].iloc[0]).lower() in ("true", "1")
    label = ("MOCK - NOT DETECTION PERFORMANCE | " if mock else "") + str(
        df["experiment_kind"].iloc[0]
    )
    specs = [
        (
            "fps_comparison.png",
            df[df.variant.eq("standard")].pivot_table(
                index="resolution", columns="pipeline_mode", values="throughput_fps", aggfunc="mean"
            ),
            "Throughput FPS",
        ),
        (
            "stage_latency.png",
            df[df.variant.eq("standard")]
            .groupby(["resolution", "pipeline_mode"])[
                [s + "_ms_mean" for s in STAGES if s != "total"]
            ]
            .mean(),
            "Mean stage latency (ms)",
        ),
        (
            "optimization_comparison.png",
            df[df.pipeline_mode.eq("optimized")].pivot_table(
                index="resolution", columns="variant", values="throughput_fps", aggfunc="mean"
            ),
            "Throughput FPS",
        ),
    ]
    files = []
    for name, table, ylabel in specs:
        if table.empty:
            print(f"[benchmark] {name}: 해당 조건 데이터 없음")
            continue
        ax = table.plot.bar(figsize=(11, 5))
        ax.set_ylabel(ylabel)
        ax.set_title(label)
        ax.figure.tight_layout()
        target = out / name
        ax.figure.savefig(target, dpi=140)
        plt.close(ax.figure)
        files.append(target)
    return files


def evaluate_events(
    truth: list[dict],
    events: list[dict],
    *,
    video_name: str,
    tolerance_s: float = 0.0,
    mock: bool = False,
    include_details: bool = False,
) -> dict:
    """truth와 단일 실행 events를 video_name·구역·시간 구간 겹침으로 일대일 최대 매칭한다.
    tolerance_s는 구간 겹침 허용 오차(초)이며 기본 0이다. 검출 수·오경보·미검출·지연 통계를 반환한다.
    중복 경보는 한 정답을 여러 번 맞힌 것으로 세지 않는다. 오경보율은 미매칭 경보/전체 경보이며
    정상 프레임 기준 FPR이 아니다. 미검출률은 미매칭 정답/전체 침입 정답이다.
    normal 행은 참고 구간일 뿐 정상 판정 수를 만들어 내지 않는다. 평균 지연은 매칭된 경보 시각에서
    정답 시작을 뺀 값이다. 분모가 없으면 NaN이다. Mock·여러 실행·미종료 사건·잘못된 구간이나
    허용 오차는 ValueError로 거부하고 필수 키·숫자 형식 오류는 전파한다."""
    if mock or any(str(e.get("mock", "")).lower() in ("true", "1") for e in events):
        raise ValueError("Mock cannot be used for detection accuracy")
    if not np.isfinite(tolerance_s) or tolerance_s < 0:
        raise ValueError("Tolerance must be nonnegative")
    if len({e.get("run_id") for e in events}) > 1:
        raise ValueError("Evaluate one run at a time")
    if any(t["event_type"] not in ("intrusion", "normal") for t in truth):
        raise ValueError("Truth event_type must be intrusion or normal")
    gt = [t for t in truth if t["video_name"] == video_name and t["event_type"] == "intrusion"]
    ev = [e for e in events if e.get("video_name", video_name) == video_name]
    if any(e.get("end_time") in (None, "") or e.get("status", "closed") != "closed" for e in ev):
        raise ValueError("Open/incomplete events require review before evaluation")
    for item in gt + ev:
        if not all(np.isfinite(float(item[k])) for k in ("start_time", "end_time")) or float(
            item["end_time"]
        ) < float(item["start_time"]):
            raise ValueError("Invalid interval")
    candidates = {
        i: [
            j
            for j, t in enumerate(gt)
            if e["zone_name"] == t["zone_name"]
            and float(e["start_time"]) <= float(t["end_time"]) + tolerance_s
            and float(e["end_time"]) >= float(t["start_time"]) - tolerance_s
        ]
        for i, e in enumerate(ev)
    }
    assigned = {}

    def match(i, visited):
        """경보 인덱스 i의 연결을 재배치하여 매칭 수를 늘릴 수 있으면 True를 반환한다.
        visited 정답 인덱스로 순환 탐색을 막는다. 단순 선착순 배정으로 가능한 매칭을 놓치지 않기 위함이다."""
        for j in candidates[i]:
            if j in visited:
                continue
            visited.add(j)
            if j not in assigned or match(assigned[j], visited):
                assigned[j] = i
                return True
        return False

    for i in range(len(ev)):
        match(i, set())
    tp = len(assigned)
    delays = [
        float(ev[i]["alert_time"]) - float(gt[j]["start_time"])
        for j, i in assigned.items()
        if ev[i].get("alert_time") not in (None, "") and np.isfinite(float(ev[i]["alert_time"]))
    ]
    result = {
        "actual_events": len(gt),
        "detected_events": tp,
        "false_alarms": len(ev) - tp,
        "missed_events": len(gt) - tp,
        "false_alarm_rate": (len(ev) - tp) / len(ev) if ev else float("nan"),
        "miss_rate": (len(gt) - tp) / len(gt) if gt else float("nan"),
        "mean_alert_delay_s": _stats(delays)["mean"],
    }
    if include_details:
        result.update(
            matches=[{"truth_index": j, "detection_index": i} for j, i in sorted(assigned.items())],
            alert_delays_s=delays,
            min_alert_delay_s=min(delays) if delays else float("nan"),
            max_alert_delay_s=max(delays) if delays else float("nan"),
        )
    return result


def run_suite(
    source: str,
    config: dict,
    *,
    seconds=60.0,
    repeats=3,
    experiment_kind="throughput",
    mock=False,
    no_display=False,
    resolutions=((640, 480), (1280, 720), (1920, 1080)),
    ablations=False,
) -> None:
    """동일한 source 파일을 해상도·모드·반복 조건별로 실행하고 그래프를 저장한다. 반환값은 없다.
    config를 조건별로 복사하며 seconds는 준비 구간 이후 벽시계 측정 시간, repeats는 반복 수다.
    60초 미만·3회 미만 또는 존재하지 않는 영상 파일은 ValueError로 거부한다.
    throughput은 최대 처리량, realtime은 예정 시각에 맞춘 투입이다. mock은 연결 검증용으로만 사용한다.
    입력 해상도를 바꾸면 구역 좌표도 변환하되 녹화 원본은 유지한다. 짧은 파일은 반복해서 읽고
    경계마다 모델·상태를 초기화하며 진행 중 클립은 절단한다. 준비 구간 제외는 실행 최초에만 적용한다.
    ablations는 최적화 항목을 개별 적용한 조건을 추가한다. 실제 영상이나 성능 수치를 합성하지 않으며
    하위 파이프라인 오류는 전파한다."""
    from .pipeline import run_pipeline

    if seconds < 60 or repeats < 3:
        raise ValueError("Formal benchmark requires >=60 seconds and >=3 repeats")
    # 실제 감지에서는 연속 원본 프레임 기준을 보존하기 위해 간격 1만 허용한다.
    # interval_only(간격 2)를 포함한 실험은 다른 조건을 장시간 실행하기 전에 거부한다.
    # 기존 Mock ablation과 실제 standard 세 모드 실험은 그대로 사용할 수 있다.
    if ablations and not mock:
        raise ValueError(
            "Real detection requires frame_interval=1; --ablations includes interval_only=2"
        )
    if str(source).isdigit() or not Path(source).is_file():
        raise ValueError("Benchmark requires an existing video file")
    suite_id = "suite-" + uuid.uuid4().hex[:10]
    variants = [
        ("baseline", "standard", {}),
        ("threaded", "standard", {}),
        ("optimized", "standard", {}),
    ]
    if ablations:
        base = dict(roi_enabled=False, resize_enabled=False, frame_interval=1)
        variants += [
            ("optimized", name, {**base, **changes})
            for name, changes in (
                ("none", {}),
                ("roi_only", {"roi_enabled": True}),
                ("resize_only", {"resize_enabled": True}),
                ("interval_only", {"frame_interval": 2}),
                ("queue_only", {"queue_max_size": 2}),
            )
        ]
    for resolution in resolutions:
        for repeat in range(1, repeats + 1):
            order = variants[repeat % len(variants) :] + variants[: repeat % len(variants)]
            for mode, variant, changes in order:
                current = {
                    **config,
                    **changes,
                    "input_resolution": list(resolution),
                    "duration_seconds": seconds,
                    "loop_video": True,
                    "suite_run": True,
                    "suite_id": suite_id,
                    "experiment_kind": experiment_kind,
                    "mock_detection": mock,
                    "no_display": no_display,
                    "measure_fps": True,
                    "repeat": repeat,
                    "variant": variant,
                }
                run_pipeline(source, mode, current)
    root = Path(config.get("output_dir", "results"))
    if mock:
        root /= "mock"
    root /= experiment_kind
    root /= suite_id
    generate_plots(root / "benchmark_summary.csv", root)


def main(argv=None) -> int:
    """argv로 공식 실험 또는 기존 CSV 그래프 생성 경로를 선택한다. None이면 프로세스 인자를 읽는다.
    성공 시 0을 반환한다. 처리 대상 설정·입출력·미구현 오류는 argparse 오류로 바꿔
    SystemExit(2)로 종료한다. --plot-only는 새 성능 실험을 실행하지 않는다."""
    from .utils import load_config

    ap = argparse.ArgumentParser(description="Real video benchmark; mock results are isolated")
    ap.add_argument("--source")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--experiment-kind", choices=("throughput", "realtime"), default="throughput")
    ap.add_argument("--mock-detection", action="store_true")
    ap.add_argument("--no-display", action="store_true")
    ap.add_argument("--ablations", action="store_true")
    ap.add_argument("--plot-only", metavar="SUMMARY_CSV")
    ap.add_argument("--output-dir")
    args = ap.parse_args(argv)
    try:
        if args.plot_only:
            generate_plots(args.plot_only, args.output_dir or str(Path(args.plot_only).parent))
        else:
            config = load_config(args.config)
            if args.output_dir:
                config["output_dir"] = args.output_dir
            run_suite(
                args.source or config["source"],
                config,
                seconds=args.seconds,
                repeats=args.repeats,
                experiment_kind=args.experiment_kind,
                mock=args.mock_detection,
                no_display=args.no_display,
                ablations=args.ablations,
            )
    except (ValueError, OSError, NotImplementedError, RuntimeError) as exc:
        ap.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
