"""Experiment adapter tests: synthetic data are never reported as measurements."""

import math
import pytest
from scripts import run_experiments as exp
from src.benchmark import write_table


def gt(start=1, end=5, zone="z"):
    return dict(
        video_name="v.mp4", zone_name=zone, start_time=start, end_time=end, event_type="intrusion"
    )


def det(start=2, end=4, **kw):
    return dict(
        video_name="v.mp4",
        zone_name="z",
        start_time=start,
        end_time=end,
        alert_time=3,
        event_id="e",
        **kw,
    )


@pytest.mark.parametrize(
    "truth,events,expected",
    [
        ([gt()], [det()], (1, 0, 0)),
        ([], [det()], (0, 1, 0)),
        ([gt()], [], (0, 0, 1)),
        ([], [], (0, 0, 0)),
        ([gt()], [det(), det()], (1, 1, 0)),
        ([gt(), gt(6, 9)], [det(2, 8)], (1, 0, 1)),
        ([gt(), gt(4, 8)], [det(2, 6), det(2, 3)], (2, 0, 0)),
        ([gt(zone="other")], [det()], (0, 1, 1)),
        ([gt()], [det(5, 6)], (1, 0, 0)),
    ],
)
def test_matching(truth, events, expected):
    metrics, details = exp.evaluate(truth, events, "v.mp4", 0)
    assert tuple(metrics[k] for k in ("tp", "fp", "fn")) == expected
    assert len(details) == sum(expected)


def test_delay_optional_and_tolerance():
    event = det(5.1, 6)
    event["alert_time"] = ""
    metrics, _ = exp.evaluate([gt()], [event], "v.mp4", 0.2)
    assert metrics["tp"] == 1
    assert math.isnan(metrics["alert_delay"])


def test_truth_normalization_and_review(tmp_path):
    zone = tmp_path / "z.json"
    zone.write_text('{"zones":[{"name":"z"}]}')
    path = tmp_path / "gt.csv"
    fields = ["video_name", "zone_name", "start_time", "end_time", "event_type"]
    write_table(
        path,
        [{**gt(), "video_name": "/data/v.mp4", "zone_name": "/data/z.json", "event_type": "침입"}],
        fields,
    )
    options = dict(event_types={"침입": "intrusion"}, merge_zone_intervals=None)
    truth, audit, issues = exp.load_truth(path, "v.mp4", zone, options)
    assert truth == [gt()]
    assert not issues and audit[0]["original_zone"] == "/data/z.json"
    write_table(path, [gt(), gt(2, 6)], fields)
    options["event_types"]["intrusion"] = "intrusion"
    assert exp.load_truth(path, "v.mp4", zone, options)[2]
    options["merge_zone_intervals"] = True
    assert exp.load_truth(path, "v.mp4", zone, options)[0] == [gt(1, 6)]
    options["event_types"] = {}
    assert exp.load_truth(path, "v.mp4", zone, options)[2]


def test_detection_loading(tmp_path):
    path = tmp_path / "det.csv"
    row = det(mock=False, status="closed", run_id="r")
    write_table(path, [row], list(row))
    assert exp.load_detections(path)[0]["event_id"] == "e"
    row["mock"] = True
    write_table(path, [row], list(row))
    with pytest.raises(ValueError, match="Mock"):
        exp.load_detections(path)
    with pytest.raises(FileNotFoundError):
        exp.load_detections(tmp_path / "missing.csv")
    with pytest.raises(FileNotFoundError):
        exp.load_truth(tmp_path / "missing.csv", "v", tmp_path / "z", {})


@pytest.mark.parametrize(
    "fps,base,expected", [(120, 100, 20), (50, 100, -50), (100, 0, None), (100, None, None)]
)
def test_improvement(fps, base, expected):
    result = exp.improvement(fps, base)
    assert math.isnan(result) if expected is None else result == pytest.approx(expected)


def test_balanced_modes():
    rows = [
        dict(
            video="v",
            repeat=1,
            mode=m,
            status="COMPLETED",
            evaluation_status="N/A",
            fps=f,
            measured_frames=100,
            elapsed_time=100 / f,
        )
        for m, f in [("baseline", 10), ("threaded", 20)]
    ]
    rows.append(
        dict(video="failed", repeat=1, mode="baseline", status="FAILED", evaluation_status="N/A")
    )
    rows.append(
        dict(
            video="failed",
            repeat=1,
            mode="threaded",
            status="COMPLETED",
            evaluation_status="N/A",
            fps=1000,
        )
    )
    summary = exp.mode_summary(rows)
    assert summary[1]["fps"] == 20 and summary[1]["pooled_fps"] == 20
    assert summary[1]["improvement_percent"] == 100
    assert summary[1]["evaluated_runs"] == 0


def test_failed_case(tmp_path):
    row, *_ = exp.execute_case(
        {"video": str(tmp_path / "missing.mp4")}, "baseline", 1, tmp_path, {}, {}
    )
    assert row["status"] == "FAILED"
    assert "FileNotFoundError" in row["error"]


