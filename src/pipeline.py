"""B: 공통 분석, 원본 보존 순차/입력 스레드 실행, 사건 CSV 및 화면 표시.
입력: source/mode/config, 선택적 명시적 Mock backend. 출력: 종료 코드와 results.
의존: 통합한 A의 preprocess/detect/zones/state, recorder, benchmark.
A NotImplementedError는 전파한다. Mock은 고정 시간표이며 실제 검출이 아니다.
큐 드롭은 원본 보존 경로가 없는 현재 구현에서 명시적으로 비활성화한다.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import platform
from queue import Queue, Empty, Full
import threading
import time
from typing import Any
import uuid

import cv2
import numpy as np

from . import detect, preprocess, state, zones as zone_module
from .benchmark import measure, save_run_metrics, write_table
from .recorder import create_recorder, buffer_frame, record_frame, close_recorder
from . import event_snapshot, frame_analysis

ANALYSIS_FIELDS = [
    "frame_index", "cycle", "media_time_s", "analyzed", "brightness",
    "brightness_category", "motion_ratio", "motion_basis", "mock",
]
SNAPSHOT_FIELDS = [
    "event_id", "zone_name", "media_time_s", "full_path", "crop_path", "thumbnail_path",
    "status", "error",
]

EVENT_FIELDS = [
    "run_id",
    "video_name",
    "mock",
    "event_id",
    "zone_name",
    "start_time",
    "alert_time",
    "end_time",
    "duration_seconds",
    "video_path",
    "pipeline_mode",
    "status",
    "occurred_at",
    "alert_perf_counter",
    "input_perf_counter",
    "system_alert_delay_ms",
    "scheduled_alert_delay_ms",
    "onset_basis",
    "truncated",
]
CLIP_FIELDS = [
    "path",
    "event_ids",
    "zone_name",
    "alert_time_s",
    "occurred_at",
    "start_s",
    "end_s",
    "truncated",
    "pre_truncated",
    "post_truncated",
    "frames_written",
    "held_ticks",
    "resampled_inputs",
    "recording_fps",
    "resampling",
]


def validate_config(config: dict) -> dict:
    """config를 깊은 복사하여 B·C 기본값을 채우고 검증된 설정 딕셔너리를 반환한다.
    호출자의 설정과 A 함수 시그니처는 변경하지 않는다. 잘못된 값이나 지원하지 않는 드롭 정책은
    입력·writer를 열기 전에 ValueError로 거부한다. 별도 무손실 녹화 경로가 없으므로
    Queue에서 원본을 버리는 drop_oldest는 허용하지 않는다. 필수 구조의 형식 오류도 전파한다."""
    c = deepcopy(config)
    defaults = dict(
        queue_max_size=8,
        queue_policy="block",
        frame_interval=1,
        consecutive_frames=5,
        clear_frames=10,
        min_area=200,
        blur_kernel=5,
        warmup_frames=30,
        pre_seconds=3.0,
        post_seconds=5.0,
        analysis_resolution=[640, 360],
        roi_enabled=False,
        roi_margin=32,
        resize_enabled=True,
        measure_fps=True,
        no_display=False,
        mock_detection=False,
        recording_enabled=True,
        recorder_max_mb=1024,
        recording_codec="mp4v",
        output_dir="results",
        loop_video=False,
        duration_seconds=0.0,
        experiment_kind="throughput",
        camera_fps=30.0,
        timestamp_mode="cfr",
        thread_join_timeout=5.0,
        variant="standard",
        repeat=1,
        input_resolution=None,
        snapshot_enabled=True,
        frame_analysis_enabled=True,
    )
    for key, value in defaults.items():
        c.setdefault(key, value)
    for key in (
        "queue_max_size",
        "frame_interval",
        "consecutive_frames",
        "clear_frames",
        "min_area",
        "blur_kernel",
    ):
        if type(c[key]) is not int or c[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("warmup_frames", "roi_margin"):
        if type(c[key]) is not int or c[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    if c["blur_kernel"] % 2 != 1:
        raise ValueError("blur_kernel must be odd")
    for key in (
        "pre_seconds",
        "post_seconds",
        "duration_seconds",
        "camera_fps",
        "recorder_max_mb",
        "thread_join_timeout",
    ):
        if (
            isinstance(c[key], bool)
            or not isinstance(c[key], (int, float))
            or not math.isfinite(c[key])
            or c[key] < 0
        ):
            raise ValueError(f"{key} must be finite and nonnegative")
    if min(c["camera_fps"], c["recorder_max_mb"], c["thread_join_timeout"]) <= 0:
        raise ValueError("camera_fps/recorder_max_mb/thread_join_timeout must be positive")
    for key in ("analysis_resolution", "input_resolution"):
        value = c[key]
        if value is None and key == "input_resolution":
            continue
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or any(type(x) is not int or x <= 0 for x in value)
        ):
            raise ValueError(f"{key} must be [width,height] positive integers")
    for key in (
        "roi_enabled",
        "resize_enabled",
        "measure_fps",
        "no_display",
        "mock_detection",
        "recording_enabled",
        "loop_video",
        "snapshot_enabled",
        "frame_analysis_enabled",
    ):
        if type(c[key]) is not bool:
            raise ValueError(f"{key} must be bool")
    if c["queue_policy"] != "block":
        raise ValueError(
            "drop_oldest is disabled: a separate lossless recording path is not implemented; use block"
        )
    # A는 미분석 프레임에서 카운터를 유지한다. 연속 원본 프레임이라는 기준을
    # 바꾸지 않도록 실제 모드에서는 생략을 거부하고 Mock 실험만 기존 동작을 유지한다.
    if not c["mock_detection"] and c["frame_interval"] != 1:
        raise ValueError("Real detection requires frame_interval=1")
    if c["timestamp_mode"] not in ("cfr", "pts"):
        raise ValueError("timestamp_mode must be cfr or pts")
    if c["experiment_kind"] not in ("throughput", "realtime"):
        raise ValueError("experiment_kind must be throughput or realtime")
    if not isinstance(c["recording_codec"], str) or len(c["recording_codec"]) != 4:
        raise ValueError("recording_codec must be four characters")
    if c["loop_video"] and c["duration_seconds"] <= 0:
        raise ValueError("loop_video requires a duration limit")
    return c


class FrameSource:
    """캡처 하나를 소유하며 프레임 패킷을 순차 또는 제한 크기 Queue로 공급한다.
    입력 스레드는 읽기·입력 크기 변환·큐 삽입을 담당하고 분석·상태·녹화·GUI는 소비자가 담당한다.
    파일 시각은 CFR의 인덱스/FPS 또는 증가하는 PTS, 카메라는 perf_counter 경과 초를 쓴다.
    큐가 차면 생산자가 기다려 원본을 보존한다. 종료·오류는 큐 밖의 Event와 error로 전달하여
    가득 찬 큐에 종료 표식을 넣다가 멈추지 않게 한다. 소비자는 이미 큐에 있는 자료부터 받는다."""

    def __init__(self, source: str, config: dict, threaded=False, *, capture_factory=None):
        """source를 열어 FPS·크기를 검증하고 종료 신호와 큐를 준비한다. 아직 스레드는 시작하지 않는다.
        config는 검증된 설정, threaded는 입력 스레드 사용 여부, capture_factory는 테스트용 대체 생성자다.
        숫자 source는 카메라다. 파일 FPS 누락·잘못된 크기·카메라 반복은 ValueError, 열기 실패는 OSError다.
        초기화 실패 시 캡처를 해제하며 생성자 반환값은 없다."""
        self.config, self.threaded = config, threaded
        self.camera = str(source).isdigit()
        if self.camera and config["loop_video"]:
            raise ValueError("Camera input cannot loop")
        factory = capture_factory or cv2.VideoCapture
        self.cap = factory(int(source) if self.camera else str(source))
        try:
            if not self.cap.isOpened():
                raise OSError(f"Cannot open input: {source}")
            fps = float(self.cap.get(cv2.CAP_PROP_FPS))
            self.native_size = (
                int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            )
            if min(self.native_size) <= 0:
                raise ValueError("Input dimensions unavailable")
            if not self.camera and (not math.isfinite(fps) or fps <= 0):
                raise ValueError("File FPS unavailable: provide valid timestamped video")
            self.fps = fps if math.isfinite(fps) and fps > 0 else float(config["camera_fps"])
            self.size = tuple(config["input_resolution"] or self.native_size)
        except BaseException:
            self.cap.release()
            raise
        self.stop = threading.Event()
        self.done = threading.Event()
        self.queue = Queue(maxsize=config["queue_max_size"])
        self.error = None
        self.thread = None
        self.generator = None
        self.dropped = 0
        self.produced = 0
        self.closed = False
        self.first_frame = None

    def edit_first_frame(self):
        """생산 스레드 시작 전에 원본 첫 프레임으로 편집기를 열고 시작 여부를 반환한다.
        캡처를 다시 열거나 파일을 되감지 않고 첫 프레임을 보관하여 감시에도 정확히 한 번
        전달한다. UI 대기 시간은 감시 시각에 포함하지 않으며 Queue 생산도 아직 시작하지 않는다.
        """
        ok, frame = self.cap.read()
        if not ok:
            raise OSError("Input has no decodable frames")
        self.first_frame = frame
        return bool(zone_module.edit_zones(frame, self.config["zones_path"]))

    def _packets(self):
        """캡처를 읽어 프레임·인덱스·영상 시각·계측 시각이 담긴 패킷을 차례로 산출한다.
        original_frame은 디코드 원본, frame은 입력 해상도 실험에 맞춘 영상이다. 같은 크기이면
        같은 배열을 참조하므로 이후 버퍼 저장·화면 그리기에서 복사하여 원본을 보호한다.
        반복 재생은 cycle로 드러내고 누적 offset으로 영상 시각의 단조 증가를 유지한다.
        realtime 파일은 예정 시각까지 기다리며 read_ms에는 의도한 대기를 넣지 않는다.
        빈 입력·되감기 실패는 OSError, 잘못되거나 증가하지 않는 시각은 ValueError다. 종료 시 캡처를 해제한다."""
        index, cycle, local_index, offset = 0, 0, 0, 0.0
        epoch = time.perf_counter()
        last_media, first_pts = None, None
        try:
            while not self.stop.is_set():
                read_start = time.perf_counter()
                if self.first_frame is not None:
                    # UI에서 본 원본도 일반 패킷으로 전달하여 첫 프레임을 유실하지 않는다.
                    ok, frame = True, self.first_frame
                    self.first_frame = None
                else:
                    ok, frame = self.cap.read()
                if not ok:
                    if local_index == 0:
                        raise OSError("Input has no decodable frames")
                    if not self.config["loop_video"] or self.stop.is_set():
                        break
                    if not self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0):
                        raise OSError("Cannot rewind input")
                    offset = last_media + 1 / self.fps
                    cycle += 1
                    local_index, first_pts = 0, None
                    continue
                acquired = time.perf_counter()
                decode_ms = (acquired - read_start) * 1000
                if self.camera:
                    media = acquired - epoch
                elif self.config["timestamp_mode"] == "pts":
                    pts = float(self.cap.get(cv2.CAP_PROP_POS_MSEC)) / 1000
                    if not math.isfinite(pts) or pts < 0:
                        raise ValueError("Invalid file PTS; use explicitly verified CFR input")
                    if first_pts is None:
                        first_pts = pts
                    media = offset + pts - first_pts
                else:
                    media = offset + local_index / self.fps
                if last_media is not None and media <= last_media:
                    raise ValueError("Input timestamps are not strictly increasing")
                scheduled = epoch + media
                if not self.camera and self.config["experiment_kind"] == "realtime":
                    if self.stop.wait(max(0, scheduled - time.perf_counter())):
                        break
                    acquired = (
                        time.perf_counter()
                    )  # 재생 대기가 끝나 프레임이 실제 시스템에 투입된 시각
                original_frame = frame
                resize_start = time.perf_counter()
                if self.size != self.native_size:
                    frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
                read_ms = decode_ms + (time.perf_counter() - resize_start) * 1000
                packet = dict(
                    frame=frame,
                    original_frame=original_frame,
                    frame_index=index,
                    cycle=cycle,
                    cycle_frame_index=local_index,
                    media_time_s=media,
                    cycle_time_s=media - offset,
                    captured_at=acquired,
                    scheduled_at=scheduled
                    if self.config["experiment_kind"] == "realtime"
                    else None,
                    read_ms=read_ms,
                    enqueued_at=time.perf_counter(),
                    read_started_at=read_start,
                )
                self.produced += 1
                yield packet
                last_media = media
                index += 1
                local_index += 1
        finally:
            self.cap.release()

    def _produce(self):
        """패킷을 Queue에 넣는 생산자이며 반환값은 없다.
        큐가 가득 차면 짧은 타임아웃으로 재시도하면서 stop을 확인한다. 읽기 오류는 error에 보관하고
        finally에서 캡처 해제와 done 신호를 보내 소비자가 큐 소진 뒤 오류 또는 EOF를 받게 한다."""
        try:
            for packet in self._packets():
                while not self.stop.is_set():
                    try:
                        self.queue.put(packet, timeout=0.05)
                        break
                    except Full:
                        continue
                if self.stop.is_set():
                    break
        except BaseException as exc:
            self.error = exc
        finally:
            self.cap.release()
            self.done.set()

    def __iter__(self):
        """자신을 반복자로 반환하고 최초 호출에서만 스레드 또는 순차 생성기를 시작한다.
        소비자의 분석·녹화 작업은 이 메서드가 별도 스레드로 옮기지 않는다."""
        if self.threaded:
            if self.thread is None:
                self.thread = threading.Thread(
                    target=self._produce, name="opencv-input", daemon=True
                )
                self.thread.start()
        elif self.generator is None:
            self.generator = self._packets()
        return self

    def __next__(self):
        """다음 패킷을 반환한다. 스레드 모드에서는 큐의 패킷을 먼저 소비한다.
        빈 큐에서 done을 확인한 뒤 저장된 생산자 예외를 전달하거나 StopIteration으로 끝낸다.
        타임아웃을 두어 EOF 뒤 무기한 Queue.get에 갇히지 않게 한다. 먼저 iter로 시작해야 한다."""
        if not self.threaded:
            return next(self.generator)
        while True:
            try:
                return self.queue.get(timeout=0.05)
            except Empty:
                if self.done.is_set():
                    if self.error:
                        error, self.error = self.error, None
                        raise error
                    raise StopIteration

    def close(self):
        """stop 신호를 보내고 입력 스레드 종료를 기다린 뒤 캡처를 해제한다. 반환값은 없다.
        이미 닫았다면 아무 작업도 하지 않는다. thread_join_timeout 안에 끝나지 않으면 RuntimeError다.
        일부 장치의 OpenCV read는 중단 신호를 따르지 않으므로 모든 장치의 제한 시간 내 종료를 보장하지 않는다.
        daemon은 해당 스레드 때문에 인터프리터 종료가 막히는 것을 줄일 뿐 장치 읽기를 강제 취소하지 않는다."""
        if self.closed:
            return
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=self.config["thread_join_timeout"])
            if self.thread.is_alive():
                raise RuntimeError("Camera/backend read did not stop within join timeout")
        if self.generator:
            self.generator.close()
        self.cap.release()
        self.closed = True


def _analysis(frame, background_subtractor, zones, config) -> tuple[dict, list, dict]:
    """입력 frame에 A의 전처리·움직임 검출·구역 판정을 적용하고 (침입 dict, 복원 박스, 단계 ms)를 반환한다.
    background_subtractor는 실행의 배경 모델, zones는 입력 좌표의 다각형, config는 분석 설정이다.
    ROI는 모든 구역을 감싸는 범위에 여유를 더하고 입력 경계로 자른다. 전처리의 transform은
    ROI offset과 분석/입력 scale을 담는다. 검출 최소 면적에는 scale_x*scale_y를 곱하고,
    박스는 A의 restore_boxes로 입력 좌표에 복원한 뒤 같은 좌표계의 구역과 비교한다.
    여기서 입력 좌표는 input_resolution 적용 후 기준이며 녹화용 native 원본과 다를 수 있다.
    실제 변환·판정은 A 구현에 위임하고 미구현 및 OpenCV 오류는 그대로 전달한다."""
    timing = {}
    roi = None
    if config["roi_enabled"] and zones:
        points = np.concatenate([np.asarray(z["points"]) for z in zones])
        margin = config["roi_margin"]
        h, w = frame.shape[:2]
        lo = np.maximum(points.min(axis=0) - margin, [0, 0]).astype(int)
        hi = np.minimum(points.max(axis=0) + margin + 1, [w, h]).astype(int)
        roi = (int(lo[0]), int(lo[1]), int(hi[0] - lo[0]), int(hi[1] - lo[1]))
    with measure(timing, "preprocess"):
        image, transform = preprocess.preprocess_frame(frame, config, roi)
    with measure(timing, "detect"):
        observer = (
            frame_analysis.MaskObserver(background_subtractor)
            if config["frame_analysis_enabled"] else background_subtractor
        )
        boxes = detect.detect_motion(
            image,
            observer,
            config["min_area"] * transform["scale_x"] * transform["scale_y"],
        )
    if config["frame_analysis_enabled"] and observer.mask is not None:
        timing["motion_ratio"] = frame_analysis.calculate_motion_ratio(observer.mask)
    with measure(timing, "intrusion"):
        boxes = preprocess.restore_boxes(boxes, transform)
        intrusions = detect.check_intrusion(boxes, zones)
    return intrusions, boxes, timing


def analyze_frame(
    frame: np.ndarray,
    background_subtractor: Any,
    zones: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, bool]:
    """frame·배경 모델·zones·config로 분석하여 구역별 침입 bool 딕셔너리를 반환한다.
    기존 공개 조합 인터페이스를 유지하며 박스와 계측값은 내부에서만 사용한다.
    설정 검증 오류와 A 함수의 오류를 전파한다. zones 좌표는 frame 좌표와 일치해야 한다."""
    return _analysis(frame, background_subtractor, zones, validate_config(config))[0]


class RealBackend:
    """A의 기존 인터페이스를 연결하는 실제 분석 어댑터다.
    픽셀 기반 전처리·검출과 상태 판정을 A에 위임하며 실패해도 Mock으로 자동 전환하지 않는다."""

    def __init__(self, config, native_size, size):
        """config의 구역 파일을 native_size 기준으로 읽고 필요하면 size 입력 좌표로 변환한다.
        원본 구역은 스냅샷용으로 보존하고 감지 구역은 각 축의 비율을 곱해 반올림한다.
        이 입력 크기 변환은 이후 ROI·분석 축소 변환과 별개다.
        배경 모델과 상태를 초기화하며 반환값은 없다. A의 구역 로딩·모델 생성 오류는 전파한다."""
        self.config = config
        self.zones = zone_module.load_zones(config["zones_path"], native_size)
        self.original_zones = deepcopy(self.zones)
        if native_size != size:
            self.zones = deepcopy(self.zones)
            for zone in self.zones:
                zone["points"] = [
                    [round(x * size[0] / native_size[0]), round(y * size[1] / native_size[1])]
                    for x, y in zone["points"]
                ]
        self.reset()

    def reset(self):
        """반복 재생 경계에서 배경 모델을 새로 만들고 상태를 비운다. 반환값은 없다.
        이전 재생 끝과 다음 시작을 연속 사건으로 보지 않기 위함이며 A 모델 생성 오류는 전파한다."""
        self.background = detect.create_background_subtractor(self.config)
        self.states = {}
        # A에는 사건 ID가 없으므로 경보마다 새 번호를 만들고 해제까지 연결한다.
        # 이 어댑터는 소비 스레드에서만 호출되어 상태와 CSV 순서가 뒤섞이지 않는다.
        self.event_number = 0
        self.active_ids = {}

    def analyze(self, frame, packet):
        """frame을 공통 A 경로로 분석하여 (구역별 bool, 입력 좌표 박스, 단계 ms)를 반환한다.
        packet은 공통 backend 호출 규약을 위한 인자이며 여기서는 쓰지 않는다. A 오류는 전파한다."""
        return _analysis(frame, self.background, self.zones, self.config)

    def update(self, intrusions, packet):
        """intrusions와 packet의 원본 인덱스·영상 시각을 A 상태 함수에 전달하고 (상태, 전이)를 반환한다.
        A 반환 키를 B 표시·사건 형식으로 정규화한다. 실제 실행은 간격 1로 검증되며 A 오류는 전파한다."""
        self.states, events = state.update_state(
            self.states, intrusions, packet["frame_index"], packet["media_time_s"], self.config
        )
        normalized = []
        for event in events:
            zone = event["zone"]
            if event["event"] == "alert":
                self.event_number += 1
                self.active_ids[zone] = str(self.event_number)
            normalized.append(
                {
                    **event,
                    "zone_name": zone,
                    "type": event["event"],
                    "event_id": self.active_ids[zone],
                }
            )
            if event["event"] == "cleared":
                self.active_ids.pop(zone)
        # A의 내부 state는 그대로 유지하고 B 화면에 필요한 이름만 출력에 추가한다.
        # media_time_s는 변경하지 않아 녹화가 해제 확정 시각부터 5초 이어진다.
        return {
            name: {**value, "status": value["state"]} for name, value in self.states.items()
        }, normalized


class MockBackend:
    """픽셀 대신 미리 정한 시간표로 동작하는 명시적 테스트 대역이다.
    기본 관측 시작은 1초, 경보는 1.5초, 해제는 2.5초다. A의 검출 알고리즘이나
    연속 감지 N 조건을 구현하지 않으므로 결과로 실제 정확도나 검출 속도를 판단하면 안 된다."""

    is_mock = True

    def __init__(self, config, size):
        """config의 mock_events 시간표를 복사하고 size 전체를 덮는 가상 구역을 만든다.
        관측 시작≤경보<해제 조건 위반은 ValueError다. 필수 키 오류도 전파하며 반환값은 없다."""
        self.schedule = deepcopy(
            config.get("mock_events", [dict(zone_name="MOCK_ZONE", start=1.0, alert=1.5, end=2.5)])
        )
        for e in self.schedule:
            if not (0 <= e["start"] <= e["alert"] < e["end"]):
                raise ValueError("Invalid mock schedule")
        w, h = size
        self.zones = [
            dict(name=name, points=[[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
            for name in sorted({e["zone_name"] for e in self.schedule})
        ]
        self.reset()

    def reset(self):
        """재생 경계에서 전이 발행 여부와 구역 상태를 초기화한다. 반환값은 없다.
        다음 재생에서도 같은 시간표를 다시 시험할 수 있도록 한다."""
        self.alerted, self.cleared = set(), set()
        self.states = {z["name"]: {"status": "IDLE"} for z in self.zones}

    def analyze(self, frame, packet):
        """packet의 재생 내 시각으로 (구역별 bool, 가상 박스, detect_ms)를 반환한다.
        frame은 박스 크기 계산에만 쓰고 픽셀을 검사하지 않는다. 이 시간은 실제 검출 비용이 아니다."""
        start = time.perf_counter()
        t = packet["cycle_time_s"]
        values = {
            z["name"]: any(
                e["zone_name"] == z["name"] and e["start"] <= t < e["end"] for e in self.schedule
            )
            for z in self.zones
        }
        h, w = frame.shape[:2]
        return (
            values,
            ([(w // 4, h // 4, w // 2, h // 2)] if any(values.values()) else []),
            {"detect_ms": (time.perf_counter() - start) * 1000},
        )

    def update(self, intrusions, packet):
        """분석한 프레임에서만 시간표 전이를 발행하고 (상태, 전이 목록)을 반환한다.
        intrusions=None이면 상태를 유지하고 빈 전이를 반환한다. 생략 프레임을 감지 성공으로 세지 않는다.
        packet의 누적 영상 시각을 전이에 넣되 시간표 판단은 재생 내 시각을 사용한다."""
        if intrusions is None:
            return self.states, []
        t = packet["cycle_time_s"]
        events = []
        for i, e in enumerate(self.schedule):
            name = e["zone_name"]
            if e["start"] <= t < e["alert"]:
                self.states[name] = {"status": "DETECTING"}
            if e["alert"] <= t < e["end"] and i not in self.alerted:
                self.alerted.add(i)
                self.states[name] = {"status": "ALERT"}
                events.append(
                    dict(
                        type="alert",
                        event_id=f"mock-{i}",
                        zone_name=name,
                        media_time_s=packet["media_time_s"],
                    )
                )
            if t >= e["end"] and i in self.alerted and i not in self.cleared:
                self.cleared.add(i)
                self.states[name] = {"status": "CLEARED"}
                events.append(
                    dict(
                        type="cleared",
                        event_id=f"mock-{i}",
                        zone_name=name,
                        media_time_s=packet["media_time_s"],
                    )
                )
        return self.states, events


class EventLedger:
    """A의 전이를 사건 CSV 메타데이터로 보강하는 기록 어댑터이며 경보를 결정하지 않는다.
    시작은 최초 침입 관측, 끝은 해제 확정으로 이어진 첫 비침입 관측을 사용한다.
    생략된 관측은 미확정 시작·해제 후보를 무효화하지만 확정 사건을 임의로 끝내지 않는다."""

    def __init__(self, run_id, source, mode, mock):
        """run_id·source 파일명·mode·mock 정보를 보관하고 사건 및 구역별 관측 맵을 준비한다.
        실행과 반복 사이의 사건 ID 충돌을 막기 위한 초기 상태이며 반환값은 없다."""
        self.run_id, self.source, self.mode, self.mock = run_id, Path(str(source)).name, mode, mock
        self.rows, self.active, self.starts, self.false_starts = {}, {}, {}, {}
        self.ids = {}
        self.sampled_spans = {}

    def observe(self, intrusions, t):
        """intrusions와 영상 초 t로 시작·해제 후보를 갱신한다. 반환값은 없다.
        None은 알 수 없는 관측이므로 검출로 세지 않는다. sampled_spans는 관측된 양성 시각 범위이며
        생략 구간 전체가 실제 침입이었다는 증거나 연속 검출 횟수를 뜻하지 않는다."""
        if intrusions is None:
            self.starts = {z: start for z, start in self.starts.items() if z in self.active}
            self.false_starts.clear()
            return
        for zone, present in intrusions.items():
            if present:
                first, _ = self.sampled_spans.get(zone, (t, t))
                self.sampled_spans[zone] = (first, t)
                self.starts.setdefault(zone, t)
                self.false_starts.pop(zone, None)
            elif zone in self.active:
                self.sampled_spans.pop(zone, None)
                self.false_starts.setdefault(zone, t)
            else:
                self.sampled_spans.pop(zone, None)
                self.starts.pop(zone, None)

    def transitions(self, events, packet, cycle):
        """events를 복사하여 실행 전체의 고유 ID로 정규화하고 새로 처리한 전이 목록을 반환한다.
        packet의 실제·예정 투입 시각으로 시스템 지연을 계산하고 cycle로 반복 간 ID 충돌을 막는다.
        A가 시작·끝 시각을 제공하면 우선하며 없으면 관측 후보로 보완한다. 중복 전이는 무시한다.
        시작이 경보보다 늦거나 끝이 시작보다 빠르면 ValueError이며 입력 사건 딕셔너리는 수정하지 않는다."""
        normalized = []
        now = time.perf_counter()
        for original in events:
            event = dict(original)
            zone = event["zone_name"]
            local_id = (cycle, zone, str(event["event_id"]))
            key = self.ids.setdefault(local_id, f"{self.run_id}-{len(self.ids) + 1:04d}")
            event["event_id"] = key
            t = float(event["media_time_s"])
            if event["type"] == "alert" and key not in self.rows:
                start = float(event.get("start_time_s", self.starts.get(zone, t)))
                if start > t:
                    raise ValueError("Event onset is after alert")
                scheduled = packet.get("scheduled_at")
                self.rows[key] = dict(
                    run_id=self.run_id,
                    video_name=self.source,
                    mock=self.mock,
                    event_id=key,
                    zone_name=zone,
                    start_time=start,
                    alert_time=t,
                    end_time=None,
                    duration_seconds=None,
                    video_path="",
                    pipeline_mode=self.mode,
                    status="open",
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                    alert_perf_counter=now,
                    input_perf_counter=packet["captured_at"],
                    system_alert_delay_ms=(now - packet["captured_at"]) * 1000,
                    scheduled_alert_delay_ms=(now - scheduled) * 1000
                    if scheduled is not None
                    else None,
                    onset_basis="backend" if "start_time_s" in event else "first_observed_frame",
                    truncated=False,
                )
                self.active[zone] = key
                normalized.append(event)
            elif (
                event["type"] == "cleared"
                and key in self.rows
                and self.rows[key]["status"] == "open"
            ):
                end = float(event.get("end_time_s", self.false_starts.get(zone, t)))
                row = self.rows[key]
                if end < row["start_time"]:
                    raise ValueError("Event ends before onset")
                row.update(end_time=end, duration_seconds=end - row["start_time"], status="closed")
                self.active.pop(zone, None)
                self.starts.pop(zone, None)
                self.false_starts.pop(zone, None)
                normalized.append(event)
        return normalized

    def attach_clips(self, clips):
        """완료된 clips의 경로와 절단 여부를 해당 사건 행에 연결한다. 반환값은 없다.
        파일별 독립 event_ids를 유지하며 장부에 없는 ID는 건너뛴다."""
        for clip in clips:
            for key in clip["event_ids"]:
                if key in self.rows:
                    self.rows[key].update(video_path=clip["path"], truncated=clip["truncated"])

    def finish(self, reason):
        """미해제 사건에 reason 상태를 남기고 관측 후보를 비운다. 반환값은 없다.
        EOF·오류 시 실제 해제 시각을 알 수 없으므로 종료 시각과 지속 시간을 만들어 넣지 않는다."""
        for key in self.active.values():
            self.rows[key]["status"] = reason
        self.active.clear()
        self.starts.clear()
        self.false_starts.clear()
        self.sampled_spans.clear()


def _draw(frame, mode, fps, zones, boxes, states, recording, mock):
    """frame 복사본에 구역·박스·상태·녹화 여부·Mock 구분과 fps를 그려 반환한다.
    zones와 boxes는 frame과 같은 입력 좌표여야 한다. 원본 녹화에 표시가 섞이지 않도록 복사한다.
    fps는 직전 프레임 처리 완료 간격의 역수이며 집계 처리량과 다르다. 메인 스레드에서 호출하며
    잘못된 도형·프레임의 OpenCV 오류는 전파한다."""
    out = frame.copy()
    for index, zone in enumerate(zones):
        points = np.asarray(zone["points"], dtype=np.int32)
        cv2.polylines(out, [points], True, (0, 220, 220), 2)
        cv2.putText(
            out,
            f"Zone {index + 1}",
            tuple(points[0]),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 220, 220),
            1,
        )
        # OpenCV 기본 글꼴은 한글을 지원하지 않아 번호로 표시하고 실제 이름은 메타데이터에 보존한다.
    for x, y, w, h in boxes:
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 255, 0), 2)
    alert = any(s.get("status") == "ALERT" for s in states.values())
    if alert:
        cv2.rectangle(out, (1, 1), (out.shape[1] - 2, out.shape[0] - 2), (0, 0, 255), 6)
    lines = [
        f"{mode} FPS {fps:.1f} REC {recording}",
        "MOCK - NOT REAL DETECTION" if mock else "REAL DETECTION",
    ]
    lines += [
        f"Zone {i + 1}: {states.get(z['name'], {}).get('status', 'IDLE')}"
        for i, z in enumerate(zones)
    ]
    if alert:
        lines.append("WARNING: INTRUSION")
    for i, text in enumerate(lines):
        cv2.putText(
            out,
            text,
            (10, 25 + i * 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 255) if alert else (255, 255, 255),
            1,
        )
    return out


def _file_hash(source):
    """source 파일을 작은 청크로 읽어 SHA-256 문자열을 반환하고 카메라 번호이면 None을 반환한다.
    실험 입력의 동일성을 확인하되 영상 전체를 메모리에 올리지 않기 위함이다. 파일 오류는 전파한다."""
    if str(source).isdigit():
        return None
    digest = hashlib.sha256()
    with open(source, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_pipeline(source: str, mode: str, config: dict[str, Any], *, backend=None) -> int:
    """source·mode·config로 감시를 실행하고 정상 종료 시 0을 반환한다.
    baseline은 순차 입력, threaded와 optimized는 입력 스레드와 Queue를 사용한다.
    분석·상태·녹화·표시는 소비 경로에서 수행하며 optimized에서만 ROI·분석 축소·간격을 적용한다.
    선택적 backend는 reset/analyze/update/zones와 is_mock=True를 갖춘 테스트 대역이어야 한다.
    원본은 분석 전에 버퍼에 넣고 분석 생략 시에도 record_frame에 전달한다. None 관측으로
    상태를 갱신하므로 생략을 비침입이나 검출 성공으로 바꾸지 않는다. 큐 드롭은 허용하지 않는다.
    EOF·q·시간 제한·오류·Ctrl-C에서 정리와 결과 저장을 시도한다. 중단 시 큐에 남은 프레임까지
    모두 녹화하는 것은 아니며 pending_at_stop에 생산·처리 차이를 남긴다.
    잘못된 설정·모드·대역은 ValueError, 메인 스레드 밖 GUI는 RuntimeError이며 A 오류도 전파한다."""
    c = validate_config(config)
    if mode not in ("baseline", "threaded", "optimized"):
        raise ValueError("Unknown mode")
    mock = c["mock_detection"] or backend is not None
    if backend is not None and not getattr(backend, "is_mock", False):
        raise ValueError("Injected test backend must declare is_mock=True")
    if not c["no_display"] and threading.current_thread() is not threading.main_thread():
        raise RuntimeError("GUI must run on main thread")
    reader = FrameSource(source, c, threaded=mode != "baseline")
    rec, ledger, output, run_dir = None, None, None, None
    rows, all_clips = [], []
    analysis_rows, snapshot_rows = [], []
    status, current_cycle = "failed", None
    started_measurement, ended_measurement = None, None
    metadata = None
    error = None
    processed_frames = 0
    try:
        if not mock and c.get("edit_zones", False) and not c["no_display"]:
            if not reader.edit_first_frame():
                return 0  # Q/Esc 취소도 finally에서 캡처와 창을 정리한다.
        if mode != "optimized":
            c.update(roi_enabled=False, resize_enabled=False, frame_interval=1)
        if not c["resize_enabled"]:
            c["analysis_resolution"] = list(reader.size)
        backend = backend or (
            MockBackend(c, reader.size) if mock else RealBackend(c, reader.native_size, reader.size)
        )
        if mock:
            print(
                "[MOCK] Scripted test detector/state; NOT real detection or accuracy data.",
                flush=True,
            )
        output = Path(c["output_dir"])
        if mock:
            output /= "mock"
        if c.get("suite_run", False):
            output /= c["experiment_kind"]
            output /= c["suite_id"]
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:10]
        run_dir = output / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        c["recording_dir"] = str(run_dir / "clips")
        ledger = EventLedger(run_id, source, mode, mock)
        metadata = dict(
            run_id=run_id,
            source=str(source),
            pipeline_mode=mode,
            resolution=f"{reader.size[0]}x{reader.size[1]}",
            variant=c["variant"],
            repeat=c["repeat"],
            experiment_kind=c["experiment_kind"],
            mock=mock,
            source_fps=reader.fps,
            run_status="running",
        )
        environment = dict(
            config=c,
            zones=backend.zones,
            native_size=reader.native_size,
            source_sha256=_file_hash(source),
            python=platform.python_version(),
            platform=platform.platform(),
            processor=platform.processor(),
            opencv=cv2.__version__,
            numpy=np.__version__,
            timestamp_basis="monotonic" if reader.camera else c["timestamp_mode"],
            repeat_boundary="reset_model_and_state_finalize_truncated_clips",
        )
        (run_dir / "effective_config.json").write_text(
            json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        previous_completed = None
        display_fps = 0.0
        boxes = []
        for packet in reader:
            work_start = time.perf_counter()
            if current_cycle != packet["cycle"]:
                if current_cycle is not None:
                    if rec:
                        clips = close_recorder(rec)
                        ledger.attach_clips(clips)
                        all_clips.extend(clips)
                    ledger.finish("open_at_loop_boundary")
                    backend.reset()
                current_cycle = packet["cycle"]
                if c["recording_enabled"]:
                    rec = create_recorder(c, reader.fps, reader.native_size)
            warmup = packet["frame_index"] < c["warmup_frames"]
            if not warmup and started_measurement is None:
                started_measurement = max(
                    packet["read_started_at"], previous_completed or packet["read_started_at"]
                )
            row = {
                key: metadata[key]
                for key in (
                    "run_id",
                    "source",
                    "pipeline_mode",
                    "resolution",
                    "variant",
                    "repeat",
                    "experiment_kind",
                    "mock",
                    "source_fps",
                )
            }
            row.update(
                {
                    key: packet[key]
                    for key in (
                        "frame_index",
                        "cycle",
                        "media_time_s",
                        "captured_at",
                        "scheduled_at",
                        "read_ms",
                    )
                }
            )
            row.update(
                processing_started_at=work_start,
                queue_wait_ms=(work_start - packet["enqueued_at"]) * 1000,
                dropped=False,
                warmup=warmup,
            )
            frame, t = packet["frame"], packet["media_time_s"]
            record_ms = 0.0
            if rec:
                begin = time.perf_counter()
                buffer_frame(rec, packet["original_frame"], t)
                record_ms += (time.perf_counter() - begin) * 1000
            analyzed = packet["cycle_frame_index"] % c["frame_interval"] == 0
            row["analyzed"] = analyzed
            if analyzed:
                intrusions, boxes, timing = backend.analyze(frame, packet)
                row.update(timing)
            else:
                intrusions, boxes = None, []
            # 보조 분석 연동: 표시 전 입력 전체 밝기와 실제 분석 영역의 전경 비율.
            if c["frame_analysis_enabled"]:
                brightness = frame_analysis.calculate_brightness(frame)
                analysis_rows.append(dict(
                    frame_index=packet["frame_index"], cycle=packet["cycle"],
                    media_time_s=t, analyzed=analyzed, brightness=brightness,
                    brightness_category=frame_analysis.classify_brightness(brightness),
                    motion_ratio=row.get("motion_ratio") if analyzed and not mock else None,
                    motion_basis="analysis_region_mog2_255", mock=mock,
                ))
            with measure(row, "state"):
                ledger.observe(intrusions, t)
                states, transitions = backend.update(intrusions, packet)
                events = ledger.transitions(transitions, packet, current_cycle)
            # 사건 이미지 연동: 정규화된 경보 전이에만 한 번 저장한다.
            if c["snapshot_enabled"]:
                for event in events:
                    if event["type"] == "alert":
                        snapshot = event_snapshot.save_alert_snapshots(
                            packet["original_frame"], frame.shape, backend.zones,
                            event, run_dir / "snapshots",
                            original_zones=getattr(backend, "original_zones", None),
                        )
                        snapshot_rows.append(snapshot)
                        if snapshot["error"]:
                            logging.getLogger(__name__).warning(
                                "Snapshot %s (%s): %s", event["event_id"],
                                snapshot["status"], snapshot["error"],
                            )
            if ledger.sampled_spans:
                row["observed_duration_s"] = max(
                    last - first for first, last in ledger.sampled_spans.values()
                )
            alert_rows = [ledger.rows[e["event_id"]] for e in events if e["type"] == "alert"]
            if alert_rows:
                row["alert_delay_s"] = sum(
                    e["alert_time"] - e["start_time"] for e in alert_rows
                ) / len(alert_rows)
                row["system_alert_delay_ms"] = sum(
                    e["system_alert_delay_ms"] for e in alert_rows
                ) / len(alert_rows)
                scheduled_delays = [
                    e["scheduled_alert_delay_ms"]
                    for e in alert_rows
                    if e["scheduled_alert_delay_ms"] is not None
                ]
                if scheduled_delays:
                    row["scheduled_alert_delay_ms"] = sum(scheduled_delays) / len(scheduled_delays)
            quit_requested = False
            if not c["no_display"]:
                with measure(row, "display"):
                    display = _draw(
                        frame,
                        mode,
                        display_fps,
                        backend.zones,
                        boxes,
                        states,
                        bool(rec and (rec["active"] or any(e["type"] == "alert" for e in events))),
                        mock,
                    )
                    cv2.imshow("OpenCV safety monitor", display)
                    quit_requested = cv2.waitKey(1) & 0xFF == ord("q")
            if rec:
                begin = time.perf_counter()
                clips = record_frame(rec, packet["original_frame"], t, events)
                record_ms += (time.perf_counter() - begin) * 1000
                ledger.attach_clips(clips)
                all_clips.extend(clips)
            row["record_ms"] = record_ms if rec else None
            processed_frames += 1
            completed = time.perf_counter()
            row["total_ms"] = (
                (completed - work_start) * 1000 + row["queue_wait_ms"] + packet["read_ms"]
            )
            row["completed_at"] = completed
            if previous_completed is not None and completed > previous_completed:
                display_fps = 1 / (completed - previous_completed)
            previous_completed = completed
            if c["measure_fps"]:
                rows.append(row)
            ended_measurement = completed
            if quit_requested:
                status = "interrupted"
                break
            if (
                started_measurement is not None
                and c["duration_seconds"]
                and completed - started_measurement >= c["duration_seconds"]
            ):
                status = "completed"
                break
        else:
            status = "completed"
    except BaseException as exc:
        error = exc
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
    finally:
        try:
            reader.close()
        except BaseException as exc:
            error = error or exc
            status = "failed"
        if rec:
            try:
                clips = close_recorder(rec)
                if ledger:
                    ledger.attach_clips(clips)
                all_clips.extend(clips)
            except BaseException as exc:
                error = error or exc
                status = "failed"
        if not c["no_display"]:
            cv2.destroyAllWindows()
        if ledger and run_dir:
            ledger.finish("open_at_eof" if status == "completed" else status)
            event_rows = list(ledger.rows.values())
            write_table(run_dir / "events.csv", event_rows, EVENT_FIELDS)
            write_table(output / "events.csv", event_rows, EVENT_FIELDS, append=True)
            write_table(run_dir / "clips.csv", all_clips, CLIP_FIELDS)
            if c["frame_analysis_enabled"]:
                write_table(run_dir / "frame_analysis.csv", analysis_rows, ANALYSIS_FIELDS)
            if c["snapshot_enabled"]:
                write_table(run_dir / "snapshots.csv", snapshot_rows, SNAPSHOT_FIELDS)
            metadata["run_status"] = status
            if c["measure_fps"]:
                for row in rows:
                    row["measurement_start_at"] = (
                        started_measurement
                        if started_measurement is not None
                        else row["processing_started_at"]
                    )
                    row["measurement_end_at"] = ended_measurement or time.perf_counter()
                save_run_metrics(output, run_dir, rows, metadata)
            (run_dir / "status.json").write_text(
                json.dumps(
                    dict(
                        status=status,
                        error=str(error) if error else None,
                        produced_frames=reader.produced,
                        processed_frames=processed_frames,
                        pending_at_stop=reader.produced - processed_frames,
                        analyzed_queue_drops=reader.dropped,
                    ),
                    indent=2,
                ),
                encoding="utf-8",
            )
    if error:
        raise error
    print(f"[pipeline] {mode}: {status}; results: {run_dir}", flush=True)
    return 0
