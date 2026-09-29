# 작업장 위험 구역 감시 및 실시간 영상 처리 최적화

Python/OpenCV로 작업장 다각형 위험 구역의 움직임을 감시하고 같은 영상으로 순차 처리·멀티스레드·최적화 성능을 비교하는 5일 팀 프로젝트입니다. MOG2 배경 차분과 외곽선을 사용하며 외부 AI 객체 검출 모델은 필수가 아닙니다. 사람 식별·객체 추적은 기본 범위 밖입니다.

**A의 실제 전처리·MOG2·침입·상태 함수를 B·C 파이프라인에 통합했습니다.** GUI 실행에서는 첫 프레임으로 다각형을 편집하고, headless 실행에서는 `--zones`로 같은 JSON을 사용합니다. 실제 모드 오류를 Mock으로 숨기지 않습니다. `--mock-detection`은 B·C 연결을 검증하는 시험용 시간표입니다. 실제 촬영 영상의 FPS·정확도·안전성은 아직 측정하지 않았습니다.

## 문제 정의와 개발 목표

고정 카메라 영상에서 위험 구역 침입을 검출하고 N프레임 연속 감지 후 경고합니다. 사건 확정 전 3초와 이후 5초 이상 영상을 보존하고, 침입 시작·경보 확정·종료를 구분하여 CSV에 기록합니다. 같은 입력·판정·출력 조건에서 FPS, 단계별 병목, 경고 지연, 오경보·미검출을 함께 비교합니다.

## 구조 및 담당 파일

```text
team_project/
├── README.md
├── CONTRIBUTING.md
├── requirements.txt
├── config.json
├── .gitignore
├── check_env.py
├── src/
│   ├── main.py
│   ├── utils.py
│   ├── preprocess.py
│   ├── detect.py
│   ├── zones.py
│   ├── state.py
│   ├── recorder.py
│   ├── pipeline.py
│   └── benchmark.py
├── data/
│   ├── samples/                     # Git 제외, main에서 생성
│   └── SOURCES.md
├── results/                         # Git 제외, 실행 시 생성
├── tests/
│   ├── test_benchmark.py
│   ├── test_recorder.py
│   └── test_pipeline.py
└── docs/
    ├── 수행계획서.md
    ├── 실험기록.md
    ├── 최종보고서.md
    └── ground_truth_template.csv
```

기존 교육용 HTML 및 부가 파일은 보존합니다. `src`는 namespace package이며 루트에서 모듈로 실행합니다.

| 파일 | 담당 | 책임 / 분리 이유 |
|---|---|---|
| src/preprocess.py | A | 공통 전처리·좌표 복원 (A 실제 구현 선별 반영) |
| src/detect.py | A | MOG2 움직임 및 침입 판정만 (A 실제 구현 선별 반영) |
| src/zones.py | A | 다각형 마우스 편집과 JSON 저장·로딩 분리 (A 실제 구현 선별 반영) |
| src/state.py | A | N프레임·경보 해제·상태 전이 분리 (A 실제 구현 선별 반영) |
| src/main.py | B | CLI·설정·기존 그레이스케일 preview |
| src/pipeline.py | B | 실행 모드, A 호환 계층, 입력 스레드, Mock, 화면, 사건 CSV |
| src/recorder.py | B | 원본 시간 버퍼·구역별 사건 영상만 저장; 감지·CSV 제외 |
| src/benchmark.py | C | 단계 계측·통계·CSV·실험 반복·그래프·정답 매칭 |
| src/utils.py | 공통 | 기존 유틸리티 구현·시그니처 보존 |
| config.json / README.md | 공통 | 초기 실험 설정 / 실행·인터페이스 계약 |
| requirements.txt / check_env.py | 공통 | 기존 고정 패키지 / 환경 점검 보존 |
| CONTRIBUTING.md / .gitignore | 공통 | Fork·PR 규칙 / 데이터·환경·비밀 파일 제외 |
| data/SOURCES.md | 공통 | 영상 출처·촬영자·촬영일·권한 기록 |
| docs/수행계획서.md | 공통 | 5일 일정·팀 역할 |
| docs/실험기록.md / 최종보고서.md | C | 실제 측정 기록·병목 및 정확도 분석 |
| docs/ground_truth_template.csv | C | 추가: 실제 영상 정답 사건표의 고정 컬럼 |
| tests/test_*.py | B·C | 추가: 합성 프레임·임시 영상으로 기능과 실패 경로 검증 |

## 시스템 아키텍처와 세 버전

