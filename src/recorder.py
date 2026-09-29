"""원본 시간 버퍼 및 독립 사건 영상 저장 (담당 B).
입력: 원본 BGR 프레임, 영상 초, 외부 alert/cleared 전이. 출력: 클립 메타데이터.
의존: collections.deque, OpenCV. 감지/상태 판정 및 사건 CSV는 수행하지 않는다.
불규칙 시각은 직전 프레임 유지 방식으로 고정 FPS에 재표본화한다.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import math
from pathlib import Path
import re
from typing import Any
import uuid

import cv2
import numpy as np


def create_recorder(
    config: dict[str, Any], source_fps: float, frame_size: tuple[int, int]
) -> dict[str, Any]:
    """원본 보존용 녹화 세션 딕셔너리를 생성한다.
    config의 사전·사후 시간은 기본 3초·5초이며 source_fps는 출력 고정 FPS,
    frame_size는 원본 (너비, 높이)이다. FPS·크기·시간 또는 예상 버퍼 용량이 잘못되면 ValueError를 낸다.
    경보 전에는 영상 파일을 열지 않는다. 사건마다 별도 writer를 두어 겹치는 사건도
    각자의 시작·종료 시각과 파일을 유지하며, 호출자는 finally에서 close_recorder를 호출해야 한다."""
    if not math.isfinite(source_fps) or source_fps <= 0:
        raise ValueError("Recording requires positive finite source/recording FPS")
    if len(frame_size) != 2 or any(x <= 0 for x in frame_size):
        raise ValueError("Invalid frame size")
    pre, post = float(config.get("pre_seconds", 3)), float(config.get("post_seconds", 5))
    if not all(math.isfinite(x) and x >= 0 for x in (pre, post)):
        raise ValueError("Recording durations must be finite and nonnegative")
    budget = int(config.get("recorder_max_mb", 1024) * 1024**2)
    if (math.ceil(pre * source_fps) + 1) * frame_size[0] * frame_size[1] * 3 > budget:
        raise ValueError("Pre-buffer exceeds recorder_max_mb; increase budget explicitly")
    return dict(
        buffer=deque(),
        active={},
        seen=set(),
        clips=[],
        fps=float(source_fps),
        size=tuple(frame_size),
        pre=pre,
        post=post,
        last_time=None,
        first_time=None,
        budget=budget,
        buffer_bytes=0,
        output=Path(config.get("recording_dir", "results/clips")),
        codec=config.get("recording_codec", "mp4v"),
        closed=False,
    )


def buffer_frame(session: dict, frame: np.ndarray, media_time_s: float) -> None:
    """분석 전에 원본 frame을 복사하여 (media_time_s, 프레임) 형태로 시간 버퍼에 보관한다.
    반환값은 없으며 오래된 프레임은 사전 녹화 시간 범위를 벗어날 때 제거한다.
    복사본은 분석·화면 표시에서 원본 배열을 변경해도 녹화 내용이 달라지지 않게 한다.
    record_frame에서도 호출하므로 같은 시각의 재호출은 추가 저장하지 않는다.
    닫힌 세션은 RuntimeError, 역행·비정상 시각이나 원본 크기·형식 불일치는 ValueError,
    실제 버퍼 용량 초과는 MemoryError로 알린다. 용량 부족을 프레임 유실로 숨기지 않는다."""
    if session["closed"]:
        raise RuntimeError("Recorder is closed")
    t = float(media_time_s)
    if not math.isfinite(t) or t < 0:
        raise ValueError("Invalid media timestamp")
    if frame.dtype != np.uint8 or frame.shape != (session["size"][1], session["size"][0], 3):
        raise ValueError("Recorder requires original uint8 BGR size")
    last = session["last_time"]
    if last is not None and t < last:
        raise ValueError("Recording timestamps must increase")
    if last == t:
        return
    if session["first_time"] is None:
        session["first_time"] = t
    session["last_time"] = t
    buf = session["buffer"]
    # 복사 전에 만료 프레임을 제거하여 순간 메모리 사용량이 원본 한 장만큼 더 늘지 않게 한다.
    while buf and buf[0][0] < t - session["pre"] - 1e-9:
        _, old = buf.popleft()
        session["buffer_bytes"] -= old.nbytes
    if session["buffer_bytes"] + frame.nbytes > session["budget"]:
        raise MemoryError("Actual pre-buffer exceeds recorder_max_mb")
    copy = frame.copy()
    buf.append((t, copy))
    session["buffer_bytes"] += copy.nbytes


def _write_sample(session: dict, clip: dict, frame: np.ndarray, timestamp: float) -> None:
    """원본 frame과 timestamp를 clip의 고정 FPS 출력 시각에 맞춰 기록한다.
    반환값 없이 clip의 진행 상태를 갱신한다. 출력 시각과 입력 시각이 일치하면 현재 프레임,
    그 사이의 빈 출력 시각에는 직전 프레임을 사용한다. 해제 뒤에는 deadline까지만 쓴다.
    모든 입력을 받아도 한 출력 간격 안의 여러 입력을 CFR 파일에 모두 표현할 수는 없다.
    held_ticks는 직전 프레임으로 채운 수, resampled_inputs는 현재 입력을 정확한 출력 시각에
    직접 쓰지 못한 횟수다. writer 오류는 전파하며 입력 검증은 호출자가 담당한다."""
    if clip["last_input"] == timestamp:
        return
    limit = min(timestamp, clip["deadline"]) if not clip["alert_active"] else timestamp
    step = 1 / session["fps"]
    if clip["previous"] is None:
        clip["previous"] = frame
    wrote_current = False
    while clip["next_tick"] <= limit + 1e-8:
        exact = abs(clip["next_tick"] - timestamp) <= 1e-8
        clip["writer"].write(frame if exact else clip["previous"])
        clip["frames_written"] += 1
        if not exact:
            clip["held_ticks"] += 1
        else:
            wrote_current = True
        clip["last_written_s"] = clip["next_tick"]
        clip["next_tick"] += step
    if not wrote_current:
        clip["resampled_inputs"] += 1
    clip["previous"] = frame.copy()
    clip["last_input"] = timestamp


def _finish(session: dict, event_id: str, truncated: bool) -> dict:
    """event_id에 해당하는 writer 하나를 해제하고 저장 가능한 클립 메타데이터를 반환한다.
    사전 구간 부족과 인자로 받은 사후 절단 여부를 따로 기록하고 세션의 clips에도 추가한다.
    CSV 저장은 호출자 책임이다. 존재하지 않는 ID의 KeyError와 writer 해제 오류는 전파한다."""
    clip = session["active"].pop(event_id)
    clip["writer"].release()
    info = {
        k: clip[k]
        for k in (
            "path",
            "event_ids",
            "zone_name",
            "alert_time_s",
            "occurred_at",
            "start_s",
            "frames_written",
            "held_ticks",
            "resampled_inputs",
        )
    }
    info.update(
        end_s=clip["last_written_s"],
        truncated=bool(truncated or clip["pre_truncated"]),
        pre_truncated=clip["pre_truncated"],
        post_truncated=bool(truncated),
        recording_fps=session["fps"],
        resampling="previous_frame_hold",
    )
    session["clips"].append(info)
    return info


def record_frame(
    session: dict[str, Any], frame: np.ndarray, media_time_s: float, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """원본 frame과 영상 시각, 외부 events 전이를 받아 이번 호출에서 끝난 클립 목록을 반환한다.
    경보 확정 시각에서 기본 3초 전까지의 버퍼를 새 사건 파일에 먼저 쓰고 현재 프레임을 이어 쓴다.
    여기서 사전 3초의 기준은 최초 침입 관측이 아니라 alert 시각이다. 사전 영상이 부족하면 표시한다.
    경보가 유지되면 계속 녹화하고 cleared 전이 이후 기본 5초까지 연장한다.
    종료 예정 시각은 max(alert+post, clear+post)이며 EOF에서 부족한 후반부를 임의로 만들지 않는다.
    서로 겹치는 사건도 독립 writer를 사용하여 한 사건의 해제가 다른 사건의 영상을 닫지 않게 한다.
    같은 ID의 중복 전이는 다시 처리하지 않는다. 경보와 현재 시각 불일치는 ValueError,
    writer 열기 실패는 OSError이며 버퍼 검증 오류도 전파한다. 사건 판정은 이 함수가 하지 않는다."""
    buffer_frame(session, frame, media_time_s)
    t = float(media_time_s)
    finished = []
    for event in events:
        key = str(event["event_id"])
        if event["type"] == "alert" and key not in session["seen"]:
            alert = float(event["media_time_s"])
            if abs(alert - t) > 1e-6:
                raise ValueError("Alert must be delivered with its original frame")
            session["output"].mkdir(parents=True, exist_ok=True)
            safe_id = re.sub(r"[^\w.-]", "_", key)
            stamp = datetime.now(timezone.utc)
            path = session["output"] / f"{stamp:%Y%m%dT%H%M%S}_{safe_id}_{uuid.uuid4().hex[:8]}.mp4"
            writer = cv2.VideoWriter(
                str(path),
                cv2.VideoWriter_fourcc(*session["codec"]),
                session["fps"],
                session["size"],
            )
            if not writer.isOpened():
                writer.release()
                raise OSError(f"Cannot open video writer: {path}")
            first = session["buffer"][0][0]
            clip = dict(
                writer=writer,
                path=str(path),
                event_ids=[key],
                zone_name=event["zone_name"],
                alert_time_s=alert,
                occurred_at=stamp.isoformat(),
                start_s=first,
                pre_truncated=first > alert - session["pre"] + 1e-8,
                alert_active=True,
                deadline=alert + session["post"],
                next_tick=first,
                previous=None,
                last_input=None,
                last_written_s=None,
                frames_written=0,
                held_ticks=0,
                resampled_inputs=0,
            )
            session["active"][key] = clip
            session["seen"].add(key)
            for old_t, old_frame in session["buffer"]:
                _write_sample(session, clip, old_frame, old_t)
        elif event["type"] == "cleared" and key in session["active"]:
            clip = session["active"][key]
            if clip["alert_active"]:
                clip["alert_active"] = False
                clip["deadline"] = max(
                    clip["deadline"], float(event["media_time_s"]) + session["post"]
                )
    for key, clip in list(session["active"].items()):
        _write_sample(session, clip, frame, t)
        if not clip["alert_active"] and t >= clip["deadline"] - 1e-8:
            finished.append(_finish(session, key, False))
    return finished


def close_recorder(session: dict[str, Any]) -> list[dict[str, Any]]:
    """EOF·중단·오류 시 남은 모든 클립을 절단 상태로 닫고 메타데이터 목록을 반환한다.
    한 writer 해제에 실패해도 나머지 해제를 시도하고 버퍼를 비운 뒤 첫 예외를 다시 발생시킨다.
    이미 닫은 세션을 다시 닫으면 빈 목록을 반환한다. 없는 사후 영상을 합성하지 않는다."""
    if session["closed"]:
        return []
    result = []
    first_error = None
    try:
        for key in list(session["active"]):
            try:
                result.append(_finish(session, key, True))
            except Exception as exc:
                first_error = first_error or exc
    finally:
        session["buffer"].clear()
        session["buffer_bytes"] = 0
        session["closed"] = True
    if first_error:
        raise first_error
    return result
