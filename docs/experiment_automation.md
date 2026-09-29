# 최종 실험 자동화

## 조사한 구현

- `src.main`: `--source`, `--config`, `--mode baseline|threaded|optimized`, `--no-display`, `--output-dir`, `--zones`, `--duration`, `--loop-video`, `--experiment-kind throughput|realtime`, `--queue-policy`, `--mock-detection`, `--preview`, `--dry-run`.
- `pipeline.FrameSource`: 파일 또는 카메라 입력. 파일 기본 시간은 원본 인덱스/FPS(CFR), 선택적으로 PTS. baseline은 순차 read, 나머지는 입력 producer와 bounded Queue. 실제 감지는 `frame_interval=1`, `queue_policy=block`만 허용.
- `RealBackend`는 모든 모드에서 동일한 preprocess/detect/state를 호출한다. baseline/threaded는 ROI와 resize를 비활성화한다. optimized는 config의 ROI·resize를 적용한다. 현재 ROI=false, 분석 상한 640×360, 영상 384×288이므로 실제 축소 효과가 없다.
- `zones.load_zones`: schema_version=1, frame_size 일치, 유효한 단순 다각형 검증. 각 JSON에 실제 구역명 `zone1` 하나가 있다.
- 감지: BGR ROI/resize → GaussianBlur → MOG2 → threshold 200(그림자 제외) → 3×3 opening/closing → contour 면적 → 박스 하단 중앙의 pointPolygonTest. 기본 감지 5프레임, 해제 10프레임 연속.
- `EventLedger`: alert가 발생해야 사건 행이 생성된다. start는 첫 양성 관측, end는 확정 해제로 이어진 첫 음성 관측, alert_time은 ALERT 전이 시각. 끝나지 않은 사건은 `open_at_eof`이며 종료 시각을 만들지 않는다. 따라서 이 평가는 모든 순간적 intrusion 프레임이 아닌 **경보 사건** 평가다.
- 기존 events.csv: `run_id,video_name,mock,event_id,zone_name,start_time,alert_time,end_time,duration_seconds,video_path,pipeline_mode,status,occurred_at,alert_perf_counter,input_perf_counter,system_alert_delay_ms,scheduled_alert_delay_ms,onset_basis,truncated`.
- `recorder`: 원본 시간 버퍼, 구역별 사건 writer, 고정 FPS 재표본화. 기본 pre=3초/post=5초, 경보 이전 원본과 해제 확정 이후 영상을 포함하며 EOF에서 부족한 후속 영상을 합성하지 않는다. clips.csv에 pre/post 절단과 실제 프레임 수를 남긴다. 반복 경보가 하나의 클립에 연결될 수 있어 사건 수와 클립 수를 구별한다.
- 기존 계측: read/queue_wait/preprocess/detect/intrusion/state/record/display/total ms, raw/summary CSV와 환경 JSON. `throughput_fps=warmup 제외 analyzed_frames/측정 elapsed`. 순간 완료 간격 역수 평균인 mean_fps 및 화면 FPS와 다르다. CSV 저장과 writer 최종 해제는 측정 종료 후다.
- 기존 `src.benchmark`는 60초 이상×3회 이상 공식 suite와 최대 개수 1:1 overlap 평가를 제공한다. 자동화는 전체 GT 시간축 평가를 위해 CLI로 반복 재생 없이 EOF까지 실행하고 기존 raw/summary 및 매칭 구현을 재사용한다. 기본 1회는 기술적 비교이며 반복 신뢰성 증명이 아니다.
- 기존 테스트는 detection integration, pipeline, recorder, benchmark를 검증한다. `tests/test_experiments.py`는 새 어댑터와 평가/집계를 검증한다.

## 실행

프로젝트 루트에서 기존 가상환경을 활성화한 뒤:

```bash
source .venv/bin/activate
python scripts/run_experiments.py
python scripts/run_experiments.py --mode baseline
python scripts/run_experiments.py --repeats 3
```

`--mode all|baseline|threaded|optimized`, `--config`, `--output-dir`, `--tolerance-s`도 지원한다. 작업 디렉터리와 관계없이 config 안 상대 경로는 저장소 루트를 기준으로 해석한다. 기본 timeout은 실행당 600초이며 실패한 실행은 FAILED로 기록하고 계속한다. 프로세스 실패가 있으면 최종 exit=1, 감지 실행이 완료되고 GT만 평가 보류면 exit=0이며 evaluation_status=N/A다.

## GT 설정과 해석