```text
main → FrameSource → 원본 시간 버퍼 → A preprocess → A detect → A state
                                      ↓                  ↓
                               원본 좌표 복원      B EventLedger
                                                        ↓
                      main thread 화면 ← 상태/박스    사건 CSV
                                                        ↓
                                               recorder 원본 클립
              모든 단계의 perf_counter 표본 → benchmark → 종료 후 CSV
```

| 모드 | 실행 구조 | 분석 설정 |
|---|---|---|
| baseline | 읽기·버퍼·분석·상태·표시·녹화 순차 | 전체 입력, 원본 분석 크기, 간격 1 |
| threaded | 입력 worker → bounded Queue → 메인 분석/표시/녹화 | baseline과 동일 |
| optimized | threaded 구조 + 개별 최적화 | ROI, 축소, 간격, 큐 크기 설정 적용 |

세 버전의 실제 검출은 모두 동일한 A 함수 경로를 호출합니다. `resize_enabled=False`이면 분석 크기를 입력 크기로 설정하여 A 전처리 축소를 끕니다. ROI는 모든 구역을 포함한 사각형에 `roi_margin` 픽셀 여유를 더하고 영상 범위로 제한합니다. `min_area`는 실험 입력 좌표계의 면적이며 crop/분석 축소 시 실제 scale_x×scale_y를 곱합니다. 원본 파일 해상도와 다른 실험 입력을 만들면 구역도 같은 비율로 변환됩니다. 서로 다른 **입력 해상도**의 정확도 비교에서는 물체 크기·최소 면적 효과를 별도로 해석해야 합니다.

원본 파일 프레임과 실험용 리사이즈 프레임은 packet에서 분리됩니다. 녹화는 언제나 원본 파일 크기이며 ROI/분석 축소 영상이 아닙니다. 이 실험은 같은 원본 파일의 디코딩 뒤 입력 크기를 바꾸는 방식이므로, 해상도별 디코더 성능 비교로 해석하면 안 됩니다.

### 입력 스레드와 프레임 생략

`Queue(maxsize=queue_max_size)`에 원본 프레임을 순서대로 전달합니다. 기본 `block` 정책은 timeout put/get, stop/done Event, join으로 종료하며 worker 예외를 메인에 전달합니다. GUI는 메인 스레드에서만 호출합니다. 분석 간격에 따라 생략해도 원본은 recorder에 전달합니다.

`--queue-policy drop_oldest` 옵션은 **명시적으로 오류 처리**합니다. 분석 큐와 별개로 원본을 보존할 저장 경로가 없으므로 드롭을 허용하지 않습니다. 실제 큐 드롭 수는 현재 0이며 분석 간격에 의한 생략률은 `skip_rate`로 별도 기록합니다. 종료 순간 입력 스레드가 미리 읽은 프레임은 `pending_at_stop`에 기록하며 정상 처리 중 드롭과 구분합니다. 카메라 드라이버 내부 유실은 OpenCV만으로 계수할 수 없습니다.

A 구현은 미분석 `None`에서 카운터를 유지하고 인덱스 공백을 검사하지 않습니다. **연속 원본 N프레임** 기준을 유지하기 위해 이번 실제 통합은 `frame_interval=1`, Queue `block`만 허용합니다. 분석 생략 실험은 명시적 Mock에서만 가능합니다. `consecutive_frames`가 A의 `alert_frames`보다 우선하며 기본 경보 5프레임·해제 10프레임은 유지합니다. `observed_duration_s`는 마지막으로 관측된 True와 첫 True 사이 영상 시간이며 None에서 늘리지 않습니다. 생략 구간에 침입이 지속됐다고 단정하는 지표가 아닙니다.

## 환경 설정 및 실행

프로젝트 루트에서 Python **3.10**을 사용합니다. 기존 requirements의 OpenCV contrib 4.10.0.84, NumPy 1.26.4, Pandas 2.2.2, Matplotlib 3.8.4를 유지합니다. Threading/Queue/Collections는 표준 라이브러리입니다.

```bash
python3.10 -m venv .venv
source .venv/bin/activate
# Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pip install pytest
python check_env.py
python -m pytest -q tests -p no:cacheprovider
```

현재 가능한 연결 점검과 기존 미리보기:

```bash
python -m src.main --help
python -m src.main --source data/samples/test.mp4 --mode baseline --dry-run
python -m src.main --source data/samples/test.mp4 --preview
python -m src.main --source data/samples/test.mp4 --preview --no-display
```

