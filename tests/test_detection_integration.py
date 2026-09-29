"""A의 실제 함수와 B·C 연결을 검증한다. 합성 입력은 촬영 영상 정확도 평가가 아니다."""

from src.state import update_state
import csv
import json

import cv2
import numpy as np
import pytest

from src import detect, preprocess, zones
from src.main import main
from src.pipeline import RealBackend, FrameSource, _draw, run_pipeline, validate_config


def test_reentry_immediately_after_cleared():
    """해제 다음 프레임부터 새 연속 감지를 세어 두 번째 경보가 발생해야 한다.
    A 원본의 CLEARED 고착을 먼저 재현하며 N=2 판정 기준 자체는 바꾸지 않는다.
    """
    states = {}
    observed, transitions = [], []
    for index, detected in enumerate((True, True, False, True, True)):
        states, events = update_state(
            states, {"zone1": detected}, index, index / 10, {"alert_frames": 2, "clear_frames": 1}
        )
        observed.append(states["zone1"]["state"])
        transitions.extend(e["event"] for e in events)
    assert observed == ["DETECTING", "ALERT", "CLEARED", "DETECTING", "ALERT"]
    assert transitions == ["alert", "cleared", "alert"]


def test_json_and_display_coordinates(tmp_path):
    """축소 화면의 점을 원본으로 복원하고 저장·불러오기 결과가 같은지 확인한다."""
    # 각 축의 반올림된 표시 크기로 역변환해야 너비 기준 배율만 쓸 때의 오차가 없다.
    size = (1920, 1080)
    shown = zones.display_size(size, (853, 480))
    point = zones.to_original((shown[0] // 2, shown[1] // 2), size, shown)
    assert abs(point[0] - 960) <= 2 and abs(point[1] - 540) <= 2
    items = [{"name": "위험 구역", "points": [[10, 20], point, [100, 900]]}]
    path = tmp_path / "zones.json"
    zones.save_zones(str(path), items, size)
    assert zones.load_zones(str(path), size) == items
    assert set(json.loads(path.read_text())) == {"schema_version", "frame_size", "zones"}
    with pytest.raises(ValueError):
        zones.load_zones(str(path), (1280, 720))
    old = path.read_bytes()
    for points in (
        [[1, 1], [2, 2]],
        [[1, 1], [2, 2], [3, 3]],
        [[0, 0], [100, 100], [0, 100], [100, 0]],
        [[-1, 0], [100, 0], [100, 100]],
    ):
        with pytest.raises(ValueError):
            zones.save_zones(str(path), [{"name": "bad", "points": points}], size)
        assert path.read_bytes() == old


def test_roi_rounding_and_mog2_config():
    """ROI 오프셋과 서로 다른 실제 축척을 검증하고 B의 중첩 MOG2 설정 적용을 확인한다."""
    frame = np.zeros((151, 211, 3), np.uint8)
    image, transform = preprocess.preprocess_frame(
        frame, {"analysis_resolution": [63, 40]}, (11, 17, 123, 79)
    )
    assert image.shape[:2] == (40, 62)
    assert transform["scale_x"] == 62 / 123
    assert transform["scale_y"] == 40 / 79
    assert preprocess.restore_boxes([(0, 0, 62, 40)], transform) == [(11, 17, 123, 79)]
    model = detect.create_background_subtractor(
        {"mog2": {"history": 77, "var_threshold": 23, "detect_shadows": False}}
    )
    assert model.getHistory() == 77 and model.getVarThreshold() == 23
    assert not model.getDetectShadows()


def test_backend_default_threshold_and_warning(tmp_path):
    """A의 기본 1프레임 대신 B의 5프레임을 적용하며 전이를 화면 경고로 전달한다.
    이 검사는 상태 어댑터에 관측값을 직접 주며 실제 MOG2 픽셀 검사는 아래 영상 테스트가 한다.
    """
    path = tmp_path / "zones.json"
    zones.save_zones(
        str(path), [{"name": "zone1", "points": [[0, 0], [99, 0], [99, 79], [0, 79]]}], (100, 80)
    )
    backend = RealBackend(validate_config({"zones_path": str(path)}), (100, 80), (100, 80))
    for index in range(5):
        states, events = backend.update(
            {"zone1": True}, {"frame_index": index, "media_time_s": index / 10}
        )
        assert bool(events) == (index == 4)
    assert states["zone1"]["status"] == "ALERT"
    out = _draw(
        np.zeros((80, 100, 3), np.uint8), "baseline", 0, backend.zones, [], states, True, False
    )
    assert tuple(out[1, 1]) == (0, 0, 255)
    assert events[0]["type"] == "alert" and events[0]["zone_name"] == "zone1"
    with pytest.raises(ValueError, match="frame_interval"):
        validate_config({"frame_interval": 2})


@pytest.fixture
def real_video(tmp_path):
    """10 FPS 합성 영상에 움직이는 밝은 직사각형을 넣는다. 실제 촬영 자료는 아니다.
    MOG2를 대체하지 않고 실제 픽셀 분석을 실행하며 3초 사전·5초 사후 녹화 여유를 둔다.
    """
    path = tmp_path / "synthetic_actual_mog2.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 120))
    assert writer.isOpened()
    for index in range(210):
        frame = np.zeros((120, 160, 3), np.uint8)
        if 40 <= index < 140:
            x = 20 + (index * 7) % 95
            cv2.rectangle(frame, (x, 40), (x + 20, 80), (255, 255, 255), -1)
        writer.write(frame)
    writer.release()
    zone_path = tmp_path / "zones.json"
    zones.save_zones(
        str(zone_path),
        [{"name": "zone1", "points": [[5, 5], [155, 5], [155, 110], [5, 110]]}],
        (160, 120),
    )
    return path, zone_path


def test_real_three_modes_csv_and_recording(real_video, tmp_path):
    """세 모드에서 실제 MOG2→상태→CSV→원본 녹화를 검증한다.
    파일 시각으로 경보 전 3초·해제 후 5초를 확인한다. 출력 영상은 재디코딩하여
    해상도와 프레임 수를 확인하며 이 합성 결과를 실제 촬영 정확도나 성능으로 해석하지 않는다.
    """
    source, zone_path = real_video
    signatures = []
    for mode in ("baseline", "threaded", "optimized"):
        root = tmp_path / mode
        config = dict(
            zones_path=str(zone_path),
            output_dir=str(root),
            no_display=True,
            mock_detection=False,
            warmup_frames=0,
            min_area=30,
            roi_enabled=True,
            roi_margin=0,
            analysis_resolution=[120, 90],
            mog2={"history": 500, "var_threshold": 16, "detect_shadows": False},
        )
        assert run_pipeline(str(source), mode, config) == 0
        run = next((root / "runs").iterdir())
        with (run / "events.csv").open() as stream:
            events = list(csv.DictReader(stream))
        with (run / "clips.csv").open() as stream:
            clips = list(csv.DictReader(stream))
        assert len(events) == len(clips) == 1
        event, clip = events[0], clips[0]
        assert event["mock"] == "False" and event["status"] == "closed"
        assert float(event["alert_time"]) >= 4.4
        assert float(clip["start_s"]) == pytest.approx(float(event["alert_time"]) - 3)
        # CSV end_time은 첫 비침입 관측이다. clear_frames=10이면 해제 확정은 0.9초 뒤다.
        assert float(clip["end_s"]) == pytest.approx(float(event["end_time"]) + 0.9 + 5)
        assert float(clip["end_s"]) > float(event["alert_time"]) + 5
        assert clip["truncated"] == "False" and event["video_path"] == clip["path"]
        cap = cv2.VideoCapture(clip["path"])
        count = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            assert frame.shape == (120, 160, 3)
            count += 1
        cap.release()
        assert count == int(clip["frames_written"])
        effective = json.loads((run / "effective_config.json").read_text())
        assert effective["zones"] == zones.load_zones(str(zone_path), (160, 120))
        assert effective["config"]["frame_interval"] == 1
        assert effective["config"]["queue_policy"] == "block"
        signatures.append((event["alert_time"], event["end_time"]))
    assert signatures[0] == signatures[1] == signatures[2]


def test_headless_cli_zones(real_video, tmp_path):
    """main.py의 --zones와 --no-display 조합이 편집기 없이 실제 감지를 실행하는지 확인한다."""
    source, zone_path = real_video
    assert (
        main(
            [
                "--source",
                str(source),
                "--zones",
                str(zone_path),
                "--no-display",
                "--output-dir",
                str(tmp_path / "cli"),
            ]
        )
        == 0
    )


def test_editor_keys_without_real_gui(tmp_path, monkeypatch):
    """GUI 호출만 대체하고 실제 편집 콜백·키 분기·JSON·좌표 변환을 실행한다.
    물리 화면의 HiDPI나 창 관리자 동작 검증을 대신하지 않는다.
    """
    callback = {}
    path = tmp_path / "edited.json"
    actions = [
        (13, [(10, 10), (20, 10)]),  # 두 점은 확정 불가
        (ord("r"), []),
        (8, [(10, 10), (100, 10), (100, 100), (110, 110)]),
        (13, []),  # 마지막 점 취소 후 첫 삼각형 확정
        (13, [(150, 20), (200, 20), (200, 100)]),
        (ord("s"), []),
        (ord("l"), [(250, 150)]),  # 불러오기는 저장된 두 구역을 복원하고 작성 점을 비움
        (32, []),
    ]

    def key(delay):
        """예약된 클릭을 실제 콜백으로 전달하고 다음 키를 반환한다."""
        code, clicks = actions.pop(0)
        for x, y in clicks:
            callback["mouse"](cv2.EVENT_LBUTTONDOWN, x, y, 0, None)
        return code

    monkeypatch.setattr(cv2, "namedWindow", lambda *args: None)
    monkeypatch.setattr(cv2, "setMouseCallback", lambda name, fn: callback.update(mouse=fn))
    monkeypatch.setattr(cv2, "imshow", lambda *args: None)
    monkeypatch.setattr(cv2, "waitKeyEx", key)
    monkeypatch.setattr(cv2, "getWindowProperty", lambda *args: 1)
    monkeypatch.setattr(cv2, "destroyWindow", lambda *args: None)
    result = zones.edit_zones(np.zeros((600, 800, 3), np.uint8), str(path), bounds=(400, 300))
    assert len(result) == 2
    assert result[0]["points"] == [[20, 20], [200, 20], [200, 200]]
    assert result == zones.load_zones(str(path), (800, 600))
    assert not actions


@pytest.mark.parametrize("exit_key", [27, ord("q")])
def test_editor_cancel(tmp_path, monkeypatch, exit_key):
    """Q/Esc는 저장 파일을 만들지 않고 감시 취소를 반환한다."""
    monkeypatch.setattr(cv2, "namedWindow", lambda *args: None)
    monkeypatch.setattr(cv2, "setMouseCallback", lambda *args: None)
    monkeypatch.setattr(cv2, "imshow", lambda *args: None)
    monkeypatch.setattr(cv2, "waitKeyEx", lambda delay: exit_key)
    monkeypatch.setattr(cv2, "destroyWindow", lambda *args: None)
    path = tmp_path / "not_saved.json"
    assert zones.edit_zones(np.zeros((100, 100, 3), np.uint8), str(path), bounds=(100, 100)) == []
    assert not path.exists()


@pytest.mark.parametrize("threaded", [False, True])
def test_editor_preserves_first_packet(real_video, monkeypatch, threaded):
    """편집기에서 읽은 첫 원본을 순차·스레드 입력 모두 한 번만 전달하는지 확인한다.
    UI 대기 이전에 생산 스레드를 시작하지 않으므로 설정 중 프레임이 Queue에 쌓이지 않는다.
    """
    source, path = real_video
    reader = FrameSource(str(source), validate_config({"zones_path": str(path)}), threaded=threaded)
    seen = []

    def edit(frame, path):
        """물리 GUI 대신 첫 프레임을 보관하고 기존 구역을 반환한다."""
        assert reader.thread is None
        seen.append(frame.copy())
        return zones.load_zones(path, (160, 120))

    monkeypatch.setattr(zones, "edit_zones", edit)
    try:
        assert reader.edit_first_frame()
        packets = list(reader)
        assert len(packets) == 210
        assert packets[0]["frame_index"] == 0 and packets[0]["media_time_s"] == 0
        assert np.array_equal(packets[0]["original_frame"], seen[0])
        assert [p["frame_index"] for p in packets] == list(range(210))
    finally:
        reader.close()


def test_input_resize_keeps_native_recording(real_video, tmp_path):
    """입력 실험 축소·ROI·분석 축소를 함께 적용해도 JSON과 녹화는 원본 기준을 유지한다."""
    source, zone_path = real_video
    config = validate_config(
        dict(
            zones_path=str(zone_path),
            no_display=True,
            output_dir=str(tmp_path / "resized"),
            input_resolution=[80, 60],
            roi_enabled=True,
            analysis_resolution=[60, 45],
            min_area=10,
        )
    )
    backend = RealBackend(config, (160, 120), (80, 60))
    assert backend.zones[0]["points"] == [[2, 2], [78, 2], [78, 55], [2, 55]]
    assert run_pipeline(str(source), "optimized", config) == 0
    clip_paths = list((tmp_path / "resized").glob("runs/*/clips/*.mp4"))
    assert clip_paths
    for path in clip_paths:
        cap = cv2.VideoCapture(str(path))
        try:
            ok, frame = cap.read()
            assert ok and frame.shape == (120, 160, 3)
        finally:
            cap.release()


def test_main_connects_editor_before_monitor(real_video, tmp_path, monkeypatch):
    """main의 GUI 경로가 첫 프레임 편집·저장 후 동일 JSON으로 감시를 시작하는지 검사한다.
    창 출력만 대체한 자동 검사이며 실제 마우스 조작 성공을 주장하지 않는다.
    """
    source, _ = real_video
    path = tmp_path / "from_editor.json"
    calls = []

    def edit(frame, target):
        """편집 완료를 모사하되 실제 JSON 저장 함수를 호출한다."""
        calls.append(frame.shape)
        items = [{"name": "edited", "points": [[5, 5], [150, 5], [150, 110]]}]
        zones.save_zones(target, items, (160, 120))
        return items

    monkeypatch.setattr(zones, "edit_zones", edit)
    monkeypatch.setattr(cv2, "imshow", lambda *args: None)
    monkeypatch.setattr(cv2, "waitKey", lambda delay: ord("q"))
    monkeypatch.setattr(cv2, "destroyAllWindows", lambda: None)
    output = tmp_path / "gui_flow"
    assert main(["--source", str(source), "--zones", str(path), "--output-dir", str(output)]) == 0
    assert calls == [(120, 160, 3)]
    run = next((output / "runs").iterdir())
    effective = json.loads((run / "effective_config.json").read_text())
    assert effective["zones"] == zones.load_zones(str(path), (160, 120))
    assert json.loads((run / "status.json").read_text())["processed_frames"] == 1


def test_real_ablation_rejected_before_work(real_video, monkeypatch):
    """간격 2가 포함된 실제 실험을 부분 실행하지 않고 사전 거부하는지 확인한다."""
    from src import benchmark

    source, _ = real_video
    with pytest.raises(ValueError, match="interval_only"):
        benchmark.run_suite(str(source), {}, ablations=True)
