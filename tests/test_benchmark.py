"""C 계측 테스트: 합성 시각·사건·CSV로 통계와 그래프·실험 호출 구성을 검증한다.
출력은 단언 결과 및 pytest 임시 폴더의 그래프다. 일부 실행 함수는 대역으로 바꾼다.
합성 수치는 실험 성능이 아니며 실제 60초 반복 측정·검출 정확도·GUI는 검증하지 않는다."""

import pytest
from src.benchmark import summarize_metrics, measure, generate_plots, evaluate_events


def test_fps_and_stats():
    """완료 시각 1·2·3초의 합성 표본으로 처리량·FPS·모표준편차·95백분위 계산을 확인한다.
    계산식 검증용 수치이며 실제 측정 결과가 아니다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    rows = [
        dict(completed_at=t, measurement_start_at=0, measurement_end_at=3, analyzed=True, read_ms=v)
        for t, v in [(1, 1), (2, 2), (3, 3)]
    ]
    s = summarize_metrics(rows)
    assert s["throughput_fps"] == 1
    assert s["mean_fps"] == 1
    assert s["std_fps"] == 0
    assert s["read_ms_mean"] == 2
    assert s["read_ms_median"] == 2
    assert s["read_ms_std"] == pytest.approx((2 / 3) ** 0.5)
    assert s["read_ms_p95"] == pytest.approx(2.9)


def test_measure_on_exception():
    """계측 블록이 RuntimeError로 끝나도 음수가 아닌 detect_ms를 남기는지 확인한다.
    타이머 정밀도나 실제 검출 지연의 목표값은 검증하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    row = {}
    with pytest.raises(RuntimeError):
        with measure(row, "detect"):
            raise RuntimeError("test")
    assert row["detect_ms"] >= 0


def test_no_data_plots(tmp_path):
    """존재하지 않는 CSV에는 빈 결과를 반환하고 그래프 파일을 만들지 않는지 확인한다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    assert generate_plots(tmp_path / "missing.csv", tmp_path) == []
    assert not list(tmp_path.glob("*.png"))


def test_one_to_one_matching():
    """한 정답에 중복 경보 두 개면 한 건만 매칭하고 나머지를 오경보로 세는지 확인한다.
    정답 기준 경보 지연과 Mock 평가 거부도 검증하며 실제 정답 라벨의 품질은 다루지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    truth = [dict(video_name="v", zone_name="z", start_time=1, end_time=5, event_type="intrusion")]
    ev = [dict(zone_name="z", start_time=2, alert_time=3, end_time=4)] * 2
    s = evaluate_events(truth, ev, video_name="v")
    assert s["detected_events"] == 1
    assert s["false_alarms"] == 1
    assert s["false_alarm_rate"] == 0.5
    assert s["mean_alert_delay_s"] == 2
    with pytest.raises(ValueError, match="Mock"):
        evaluate_events(truth, ev, video_name="v", mock=True)


def test_plot_measured_mock_data(tmp_path):
    """정해진 Mock 요약값으로 세 그래프가 임시 폴더에 생성되고 비어 있지 않은지 확인한다.
    실제 성능 측정이나 그래프의 시각적 품질을 평가하는 테스트는 아니다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    from src.benchmark import write_table

    # 계산과 생성 경로만 검증하는 고정 입력이며 모든 산출물은 pytest 임시 폴더에 저장한다.
    rows = []
    for mode in ("baseline", "threaded", "optimized"):
        row = dict(
            source="TEST_ONLY",
            resolution="64x48",
            pipeline_mode=mode,
            variant="standard",
            throughput_fps=1.0,
            experiment_kind="throughput",
            mock=True,
            run_status="completed",
        )
        from src.benchmark import STAGES

        row.update({s + "_ms_mean": 1.0 for s in STAGES})
        rows.append(row)
    path = tmp_path / "summary.csv"
    write_table(path, rows, list(rows[0]))
    files = generate_plots(path, tmp_path)
    assert {p.name for p in files} == {
        "fps_comparison.png",
        "stage_latency.png",
        "optimization_comparison.png",
    }
    assert all(p.stat().st_size > 0 for p in files)


def test_suite_matrix(monkeypatch, tmp_path):
    """실행·그래프 함수를 대체하여 3해상도×3모드×3반복의 27개 호출과 60초 설정을 확인한다.
    파일을 디코드하거나 실제 60초 실험을 돌리지 않는다. 짧은 공식 실험 시간의 거부도 확인한다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    from src import benchmark, pipeline

    source = tmp_path / "TEST_ONLY.file"
    source.write_text("not decoded; orchestration test")
    calls = []
    monkeypatch.setattr(
        pipeline, "run_pipeline", lambda source, mode, config: calls.append((mode, config))
    )
    monkeypatch.setattr(benchmark, "generate_plots", lambda *args: [])
    benchmark.run_suite(str(source), {}, mock=True, no_display=True)
    assert len(calls) == 27
    assert {tuple(c["input_resolution"]) for _, c in calls} == {
        (640, 480),
        (1280, 720),
        (1920, 1080),
    }
    assert all(c["duration_seconds"] == 60 and c["loop_video"] for _, c in calls)
    assert len({c["suite_id"] for _, c in calls}) == 1
    with pytest.raises(ValueError):
        benchmark.run_suite(str(source), {}, seconds=10)


def test_skip_drop_and_warmup_stats():
    """준비 구간 제외 후 분석·드롭·생략을 서로 다르게 집계하는지 합성 표본으로 확인한다.
    실제 파이프라인의 드롭 정책을 활성화하는 테스트는 아니다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    rows = [
        dict(
            completed_at=i + 1,
            measurement_start_at=1,
            measurement_end_at=4,
            analyzed=i == 1,
            dropped=i == 2,
            warmup=i == 0,
        )
        for i in range(4)
    ]
    s = summarize_metrics(rows)
    assert s["input_frames"] == 3
    assert s["analyzed_frames"] == 1
    assert s["throughput_fps"] == pytest.approx(1 / 3)
    assert s["drop_rate"] == pytest.approx(1 / 3)
    assert s["skip_rate"] == pytest.approx(1 / 3)


def test_failed_and_mixed_graphs(tmp_path):
    """실패 실행만 있으면 그래프를 생략하고 Mock·실제 결과가 섞이면 ValueError인지 확인한다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    from src.benchmark import write_table

    row = dict(
        source="TEST",
        resolution="64x48",
        pipeline_mode="baseline",
        variant="standard",
        throughput_fps=1,
        experiment_kind="throughput",
        mock=True,
        run_status="failed",
    )
    path = tmp_path / "summary.csv"
    write_table(path, [row], list(row))
    assert generate_plots(path, tmp_path) == []
    row["run_status"] = "completed"
    write_table(path, [row, {**row, "mock": False}], list(row))
    with pytest.raises(ValueError, match="Separate"):
        generate_plots(path, tmp_path)
