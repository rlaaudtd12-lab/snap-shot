"""목적: 기존 개발 환경과 이미지 저장 점검 스크립트.
담당 팀원: 공통. 입력 데이터: 설치된 패키지 및 런타임.
출력 데이터: 콘솔 버전 정보와 env_test.png.
의존 관계: OpenCV, NumPy, sys. 구현 TODO: GUI/영상 검증은 별도 수행.
기존 구현 유지; 함수 인터페이스 없음.
"""

# check_env.py — 개발 환경 점검 스크립트
import sys

import cv2
import numpy as np

print(f"Python  : {sys.version.split()[0]}")
print(f"OpenCV  : {cv2.__version__}")
print(f"NumPy   : {np.__version__}")

if sys.version_info[:2] not in [(3, 9), (3, 10)]:
    print("[주의] 이 과정은 Python 3.9 또는 3.10 기준입니다.")

print(f"SIFT    : {'사용 가능' if hasattr(cv2, 'SIFT_create') else '없음'}")
has_csrt = hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerCSRT_create")
print(f"CSRT    : {'사용 가능' if has_csrt else '없음 → opencv-contrib-python 설치 확인'}")

img = np.zeros((120, 240, 3), dtype=np.uint8)
cv2.putText(img, "OpenCV OK", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
ok = cv2.imwrite("env_test.png", img)
print(f"이미지 저장: {'성공' if ok else '실패'} (env_test.png)")
