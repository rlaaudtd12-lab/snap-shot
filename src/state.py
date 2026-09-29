"""A 상태 전이와 사건 메타데이터를 B·C의 config 호출 규약으로 제공한다."""

from __future__ import annotations

from typing import Any

STATES = ("IDLE", "DETECTING", "ALERT", "CLEARED")


def update_state(
    states: dict[str, dict[str, Any]],
    intrusions: dict[str, bool] | None,
    frame_index: int,
    media_time_s: float,
    config: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """[담당: 팀원 A]
    목적: 구역별 상태 전이 계산
    매개변수 / 입력 타입: states: 초기 {}; intrusions: 구역 bool 또는 미분석 None; frame_index: 원본 인덱스; media_time_s: 영상 초; config: 설정
    반환 / 출력 타입: (새 states, 사건 목록); 상세 필드는 README 참조
    데이터 처리 순서:
        1. IDLE에서 True면 DETECTING, N번째 연속 True에서 ALERT (N=1 즉시)
        2. False면 감지 카운터 초기화; ALERT 중 해제 기준 연속 False면 CLEARED
        3. CLEARED 다음 분석에서 IDLE 기준으로 재판정
        4. alert/cleared 사건 생성; 지속 시간은 clear 시각-alert 시각
    예외 및 경계 조건: None은 카운터 및 상태 유지. 인덱스 공백은 검사하지 않으므로 실제 파이프라인은 간격 1만 허용; 시각 역행 ValueError
    모듈 연결: pipeline이 전이를 recorder에 전달하고 CSV 기록
    """
    if not isinstance(frame_index, int):
        raise ValueError(f"frame_index는 정수여야 합니다: {frame_index}")

    alert_frames = config.get(
        "consecutive_frames", config.get("alert_frames", 5)
    )  # ALERT 진입에 필요한 연속 감지 횟수
    clear_frames = config.get("clear_frames", 10)  # CLEARED 진입에 필요한 연속 미감지 횟수

    new_states: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []

    zone_names = set(states.keys())
    if intrusions:
        zone_names |= set(intrusions.keys())

    for zone_name in zone_names:
        zone_state = states.get(
            zone_name,
            {
                "state": "IDLE",
                "hit_count": 0,
                "miss_count": 0,
                "alert_time_s": None,
            },
        )

        # dict 복사 (원본 훼손 방지)
        zone_state = dict(zone_state)

        detected = None if intrusions is None else intrusions.get(zone_name)
        prev_time = zone_state.get("_last_time_s")
        if prev_time is not None and media_time_s < prev_time:
            raise ValueError(f"media_time_s가 역행했습니다: {media_time_s} < {prev_time}")
        zone_state["_last_time_s"] = media_time_s

        current = zone_state["state"]

        # 해제는 한 분석 프레임의 전이 상태다. 다음 관측을 새 사건으로 처리하되
        # 미분석(None)에서는 상태를 바꾸지 않아 관측을 임의로 만들어 내지 않는다.
        if current == "CLEARED" and detected is not None:
            current = zone_state["state"] = "IDLE"
            zone_state["hit_count"] = zone_state["miss_count"] = 0
            zone_state["alert_time_s"] = None

        if detected is None:
            # None: 카운터 증가 금지. ALERT는 유지, 그 외는 그대로.
            pass
        elif detected:
            zone_state["hit_count"] += 1
            zone_state["miss_count"] = 0

            if current == "IDLE":
                zone_state["state"] = "DETECTING"

            if current in ("IDLE", "DETECTING") and zone_state["hit_count"] >= alert_frames:
                if current != "ALERT":
                    zone_state["state"] = "ALERT"
                    zone_state["alert_time_s"] = media_time_s
                    events.append(
                        {
                            "zone": zone_name,
                            "event": "alert",
                            "type": "alert",
                            "zone_name": zone_name,
                            "frame_index": frame_index,
                            "media_time_s": media_time_s,
                        }
                    )
        else:
            zone_state["miss_count"] += 1
            zone_state["hit_count"] = 0

            if current == "ALERT" and zone_state["miss_count"] >= clear_frames:
                zone_state["state"] = "CLEARED"
                alert_time = zone_state.get("alert_time_s")
                duration = None if alert_time is None else media_time_s - alert_time
                events.append(
                    {
                        "zone": zone_name,
                        "event": "cleared",
                        "type": "cleared",
                        "zone_name": zone_name,
                        "alert_time_s": alert_time,
                        "frame_index": frame_index,
                        "media_time_s": media_time_s,
                        "duration_s": duration,
                    }
                )
            elif current == "DETECTING":
                zone_state["state"] = "IDLE"
            elif current == "CLEARED":
                # 3. CLEARED 다음 분석에서 IDLE 기준으로 재판정
                zone_state["state"] = "IDLE"

        new_states[zone_name] = zone_state

    return new_states, events
