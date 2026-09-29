"""A PR #2의 MOG2 처리와 B·C 설정/감지 인터페이스를 연결한다."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


def create_background_subtractor(config: dict[str, Any]) -> Any:
    """[담당: 팀원 A]
    목적: 실행별 MOG2 인스턴스 생성
    매개변수 / 입력 타입: config: 검증 설정 dict
    반환 / 출력 타입: OpenCV BackgroundSubtractorMOG2
    데이터 처리 순서:
        1. history, var_threshold, detect_shadows 적용
        2. 각 실행 시작마다 새 인스턴스 생성
    예외 및 경계 조건: 유효하지 않은 설정은 ValueError
    모듈 연결: pipeline에서 1회 생성
    """
    # B의 중첩 설정을 우선하고 A의 독립 호출 설정도 유지한다.
    config = {**config, **config.get("mog2", {})}
    history = config.get("history", 500)
    var_threshold = config.get("var_threshold", 16)
    detect_shadows = config.get("detect_shadows", True)

    if not isinstance(history, int) or history <= 0:
        raise ValueError(f"history는 양의 정수여야 합니다: {history}")
    if not isinstance(var_threshold, (int, float)) or var_threshold <= 0:
        raise ValueError(f"var_threshold는 양수여야 합니다: {var_threshold}")

    if not isinstance(detect_shadows, bool):
        raise ValueError("mog2.detect_shadows는 bool이어야 합니다")

    subtractor = cv2.createBackgroundSubtractorMOG2(
        history=history,
        varThreshold=var_threshold,
        detectShadows=detect_shadows,
    )
    return subtractor


def detect_motion(
    frame: np.ndarray,
    background_subtractor: Any,
    min_area: float,
) -> list[tuple[int, int, int, int]]:
    """[담당: 팀원 A]
    목적: 움직이는 객체 영역 검출
    입력: frame(분석 BGR uint8), background_subtractor(MOG2), min_area(분석 해상도 기준 최소 면적)
    반환: (x, y, w, h) 바운딩 박스 목록
    """
    if frame is None or frame.size == 0:
        raise ValueError("빈 프레임입니다.")

    fg_mask = background_subtractor.apply(frame)

    # 그림자(127)는 배경으로 취급하고 순수 전경(255)만 사용
    _, binary = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)

    # 노이즈 제거: opening(침식->팽창)으로 작은 점 노이즈 제거,
    # closing(팽창->침식)으로 객체 내부 구멍 메우기
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes: list[tuple[int, int, int, int]] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        boxes.append((x, y, w, h))

    return boxes


def check_intrusion(
    boxes: list[tuple[int, int, int, int]], zones: list[dict[str, Any]]
) -> dict[str, bool]:
    """[담당: 팀원 A]
    목적: 박스 하단 중앙점이 구역에 침입했는지 판단
    매개변수 / 입력 타입: boxes: 원본 좌표; zones: name, points를 가진 목록
    반환 / 출력 타입: {구역 이름: 침입 bool} (모든 구역 포함)
    데이터 처리 순서:
        1. 박스 하단 중앙점 계산
        2. pointPolygonTest로 경계를 포함한 내부 판정
        3. 구역별 하나 이상이면 True
    예외 및 경계 조건: 박스가 없으면 False; 중복 구역명은 ValueError
    모듈 연결: pipeline → state; 공통 침입 판단 기준
    """
    result: dict[str, bool] = {}

    for zone in zones:
        zone_name = zone.get("id") or zone.get("name")
        if not zone_name:
            raise ValueError(f"구역 이름이 없습니다: {zone}")

        if zone_name in result:
            raise ValueError(f"중복 구역 이름입니다: {zone_name}")
        points = zone.get("points")
        if not points or len(points) < 3:
            raise ValueError(f"구역 {zone_name}의 다각형 점이 3개 미만입니다.")

        polygon = np.array(points, dtype=np.float32)
        intruded = False

        for x, y, w, h in boxes:
            # 1. 박스 하단 중앙점 계산
            cx = x + w / 2
            cy = y + h

            # 2. pointPolygonTest로 점 포함 내부 판정
            inside = cv2.pointPolygonTest(polygon, (float(cx), float(cy)), False)
            if inside >= 0:
                intruded = True
                break

        # 3. 구역별 하나 이상이면 True
        result[zone_name] = intruded

    return result
