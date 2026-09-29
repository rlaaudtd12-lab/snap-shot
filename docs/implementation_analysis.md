# 실제 코드 기반 구현 분석

Phase 2 기준: EOF 평가 adapter 수정 및 전체 pytest 79개 통과 후 분석. README의 선언이 아닌 실제 함수·설정·테스트 기준이다. GT와 기존 측정 CSV는 보존한다.

| 기능 | 상태 | 관련 파일 | Class/Function | 동작 | 필요한 이유 | 검증 방법 |
|---|---|---|---|---|---|---|
| 마우스 Polygon | IMPLEMENTED | src/zones.py | edit_zones / save_zones / load_zones | 클릭·확정·취소·원본 좌표 검증 | 위험 구역 정의와 재현 | test_editor_keys_without_real_gui / test_json_and_display_coordinates |
| Polygon JSON 저장/불러오기 | IMPLEMENTED | src/zones.py | edit_zones / save_zones / load_zones | 클릭·확정·취소·원본 좌표 검증 | 위험 구역 정의와 재현 | test_editor_keys_without_real_gui / test_json_and_display_coordinates |
| 움직임 검출 | IMPLEMENTED | src/detect.py | create_background_subtractor / detect_motion | MOG2, 그림자 제외, morphology, contour | 배경 대비 움직임 추출 | test_real_three_modes_csv_and_recording |
| 위험구역 내부 판정 | IMPLEMENTED | src/detect.py | check_intrusion | 박스 하단 중앙점 pointPolygonTest, 경계 포함 | 픽셀 검출을 구역 침입으로 연결 | test_common_a_adapter_roi_coordinates |
| N-frame 연속 감지 | IMPLEMENTED | src/state.py | update_state | IDLE → DETECTING → ALERT → CLEARED; 기본 5/10 frame | 순간 노이즈 억제 | test_reentry_immediately_after_cleared / test_backend_default_threshold_and_warning |
| 상태 전환 | IMPLEMENTED | src/state.py | update_state | IDLE → DETECTING → ALERT → CLEARED; 기본 5/10 frame | 순간 노이즈 억제 | test_reentry_immediately_after_cleared / test_backend_default_threshold_and_warning |
| 현재 상태 Overlay | IMPLEMENTED | src/pipeline.py | _draw | 상태/FPS/REC 및 WARNING: INTRUSION | 운전자 상황 인지 | test_backend_default_threshold_and_warning |
| 빨간 경보 테두리 | IMPLEMENTED | src/pipeline.py | _draw | 상태/FPS/REC 및 WARNING: INTRUSION | 운전자 상황 인지 | test_backend_default_threshold_and_warning |
| 경고 문구 | IMPLEMENTED | src/pipeline.py | _draw | 상태/FPS/REC 및 WARNING: INTRUSION | 운전자 상황 인지 | test_backend_default_threshold_and_warning |
| 사건 영상 자동 저장 | IMPLEMENTED | src/recorder.py | create_recorder / buffer_frame / record_frame / close_recorder | deque 원본 버퍼, 독립 writer; alert 기준 pre, clear 기준 post; EOF 절단 표시 | 사건 전후 증거 보존 | test_three_second_buffer / test_event_file_and_long_extension |
| 사건 이전 3초 | IMPLEMENTED | src/recorder.py | create_recorder / buffer_frame / record_frame / close_recorder | deque 원본 버퍼, 독립 writer; alert 기준 pre, clear 기준 post; EOF 절단 표시 | 사건 전후 증거 보존 | test_three_second_buffer / test_event_file_and_long_extension |
| 사건 종료 이후 5초 | IMPLEMENTED | src/recorder.py | create_recorder / buffer_frame / record_frame / close_recorder | deque 원본 버퍼, 독립 writer; alert 기준 pre, clear 기준 post; EOF 절단 표시 | 사건 전후 증거 보존 | test_three_second_buffer / test_event_file_and_long_extension |
| 사건 CSV | IMPLEMENTED | src/pipeline.py | EventLedger.transitions / EventLedger.finish | 사건 식별자·영상 시각·종료·상태 저장; open 종료는 공란 | 사건 추적 및 평가 | test_ledger_concurrent_dedup / test_real_three_modes_csv_and_recording |
| 사건 시각 | IMPLEMENTED | src/pipeline.py | EventLedger.transitions / EventLedger.finish | 사건 식별자·영상 시각·종료·상태 저장; open 종료는 공란 | 사건 추적 및 평가 | test_ledger_concurrent_dedup / test_real_three_modes_csv_and_recording |
| 지속시간 | IMPLEMENTED | src/pipeline.py | EventLedger.transitions / EventLedger.finish | 사건 식별자·영상 시각·종료·상태 저장; open 종료는 공란 | 사건 추적 및 평가 | test_ledger_concurrent_dedup / test_real_three_modes_csv_and_recording |
| Zone 이름 | IMPLEMENTED | src/pipeline.py | EventLedger.transitions / EventLedger.finish | 사건 식별자·영상 시각·종료·상태 저장; open 종료는 공란 | 사건 추적 및 평가 | test_ledger_concurrent_dedup / test_real_three_modes_csv_and_recording |
| Optical Flow 방향/속도 | NOT IMPLEMENTED | — | — | 관련 실행 경로 없음 | 속도/위험도 확장 | 미검증; 향후 과제 |
| 빠른 접근 경고 | NOT IMPLEMENTED | — | — | 관련 실행 경로 없음 | 속도/위험도 확장 | 미검증; 향후 과제 |
| 주의/금지 단계 | NOT IMPLEMENTED | — | — | 관련 실행 경로 없음 | 속도/위험도 확장 | 미검증; 향후 과제 |
| 여러 Polygon Zone | IMPLEMENTED | src/zones.py; src/pipeline.py | edit_zones / RealBackend.update | 여러 구역 독립 상태·사건 | 다수 구역 감시 | test_ledger_concurrent_dedup / test_independent_duplicate_events_and_close |
| 침입 Heatmap | NOT IMPLEMENTED | — | — | 실제 pipeline 옵션 없음 | 분포 분석 / 저조도 대응 | 미검증; 최종 GT 영상만으로 효과 검증 불충분 |
| CLAHE | NOT IMPLEMENTED | — | — | 실제 pipeline 옵션 없음 | 분포 분석 / 저조도 대응 | 미검증; 최종 GT 영상만으로 효과 검증 불충분 |
| Gamma Correction | NOT IMPLEMENTED | — | — | 실제 pipeline 옵션 없음 | 분포 분석 / 저조도 대응 | 미검증; 최종 GT 영상만으로 효과 검증 불충분 |
| Night Mode | NOT IMPLEMENTED | — | — | 실제 pipeline 옵션 없음 | 분포 분석 / 저조도 대응 | 미검증; 최종 GT 영상만으로 효과 검증 불충분 |
| Resize | IMPLEMENTED | src/preprocess.py | preprocess_frame | 축소·GaussianBlur | 분석 비용과 노이즈 감소 | test_roi_rounding_and_mog2_config |
| Blur | IMPLEMENTED | src/preprocess.py | preprocess_frame | 축소·GaussianBlur | 분석 비용과 노이즈 감소 | test_roi_rounding_and_mog2_config |
| Detection | IMPLEMENTED | src/detect.py | detect_motion | MOG2 → threshold → open/close → findContours | 움직임 영역 추출 | test_real_three_modes_csv_and_recording |
| Morphology | IMPLEMENTED | src/detect.py | detect_motion | MOG2 → threshold → open/close → findContours | 움직임 영역 추출 | test_real_three_modes_csv_and_recording |
| Contour | IMPLEMENTED | src/detect.py | detect_motion | MOG2 → threshold → open/close → findContours | 움직임 영역 추출 | test_real_three_modes_csv_and_recording |
| Overlay | IMPLEMENTED | src/pipeline.py | _draw | 원본 복사 후 도형·글자 | 원본 녹화와 UI 분리 | test_backend_default_threshold_and_warning |
| baseline | IMPLEMENTED | src/pipeline.py | run_pipeline / FrameSource | 순차 / 입력 생산자 / bounded Queue / ROI·축소 | 입출력 중첩과 연산 축소 | test_modes_cli / test_real_three_modes_csv_and_recording |
| threaded | IMPLEMENTED | src/pipeline.py | run_pipeline / FrameSource | 순차 / 입력 생산자 / bounded Queue / ROI·축소 | 입출력 중첩과 연산 축소 | test_modes_cli / test_real_three_modes_csv_and_recording |
| Queue | IMPLEMENTED | src/pipeline.py | run_pipeline / FrameSource | 순차 / 입력 생산자 / bounded Queue / ROI·축소 | 입출력 중첩과 연산 축소 | test_modes_cli / test_real_three_modes_csv_and_recording |
| optimized | IMPLEMENTED | src/pipeline.py | run_pipeline / FrameSource | 순차 / 입력 생산자 / bounded Queue / ROI·축소 | 입출력 중첩과 연산 축소 | test_modes_cli / test_real_three_modes_csv_and_recording |
| 단계별 처리시간 | IMPLEMENTED | src/benchmark.py; scripts/run_experiments.py | measure / summarize_metrics / report | 단계 평균·p95 및 순위 | 실측 기반 개선 | test_measure_on_exception / test_fps_and_stats |
| 병목 분석 | IMPLEMENTED | src/benchmark.py; scripts/run_experiments.py | measure / summarize_metrics / report | 단계 평균·p95 및 순위 | 실측 기반 개선 | test_measure_on_exception / test_fps_and_stats |
| 화면 병목 단계 표시 | NOT IMPLEMENTED | src/pipeline.py | _draw | FPS만 표시; 단계 병목은 CSV/보고서 | 실시간 진단 확장 | 화면 문자열 코드 확인 |
| 해상도별 FPS | PARTIAL | src/benchmark.py | run_suite | 3 input resolution × 3 mode 지원; 실제 GT 포함 비교 미완료 | 입력 크기 영향 분리 | test_suite_matrix; 최종 비교 자동화 추가 예정 |
| mode별 FPS | IMPLEMENTED | src/benchmark.py; scripts/run_experiments.py | generate_plots / plots / mode_summary | 실측 CSV에서 그래프 생성 | 결과 비교 | test_plot_measured_mock_data; 실제 실험 별도 수행 |
| matplotlib 그래프 | IMPLEMENTED | src/benchmark.py; scripts/run_experiments.py | generate_plots / plots / mode_summary | 실측 CSV에서 그래프 생성 | 결과 비교 | test_plot_measured_mock_data; 실제 실험 별도 수행 |
| Downscale + 좌표 복원 | IMPLEMENTED | src/preprocess.py; src/pipeline.py | preprocess_frame / restore_boxes / _analysis | optimized 전용, 최소 면적도 scale 보정 | 감지 기준 유지하며 연산 감소 | test_roi_rounding_and_mog2_config / test_common_a_adapter_roi_coordinates |
| ROI processing | IMPLEMENTED | src/preprocess.py; src/pipeline.py | preprocess_frame / restore_boxes / _analysis | optimized 전용, 최소 면적도 scale 보정 | 감지 기준 유지하며 연산 감소 | test_roi_rounding_and_mog2_config / test_common_a_adapter_roi_coordinates |
| NumPy vectorization | PARTIAL | src/pipeline.py; src/benchmark.py | _analysis / _stats | ROI min/max 및 통계 벡터화; zone/box 반복은 Python | 다점/다표본 집계 | 독립 OFF/ON 실험 없음 |
| Heavy operation frame skipping | EXPERIMENTAL / CONFIGURED | src/pipeline.py | validate_config / run_pipeline | Mock만 허용; 실제 감지 interval=1 강제 | 연속 원본 프레임 의미 보존 | test_optimized_skip_preserves_recording / test_real_ablation_rejected_before_work |
| Intermediate result reuse | PARTIAL | src/pipeline.py | FrameSource._packets / RealBackend.reset | 동일 크기 원본 참조, MOG2 인스턴스 재사용; 검출결과 frame reuse 없음 | 불필요한 생성 억제 | 코드 확인; 독립 성능 측정 없음 |
| cv2.UMat / OpenCL | NOT IMPLEMENTED | — | — | 현재 경로 없음; OpenCL haveOpenCL=False | 환경별 가속 | 실행 환경 확인; OpenCL N/A |
| cv2.setNumThreads | NOT IMPLEMENTED | — | — | 현재 경로 없음; OpenCL haveOpenCL=False | 환경별 가속 | 실행 환경 확인; OpenCL N/A |
| bounded queue | IMPLEMENTED | src/pipeline.py | FrameSource.__init__ / FrameSource._produce | maxsize=8 기본, block backpressure | 메모리 증가 제한 | test_full_queue_block_and_shutdown |
| old frame dropping | NOT IMPLEMENTED | src/pipeline.py | validate_config | drop_oldest 명시 거부; 무손실 녹화 경로 없음 | 실시간 최신성; 기존 녹화 보존과 충돌 | test_drop_policy_rejected |
| latency accumulation 방지 | PARTIAL | src/pipeline.py | FrameSource._produce | bounded queue만 보장; 카메라 backend 지연 상한 미보장 | 실시간 응답 | test_realtime_schedule; 실제 과부하 카메라 미검증 |

