"""B 녹화 테스트: 작은 합성 프레임과 전이로 버퍼·독립 사건 파일·종료 정리를 검증한다.
입력은 합성 BGR 영상과 초 단위 시각이며 출력은 단언 결과와 임시 클립이다.
3초 사전·해제 후 5초, 재표본화 및 오류 분기를 확인한다. 실제 촬영·장치 성능은 검증하지 않는다."""

import cv2
import numpy as np
import pytest
from src.recorder import create_recorder, buffer_frame, record_frame, close_recorder


def session(tmp_path, **extra):
    """tmp_path와 extra 설정으로 10 FPS·32×24 녹화 세션을 반환한다. 설정 검증 오류는 전파한다."""
    return create_recorder({"recording_dir": str(tmp_path), **extra}, 10, (32, 24))


def frame():
    """내용이 일정한 24×32 BGR uint8 합성 프레임을 반환한다. 실제 검출 성능을 검증하는 입력은 아니다."""
    return np.full((24, 32, 3), 80, dtype=np.uint8)


def event(kind, t, key="one", zone="z"):
    """kind·영상 초 t·key·zone으로 A 전이 형식의 테스트 딕셔너리를 반환한다. 사건 판정은 수행하지 않는다."""
    return dict(type=kind, event_id=key, zone_name=zone, media_time_s=t)


def test_three_second_buffer(tmp_path):
    """10 FPS에서 7~10초 양끝을 포함한 31프레임과 원본 복사 보존을 확인한다.
    입력 배열을 바꿔도 버퍼가 변하지 않아야 하며 실제 고해상도 메모리 부하는 측정하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    s = session(tmp_path)
    f = frame()
    for n in range(101):
        buffer_frame(s, f, n / 10)
    assert s["buffer"][0][0] == 7
    assert s["buffer"][-1][0] == 10
    assert len(s["buffer"]) == 31
    f[:] = 0
    assert s["buffer"][0][1].mean() == 80
    close_recorder(s)


def test_event_file_and_long_extension(tmp_path):
    """4초 경보·12초 해제에서 1~17초의 161프레임 녹화를 확인한다.
    경보 후 5초가 지나도 유지 중이면 닫지 않고 해제 후 5초까지 연장해야 한다.
    작은 합성 영상의 범위·해상도·프레임 수 검증이며 실제 촬영 영상의 화질 평가는 아니다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    s = session(tmp_path)
    for n in range(181):
        t = n / 10
        ev = [event("alert", t)] if n == 40 else [event("cleared", t)] if n == 120 else []
        record_frame(s, frame(), t, ev)
        if n == 100:
            assert (
                "one" in s["active"]
            )  # 경보 후 5초가 지났어도 해제되지 않았으므로 녹화를 유지한다.
    assert not s["active"]
    clip = s["clips"][0]
    assert clip["start_s"] == 1
    assert clip["end_s"] == pytest.approx(17)
    assert not clip["truncated"]
    cap = cv2.VideoCapture(clip["path"])
    assert cap.isOpened()
    assert cap.get(cv2.CAP_PROP_FRAME_COUNT) == 161
    assert cap.get(cv2.CAP_PROP_FRAME_WIDTH) == 32
    cap.release()
    close_recorder(s)


