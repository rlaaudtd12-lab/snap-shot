"""밝기와 MOG2 전경 비율을 계산하는 보조 분석 함수."""

from __future__ import annotations

from typing import Literal
from numbers import Real
import math
import argparse
import json
from pathlib import Path

import cv2
import numpy as np

if __package__:
    from .utils import imread_unicode
else:
    from utils import imread_unicode

class MaskObserver:
    """기존 검출기의 apply 호출 결과를 보관한다. 배경 모델은 한 번만 갱신된다."""

    def __init__(self, background_subtractor):
        self.background_subtractor = background_subtractor
        self.mask = None

    def apply(self, frame, *args, **kwargs):
        self.mask = self.background_subtractor.apply(frame, *args, **kwargs)
        return self.mask

def calculate_brightness(frame: np.ndarray) -> float:
    """평균 밝기를 구하여 어두운 환경 확인과 야간 실험 분석에 쓰기 위한 함수.

    입력: frame은 비어 있지 않은 (H, W, 3) uint8 BGR 배열이다.
    반환: 모든 픽셀의 grayscale 평균 밝기인 0.0~255.0 범위 Python float.
    구현: cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)로 변환한 후
    cv2.mean의 첫 채널 평균을 사용한다. BGR 채널 평균을 직접 쓰지 않는다.
    연동: pipeline._analysis 진입 시 표시가 그려지지 않은 frame을 받는
    분석 보조 단계가 후보다. 입력 해상도 전체 기준인지 실험 기록에 명시한다.
    주의: 빈 배열, 다른 채널 수/dtype은 ValueError로 거부한다.
    원본을 수정하거나 ROI 밝기와 전체 프레임 밝기를 혼동하지 않는다.
    기존 detection threshold, MOG2 설정, State Machine, benchmark 출력,
    config는 변경하지 않는다. 밝기 결과로 감지 설정을 자동 변경하지 않는다.
    """
    if (
        not isinstance(frame, np.ndarray)
        or frame.ndim != 3
        or frame.shape[0] == 0
        or frame.shape[1] == 0
        or frame.shape[2] != 3
        or frame.dtype != np.uint8
    ):
        raise ValueError("frame은 비어 있지 않은 (H, W, 3) uint8 BGR 배열이어야 합니다.")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.mean(gray)[0])



def classify_brightness(
    brightness: float,
    dark_threshold: float = 60,
    bright_threshold: float = 180,
) -> Literal["DARK", "NORMAL", "BRIGHT"]:
    """평균 밝기를 세 등급으로 분류하여 환경별 결과 비교를 돕기 위한 함수.

    입력: brightness는 calculate_brightness의 0~255 유한한 수치이다.
    두 threshold 역시 유한한 수치이며 0 <= dark_threshold <
    bright_threshold <= 255를 만족해야 한다. 기본값 외 임계값도 인자로 받는다.
    반환: brightness < dark_threshold이면 DARK, brightness >
    bright_threshold이면 BRIGHT, 두 경계값을 포함한 나머지는 NORMAL 문자열.
    구현: 수치 비교만 필요하므로 직접 사용할 OpenCV 함수는 없다.
    앞 단계의 cv2.cvtColor/cv2.mean 결과를 받아 처리한다.
    연동: pipeline의 밝기 측정 직후 호출하는 보조 분석 단계가 후보이며,
    향후 호출자가 config 값을 전달할 수 있다. 여기서 config를 읽지 않는다.
    주의: 범위 밖 수치·NaN·무한대·역전/동일 임계값은 ValueError로
    거부하고 경계값을 일관되게 처리한다. bool은 밝기 수치로 받지 않는다.
    기존 config 기본값, 검출 민감도, state.update_state 및 사건 발생 조건은
    변경하지 않는다. 분류는 실험용 정보이며 자동 야간 모드 전환이 아니다.
    """
    values = (brightness, dark_threshold, bright_threshold)
    if any(
        isinstance(value, (bool, np.bool_))
        or not isinstance(value, Real)
        or not math.isfinite(value)
        for value in values
    ):
        raise ValueError("밝기와 임계값은 유한한 실수여야 합니다.")
    if not (0 <= brightness <= 255 and 0 <= dark_threshold < bright_threshold <= 255):
        raise ValueError("0~255 범위에서 dark_threshold < bright_threshold여야 합니다.")
    if brightness < dark_threshold:
        return "DARK"
    if brightness > bright_threshold:
        return "BRIGHT"
    return "NORMAL"