`--dry-run`은 설정만 검증하며 파일·카메라·A 기능을 검증하지 않습니다. `--preview`는 기존 그레이스케일 재생이며 실제 감시 Baseline으로 사용하지 않습니다. `q` 또는 Ctrl-C로 종료합니다. 기존 `python src/main.py`도 지원합니다. Linux 입력은 B 안에서 OpenCV 기본 backend를 사용하며 utils의 기존 CAP_DSHOW 코드는 변경하지 않았습니다.

실제 영상으로 다음 명령을 실행합니다. GUI에서는 첫 원본 프레임이 표시됩니다.
좌클릭: 꼭짓점 추가, Backspace/우클릭: 마지막 점 취소, Enter: 다각형 확정,
R: 작성 중인 점 초기화, S: JSON 저장, L: JSON 불러오기,
Space: 저장 후 감시 시작, Q/Esc: 감시 시작 없이 종료입니다.
여러 구역을 확정할 수 있으며 최소 3점·범위·중복·자기 교차·면적을 검사합니다.
S/Space 전에 작성 중인 점은 Enter로 확정하거나 R로 지워야 합니다.
기존 구역은 자동 불러오며, 원본 해상도가 다른 JSON은 거부합니다.
`--zones data/zones.json`은 저장/불러오기 경로이며 생략 시 config의 `zones_path`를 사용합니다.
축소 표시의 마우스 좌표는 실제 가로·세로 배율로 원본 픽셀에 복원합니다.
OpenCV 기본 글꼴 제약 때문에 화면 조작법은 영문으로 표시합니다.

```bash
python -m src.main --source data/samples/test.mp4 --mode baseline
python -m src.main --source data/samples/test.mp4 --mode threaded
python -m src.main --source data/samples/test.mp4 --mode optimized
python -m src.main --source data/samples/test.mp4 --mode baseline --zones data/zones.json --no-display
```

B·C만 검증할 때는 준비한 영상에 명시적 Mock을 사용합니다.

```bash
python -m src.main --source data/samples/test.mp4 --mode baseline --no-display --mock-detection
python -m src.main --source data/samples/test.mp4 --mode threaded --no-display --mock-detection
python -m src.main --source data/samples/test.mp4 --mode optimized --no-display --mock-detection
```

Mock은 픽셀을 분석하지 않고 1초에 관측 시작, 1.5초에 경보, 2.5초에 해제하는 시간표를 사용합니다. 스킵 중에는 전이를 발생시키지 않습니다. 영상이 짧으면 경보가 없을 수 있습니다. 화면·로그에 MOCK을 표시하고 결과는 `results/mock/`에만 저장합니다. Mock의 N·정확도·최적화 속도를 A 알고리즘 성능으로 해석하지 마세요. 테스트에서는 `run_pipeline(..., backend=fixture)`로 `is_mock=True`인 객체만 주입할 수 있습니다.

`--config`로 설정 파일을 선택하고 `--source`가 config source보다 우선합니다. `--output-dir`, `--duration`(측정 벽시계 초), `--loop-video`, `--experiment-kind throughput|realtime`도 지원합니다. 상대 경로는 프로젝트 루트에서 실행하는 것을 기준으로 합니다.

## 자동 녹화 및 사건 CSV

`buffer_frame`은 분석 전에 `(영상 시각, 원본 copy)`를 deque에 보관합니다. `record_frame`은 같은 프레임을 중복 추가하지 않고 외부 사건 전이를 소비합니다. 기존 세 함수 시그니처는 보존했습니다.

- 시작: 경보 확정 시각 `alert_time - pre_seconds`부터 확보한 원본을 기록합니다.
- 종료: 경보가 유지되는 동안 계속 기록하고, 해제 전이를 받은 시각 이후 `post_seconds`를 추가 기록합니다. 최소 종료 예정 시각은 alert+post이며 장시간 침입은 해제까지 연장합니다.
- 시작부터 3초가 없거나 EOF/중단/반복 경계에서 사후 구간이 부족하면 truncated로 표시합니다. 없는 프레임을 생성해 사후 5초를 채우지 않습니다.
- 여러 구역·사건은 **독립 writer와 파일**을 사용합니다. 초기 설계의 겹침 병합 대신 이번 요구사항에 맞춰 분리했습니다. 같은 event_id의 반복 alert/clear는 중복 저장하지 않습니다.
- 클립은 UTC 날짜와 사건 ID를 포함한 이름입니다. 명시적인 `recording_enabled=False`는 파일 저장만 끕니다.