## Phase 3 구현 범위

핵심 감지·녹화는 이미 동작한다. 기존 pipeline 및 감지 알고리즘 변경 금지 조건을 유지하며 실험 adapter에 해상도 3×3 및 최소 4개 최적화 비교 orchestration, source EOF 평가, 원본 무결성 검증을 추가한다. 입력 스레드, ROI, downscale, queue 용량을 실제 비교한다. OpenCV thread 수는 실험 subprocess 시작 시에만 설정하여 비교한다.

실제 감지 frame skipping/drop_oldest를 허용하려면 연속 감지 의미 및 무손실 녹화 경로를 변경해야 하므로 이번 범위에서 구현하지 않는다. 도전 기능도 검증할 저조도·속도 GT가 없어 추가하지 않는다. 화면 병목은 기존 UI 변경 금지 범위에 해당하여 CSV/그래프 병목 분석을 사용한다. 최종 구현 상태와 실측은 최종 보고서에 반영한다.


## 최종 반영 확인

- 해상도별 FPS: **IMPLEMENTED**. `scripts/final_studies.py:study_cases / main`이 실제 input_resolution 3종×mode 3종을 Browse/Danger에서 각 2회 측정했다. 분석/녹화 파이프라인은 기존 것을 재사용한다.
- cv2.setNumThreads: **EXPERIMENTAL / CONFIGURED**. `scripts/experiment_worker.py:run`이 비교 subprocess에만 적용하며 실제 OFF/ON 성능·정확도는 `outputs/final_report/tables/optimization_comparison.csv`에 있다.
- 평가·재평가: `evaluation_intervals / video_metadata / evaluate`, `scripts/reevaluate_experiments.py:main`으로 원본 사건 보존과 EOF 평가를 확인했다.
- 최종 검증: 전체 pytest 84개, Ruff correctness/format 통과. 기존 감지·상태·녹화 구현의 AST 동일 및 기존 GT/events 파일 173개 해시 동일.
- 미구현/부분 구현 상태는 위 분석대로 남는다. 특히 drop_oldest, 실제 frame skipping, 도전 기능을 완성했다고 주장하지 않는다.
