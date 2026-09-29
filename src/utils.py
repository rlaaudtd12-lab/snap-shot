"""목적: 기존 공통 영상 I/O, FPS, JSON 유틸리티 제공.
담당 팀원: 공통 A/B/C. 입력 데이터: 경로, 영상, 설정 파일.
출력 데이터: 영상, 캡처, FPS, 설정 dict.
의존 관계: OpenCV, NumPy, 표준 라이브러리; main/pipeline이 사용.
구현 TODO: 기존 동작 보존; Linux 카메라 backend는 팀 합의 후 개선.
"""

# utils.py — 모든 팀이 공통으로 쓰는 도우미 함수
import json
import os
import time
from collections import deque

import cv2
import numpy as np


def imread_unicode(path, flags=cv2.IMREAD_COLOR):
    """[담당: 공통(A/B/C)]
    목적: imread_unicode 공통 유틸리티 (기존 구현 유지).
    매개변수 / 입력 타입: path: str; flags: int
    반환 / 출력 타입: np.ndarray 또는 None
    구현 순서 (기존): 경로 확인 → 바이트 읽기 → imdecode
    예외 및 경계 조건: 없거나 디코딩 실패 시 None; I/O 예외 전파
    모듈 연결: main/pipeline에서 재사용.
    TODO: 호출부 통합 검증; 인터페이스 변경 전 팀 합의.
    """
    if not os.path.exists(path):
        return None
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_unicode(path, img, params=None, *, overwrite=True):
    """[담당: 공통(A/B/C)]
    목적: imwrite_unicode 공통 유틸리티 (기존 구현 유지).
    매개변수 / 입력 타입: path: str; img: np.ndarray; params: list 또는 None
    반환 / 출력 타입: bool. overwrite=False이면 기존 파일을 보호하며
    같은 경로가 존재할 때 FileExistsError를 발생시킨다.
    구현 순서 (기존): 확장자 확인 → imencode → tofile
    예외 및 경계 조건: 지원하지 않는 확장자 및 I/O 오류 전파
    모듈 연결: main/pipeline에서 재사용.
    TODO: 호출부 통합 검증; 인터페이스 변경 전 팀 합의.
    """
    ext = os.path.splitext(path)[1]
    ok, buf = cv2.imencode(ext, img, params or [])
    if ok:
        with open(path, "wb" if overwrite else "xb") as stream:
            stream.write(buf.tobytes())
    return ok


def open_source(source):
    """[담당: 공통(A/B/C)]
    목적: open_source 공통 유틸리티 (기존 구현 유지).
    매개변수 / 입력 타입: source: str 또는 int
    반환 / 출력 타입: cv2.VideoCapture
    구현 순서 (기존): 숫자면 카메라 → 그 외 파일 → 열림 확인
    예외 및 경계 조건: 실패 시 SystemExit; 기존 CAP_DSHOW는 Linux 카메라 후속 검토 필요
    모듈 연결: main/pipeline에서 재사용.
    TODO: 호출부 통합 검증; 인터페이스 변경 전 팀 합의.
    """
    if str(source).isdigit():
        # Windows에서는 DirectShow(CAP_DSHOW)로 열면 카메라가 빨리 켜집니다.
        cap = cv2.VideoCapture(int(source), cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise SystemExit(f"[ERROR] 입력을 열 수 없습니다: {source}")
    return cap


class FPSMeter:
    """최근 N프레임 이동평균으로 FPS를 계산한다."""

    def __init__(self, window=30):
        """[담당: 공통(A/B/C)]
        목적: __init__ 공통 유틸리티 (기존 구현 유지).
        매개변수 / 입력 타입: window: int = 30
        반환 / 출력 타입: None
        구현 순서 (기존): deque 생성 → perf_counter 시작
        예외 및 경계 조건: 양의 window 사용; 검증 보강 TODO
        모듈 연결: main/pipeline에서 재사용.
        TODO: 호출부 통합 검증; 인터페이스 변경 전 팀 합의.
        """
        self.intervals = deque(maxlen=window)
        self.last = time.perf_counter()

    def tick(self):
        """[담당: 공통(A/B/C)]
        목적: tick 공통 유틸리티 (기존 구현 유지).
        매개변수 / 입력 타입: 없음
        반환 / 출력 타입: float: 이동평균 FPS
        구현 순서 (기존): 시간 차 기록 → 합산 → FPS 반환
        예외 및 경계 조건: 합계 0이면 0.0; 첫 표본은 초기화 비용 포함
        모듈 연결: main/pipeline에서 재사용.
        TODO: 호출부 통합 검증; 인터페이스 변경 전 팀 합의.
        """
        now = time.perf_counter()
        self.intervals.append(now - self.last)
        self.last = now
        total = sum(self.intervals)
        return len(self.intervals) / total if total > 0 else 0.0


def load_config(path=None):
    """[담당: 공통(A/B/C)]
    목적: load_config 공통 유틸리티 (기존 구현 유지).
    매개변수 / 입력 타입: path: str 또는 None
    반환 / 출력 타입: dict
    구현 순서 (기존): 기본 경로 결정 → UTF-8 JSON 로딩
    예외 및 경계 조건: FileNotFoundError/JSONDecodeError 전파; 스키마 검증은 main
    모듈 연결: main/pipeline에서 재사용.
    TODO: 호출부 통합 검증; 인터페이스 변경 전 팀 합의.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)