def test_runner_continues(tmp_path, monkeypatch):
    base = tmp_path / "config.json"
    base.write_text("{}")
    calls = []

    def executor(spec, mode, repeat, *args):
        calls.append(mode)
        return (
            dict(
                video="v",
                mode=mode,
                repeat=repeat,
                status="FAILED" if mode == "baseline" else "COMPLETED",
                evaluation_status="N/A",
                error="test",
                evaluation_note="",
            ),
            [],
            [],
            [],
            [],
            [],
        )

    monkeypatch.setattr(exp, "plots", lambda *args: [])
    monkeypatch.setattr(exp, "report", lambda *args: None)
    options = dict(
        base_config=str(base),
        output_dir=str(tmp_path / "out"),
        ground_truth=str(tmp_path / "missing"),
        videos=[dict(video="v")],
        video_glob="no-such-file-*",
        repeats=1,
    )
    rows, summary = exp.run(options, executor=executor)
    assert calls == list(exp.MODES)
    assert rows[0]["status"] == "FAILED" and rows[-1]["status"] == "COMPLETED"
    assert (summary / "experiment_summary.csv").is_file()


@pytest.mark.parametrize(
    "truth,events,expected",
    [
        ([gt()], [det(), det(8, "", status="open_at_eof")], (1, 1, 0)),
        ([gt()], [det(2, "", status="open_at_eof")], (1, 0, 0)),
        ([gt()], [det(8, None, status="open_at_eof")], (0, 1, 1)),
        ([gt()], [det(status="closed")], (1, 0, 0)),
    ],
)
def test_eof_matching_preserves_input(truth, events, expected):
    from copy import deepcopy

    for event in events:
        event["alert_time"] = float(event["start_time"]) + 0.2
    before = deepcopy(events)
    metrics, details = exp.evaluate(truth, events, "v.mp4", 0, 10)
    assert tuple(metrics[k] for k in ("tp", "fp", "fn")) == expected
    assert events == before
    for detail in details:
        if detail.get("status") == "open_at_eof":
            assert detail["evaluation_end_time"] == 10
            assert detail["detected_end"] in ("", None)


@pytest.mark.parametrize(
    "changes,eof",
    [
        ({"start_time": ""}, 10),
        ({"alert_time": ""}, 10),
        ({"alert_time": "broken"}, 10),
        ({"start_time": -1}, 10),
        ({}, None),
        ({}, float("nan")),
        ({}, 1),
    ],
)
def test_corrupt_open_event_requires_review(changes, eof):
    event = {**det(2, "", status="open_at_eof"), **changes}
    with pytest.raises(ValueError, match="review"):
        exp.evaluate([gt()], [event], "v.mp4", 0, eof)


def test_source_eof_metadata(tmp_path):
    import cv2
    import numpy as np

    path = tmp_path / "source.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 12, (32, 24))
    assert writer.isOpened()
    for _ in range(30):
        writer.write(np.zeros((24, 32, 3), dtype=np.uint8))
    writer.release()
    meta = exp.video_metadata(path)
    assert meta == dict(frame_count=30, source_fps=12, video_eof_time=2.5)


def test_closed_missing_end_still_requires_review():
    with pytest.raises(ValueError):
        exp.evaluate([gt()], [det(end="", status="closed")], "v.mp4", 0, 10)


def test_final_study_matrix():
    from scripts.final_studies import study_cases

    cases = study_cases()
    resolutions = [c for c in cases if c["study"] == "resolution"]
    assert len(resolutions) == 9
    assert len({c["setting"] for c in resolutions}) == 3
    optimizations = [c for c in cases if c["study"] == "optimization"]
    assert len({c["technique"] for c in optimizations}) >= 4
    assert all(c["config"].get("frame_interval", 1) == 1 for c in cases)
    for technique in {c["technique"] for c in optimizations}:
        assert {c["setting"] for c in optimizations if c["technique"] == technique} == {"OFF", "ON"}


def test_open_event_participates_in_one_to_one_matching():
    events = [det(2, "", status="open_at_eof"), det(2, 4, status="closed")]
    metrics, details = exp.evaluate([gt()], events, "v.mp4", 0, 10)
    assert (metrics["tp"], metrics["fp"], metrics["fn"]) == (1, 1, 0)
    assert len([d for d in details if d["outcome"] == "TP"]) == 1


@pytest.mark.parametrize("fps,count", [(0, 30), (12, 0), (float("nan"), 30)])
def test_source_metadata_invalid_requires_review(monkeypatch, fps, count):
    import cv2

    class Capture:
        released = False

        def isOpened(self):
            return True

        def get(self, key):
            return fps if key == cv2.CAP_PROP_FPS else count

        def release(self):
            self.released = True

    capture = Capture()
    monkeypatch.setattr(cv2, "VideoCapture", lambda _: capture)
    with pytest.raises(ValueError, match="source"):
        exp.video_metadata("invalid")
    assert capture.released