`scripts/experiments.json`에 영상별 zone 경로, GT CSV, 라벨 매핑, 반복 수를 모았다. GT 원본의 경로는 `/data/...` 또는 이전 사건 폴더이지만 실제 영상은 `data/samples`에 있으므로 고유 basename으로 연결한다. zone_name 열의 JSON 경로는 해당 파일에 구역이 하나일 때만 `zone1`으로 정규화한다. GT 원본은 수정하지 않는다.

- `침입 → intrusion`, `none → normal`은 명시적으로 매핑했다. `none,none,none` 행은 영상 전체 무사건 표시다.
- `오래 머물기`, `스쳐 지나감`, `구역 나감`, `근처에 머무름`은 의미 확인 전 null이다. 하나라도 미확정이면 해당 영상의 정확도 전체를 N/A 처리한다. 확인 후 event_types에서 `intrusion` 또는 `normal`로 설정한다.
- LeftBox의 겹치는 3개 침입은 `merge_zone_intervals` 확인 전 평가 보류다. 구역별 침입 연속 사건이면 true로 합집합을 취하고, 개별 정답 사건이면 false로 유지한다. false인 경우 객체를 추적하지 않는 구역별 detector 특성상 미검출 수가 늘어날 수 있다.
- `truth_complete=true`는 이 영상의 GT가 전체 관측 구간을 주석 처리했다는 데이터 계약이다. 일부 사건만 주석 처리했다면 false로 바꿔야 한다. 자동 탐색한 추가 영상은 zone 미지정으로 FAILED 처리하며, 임의 구역을 적용하지 않는다.
- 같은 영상·구역의 닫힌 구간 overlap(끝점 접촉 포함), 허용오차 기본 0초, 최대 cardinality 1:1 매칭. 허용오차는 GT 구간 양쪽 확장으로 적용한다. 동률은 CSV 순서로 결정되며 최소 지연/최대 IoU 매칭은 아니다.
- EOF 미종료 Detection은 실제 끝을 알 수 없어 해당 영상 정확도를 N/A 처리한다. 일부 closed 사건만 평가하고 나머지를 숨기지 않는다. 사건과 녹화 결과 자체는 보존한다.
- Precision=TP/(TP+FP), Recall=TP/(TP+FN), F1=2TP/(2TP+FP+FN). 분모 0이면 N/A. FP count와 false discovery fraction을 제공하며 TN 기반 FPR은 계산하지 않는다.
- alert delay는 매칭된 alert_time−GT start_time, 영상 초 단위이며 음수도 유지한다. 처리 시스템 지연과 다르다.

## 출력

재실행 결과 혼합 및 과거 파일 덮어쓰기를 막기 위해 배치 한 단계가 추가된다.

```text
outputs/experiments/
  latest.json
  batch-<UTC>-<id>/
    manifest.json
    baseline|threaded|optimized/
      <video>-r<repeat>/
        requested_config.json, command.json, run.log
        events.csv, benchmark_raw.csv, benchmark_summary.csv
        runs/<run_id>/
          clips/*.mp4
          events.csv, clips.csv
          benchmark_raw.csv, benchmark_summary.csv
          effective_config.json, status.json
    summary/
      experiment_summary.csv, mode_summary.csv
      events.csv, event_matches.csv
      ground_truth_original.csv, ground_truth_audit.csv, ground_truth_normalized.csv
      false_alarm_analysis.csv, detection_comparison.csv, event_consistency.csv
      fps_comparison.csv, alert_delay.csv, stage_timings.csv
      optimization_checklist.md, final_performance_report.md
      figures/*.png
```

원래 schema와 녹화 경로를 유지하여 clips 링크가 깨지지 않는다. events.csv는 Ground Truth가 아닌 실제 경보 결과만 통합한다. summary는 가장 최근 실행 배치만 사용한다. 실패 실행의 부분 자료는 해당 실행 폴더에 남기고 성능 평균에 넣지 않는다.

평균 FPS는 선택한 모든 모드에서 성공한 공통 영상/반복 집합의 산술평균이고 pooled_fps는 합산 프레임/시간이다. 정확도는 모든 모드에서 평가 가능한 공통 집합의 micro 집계다. 전체 데이터셋 정확도와 혼동하지 않도록 평가 실행 수를 함께 표시한다. stage별 평균은 기존 계측을 재사용하며 MOG2·morphology 개별 비용, GUI, 종료 CSV I/O 병목은 미측정으로 명시한다.

그래프는 유효 데이터가 있을 때만 FPS 평균/영상별, Precision, Recall, F1, alert delay를 생성한다. 정확한 사건 시각의 모드 간 동일성은 `event_consistency.csv`에서 GT 평가 가능 여부와 별개로 확인한다. 이는 정답 정확도의 증명이 아니다.