CFR 파일은 기본 `frame_index / source_fps`를 사용합니다. VFR 파일은 `timestamp_mode=pts`로 POS_MSEC를 사용하고 단조 증가하지 않으면 오류로 종료합니다. 카메라는 perf_counter 경과 시각이며 nominal FPS가 없으면 명시된 camera_fps를 사용합니다. 잘못된 파일 FPS는 조용히 추정하지 않습니다.

VideoWriter는 고정 FPS이므로 불규칙 시각은 **직전 프레임 유지**로 재표본화합니다. 누락된 출력 tick은 이전 프레임으로 채우고, 한 tick 안에 여러 입력이 있으면 모두 표현할 수 없습니다. `held_ticks`, `resampled_inputs`, `recording_fps`를 clips.csv에 기록합니다. 녹화 경로에 전달된 원본이 분석 생략 때문에 유실되는 것과 CFR 표현 한계를 구분합니다. native codec·해상도 지원 여부는 writer 열림으로 검사합니다.

사전 버퍼 메모리는 대략 너비×높이×3×FPS×pre_seconds 바이트이며 큐·동시 writer 메모리는 별도입니다. recorder_max_mb를 넘으면 무단 프레임 삭제 대신 오류로 종료합니다.

`results/events.csv`의 필수 컬럼:

| 컬럼 | 의미 |
|---|---|
| event_id / zone_name | 실행 내·실행 간 유일한 사건 ID / 구역 |
| start_time | 최초 침입 관측 영상 초 (A가 start_time_s를 제공하면 우선) |
| alert_time | A가 경보를 확정한 영상 초 |
| end_time | 해제 확정으로 이어진 첫 비침입 관측 영상 초 |
| duration_seconds | end_time - start_time; 경보 시작 기준이 아님 |
| video_path | 독립 사건 클립 파일 경로 |
| pipeline_mode | baseline / threaded / optimized |

추가로 run_id, video_name, mock, status, occurred_at(처리 시각 ISO 8601), 입력·경보 perf_counter, 시스템 지연, onset_basis, truncated를 기록합니다. 관측 시각은 실제 촬영 정답을 대신하지 않습니다. 종료까지 해제되지 않은 사건은 end_time/duration을 빈칸으로 두고 open_at_eof/open_at_loop_boundary/failed 등으로 구분합니다. 정상 사건도 영상 후반 클립이 잘릴 수 있어 status와 truncated는 별개입니다.

CSV는 사건당 한 행을 메모리에 유지하고 종료 시 저장합니다. 강제 프로세스 종료·전원 차단까지 내구성을 보장하지 않으며 여러 프로세스가 같은 output_dir에 동시에 쓰지 않도록 각자 경로를 지정하세요.

## A 팀원과 연결하는 인터페이스

A Fork의 `0c6476bd36b6f17f80087ea22ec195edda92fc87` (`Add detection`)에서 루트의 `preprocess.py`, `detect.py`, `zones.py`, `state.py`를 `src/`로 선별 반영했습니다. 공통 조상이 없어 Git merge하지 않았습니다. B·C와 `config.json`의 기존 기본값을 보존하고 `RealBackend`가 다음 실제 함수를 호출합니다.

| 함수 | 입력 → 출력 |
|---|---|
| zones.load_zones | JSON 경로, 원본 (w,h) → [{name, points}] |
| detect.create_background_subtractor | config → 실행별 MOG2 |
| preprocess.preprocess_frame | BGR uint8, 유효 config, ROI/None → 분석 영상, 변환 dict |
| detect.detect_motion | 분석 영상, MOG2, 변환된 최소 면적 → 분석 박스 |
| preprocess.restore_boxes | 분석 박스, 변환 dict → 원본 박스 |
| detect.check_intrusion | 원본 박스, 구역 → 모든 구역의 bool |
| state.update_state | 상태, 침입 dict/None, 원본 index, 영상 초, config → 새 상태, 전이 목록 |

박스는 (x,y,w,h), points는 [[x,y],...], transform은 offset_x/offset_y/scale_x/scale_y이며 scale은 분석/입력 비율입니다. 다각형 침입은 기존 A 계약대로 박스 하단 중앙점과 경계를 포함합니다. 구역 JSON 예:

```json
{"schema_version":1,"frame_size":[1280,720],"zones":[{"name":"위험구역 A","points":[[100,100],[400,100],[400,400]]}]}
```

