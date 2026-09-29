"""A 전처리에 integration의 축소 설정과 실제 축별 좌표 복원을 연결한다."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


def preprocess_frame(
    frame: np.ndarray, config: dict[str, Any], roi: tuple[int, int, int, int] | None = None
) -> tuple[np.ndarray, dict[str, float]]:
    """[담당: 팀원 A]
    목적: 공통 전처리 수행
    매개변수 / 입력 타입: frame: HxWx3 uint8; config: 설정; roi: 튜플 x,y,w,h 또는 None
    반환 / 출력 타입: (분석 BGR 영상, {offset_x, offset_y, scale_x, scale_y}); scale은 분석/원본 비율
    데이터 처리 순서:
        1. 프레임 및 ROI 검증
        2. ROI crop 후 종횡비를 유지하여 분석 해상도로 축소
        3. 동일 blur_kernel로 블러
        4. 실제 축소 비율과 원본 오프셋 반환
    예외 및 경계 조건: 빈 프레임·범위 밖 ROI는 ValueError; 커널은 양의 홀수
    모듈 연결: 모든 버전 pipeline → preprocess → detect
    """
    if frame is None or frame.size == 0:
        raise ValueError("빈 프레임입니다.")

    h, w = frame.shape[:2]

    # 1. 프레임 및 ROI 검증, crop
    if roi is not None:
        x, y, rw, rh = roi
        if x < 0 or y < 0 or rw <= 0 or rh <= 0 or x + rw > w or y + rh > h:
            raise ValueError(f"ROI가 프레임 범위를 벗어났습니다: {roi}, frame={w}x{h}")
        cropped = frame[y : y + rh, x : x + rw]
        offset_x, offset_y = x, y
    else:
        cropped = frame
        offset_x, offset_y = 0, 0

    ch, cw = cropped.shape[:2]

    # 2. 종횡비를 유지하여 분석 해상도로 축소
    # B의 기존 분석 해상도를 우선하고 A 단독 호출의 target_width도 지원한다.
    # ROI를 자른 실제 크기에서 두 축 제한을 모두 적용하며 확대하지 않는다.
    if config.get("resize_enabled", True):
        bounds = config.get("analysis_resolution", (config.get("target_width", cw), ch))
        if bounds[0] <= 0 or bounds[1] <= 0:
            raise ValueError("analysis_resolution은 양수여야 합니다")
        scale = min(1.0, bounds[0] / cw, bounds[1] / ch)
    else:
        scale = 1.0
    target_w, target_h = max(1, round(cw * scale)), max(1, round(ch * scale))
    resized = cv2.resize(cropped, (target_w, target_h), interpolation=cv2.INTER_AREA)

    # 3. 동일 blur_kernel로 블러 (양의 홀수 검증)
    blur_kernel = config.get("blur_kernel", 5)
    if not isinstance(blur_kernel, int) or blur_kernel <= 0 or blur_kernel % 2 != 1:
        raise ValueError("blur_kernel은 양의 홀수여야 합니다")
    blurred = cv2.GaussianBlur(resized, (blur_kernel, blur_kernel), 0)

    # 4. 실제 축소 비율과 원본 오프셋 반환
    transform = {
        "offset_x": float(offset_x),
        "offset_y": float(offset_y),
        "scale_x": target_w / cw,
        "scale_y": target_h / ch,
    }
    return blurred, transform


def restore_boxes(
    boxes: list[tuple[int, int, int, int]], transform: dict[str, float]
) -> list[tuple[int, int, int, int]]:
    """[담당: 팀원 A]
    목적: 분석 좌표를 원본 좌표로 복원
    매개변수 / 입력 타입: boxes: 분석 x,y,w,h들; transform: 전처리 변환 정보
    반환 / 출력 타입: 원본 x,y,w,h 목록
    데이터 처리 순서:
        1. x/scale_x+offset_x 등 역변환
        2. 정수 반올림 규칙 통일
    예외 및 경계 조건: 빈 박스는 []; scale은 양수
    모듈 연결: detect의 결과를 pipeline이 복원 후 침입 판정
    """
    if not boxes:
        return []

    scale_x = transform.get("scale_x", 1.0)
    scale_y = transform.get("scale_y", 1.0)
    offset_x = transform.get("offset_x", 0.0)
    offset_y = transform.get("offset_y", 0.0)

    if scale_x <= 0 or scale_y <= 0:
        raise ValueError("scale 값은 양수여야 합니다.")

    restored = []
    for x, y, w, h in boxes:
        # 1. x/scale_x+offset_x 등 역변환
        orig_x = x / scale_x + offset_x
        orig_y = y / scale_y + offset_y
        orig_w = w / scale_x
        orig_h = h / scale_y
        # 2. 정수 반올림 규칙 통일 (반올림 후 int)
        restored.append(
            (int(round(orig_x)), int(round(orig_y)), int(round(orig_w)), int(round(orig_h)))
        )

    return restored
