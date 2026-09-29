"""B 담당 실행 진입점: CLI와 JSON 설정을 합쳐 미리보기 또는 감시 파이프라인을 실행한다.
입력은 명령행 인자·설정 파일·카메라 번호 또는 영상 경로이며, 출력은 종료 코드와 실행 결과 파일이다.
설정 검증 후 실행 경로를 선택하고 오류를 종료 코드로 전달한다. 실제 A 구현의 오류를 Mock으로 숨기지 않는다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import cv2

if not __package__:
    # 공통 utils나 A의 import를 바꾸지 않고 python src/main.py 직접 실행을 지원한다.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.utils import FPSMeter, load_config
from src.pipeline import run_pipeline, validate_config


def process(frame):
    """BGR uint8 프레임을 회색조로 변환한 뒤 3채널 BGR 영상으로 반환한다.
    기존 미리보기의 출력 형식을 유지하기 위한 변환이며 감지용 전처리는 아니다.
    잘못된 프레임의 OpenCV 변환 오류(cv2.error)는 호출자에게 전달한다."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def preview(source: str, no_display: bool = False) -> int:
    """source 영상의 회색조 미리보기를 재생하고 정상 종료 시 0을 반환한다.
    숫자 문자열은 카메라 번호로 해석하며, no_display=True이면 GUI 없이 끝까지 읽는다.
    입력 열기 실패는 OSError, 변환·표시 실패는 OpenCV 오류로 전달한다.
    EOF 또는 q 입력으로 종료하며 예외가 나도 캡처와 사용한 창을 정리한다."""
    cap = cv2.VideoCapture(int(source) if str(source).isdigit() else source)
    try:
        if not cap.isOpened():
            raise OSError(f"Cannot open input: {source}")
        fps = FPSMeter()
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            out = process(frame)
            cv2.putText(
                out,
                f"FPS {fps.tick():.1f}",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
            )
            if not no_display:
                cv2.imshow("result", out)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cap.release()
        if not no_display:
            cv2.destroyAllWindows()
    return 0


def main(argv=None) -> int:
    """argv를 해석하고 설정을 검증한 뒤 dry-run, 미리보기 또는 감시를 실행한다.
    argv=None이면 프로세스 인자를 사용하며 명시한 CLI 값이 JSON 설정보다 우선한다.
    정상 실행은 0, 처리한 설정·입출력·A 미구현 오류는 2, Ctrl-C는 130을 반환한다.
    argparse의 인자 오류는 SystemExit로 종료한다. dry-run은 영상이나 A 구현을 검증하지 않는다."""
    ap = argparse.ArgumentParser(
        description="OpenCV safety monitor: B/C pipeline with explicit A/mock backend"
    )
    ap.add_argument("--source", help="camera number or video file")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config.json"))
    ap.add_argument("--mode", choices=("baseline", "threaded", "optimized"), default="baseline")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--no-display", action="store_true", default=None)
    ap.add_argument("--mock-detection", action="store_true", default=None)
    ap.add_argument("--output-dir")
    ap.add_argument("--duration", type=float, help="measured wall-time limit in seconds; 0=EOF")
    ap.add_argument("--loop-video", action="store_true", default=None)
    ap.add_argument("--experiment-kind", choices=("throughput", "realtime"))
    ap.add_argument("--queue-policy", choices=("block", "drop_oldest"))
    ap.add_argument("--zones", help="zone JSON path (original video coordinates)")
    args = ap.parse_args(argv)
    try:
        config = load_config(args.config)
        for name in (
            "no_display",
            "mock_detection",
            "output_dir",
            "loop_video",
            "experiment_kind",
            "queue_policy",
        ):
            value = getattr(args, name)
            if value is not None:
                config[name] = value
        if args.duration is not None:
            config["duration_seconds"] = args.duration
        if args.zones is not None:
            config["zones_path"] = args.zones
        # 실제 GUI 실행은 첫 프레임 편집→Space 저장→감시 순서로 진행한다.
        # headless에서는 파일만 읽으며 Mock 테스트에는 편집기를 끼워 넣지 않는다.
        config["edit_zones"] = not config.get("no_display", False) and not config.get(
            "mock_detection", False
        )
        config = validate_config(config)
        source = args.source if args.source is not None else config["source"]
        for folder in ("data/samples", config["output_dir"]):
            Path(folder).mkdir(parents=True, exist_ok=True)
        if args.dry_run:
            print(
                json.dumps(
                    dict(
                        source=source,
                        mode=args.mode,
                        config_valid=True,
                        mock=config["mock_detection"],
                        note="Input/A implementation not exercised",
                    ),
                    ensure_ascii=False,
                )
            )
            return 0
        if args.preview:
            return preview(source, config["no_display"])
        return run_pipeline(source, args.mode, config)
    except NotImplementedError as exc:
        print(
            f"[A 미구현] {exc}. B/C 검증에는 명시적으로 --mock-detection을 사용하세요.",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, cv2.error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