A는 `{event, zone, frame_index, media_time_s}`를 반환하며 상태 키는 `state`입니다. `RealBackend`가 사건 ID를 발급하고 B의 키로 변환합니다. B로 전달하는 전이는 `{type: "alert"|"cleared", event_id, zone_name, media_time_s}`를 포함해야 합니다. 사건 ID는 alert와 cleared에서 같아야 합니다. 상태 dict의 각 구역은 `status`를 포함하며 IDLE/DETECTING/ALERT/CLEARED를 사용합니다. B는 선택적 `start_time_s`, `end_time_s`를 지원하지만 A에게 새 필드를 강제하지 않습니다. 없으면 B의 관측 메타데이터로 보완합니다. A가 반환하는 기존 `duration_s`는 CSV의 새로운 지속 시간 정의에 사용하지 않습니다.

A의 CLEARED 직후 재침입 고착은 실패 재현 테스트를 먼저 작성한 후 다음 관측에서 새 사건을 시작하도록 최소 수정했습니다. MOG2의 그림자 제거·형태학 연산·하단 중앙점 침입 기준은 보존했습니다. 화면의 OpenCV 기본 폰트는 한글을 지원하지 않아 구역은 Zone 1/2 순서로 표시하며 이름 매핑은 effective_config.json에 보존합니다.

## 설정값

기본값은 검증된 최적값이 아닌 초기 실험 제안입니다.

| 키 | 기본값 | 의미 / 조정 |
|---|---|---|
| source / zones_path | data/samples/test.mp4 / data/zones.json | 입력과 원본 구역 파일 |
| mog2.history / var_threshold / detect_shadows | 500 / 16.0 / true | A가 사용하는 동일 MOG2 기준 |
| min_area / blur_kernel | 200 / 5 | 입력 좌표 면적 / 양의 홀수 커널 |
| canny_low / canny_high | 50 / 150 | 기존 키 보존, MOG2 기본 경로에서는 미사용 |
| consecutive_frames / clear_frames | 5 / 10 | A의 연속 원본 감지 / 해제 기준 |
| pre_seconds / post_seconds | 3.0 / 5.0 | 사전 / 경보 해제 후 녹화 초 |
| analysis_resolution | [640,360] | optimized 분석 최대 크기; 종횡비 처리는 A 계약 |
| resize_enabled / roi_enabled | true / false | optimized 분석 축소 / ROI |
| roi_margin | 32 | 입력 좌표계 ROI 여유 픽셀 |
| frame_interval | 1 | optimized에서 매 N번째 분석; 생략은 None |
| queue_max_size / queue_policy | 8 / block | 제한 큐 크기 / 원본 보존 정책; drop_oldest 거부 |
| measure_fps / warmup_frames | true / 30 | 계측 / 실행 최초 집계 제외 원본 프레임 수 |
| no_display / mock_detection | false / false | GUI 생략 / 명시적 시험 backend |
| recording_enabled / recording_codec | true / mp4v | 영상 저장 / fourcc |
| recorder_max_mb | 1024 | 사전 버퍼 상한; 큐·writer 메모리는 별도 |
| output_dir | results | 결과 루트; Mock은 항상 하위 mock/ |
| input_resolution | null | null이면 원본; suite에서 입력 크기 변경, 녹화는 원본 유지 |
| experiment_kind | throughput | 최대 처리량 또는 realtime 재생 |
| timestamp_mode | cfr | 파일 index/FPS; VFR은 pts를 명시 |
| camera_fps | 30.0 | 카메라 nominal FPS를 못 읽었을 때 CFR 출력 기준 |
| duration_seconds / loop_video | 0.0 / false | 측정 벽시계 제한(0=EOF) / EOF 재생 반복 |
| thread_join_timeout | 5.0 | 종료 대기 상한; 블로킹 카메라 read는 장치별 한계 |

테스트에서만 `mock_events=[{zone_name, start, alert, end}]` 시간표를 설정할 수 있습니다. 기존 Mock 테스트와 실제 MOG2 합성 영상 통합 테스트는 구분합니다. 실제 촬영 정확도 검사는 별도입니다.

## Benchmark 실행과 측정 정의

영상 출처·촬영일·권한은 data/SOURCES.md에 작성합니다. 비침입, 짧은 침입, 장시간 침입, 경계, 다중 구역, 조명 변화, EOF 직전 사건을 촬영하고 정답을 작성하세요. 실제 영상은 현재 포함되어 있지 않습니다.