def calculate_motion_ratio(foreground_mask: np.ndarray) -> float:
    """전체 분석 영역에서 움직임 픽셀 비율을 구해 활동량을 비교하기 위한 함수.

    입력: foreground_mask는 기존 MOG2에서 얻은 비어 있지 않은 (H, W)
    단일 채널 uint8 마스크다. 0은 배경, 127은 그림자, 255는 전경이다.
    0/255 이진 마스크도 허용한다. 그 외 값은 ValueError로 거부한다.
    반환: 전경(255) 픽셀 수 / 전체 H*W 픽셀 수인 0.0~1.0 Python float.
    구현: 그림자를 제외한 별도 이진 마스크에 cv2.countNonZero를
    적용한다. 원본 MOG2 마스크의 모든 비영 픽셀을 움직임으로 세지 않는다.
    연동: pipeline._analysis에서 MaskObserver로 기존 검출기의 fg_mask를 받는다.
    detect_motion의 박스 반환 계약과 배경 모델 갱신 횟수를 유지한다.
    주의: 빈 마스크의 0 나눗셈과 다채널 입력을 검증한다. ROI/축소 마스크라면
    분모도 해당 분석 영역이며 원본 전체 영상의 비율로 해석하지 않는다.
    기존 마스크를 수정하거나 MOG2 객체를 새로 만들거나 apply를 재호출하지 않는다.
    기존 detect.py 반환 계약, morphology, threshold, State Machine과 config는
    이번 단계에서 변경하지 않는다. 비율로 침입 여부를 재판정하지 않는다.
    """

    if (
        not isinstance(foreground_mask, np.ndarray)
        or foreground_mask.ndim != 2
        or foreground_mask.shape[0] == 0
        or foreground_mask.shape[1] == 0
        or foreground_mask.dtype != np.uint8
    ):
        raise ValueError("foreground_mask는 비어 있지 않은 (H, W) uint8 배열이어야 합니다.")
    if not np.all(np.isin(foreground_mask, (0, 127, 255))):
        raise ValueError("마스크 픽셀은 0, 127, 255 중 하나여야 합니다.")
    foreground = (foreground_mask == 255).astype(np.uint8)
    return float(cv2.countNonZero(foreground) / foreground_mask.size)



# pipeline.py에서 보조 분석 및 경보 이미지 저장에 사용한다.

def main() -> None:
    """앞 단계 이미지와 영상으로 밝기 및 예시 MOG2 전경 비율을 계산한다."""
    parser = argparse.ArgumentParser(description="저장된 대표 이미지·영상의 보조 통계")
    parser.add_argument("result_dir", type=Path, help="event_snapshot.py가 만든 결과 폴더")
    args = parser.parse_args()
    metadata = json.loads((args.result_dir / "snapshot_meta.json").read_text(encoding="utf-8"))
    image = imread_unicode(args.result_dir / metadata["full_image"])
    if image is None:
        raise OSError("대표 이미지를 읽을 수 없습니다.")
    brightness = calculate_brightness(image)
    category = classify_brightness(brightness)

    # 같은 영상의 첫 프레임부터 순차 적용한다. 기존 검출기의 MOG2 상태와는 별개다.
    target_index = metadata["frame_index"]
    capture = cv2.VideoCapture(metadata["video"])
    if not capture.isOpened():
        raise OSError("원본 영상을 열 수 없습니다.")
    subtractor = cv2.createBackgroundSubtractorMOG2(detectShadows=True)
    mask = None
    try:
        for _ in range(target_index + 1):
            ok, frame = capture.read()
            if not ok:
                raise OSError("원본 영상에서 대상 프레임을 찾지 못했습니다.")
            mask = subtractor.apply(frame)
    finally:
        capture.release()
    motion_ratio = calculate_motion_ratio(mask)
    result = {"brightness": brightness, "category": category,
              "motion_ratio": motion_ratio, "demo_only": True}
    output = args.result_dir / "analysis.json"
    if output.exists():
        raise FileExistsError(output)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"평균 밝기: {brightness:.2f} / 분류: {category}")
    print(f"예시 MOG2 전경 비율: {motion_ratio:.2%} (그림자 제외)")
    print(f"저장 완료: {output}")


if __name__ == "__main__":
    main()
