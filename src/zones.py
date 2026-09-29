"""A의 schema_version=1 JSON과 다각형 편집을 integration 조작으로 제공한다.
표시 좌표는 실제 축별 크기로 복원하고 저장/불러오기에 동일 검증을 적용한다.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

import json
from pathlib import Path
import math


def validate_zones(zones, frame_size):
    """구역 이름·좌표 범위·면적·자기 교차를 검사하고 독립 복사본을 반환한다.
    A의 name/points 형식을 유지한다. id만 있는 기존 입력은 name으로도 접근 가능하게
    보완하되 서로 다른 id/name은 상태와 화면이 다른 구역을 가리키므로 거부한다.
    """
    if (
        not isinstance(frame_size, (list, tuple))
        or len(frame_size) != 2
        or any(type(v) is not int or v <= 0 for v in frame_size)
    ):
        raise ValueError("frame_size must contain positive integer width and height")
    if not isinstance(zones, list) or not zones:
        raise ValueError("At least one valid zone is required")
    result, names = [], set()
    w, h = frame_size
    for zone in zones:
        if not isinstance(zone, dict):
            raise ValueError("Zone must be an object")
        name = zone.get("name") or zone.get("id")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Zone names must be nonempty and unique")
        if zone.get("id") and zone["id"] != name:
            raise ValueError("Zone id and name must agree")
        points = zone.get("points")
        if not isinstance(points, list) or len(points) < 3:
            raise ValueError("Polygon requires at least 3 vertices")
        for point in points:
            if (
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    for v in point
                )
            ):
                raise ValueError("Vertices must be finite numeric pairs")
            if not (0 <= point[0] < w and 0 <= point[1] < h):
                raise ValueError("Vertex outside original frame")
        if len({tuple(p) for p in points}) != len(points):
            raise ValueError("Repeated vertices are invalid")
        # 인접하지 않은 변의 교차와 접촉도 거부한다. 오목하지만 단순한 다각형은 허용한다.
        count = len(points)
        for i in range(count):
            # 인접 변도 이전 방향으로 되돌아가면 겹친다. 일직선으로 이어지는 점은 허용하되
            # 되짚는 꼭짓점은 단순 다각형이 아니므로 저장하지 않는다.
            a, b, c = points[i - 1], points[i], points[(i + 1) % count]
            cross = (a[0] - b[0]) * (c[1] - b[1]) - (a[1] - b[1]) * (c[0] - b[0])
            dot = (a[0] - b[0]) * (c[0] - b[0]) + (a[1] - b[1]) * (c[1] - b[1])
            if cross == 0 and dot > 0:
                raise ValueError("Overlapping adjacent edges")
            for j in range(i + 1, count):
                if j == i + 1 or (i == 0 and j == count - 1):
                    continue
                if _intersects(
                    points[i], points[(i + 1) % count], points[j], points[(j + 1) % count]
                ):
                    raise ValueError("Self-intersecting polygon")
        area = sum(
            points[i][0] * points[(i + 1) % count][1] - points[(i + 1) % count][0] * points[i][1]
            for i in range(count)
        )
        if abs(area) < 1e-8:
            raise ValueError("Polygon area must be positive")
        # 검출은 정수 격자를 사용하므로 소수 좌표도 같은 격자에서 유효해야 한다.
        if any(type(v) is float and not v.is_integer() for p in points for v in p):
            raise ValueError("Vertices must use integer pixel coordinates")
        names.add(name)
        result.append({**zone, "name": name, "points": [[int(x), int(y)] for x, y in points]})
    return result


def _intersects(a, b, c, d):
    """두 닫힌 선분의 교차·끝점 접촉·겹침 여부를 외적 부호로 판정한다."""

    def cross(p, q, r):
        """p→q와 p→r의 외적을 반환하여 점이 직선의 어느 쪽인지 구한다."""
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def contains(p, q, r):
        """일직선인 점 r이 선분 p-q의 닫힌 범위 안에 있는지 확인한다."""
        return min(p[0], q[0]) <= r[0] <= max(p[0], q[0]) and min(p[1], q[1]) <= r[1] <= max(
            p[1], q[1]
        )

    x, y, z, t = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
    return (
        x * y < 0
        and z * t < 0
        or x == 0
        and contains(a, b, c)
        or y == 0
        and contains(a, b, d)
        or z == 0
        and contains(c, d, a)
        or t == 0
        and contains(c, d, b)
    )


def save_zones(path: str, zones: list[dict[str, Any]], frame_size: tuple[int, int]) -> None:
    """A와 동일한 JSON을 저장한다. 검증 실패 시 기존 파일을 열거나 덮어쓰지 않는다.
    저장은 S 또는 감시 시작 Space라는 명시적 사용자 동작에서만 호출한다.
    """
    valid = validate_zones(zones, frame_size)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(
            json.dumps(
                dict(schema_version=1, frame_size=list(frame_size), zones=valid),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise OSError(f"구역 JSON 저장 실패: {path}") from exc


def load_zones(path: str, frame_size: tuple[int, int]) -> list[dict[str, Any]]:
    """A의 JSON을 읽고 원본 해상도 일치를 검사한다. 다른 해상도로 자동 변환하지 않는다."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
    ):
        raise ValueError("Unsupported zone schema_version")
    if data.get("frame_size") != list(frame_size):
        raise ValueError("Saved frame_size does not match original video")
    return validate_zones(data.get("zones"), frame_size)


