"""Isolated benchmark process; optional OpenCV thread setting is not a detector change."""

import argparse
import json
from pathlib import Path
import sys

import cv2

from src.main import main


def run(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--opencv-threads", type=int)
    args, cli = parser.parse_known_args(argv)
    if args.opencv_threads is not None:
        if args.opencv_threads < 0:
            parser.error("opencv-threads must be nonnegative")
        cv2.setNumThreads(args.opencv_threads)
    config_path = Path(cli[cli.index("--config") + 1])
    config = json.loads(config_path.read_text())
    (Path(config["output_dir"]) / "opencv_environment.json").write_text(
        json.dumps(
            {
                "opencv_threads": cv2.getNumThreads(),
                "opencl_available": cv2.ocl.haveOpenCL(),
                "opencl_enabled": cv2.ocl.useOpenCL(),
            },
            indent=2,
        )
    )
    return main(cli)


if __name__ == "__main__":
    sys.exit(run())
