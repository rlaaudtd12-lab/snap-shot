"""사건 경보의 원본·구역·썸네일 이미지를 저장한다."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
import cv2
import re
import argparse
import json
import math
import hashlib

if __package__:
    from .utils import imwrite_unicode
else:
    from utils import imwrite_unicode

def save_alert_snapshots(
    original_frame, input_shape, zones, event, output_dir, *, original_zones=None,
):
    """원본 구역으로 이미지를 저장하고 성공 경로와 실패 내용을 반환한다.

    original_zones가 없는 테스트 대역만 입력 좌표를 역변환한다.
    이미지 처리·저장 실패는 결과에 남기고 나머지 이미지 저장을 계속한다.
    """
    result = {key: event[key] for key in ("event_id", "zone_name", "media_time_s")}
    result.update(full_path="", crop_path="", thumbnail_path="")
    errors = []
    images = {"full": original_frame}
    try:
        candidates = original_zones if original_zones is not None else zones
        zone = next((z for z in candidates
                     if (z.get("id") or z.get("name")) == event["zone_name"]), None)
        if zone is None:
            raise ValueError(f"Snapshot zone not found: {event['zone_name']}")
        points = np.asarray(zone["points"])
        if original_zones is None:
            height, width = original_frame.shape[:2]
            input_height, input_width = input_shape[:2]
            points = np.rint(points * [width / input_width, height / input_height]).astype(np.int32)
        images["crop"] = crop_zone_snapshot(original_frame, points)
        images["thumbnail"] = make_event_thumbnail(images["crop"])
    except (ValueError, OSError, cv2.error) as exc:
        errors.append(f"crop/thumbnail: {type(exc).__name__}: {exc}")
    for kind, image in images.items():
        try:
            result[f"{kind}_path"] = str(save_event_snapshot(
                image, Path(output_dir) / kind, event["event_id"], event["zone_name"],
            ))
        except (ValueError, OSError, cv2.error) as exc:
            errors.append(f"{kind}: {type(exc).__name__}: {exc}")
    saved = sum(bool(result[f"{kind}_path"]) for kind in ("full", "crop", "thumbnail"))
    result.update(status="saved" if not errors else ("partial" if saved else "failed"),
                  error="; ".join(errors))
    return result

def crop_zone_snapshot(
    frame: np.ndarray,
    polygon: Sequence[Sequence[int]] | np.ndarray,
    padding: int = 20,
) -> np.ndarray:
    """위험구역 주변을 잘라 사건 위치를 자세히 확인할 이미지로 만들기 위한 함수.

    입력: frame은 비어 있지 않은 (H, W, 3) uint8 BGR 배열이다.
    polygon은 동일 좌표계의 (N, 2) 정수 꼭짓점 목록/배열(N >= 3)이고,
    padding은 bounding box 사방에 더할 0 이상 정수 픽셀 수이다.
    반환: 잘린 (crop_H, crop_W, 3) uint8 BGR 복사본이다. 원본과 메모리를
    공유하지 않으며 polygon 내부만 마스킹하는 기능은 포함하지 않는다.
    구현: cv2.boundingRect로 사각형을 구하고 padding을 더한 뒤,
    좌상단/우하단을 프레임 경계로 clipping하여 해당 부분을 복사한다.
    연동: pipeline.run_pipeline의 alert 이벤트 처리 후 해당 zone의 points와
    frame을 받아 save_event_snapshot에 넘길 선택적 전처리로 사용할 수 있다.
    주의: 배열 슬라이스의 끝 좌표는 제외됨을 고려한다. 잘못된 입력/음수 padding,
    면적 없는 polygon 또는 clipping 후 빈 교차 영역은 ValueError로 거부한다.
    backend.zones는 입력 해상도 기준이므로 original_frame을 쓰면 호출자가
    좌표를 먼저 맞춰야 한다. 이 함수에서 해상도 비율을 추측하지 않는다.
    기존 zones 데이터, 검출 ROI, 원본 배열, pipeline 및 recorder는 변경하지 않는다.
    """

    if (
        not isinstance(frame, np.ndarray)
        or frame.ndim != 3
        or frame.shape[2] != 3
        or frame.shape[0] == 0
        or frame.shape[1] == 0
        or frame.dtype != np.uint8
    ):
        raise ValueError("frame은 비어 있지 않은 H x W x 3 uint8 BGR 배열이어야 합니다.")
    if isinstance(padding, (bool, np.bool_)) or not isinstance(padding, (int, np.integer)) or padding < 0:
        raise ValueError("padding은 0 이상의 정수여야 합니다.")

    try:
        points = np.asarray(polygon)
    except (TypeError, ValueError) as exc:
        raise ValueError("polygon은 정수 좌표 목록이어야 합니다.") from exc
    if (
        points.ndim != 2
        or points.shape[1] != 2
        or points.shape[0] < 3
        or points.dtype.kind not in "iu"
        or points.dtype.kind == "b"
        or np.any(points < np.iinfo(np.int32).min)
        or np.any(points > np.iinfo(np.int32).max)
    ):
        raise ValueError("polygon은 세 점 이상의 (N, 2) 정수 좌표여야 합니다.")
    points = points.astype(np.int32)
    if cv2.contourArea(points) == 0:
        raise ValueError("polygon의 면적이 0입니다.")

    x, y, width, height = cv2.boundingRect(points)
    frame_height, frame_width = frame.shape[:2]
    left = max(0, x - padding)
    top = max(0, y - padding)
    right = min(frame_width, x + width + padding)
    bottom = min(frame_height, y + height + padding)
    if left >= right or top >= bottom:
        raise ValueError("polygon과 frame의 교차 영역이 없습니다.")
    return frame[top:bottom, left:right].copy()



def save_event_snapshot(
    frame: np.ndarray,
    output_dir: str | Path,
    event_id: str | int,
    zone_name: str | None = None,
) -> Path:
    """침입 확정 순간의 대표 JPG를 저장하여 사건을 빠르게 확인하기 위한 함수.

    입력: frame은 비어 있지 않은 (H, W, 3) uint8 BGR 원본 또는 crop 결과,
    output_dir은 저장 디렉터리, event_id는 비어 있지 않은 문자열 또는
    0 이상 정수 사건 ID, zone_name은 생략 가능하고 제공 시 비어 있지 않은 이름이다.
    반환: cv2.imwrite 성공을 확인한 JPG 경로인 pathlib.Path이다.
    구현: 디렉터리를 준비하고 event_{id}_{zone}.jpg로 저장한다.
    정수/숫자 문자열 ID는 최소 네 자리로 표현한다(1 또는 "1" → 0001).
    zone_name=None이면 구역 접미사를 생략한다. 예: event_0001_zone1.jpg.
    숫자가 아닌 기존 mock ID도 문자열로 수용한다.
    연동: pipeline.run_pipeline에서 정규화된 event["type"] == "alert"일 때
    event_id/zone_name과 같은 시점의 packet["original_frame"]을 전달한다.
    지속 ALERT의 매 프레임마다 저장하지 않도록 호출자가 이벤트당 한 번 호출한다.
    주의: ID/구역명의 경로 구분자·상위 경로 이동 문자를 안전한 문자로 치환하고,
    특수문자만 있거나 긴 이름은 해시를 포함한 짧은 파일명으로 바꾼다.
    같은 이름이 존재하면 덮어쓰지 않고 FileExistsError로 알리는 계약이다.
    반복 실행에는 호출자가 실행별 output_dir을 제공한다. 잘못된 입력은
    ValueError, 디렉터리/인코딩/쓰기 실패는 OSError로 알려 실패를 숨기지 않는다.
    기존 recorder.record_frame은 동영상 클립 담당이며 이 함수는 정지 이미지 전용이다.
    Recorder 버퍼·클립·CSV 구조, 사건 ID 생성, config, 원본 픽셀은 변경하지 않는다.
    """

    if (
        not isinstance(frame, np.ndarray)
        or frame.ndim != 3
        or frame.shape[2] != 3
        or frame.shape[0] == 0
        or frame.shape[1] == 0
        or frame.dtype != np.uint8
    ):
        raise ValueError("frame은 비어 있지 않은 H x W x 3 uint8 BGR 배열이어야 합니다.")
    if not isinstance(output_dir, (str, Path)) or not str(output_dir).strip():
        raise ValueError("output_dir은 비어 있지 않은 경로여야 합니다.")
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
        raise ValueError("event_id는 문자열 또는 0 이상의 정수여야 합니다.")
    if isinstance(event_id, int) and event_id < 0:
        raise ValueError("event_id는 0 이상이어야 합니다.")
    if isinstance(event_id, str) and not event_id.strip():
        raise ValueError("event_id는 비어 있지 않아야 합니다.")
    if zone_name is not None and (not isinstance(zone_name, str) or not zone_name.strip()):
        raise ValueError("zone_name은 비어 있지 않은 문자열이어야 합니다.")

    def safe_part(value: str) -> str:
        # 점·경로 구분자 등을 제거해 파일명 한 부분으로만 사용한다.
        cleaned = re.sub(r"[^\w-]+", "_", value.strip(), flags=re.UNICODE).strip("_")
        if not cleaned or len(cleaned) > 64:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            return f"{cleaned[:32] or 'item'}_{digest}"
        return cleaned

    id_text = str(event_id).strip()
    if re.fullmatch(r"[0-9]+", id_text):
        id_text = id_text.zfill(4)
    safe_id = safe_part(id_text)
    safe_zone = safe_part(zone_name) if zone_name is not None else None

    filename = f"event_{safe_id}"
    if safe_zone is not None:
        filename += f"_{safe_zone}"
    path = Path(output_dir) / f"{filename}.jpg"

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        encoded_ok = imwrite_unicode(path, frame, overwrite=False)
    except cv2.error as exc:
        raise OSError("JPG 인코딩에 실패했습니다.") from exc
    if not encoded_ok:
        raise OSError("JPG 인코딩에 실패했습니다.")

    return path



def make_event_thumbnail(
    frame: np.ndarray, width: int = 320, height: int = 180
) -> np.ndarray:
    """사건 목록·보고서 preview에 사용할 일정 크기 축소 이미지를 만들기 위한 함수.

    입력: frame은 비어 있지 않은 (H, W, 3) uint8 BGR snapshot 또는 crop,
    width/height는 양의 정수 목표 픽셀 크기이다. 기본 출력은 320x180이다.
    반환: 정확히 (height, width, 3)인 별도 uint8 BGR 배열이다.
    구현: 두 축에 동일 배율을 적용해 cv2.resize로 목표 안에 맞춘다.
    남는 공간은 cv2.copyMakeBorder의 검정 상수색 letterbox로 채운다.
    연동: alert 시점에 확보한 frame/crop을 사건 목록이나 보고서용으로
    가공할 때 호출한다. 기존 보고서 생성 코드에는 아직 연결하지 않는다.
    주의: 종횡비를 유지하고 강제 늘리기/잘라내기를 하지 않는다. 정수 반올림
    오차로 목표 크기를 넘거나 한 변이 0이 되지 않도록 제한한다. 홀수 여백은
    아래/오른쪽에 한 픽셀 더 둔다. 축소는 INTER_AREA, 확대는 INTER_LINEAR를
    사용할 예정이다. 작은 입력은 목표 영역에 맞게 확대할 수 있다.
    잘못된 프레임/0 이하 크기/bool 크기는 ValueError로 거부한다.
    파일 저장은 하지 않고 원본 배열, 검출 해상도, 기존 Recorder 영상 크기,
    config 및 benchmark/보고서 코드는 변경하지 않는다.
    """

    if (
        not isinstance(frame, np.ndarray)
        or frame.ndim != 3
        or frame.shape[2] != 3
        or frame.shape[0] == 0
        or frame.shape[1] == 0
        or frame.dtype != np.uint8
    ):
        raise ValueError("frame은 비어 있지 않은 H x W x 3 uint8 BGR 배열이어야 합니다.")
    if (
        isinstance(width, (bool, np.bool_))
        or isinstance(height, (bool, np.bool_))
        or not isinstance(width, (int, np.integer))
        or not isinstance(height, (int, np.integer))
        or width <= 0
        or height <= 0
    ):
        raise ValueError("width와 height는 양의 정수여야 합니다.")

    source_height, source_width = frame.shape[:2]
    scale = min(width / source_width, height / source_height)
    resized_width = min(width, max(1, round(source_width * scale)))
    resized_height = min(height, max(1, round(source_height * scale)))
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    resized = cv2.resize(frame, (resized_width, resized_height), interpolation=interpolation)

    remaining_width = width - resized_width
    remaining_height = height - resized_height
    left = remaining_width // 2
    top = remaining_height // 2
    thumbnail = cv2.copyMakeBorder(
        resized,
        top,
        remaining_height - top,
        left,
        remaining_width - left,
        cv2.BORDER_CONSTANT,
        value=(0, 0, 0),
    )
    return thumbnail.copy()


# ==========================================================
# 기존 시스템 연동 위치
# ==========================================================
# pipeline.run_pipeline에서 save_alert_snapshots를 호출한다.
# pipeline.run_pipeline
#   ├─ RealBackend.update → state.update_state → 정규화된 events
#   └─ event["type"] == "alert" 발생 (상태 유지와 구별)
#        ├─ packet["original_frame"] → save_event_snapshot
#        ├─ 같은 좌표계 frame + zone["points"] → crop_zone_snapshot (선택)
#        └─ snapshot/crop 배열 → make_event_thumbnail → 목록/보고서 preview
# event["event_id"]와 event["zone_name"]을 저장 인자로 전달할 수 있다.
# 실제 backend는 original_zones를 보존하여 원본 crop에 사용한다.
# recorder.record_frame의 동영상 저장과 독립적으로 동작한다.


def main() -> None:
    """영상 한 프레임을 사용한 독립 실행 예시. 실제 경보 판정은 하지 않는다."""
    parser = argparse.ArgumentParser(description="영상 프레임의 원본·구역·썸네일 이미지 저장")
    parser.add_argument("video", type=Path)
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=Path("snapshot_result"))
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 0:
        parser.error("--seconds는 0 이상의 유한한 값이어야 합니다.")
    if not args.video.is_file():
        parser.error(f"영상을 찾을 수 없습니다: {args.video}")
    if args.output.exists():
        parser.error("결과 폴더가 이미 있습니다. 새 --output 폴더를 지정하세요.")
    capture = cv2.VideoCapture(str(args.video))
    try:
        if not capture.isOpened():
            raise OSError(f"영상을 열 수 없습니다: {args.video}")
        capture.set(cv2.CAP_PROP_POS_MSEC, args.seconds * 1000)
        ok, frame = capture.read()
        if not ok:
            raise OSError("지정한 시각의 프레임을 읽을 수 없습니다.")
        position = int(capture.get(cv2.CAP_PROP_POS_FRAMES)) - 1
    finally:
        capture.release()
    height, width = frame.shape[:2]
    polygon = [[width // 4, height // 4], [3 * width // 4, height // 4],
               [3 * width // 4, 3 * height // 4], [width // 4, 3 * height // 4]]
    crop = crop_zone_snapshot(frame, polygon)
    full_path = save_event_snapshot(frame, args.output, 1, "full")
    save_event_snapshot(crop, args.output, 1, "crop")
    save_event_snapshot(make_event_thumbnail(crop), args.output, 1, "thumbnail")
    metadata = {"video": str(args.video.resolve()), "seconds": args.seconds,
                "frame_index": position, "polygon": polygon,
                "full_image": full_path.name, "demo_only": True}
    (args.output / "snapshot_meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"저장 완료: {args.output.resolve()}")


if __name__ == "__main__":
    main()