def test_independent_duplicate_events_and_close(tmp_path):
    """중복 경보는 하나로 처리하고 서로 다른 사건은 두 파일·writer로 분리되는지 확인한다.
    EOF 절단 표시, 모든 writer 해제와 반복 close의 빈 반환도 확인한다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    s = session(tmp_path)
    ev = [event("alert", 0), event("alert", 0), event("alert", 0, "two", "z2")]
    record_frame(s, frame(), 0, ev)
    assert len(s["active"]) == 2
    writers = [c["writer"] for c in s["active"].values()]
    clips = close_recorder(s)
    assert len(clips) == 2 and len({c["path"] for c in clips}) == 2
    assert all(not w.isOpened() for w in writers)
    assert close_recorder(s) == []
    assert all(c["truncated"] for c in clips)


def test_irregular_hold_and_invalid_time(tmp_path):
    """0초와 0.5초 입력 사이를 직전 프레임으로 채워 6개 출력·4개 유지 tick이 되는지 확인한다.
    뒤로 가는 영상 시각은 ValueError여야 한다. 불규칙 입력의 모든 형태를 포괄하지는 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    s = session(tmp_path)
    record_frame(s, frame(), 0, [event("alert", 0)])
    record_frame(s, frame(), 0.5, [])
    assert s["active"]["one"]["frames_written"] == 6
    assert s["active"]["one"]["held_ticks"] == 4
    with pytest.raises(ValueError):
        record_frame(s, frame(), 0.2, [])
    close_recorder(s)


def test_writer_open_failure_releases(tmp_path, monkeypatch):
    """열리지 않는 VideoWriter 대역으로 OSError 전파 전에 release를 호출하는지 확인한다.
    실제 코덱별 실패 동작 대신 자원 정리 분기를 검증한다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    from src import recorder

    class BadWriter:
        """열기 실패 상황에서 해제 호출 여부만 확인하는 writer 대역이다."""

        def __init__(self):
            """해제 여부를 False로 준비한다. 반환값은 없다."""
            self.released = False

        def isOpened(self):
            """writer 열기 실패를 모사하여 False를 반환한다."""
            return False

        def release(self):
            """실제 파일 없이 해제 호출 여부를 기록한다. 반환값은 없다."""
            self.released = True

    writer = BadWriter()
    monkeypatch.setattr(recorder.cv2, "VideoWriter", lambda *args: writer)
    s = session(tmp_path)
    with pytest.raises(OSError, match="writer"):
        record_frame(s, frame(), 0, [event("alert", 0)])
    assert writer.released
    close_recorder(s)


def test_prebuffer_memory_limit(tmp_path):
    """예상 사전 버퍼보다 작은 recorder_max_mb가 세션 생성 시 ValueError로 거부되는지 확인한다.
    실행 중 실제 메모리 사용량이나 큐·writer 메모리까지 측정하지는 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    with pytest.raises(ValueError, match="recorder_max_mb"):
        session(tmp_path, recorder_max_mb=0.0001)


def test_all_writers_release_even_if_one_release_fails(tmp_path):
    """첫 writer 해제 오류가 나도 나머지를 해제하고 세션을 닫은 뒤 오류를 전파하는지 확인한다.
    테스트 대역의 해제 호출 여부를 확인하며 운영체제 자원 회수까지 직접 측정하지 않는다.
    반환값은 없으며 기대 조건 위반은 단언 실패로 보고한다."""
    s = session(tmp_path)

    class ReleaseWriter:
        """여러 writer 중 일부의 해제 실패를 재현하는 자원 대역이다."""

        def __init__(self, fail):
            """fail로 해제 오류 여부를 지정하고 호출 기록을 준비한다. 반환값은 없다."""
            self.fail = fail
            self.released = False

        def release(self):
            """해제 호출을 기록하고 fail이면 RuntimeError를 발생시킨다. 정상 반환값은 없다."""
            self.released = True
            if self.fail:
                raise RuntimeError("release test failure")

    writers = [ReleaseWriter(True), ReleaseWriter(False)]
    common = dict(
        path="TEST",
        event_ids=[],
        zone_name="z",
        alert_time_s=0,
        occurred_at="",
        start_s=0,
        frames_written=1,
        held_ticks=0,
        resampled_inputs=0,
        last_written_s=0,
        pre_truncated=False,
    )
    s["active"] = {str(i): dict(common, writer=w) for i, w in enumerate(writers)}
    with pytest.raises(RuntimeError, match="release test failure"):
        close_recorder(s)
    assert all(w.released for w in writers)
    assert s["closed"] and not s["active"]