def display_size(frame_size, bounds):
    """화면 영역에 맞춘 표시 크기를 반환한다. 반올림된 각 축 크기는 역변환에도 사용한다."""
    scale = min(1.0, bounds[0] / frame_size[0], bounds[1] / frame_size[1])
    return tuple(max(1, round(v * scale)) for v in frame_size)


def to_original(point, frame_size, shown_size):
    """표시 좌표를 원본 픽셀로 역변환한다. 반올림 오차가 경계를 넘지 않도록 제한한다.
    실제 표시 너비·높이를 각각 사용하므로 종횡비 반올림 오차도 저장에 누적되지 않는다.
    """
    return [
        min(frame_size[i] - 1, max(0, round(point[i] * frame_size[i] / shown_size[i])))
        for i in (0, 1)
    ]


def screen_bounds():
    """사용 가능한 화면 크기를 조회한다. 조회 불가 시 보수적인 표시 크기를 사용한다.
    tkinter는 화면 크기 조회에만 쓰며 편집 이벤트와 영상 표시는 OpenCV 메인 스레드가 담당한다.
    """
    try:
        import tkinter
    except ImportError:
        return 960, 540
    try:
        root = tkinter.Tk()
        root.withdraw()
        try:
            return max(1, root.winfo_screenwidth() - 100), max(1, root.winfo_screenheight() - 200)
        finally:
            root.destroy()
    except (tkinter.TclError, RuntimeError):
        # 화면 조회 실패가 JSON을 이용한 headless 실행에는 영향을 주지 않는다.
        return 960, 540


def edit_zones(
    frame: np.ndarray, path: str = "data/zones.json", *, bounds=None
) -> list[dict[str, Any]]:
    """첫 원본 프레임에서 여러 구역을 편집하고 Space에서 저장한 구역을 반환한다.
    Q/Esc/창 닫기는 빈 목록을 반환하여 감시를 시작하지 않는다. S는 확정 구역만 저장하며
    작성 중인 점이 있으면 확정 또는 초기화를 요구한다. L 실패 시 현재 편집 내용을 보존한다.
    표시 이미지는 고정 크기 창으로 띄워 마우스 콜백 좌표와 표시 픽셀을 일치시킨다.
    """
    if frame is None or frame.size == 0:
        raise ValueError("Empty first frame")
    size = (frame.shape[1], frame.shape[0])
    shown = display_size(size, bounds or screen_bounds())
    background = cv2.resize(frame, shown, interpolation=cv2.INTER_AREA)
    confirmed, pending = [], []
    message = "Draw a polygon, then Enter"
    window = "Danger zones"
    if Path(path).exists():
        try:
            confirmed = load_zones(path, size)
            message = "Loaded existing zones"
        except (OSError, ValueError) as exc:
            message = str(exc)

    def mouse(event, x, y, flags, param):
        """좌클릭을 원본 좌표로 저장한다. 상단 안내문도 영상 위에 그려 별도 오프셋이 없다."""
        if event == cv2.EVENT_LBUTTONDOWN and 0 <= x < shown[0] and 0 <= y < shown[1]:
            pending.append(to_original((x, y), size, shown))
        elif event == cv2.EVENT_RBUTTONDOWN and pending:
            pending.pop()  # A의 기존 우클릭 취소 조작도 보존한다.

    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(window, mouse)
    try:
        while True:
            out = background.copy()
            for item in confirmed + ([{"points": pending}] if pending else []):
                points = np.asarray(
                    [
                        [round(x * shown[0] / size[0]), round(y * shown[1] / size[1])]
                        for x, y in item["points"]
                    ],
                    dtype=np.int32,
                )
                done = "name" in item
                color = (0, 220, 0) if done else (0, 180, 255)
                cv2.polylines(out, [points], done, color, 2)
                for point in points:
                    cv2.circle(out, tuple(point), 3, color, -1)
            # OpenCV 기본 글꼴은 한글을 지원하지 않아 화면 조작법만 영문으로 표시한다.
            lines = [
                "Click: vertex | Backspace: undo | Enter: confirm | R: reset",
                "S: save | L: load | Space: save & start | Q/Esc: quit",
                f"Zones: {len(confirmed)} | {message}",
            ]
            for i, line in enumerate(lines):
                cv2.putText(
                    out, line, (8, 20 + i * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3
                )
                cv2.putText(
                    out, line, (8, 20 + i * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1
                )
            cv2.imshow(window, out)
            key = cv2.waitKeyEx(30)
            if (
                key in (27, ord("q"), ord("Q"))
                or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1
            ):
                return []
            try:
                if key in (8, 127, 65288):
                    if pending:
                        pending.pop()
                elif key in (ord("r"), ord("R")):
                    pending.clear()
                elif key in (10, 13):
                    number = 1
                    names = {z["name"] for z in confirmed}
                    while f"zone{number}" in names:
                        number += 1
                    candidate = {"name": f"zone{number}", "points": [p[:] for p in pending]}
                    confirmed = validate_zones(confirmed + [candidate], size)
                    pending.clear()
                    message = "Polygon confirmed"
                elif key in (ord("l"), ord("L")):
                    confirmed = load_zones(path, size)
                    pending.clear()
                    message = "Loaded"
                elif key in (ord("s"), ord("S"), 32):
                    if pending:
                        raise ValueError("Press Enter to confirm or R to reset pending vertices")
                    save_zones(path, confirmed, size)
                    message = "Saved"
                    if key == 32:
                        return confirmed
            except (OSError, ValueError) as exc:
                message = str(exc)
    finally:
        cv2.destroyWindow(window)
