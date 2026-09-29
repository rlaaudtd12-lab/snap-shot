"""B 파이프라인 통합 테스트: 임시 합성 영상·설정과 명시적 Mock으로 실행 경로를 검증한다.
입력은 pytest fixture와 대역이며 출력은 단언 결과 및 임시 CSV·영상이다.
순차·스레드·분석 생략, 원본 녹화 보존, 종료·오류 전달을 확인한다.
실제 A 검출·카메라·GUI·공식 성능 실험은 검증하지 않는다."""

import csv
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import pytest
from src.pipeline import run_pipeline, validate_config, FrameSource, EventLedger


@pytest.fixture
def video(tmp_path):
    """tmp_path에 10 FPS·64×48·40프레임 합성 영상을 만들고 경로를 반환한다.
    실제 촬영 자료가 아니며 MJPG writer를 열 수 있어야 한다."""
    path = tmp_path / "TEST_ONLY.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
    assert writer.isOpened()
    for i in range(40):
        writer.write(np.full((48, 64, 3), i * 5, dtype=np.uint8))
    writer.release()
    return str(path)


@pytest.fixture
def config(tmp_path):
    """tmp_path 아래 결과 경로와 GUI 없는 명시적 Mock 설정을 반환한다.
    테스트 속도를 위해 사전·사후 시간을 0.5초로 줄이며 실제 기본 3초·5초 검증과 구분한다."""
    return validate_config(
        dict(
            output_dir=str(tmp_path / "results"),
            no_display=True,
            mock_detection=True,
            warmup_frames=0,
            pre_seconds=0.5,
            post_seconds=0.5,
        )
    )