공식 실험은 **640×480, 1280×720, 1920×1080 × 세 모드 × 3회**, 조건당 warm-up 이후 **벽시계 60초 이상**입니다. 같은 파일을 사용하며 반복 순서는 교차합니다.

```bash
python -m src.benchmark --source data/samples/test.mp4 --no-display --seconds 60 --repeats 3 --experiment-kind throughput
python -m src.benchmark --source data/samples/test.mp4 --no-display --seconds 60 --repeats 3 --experiment-kind realtime
python -m src.benchmark --source data/samples/test.mp4 --no-display --ablations
```

`--ablations`는 optimized의 none/ROI만/축소만/간격만/큐 크기만 조건을 추가합니다. standard optimized는 config의 조합입니다. 드롭 실험은 비활성 상태입니다. 실제 `--ablations`는 간격 2 조건이 포함되므로 실행 전에 거부합니다. 실제 비교는 standard 세 모드로 실행하세요. 자동화 연결만 확인하려면 `--mock-detection`을 추가하되 결과는 실제 성능으로 사용하지 않습니다. 이번 작업에서 공식 60초×3회 측정은 수행하지 않았습니다.

짧은 파일은 실제 파일을 다시 읽습니다. 경계에서 MOG2·상태를 초기화하고 기존 클립은 truncated로 종료합니다. 시각은 누적 offset으로 단조 증가합니다. **warm-up 제외는 실행 최초에만 적용**하며 이후 반복 경계의 재학습·초기화 비용은 측정에 포함합니다. 경계 초기화로 인한 오경보·사건 절단은 실제 연속 촬영 성능과 다르므로 정확도 평가는 반복 없는 단일 영상 실행으로 합니다.

- `throughput`: 파일을 가능한 빨리 처리합니다. 원본 FPS와 처리량 FPS는 다릅니다.
- `realtime`: perf_counter 예정 입력 시각에 맞춰 파일 프레임을 투입합니다. 느린 처리로 일정이 밀려도 과거 프레임을 버리지 않습니다. 카메라는 항상 실제 취득 시각을 사용합니다.
- `read_ms`: 디코드 및 실험 입력 리사이즈, 의도한 재생 대기는 제외합니다.
- `queue_wait_ms`: 준비된 프레임의 큐 backpressure·대기 시간.
- `preprocess/detect/intrusion/state/record/display_ms`: 각 단계의 perf_counter 차이. 미실행 단계는 빈칸입니다.
- `total_ms`: read + queue_wait + 메인 처리 시간. 멀티스레드 단계가 겹치므로 표본 합을 전체 벽시계 시간으로 해석하지 않습니다.
- `throughput_fps`: warm-up 이후 분석 완료 수 / 측정 시작~마지막 완료 경과 초. `mean/median/std/p95_fps`는 연속 분석 완료 간격의 역수 표본 통계입니다. 표준편차는 ddof=0입니다.
- 각 단계와 지연도 평균·중앙값·표준편차·95백분위수를 기록합니다. 분모/표본이 없으면 빈칸/NaN이며 임의로 0을 넣지 않습니다.
- `alert_delay_s`: 최초 침입 **관측**부터 경보까지 영상 초. 실제 정답 기준 지연은 별도 사건 매칭 결과입니다.
- `system_alert_delay_ms`: 해당 프레임의 실제 투입부터 경보까지. `scheduled_alert_delay_ms`: 실시간 파일의 예정 입력부터 경보까지로 backpressure 지연도 포함합니다.
- 동시 여러 경보가 있는 프레임의 raw 지연은 그 프레임 경보의 평균입니다. 사건별 정확한 값은 events.csv를 사용합니다.

입력·표시·저장 조건은 동일하게 고정하고 CPU/OS/OpenCV·원본 해시·config를 보관하세요. raw 표본은 메모리에 모아 종료 후 저장하므로 긴 실행에서는 계측 메모리도 증가합니다. CSV·그래프 저장 시간은 처리 FPS에서 제외합니다.

## 결과 저장 위치와 그래프

일반 실제 실행은 `results/`, Mock 실행은 `results/mock/`가 결과 루트입니다.

| 파일 | 내용 |
|---|---|
| events.csv | 누적 사건별 기록, 중복 없는 실행 ID |
| benchmark_raw.csv | 종료 시 일괄 저장한 프레임 원자료 |
| benchmark_summary.csv | 실행 조건별 FPS·단계·지연 통계 |
| runs/<run_id>/events.csv | 해당 실행만의 사건표 |
| runs/<run_id>/clips/*.mp4 | 원본 해상도의 독립 사건 클립 |
| runs/<run_id>/clips.csv | 실제 클립 범위·truncated·재표본화 정보 |
| runs/<run_id>/benchmark_*.csv | 해당 실행의 원자료와 요약 |
| runs/<run_id>/effective_config.json | 유효 설정, 구역, 패키지·OS, 원본 해시 |
| runs/<run_id>/status.json | 성공/실패, 오류, 입력·처리·종료 대기 프레임 수 |

공식 suite는 서로 다른 실험을 섞지 않도록 `results/[mock/]throughput 또는 realtime/suite-<id>/`에 같은 파일명을 저장합니다. 종료 후 다음 세 그래프를 만듭니다.

- `fps_comparison.png`: 해상도별 baseline/threaded/optimized standard의 반복 평균 처리량.
- `stage_latency.png`: 해상도·모드별 standard 단계 평균.
- `optimization_comparison.png`: optimized variant별 처리량.

데이터가 없거나 완료한 실행이 없으면 그래프를 생성하지 않고 메시지를 출력합니다. 기존 파일은 자동 삭제하지 않으므로 새 출력 폴더를 사용하세요. Mock 그래프에는 MOCK 표기를 넣습니다. 서로 다른 source·실험 종류·Mock/실제 혼합은 거부합니다. 직접 모은 일반 실행도 출력 조건·config가 동일한지 확인한 뒤 사용하세요.

```bash
python -m src.benchmark --plot-only results/benchmark_summary.csv --output-dir results
```

## 감지 정확도 검증 준비

[정답 템플릿](docs/ground_truth_template.csv)을 복사해 `video_name,zone_name,start_time,end_time,event_type`을 작성합니다. 시각은 파일 시작 기준 초, event_type은 `intrusion` 또는 `normal`입니다. 아직 실제 행은 없습니다.

`src.benchmark.evaluate_events(truth_rows, event_rows, video_name=..., tolerance_s=0.0)`는 단일 실행 결과를 구역·시간 구간으로 일대일 최대 매칭합니다. 기본 허용 오차는 **0초**이며 변경 시 기록합니다. 한 정답에 여러 경보가 있어도 정상 감지는 한 번입니다. 나머지 중복 경보는 오경보에 포함합니다.

- 실제 사건 수 = 정답 intrusion 행 수.
- 정상 감지 = 일대일 매칭 수, 미검출 = 정답 intrusion - 매칭.
- 오경보 = 전체 경보 - 매칭.
- **오경보율 = 오경보 수 / 전체 경보 수**(false discovery ratio). 정상 프레임 기반 FPR과 다릅니다.
- **미검출률 = 미검출 수 / 실제 intrusion 사건 수**.
- 평균 경고 지연 = 매칭된 경보 alert_time - 정답 start_time.

분모가 0이면 NaN입니다. normal 구간은 검토 주석이며 TN 개수를 임의로 만들지 않습니다. Mock·여러 run_id·미종료 사건은 평가를 거부합니다. 반복 재생 결과의 전역 시간과 단일 영상 정답을 직접 비교하지 마세요. 정확도 평가는 warm-up 구간 포함 여부·수동 라벨 오차를 팀에서 먼저 확정합니다.

## 현재 검증과 한계, 다음 작업

B·C 기능은 pytest의 작은 합성 프레임·임시 영상으로 검증합니다. 합성 자료는 테스트 임시 폴더에만 생성하며 실제 촬영·성능 결과로 기록하지 않습니다. 실제 영상·GUI·카메라·A 구현과의 종단 간 성능은 미검증입니다.

- A: 통합한 실제 구현을 촬영 영상에서 검토하고 경계·다중 구역·초기 배경 학습·정지 객체를 확인합니다. 실제 분석 생략은 이번 통합에서 허용하지 않습니다.
- B: 실제 장치의 FPS·PTS·codec·GUI를 확인하고 독립 녹화 보존 경로를 만든 뒤 드롭 정책을 검토합니다.
- C: 실제 영상·정답 확보 후 단일 영상 정확도 평가 → 60초×3회 공식 실험 → 병목·지연·정확도 분석을 수행합니다.
- 공통: 긴 녹화의 메모리·디스크 사용량, 강제 종료 시 데이터 보존, 카메라 read가 종료 신호를 무시하는 장치의 격리 방식을 검토합니다. 현재 join timeout은 오류를 보고하지만 장치 내부 블로킹을 강제로 해제하지 못합니다.

## 스크린샷 및 보고서

TODO: 실제 구역 편집 화면, 경고·녹화 화면, 클립 확인, 실제 FPS·단계·최적화 그래프를 삽입합니다. 결과 수치와 개선율은 미측정 상태이며 [실험기록](docs/실험기록.md), [최종보고서](docs/최종보고서.md)에 실제 실행 후 작성합니다. 배포할 이미지는 검토 후 docs/images/에 별도로 추가합니다.

## GitHub 협업 방법

원본 저장소: [whrjsdnr/opencv_teamproject](https://github.com/whrjsdnr/opencv_teamproject). 팀원은 각자 Fork하고 개인 기능 브랜치에서 작업합니다.

```bash
git clone https://github.com/YOUR_GITHUB_ID/opencv_teamproject.git
cd opencv_teamproject
git remote add upstream https://github.com/whrjsdnr/opencv_teamproject.git
git fetch upstream
git switch -c feature/detection upstream/main
```

A는 feature/detection, B는 feature/pipeline, C는 feature/benchmark를 사용합니다. 이번 B·C 통합 구현은 `feature/pipeline-benchmark`에서 작업합니다. 원본 main 직접 Push 대신 PR·리뷰를 거칩니다. 공통 파일 및 함수 계약 변경은 관련 팀원에게 공유하세요. 자세한 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.

### 통합 이전 B·C 구현 검증 기록

Python 3.10.21, OpenCV contrib 4.10.0.84 (`cv2.__version__=4.10.0`), NumPy 1.26.4, Pandas 2.2.2, Matplotlib 3.8.4, pytest 9.1.1의 격리 `.venv`에서 검증했습니다. `pip check`는 정상입니다. Matplotlib 3.8.4와 설치된 pyparsing 조합의 deprecation 경고는 테스트 실패가 아닙니다. 통합 이전 pytest 결과는 33 passed였으며, 문법 검사·CLI dry-run·빈 데이터 그래프 처리·A 및 utils 무변경도 확인했습니다. 실제 영상·카메라·GUI·공식 성능 실험은 미검증입니다.


## 감지 통합 검증

통합 브랜치는 `feature/detection-integration`이며 기반 main은 `f4240a7624ad1d4632abe7cb7656e3b82ce7ba9b`입니다.
한국어 주석과 docstring에 원본/입력/분석 좌표, 사건 키 변환, 스레드 시작 순서를 설명했습니다.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m src.main --source YOUR_VIDEO.mp4 --zones data/zones.json --mode baseline
.venv/bin/python -m src.main --source YOUR_VIDEO.mp4 --zones data/zones.json --mode threaded --no-display
.venv/bin/python -m src.main --source YOUR_VIDEO.mp4 --zones data/zones.json --mode optimized --no-display
```

통합 후 `.venv/bin/python -m pytest -q` 결과는 **47 passed, 13 warnings**입니다. 경고는 기존 Matplotlib/pyparsing 사용 중단 예정 API에서 발생했습니다. `git diff --check`, Python 문법 검사, CLI dry-run도 통과했습니다.

신규 테스트는 JSON 저장·불러오기·잘못된 다각형 거부, 표시 좌표와 ROI 축척 복원,
기본 5프레임 경보, 해제 직후 재침입, 편집기 키/마우스 콜백, 첫 프레임 유실 방지,
세 모드의 실제 MOG2→경고 상태→사건 CSV→자동 녹화를 검사합니다.
합성 영상에서 경보 전 3초, 해제 확정 후 5초와 장시간 경보 중 녹화 연장을 확인하고
출력 파일을 재디코딩하여 원본 해상도·프레임 수를 확인합니다.
CSV의 end_time은 해제 확정으로 이어진 첫 비침입 관측 시각이며, 녹화는 해제 **확정** 시각부터 5초입니다.
녹화에는 기존 B의 원본 보존 정책에 따라 경고/박스 오버레이를 굽지 않습니다. 화면에는 복원된 박스와 구역을 표시합니다.

실제 촬영 영상·카메라·물리 GUI 조작·HiDPI 창 관리자·공식 성능/정확도 측정은 미실행입니다.
GUI 자동 검사는 창 호출을 대체하여 실행 흐름을 검사한 것이므로 실제 마우스 수동 검증을 대신하지 않습니다.
MOG2는 움직임 기반이므로 정지한 객체의 배경 흡수와 초기 배경 학습 오검출은 실제 영상에서 확인해야 합니다.
초기 warmup_frames는 기존대로 계측 제외용이며 감지 억제 기준을 새로 추가하지 않았습니다.