def read_rows(path):
    """path의 UTF-8 CSV를 문자열 값의 행 딕셔너리 목록으로 반환한다. 파일 오류는 전파한다."""
    with open(path, newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.parametrize("mode", ["baseline", "threaded", "optimized"])
def test_extra_modules_integration(video, config, mode):
    config.update(recording_enabled=False, measure_fps=False, input_resolution=[32, 24])
    assert run_pipeline(video, mode, config) == 0
    run_dir = next((Path(config["output_dir"]) / "mock" / "runs").iterdir())
    snapshots = read_rows(run_dir / "snapshots.csv")
    assert len(snapshots) == 1
    from src.utils import imread_unicode
    assert imread_unicode(snapshots[0]["full_path"]).shape == (48, 64, 3)
    assert imread_unicode(snapshots[0]["thumbnail_path"]).shape == (180, 320, 3)
    assert snapshots[0]["event_id"] == read_rows(run_dir / "events.csv")[0]["event_id"]
    stats = read_rows(run_dir / "frame_analysis.csv")
    assert len(stats) == 40
    assert stats[0]["brightness_category"] == "DARK"
    assert all(row["motion_ratio"] == "" for row in stats)


def test_motion_observer_preserves_detection():
    from src import detect
    from src.pipeline import _analysis

    class Background:
        calls = 0

        def apply(self, frame):
            self.calls += 1
            mask = np.zeros(frame.shape[:2], np.uint8)
            mask[:, :10] = 127
            mask[:, 10:20] = 255
            return mask

    model = Background()
    frame = np.zeros((40, 40, 3), np.uint8)
    config = validate_config(dict(analysis_resolution=[40, 40], min_area=1))
    _, boxes, timing = _analysis(frame, model, [], config)
    assert model.calls == 1
    assert timing["motion_ratio"] == 0.25
    assert boxes == detect.detect_motion(frame, Background(), 1)


def test_snapshot_coordinates_and_unicode(tmp_path):
    from src.event_snapshot import save_alert_snapshots, save_event_snapshot
    from src.utils import imread_unicode

    frame = np.zeros((200, 400, 3), np.uint8)
    zones = [dict(name="위험구역", points=[[25, 25], [50, 25], [50, 50], [25, 50]])]
    event = dict(event_id="run-1", zone_name="위험구역", media_time_s=1)
    paths = save_alert_snapshots(frame, (100, 200, 3), zones, event, tmp_path / "한글")
    assert imread_unicode(paths["crop_path"]).shape == (91, 91, 3)
    assert not frame.any()
    with pytest.raises(FileExistsError):
        save_event_snapshot(frame, tmp_path / "한글" / "full", "run-1", "위험구역")


@pytest.mark.parametrize("failure", [None, "full", "all"])
def test_snapshot_failure_keeps_recording(video, config, monkeypatch, failure):
    from src import event_snapshot

    config["mock_events"] = [dict(zone_name="!!!", start=1, alert=1.5, end=2.5)]
    save = event_snapshot.save_event_snapshot

    def fail_selected(frame, output_dir, event_id, zone_name):
        if failure == "all" or Path(output_dir).name == failure:
            raise OSError("simulated snapshot storage failure")
        return save(frame, output_dir, event_id, zone_name)

    monkeypatch.setattr(event_snapshot, "save_event_snapshot", fail_selected)
    assert run_pipeline(video, "baseline", config) == 0
    root = next((Path(config["output_dir"]) / "mock/runs").iterdir())
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert status["processed_frames"] == 40
    event = read_rows(root / "events.csv")[0]
    assert event["status"] == "closed"
    capture = cv2.VideoCapture(event["video_path"])
    try:
        assert capture.read()[0]
    finally:
        capture.release()
    snapshot = read_rows(root / "snapshots.csv")[0]
    assert snapshot["status"] == {None: "saved", "full": "partial", "all": "failed"}[failure]
    assert bool(snapshot["error"]) == (failure is not None)
    for kind in ("full", "crop", "thumbnail"):
        if failure == "all" or kind == failure:
            assert snapshot[f"{kind}_path"] == ""
        else:
            assert Path(snapshot[f"{kind}_path"]).is_file()


def test_snapshot_uses_original_thin_zone(video, config, tmp_path, monkeypatch):
    from src.pipeline import RealBackend
    from src.zones import save_zones
    from src.utils import imread_unicode

    zone_path = tmp_path / "thin.json"
    save_zones(str(zone_path), [dict(name="thin", points=[
        [10, 5], [11, 5], [11, 40], [10, 40],
    ])], (64, 48))
    config.update(mock_detection=False, zones_path=str(zone_path),
                  input_resolution=[8, 6], consecutive_frames=1)
    # 스냅샷의 실제 backend 연결을 검증하기 위해 경보 관측만 고정한다.
    monkeypatch.setattr(RealBackend, "analyze", lambda self, frame, packet:
                        ({"thin": True}, [], {}))
    assert run_pipeline(video, "optimized", config) == 0
    root = next((Path(config["output_dir"]) / "runs").iterdir())
    snapshot = read_rows(root / "snapshots.csv")[0]
    assert snapshot["status"] == "saved"
    assert imread_unicode(snapshot["crop_path"]).shape == (48, 32, 3)


def test_missing_snapshot_zone_preserves_full_image(tmp_path):
    from src.event_snapshot import save_alert_snapshots

    frame = np.zeros((48, 64, 3), np.uint8)
    result = save_alert_snapshots(frame, frame.shape, [],
        dict(event_id="1", zone_name="missing", media_time_s=0), tmp_path)
    assert result["status"] == "partial"
    assert Path(result["full_path"]).is_file()
    assert result["crop_path"] == result["thumbnail_path"] == ""
    assert "zone not found" in result["error"]


def test_baseline_mock(video, config):
    """합성 video와 config로 순차 실행의 사건 시각·지속 시간·파일 및 40개 계측 행을 확인한다.
    Mock 결과가 별도 경로에 저장되는지 검증하며 실제 감지 정확도나 처리 속도의 합격 기준은 아니다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    assert run_pipeline(video, "baseline", config) == 0
    root = Path(config["output_dir"]) / "mock"
    rows = read_rows(root / "events.csv")
    assert len(rows) == 1
    event = rows[0]
    assert float(event["start_time"]) == 1
    assert float(event["alert_time"]) == 1.5
    assert float(event["end_time"]) == 2.5
    assert float(event["duration_seconds"]) == 1.5
    assert event["mock"] == "True"
    assert Path(event["video_path"]).is_file()
    assert not (root.parent / "events.csv").exists()
    raw = read_rows(root / "benchmark_raw.csv")
    assert len(raw) == 40
    assert all(float(r["read_ms"]) >= 0 and float(r["state_ms"]) >= 0 for r in raw)
    assert all(float(r["total_ms"]) >= 0 for r in raw)


@pytest.mark.parametrize("mode", ["baseline", "threaded", "optimized"])
def test_modes_cli(video, config, tmp_path, mode):
    """임시 영상·설정으로 세 mode의 CLI 종료 코드와 Mock 표시를 확인한다.
    subprocess를 제한 시간 내 실행하며 GUI·실제 A 검출은 검증하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.main",
            "--config",
            str(path),
            "--source",
            video,
            "--mode",
            mode,
            "--no-display",
            "--mock-detection",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert r.returncode == 0, r.stderr
    assert "[MOCK]" in r.stdout


def test_real_a_error_not_hidden(video, config):
    """실제 경로의 구역 파일 오류가 Mock으로 대체되지 않고 전파되는지 확인한다.
    A 통합 후에는 스텁 예외 대신 실제 파일 오류를 기대하여 원래의 오류 전파 목적을 유지한다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    config.update(mock_detection=False, zones_path="missing.json")
    with pytest.raises(FileNotFoundError):
        run_pipeline(video, "baseline", config)
    assert not (Path(config["output_dir"]) / "events.csv").exists()


def test_optimized_skip_preserves_recording(video, config):
    """40개 입력 중 20개만 분석해도 사건 클립은 원본 간격의 21프레임을 갖는지 확인한다.
    컨테이너 프레임 수를 검증하며 압축 후 각 픽셀의 완전 일치까지 검증하지는 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    config.update(frame_interval=2, post_seconds=0.5)
    run_pipeline(video, "optimized", config)
    root = Path(config["output_dir"]) / "mock"
    raw = read_rows(root / "benchmark_raw.csv")
    assert len(raw) == 40 and sum(r["analyzed"] == "True" for r in raw) == 20
    clips = read_rows(next(root.glob("runs/*/clips.csv")))
    cap = cv2.VideoCapture(clips[0]["path"])
    # 분석 간격으로 경보·해제가 늦어져도 1.1~3.1초 원본은 0.1초 간격의 21프레임을 유지한다.
    assert cap.get(cv2.CAP_PROP_FRAME_COUNT) == 21
    cap.release()


class FakeCapture:
    """외부 영상·카메라 없이 유한 프레임이나 읽기 오류를 제공하는 캡처 대역이다.
    장치 드라이버의 블로킹이나 코덱 처리는 재현하지 않는다."""

    def __init__(self, source, fail=False):
        """호출 규약용 source와 오류 주입 여부 fail을 받고 상태를 준비한다. 반환값은 없다."""
        self.released = False
        self.count = 0
        self.fail = fail

    def isOpened(self):
        """입력 열기 성공을 모사하는 True를 반환한다."""
        return True

    def get(self, prop):
        """prop에 해당하는 고정 FPS·크기를 반환하고 나머지 속성은 0으로 모사한다."""
        return {
            cv2.CAP_PROP_FPS: 10,
            cv2.CAP_PROP_FRAME_WIDTH: 64,
            cv2.CAP_PROP_FRAME_HEIGHT: 48,
        }.get(prop, 0)

    def read(self):
        """검은 합성 프레임을 최대 10개 반환한 뒤 (False, None)으로 EOF를 알린다.
        fail이면 두 번째 읽기에서 RuntimeError를 주입한다."""
        self.count += 1
        if self.fail and self.count == 2:
            raise RuntimeError("worker failed")
        if self.count > 10:
            return False, None
        return True, np.zeros((48, 64, 3), dtype=np.uint8)

    def release(self):
        """해제 호출 여부를 표시한다. 실제 장치 자원은 없으며 반환값은 없다."""
        self.released = True


def test_full_queue_block_and_shutdown(config):
    """용량 1의 Queue를 채워 생산자가 대기하고 드롭 없이 종료되는지 확인한다.
    FakeCapture로 스레드 종료·캡처 해제를 검증하며 실제 장치의 멈춘 read까지 재현하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    config["queue_max_size"] = 1
    reader = FrameSource("fake", config, True, capture_factory=FakeCapture)
    iter(reader)
    deadline = time.monotonic() + 2
    while reader.queue.qsize() != 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(0.1)
    assert reader.queue.qsize() == 1
    assert reader.produced <= 2
    assert reader.dropped == 0
    reader.close()
    assert not reader.thread.is_alive()
    assert reader.cap.released


def test_worker_error_delivered(config):
    """먼저 큐에 들어간 프레임 뒤에 생산자 RuntimeError가 전달되는지 확인한다.
    finally에서 종료하고 스레드와 캡처가 남지 않는지도 검증한다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    reader = FrameSource("fake", config, True, capture_factory=lambda s: FakeCapture(s, True))
    try:
        it = iter(reader)
        assert next(it)["frame_index"] == 0
        with pytest.raises(RuntimeError, match="worker failed"):
            next(it)
    finally:
        reader.close()
    assert reader.cap.released and not reader.thread.is_alive()


def test_drop_policy_rejected(config):
    """원본 보존 경로 없이 drop_oldest를 요청하면 설정 검증에서 ValueError가 나는지 확인한다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    config["queue_policy"] = "drop_oldest"
    with pytest.raises(ValueError, match="lossless"):
        validate_config(config)


def test_ledger_concurrent_dedup():
    """두 구역의 중복 경보를 사건당 한 번만 기록하고 첫 비침입 관측 기준 지속 시간을 확인한다.
    장부 메타데이터 검증이며 A의 해제 판단 알고리즘 자체를 시험하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    ledger = EventLedger("run", "v", "baseline", True)
    ledger.observe({"a": True, "b": True}, 1)
    packet = dict(captured_at=time.perf_counter(), media_time_s=2)
    events = [dict(type="alert", event_id=z, zone_name=z, media_time_s=2) for z in ("a", "b")]
    assert len(ledger.transitions(events + events, packet, 0)) == 2
    assert len(ledger.rows) == 2
    ledger.observe({"a": False, "b": False}, 3)
    cleared = [{**e, "type": "cleared", "media_time_s": 4} for e in events]
    ledger.transitions(cleared, packet, 0)
    assert all(e["duration_seconds"] == 2 for e in ledger.rows.values())


def test_input_resize_keeps_native_recording(video, config):
    """분석 입력을 32×24로 줄여도 저장 영상은 원본 64×48인지 확인한다.
    요약 CSV의 실험 해상도와 녹화 해상도를 구분하며 실제 ROI 복원 정확도는 별도다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    config["input_resolution"] = [32, 24]
    run_pipeline(video, "optimized", config)
    root = Path(config["output_dir"]) / "mock"
    ev = read_rows(root / "events.csv")[0]
    cap = cv2.VideoCapture(ev["video_path"])
    assert cap.get(cv2.CAP_PROP_FRAME_WIDTH) == 64
    assert cap.get(cv2.CAP_PROP_FRAME_HEIGHT) == 48
    cap.release()
    assert read_rows(root / "benchmark_summary.csv")[0]["resolution"] == "32x24"


def test_replay_boundary_resets_and_monotonic(video, config):
    """짧은 합성 영상을 반복하여 cycle 증가·누적 시각 단조 증가·사건 ID 고유성·스레드 정리를 확인한다.
    실제 A 배경 모델의 재학습 결과나 재생 경계의 검출 품질은 검증하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    config.update(loop_video=True, duration_seconds=0.15, recording_enabled=False)
    run_pipeline(video, "threaded", config)
    root = Path(config["output_dir"]) / "mock"
    raw = read_rows(root / "benchmark_raw.csv")
    assert max(int(r["cycle"]) for r in raw) > 0
    times = [float(r["media_time_s"]) for r in raw]
    assert all(b > a for a, b in zip(times, times[1:]))
    events = read_rows(root / "events.csv")
    assert len(events) == len({e["event_id"] for e in events})
    assert not any(t.name == "opencv-input" for t in threading.enumerate())


def test_common_a_adapter_roi_coordinates(monkeypatch):
    """monkeypatch로 A 함수를 대체하여 ROI 여유·면적 축척·복원 박스 전달을 확인한다.
    복원값을 직접 반환하는 대역이므로 A의 실제 좌표 변환이나 다각형 판정 정확도를 검증하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    from src import pipeline

    called = {}

    def prep(frame, config, roi):
        """ROI 인자를 기록하고 고정 분석 영상과 transform을 반환하는 전처리 대역이다."""
        called["roi"] = roi
        return frame[:10, :10], dict(scale_x=0.5, scale_y=0.5, offset_x=0, offset_y=0)

    def motion(frame, background, min_area):
        """전달된 min_area를 기록하고 고정 박스를 반환하여 면적 축척 연결을 검증한다."""
        called["min_area"] = min_area
        return [(1, 2, 3, 4)]

    def restore(boxes, transform):
        """입력 boxes를 기록하고 고정 복원 박스를 반환한다. 실제 좌표 변환 계산은 수행하지 않는다."""
        called["boxes"] = boxes
        return [(2, 4, 6, 8)]

    def intrusion(boxes, zones):
        """복원된 boxes가 전달되었는지 단언하고 고정 구역 침입 딕셔너리를 반환한다."""
        assert boxes == [(2, 4, 6, 8)]
        return {"z": True}

    monkeypatch.setattr(pipeline.preprocess, "preprocess_frame", prep)
    monkeypatch.setattr(pipeline.detect, "detect_motion", motion)
    monkeypatch.setattr(pipeline.preprocess, "restore_boxes", restore)
    monkeypatch.setattr(pipeline.detect, "check_intrusion", intrusion)
    c = validate_config(dict(roi_enabled=True, roi_margin=5, min_area=200))
    zones = [dict(name="z", points=[[10, 10], [20, 10], [20, 20]])]
    assert pipeline.analyze_frame(np.zeros((48, 64, 3), dtype=np.uint8), object(), zones, c) == {
        "z": True
    }
    assert called["roi"] == (5, 5, 21, 21)
    assert called["min_area"] == 50


def test_skips_do_not_extend_observed_positive_span():
    """None 관측 자체가 양성 관측 종료 시각을 늘리지 않고 미확정 시작 후보를 지우는지 확인한다.
    다음 양성 시각까지의 sampled_spans는 관측 범위이며 그 사이의 연속 침입을 입증하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    ledger = EventLedger("r", "v", "optimized", True)
    ledger.observe({"z": True}, 1)
    ledger.observe(None, 2)
    assert ledger.sampled_spans["z"] == (1, 1)
    assert "z" not in ledger.starts
    ledger.observe({"z": True}, 3)
    assert ledger.sampled_spans["z"] == (1, 3)


def test_failure_finalizes_open_writer(video, config, monkeypatch):
    """분석 중 의도한 오류로 열린 녹화가 정리되고 실패 상태와 미확정 종료 시각이 저장되는지 확인한다.
    테스트 대역으로 예외를 주입하며 파일 존재 확인이 완전한 영상 복구 보장을 의미하지는 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    from src import pipeline

    sessions = []
    original_create = pipeline.create_recorder

    def create(*args):
        """원래 생성자에 args를 전달하고 생성 세션을 수집한 뒤 반환하여 종료 상태를 검사할 수 있게 한다."""
        s = original_create(*args)
        sessions.append(s)
        return s

    class FailingMock(pipeline.MockBackend):
        """녹화가 시작된 후 분석 오류를 주입하기 위한 Mock 대역이다."""

        def analyze(self, frame, packet):
            """세 번째 입력에서 RuntimeError를 내고 나머지는 기존 Mock 분석 결과를 반환한다."""
            if packet["frame_index"] == 2:
                raise RuntimeError("analysis test failure")
            return super().analyze(frame, packet)

    config["mock_events"] = [dict(zone_name="z", start=0, alert=0, end=100)]
    backend = FailingMock(config, (64, 48))
    monkeypatch.setattr(pipeline, "create_recorder", create)
    with pytest.raises(RuntimeError, match="analysis test failure"):
        run_pipeline(video, "threaded", config, backend=backend)
    assert sessions[0]["closed"] and not sessions[0]["active"]
    assert not any(t.name == "opencv-input" for t in threading.enumerate())
    root = Path(config["output_dir"]) / "mock"
    ev = read_rows(root / "events.csv")[0]
    assert ev["status"] == "failed" and ev["end_time"] == ""
    assert Path(ev["video_path"]).is_file()
    assert json.loads(next(root.glob("runs/*/status.json")).read_text())["status"] == "failed"


def test_realtime_schedule(video, config):
    """10 FPS 합성 입력의 예정 시각 간격 0.1초와 예정 시각 이전에 투입하지 않음을 확인한다.
    상한 지연이나 실제 카메라 실시간 처리 성능은 검증하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    config.update(experiment_kind="realtime", duration_seconds=0.15, recording_enabled=False)
    run_pipeline(video, "baseline", config)
    raw = read_rows(Path(config["output_dir"]) / "mock" / "benchmark_raw.csv")
    assert len(raw) >= 2
    planned = [float(r["scheduled_at"]) for r in raw]
    assert planned[1] - planned[0] == pytest.approx(0.1)
    assert all(float(r["captured_at"]) >= float(r["scheduled_at"]) for r in raw)


def test_headless_preview(video):
    """합성 video로 --preview --no-display 경로가 0을 반환하는지 확인한다. GUI 렌더링은 검증하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    from src.main import main

    assert main(["--source", video, "--preview", "--no-display"]) == 0


def test_pts_failure_releases_capture(config):
    """반복된 PTS를 반환하는 대역으로 시각 검증 오류가 전달되고 캡처가 해제되는지 확인한다.
    실제 VFR 파일과 장치별 PTS 지원 여부는 검증하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    config["timestamp_mode"] = "pts"
    reader = FrameSource("fake", config, True, capture_factory=FakeCapture)
    try:
        it = iter(reader)
        next(it)
        with pytest.raises(ValueError, match="timestamps"):
            next(it)
    finally:
        reader.close()
    assert reader.cap.released
