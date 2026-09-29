# 팀 협업 안내

원본 저장소: https://github.com/whrjsdnr/opencv_teamproject

최초 프로젝트 등록 이후에는 main에 직접 Push하지 않고 개인 Fork의 기능 브랜치에서 원본 main으로 Pull Request(PR)를 생성합니다. 각 팀원이 자신의 GitHub 계정으로 Fork해야 합니다.

## 처음 참여할 때

1. 원본 저장소 페이지에서 **Fork**를 눌러 자신의 계정에 복사합니다.
2. 자신의 Fork를 Clone합니다. 아래 YOUR_GITHUB_ID를 본인 계정으로 바꾸세요.
3. 원본을 upstream으로 등록하고 최신 main에서 담당 브랜치를 만듭니다.

```bash
git clone https://github.com/YOUR_GITHUB_ID/opencv_teamproject.git
cd opencv_teamproject
git remote add upstream https://github.com/whrjsdnr/opencv_teamproject.git
git fetch upstream
git switch main
git merge --ff-only upstream/main
git switch -c feature/detection
```

origin은 개인 Fork, upstream은 원본입니다. 아래 담당에 따라 마지막 브랜치 이름을 바꾸세요.

| 담당 | 브랜치 | 담당 파일 |
|---|---|---|
| 팀원 A — 영상 감지 | feature/detection | src/preprocess.py, src/detect.py, src/zones.py, src/state.py |
| 팀원 B — 시스템 및 최적화 | feature/pipeline | src/main.py, src/pipeline.py, src/recorder.py |
| 팀원 C — 성능 측정 및 분석 | feature/benchmark | src/benchmark.py, docs/실험기록.md, docs/최종보고서.md |

환경 설치·실행 및 미구현 범위는 [README](README.md)를 따릅니다. 테스트 영상은 별도로 준비하며 data/SOURCES.md에 출처와 권한을 기록합니다.

## 개발 → Commit → Push → PR

담당 기능을 개발하고 검증한 뒤 변경 내용을 검토합니다. 아래는 A의 예시이며 B/C는 담당 파일과 브랜치로 바꾸세요.

```bash
git diff
git add src/preprocess.py src/detect.py src/zones.py src/state.py
git diff --cached --stat
git diff --cached
git commit -m "feat: implement polygon intrusion detection"
git push -u origin feature/detection
```

GitHub에서 **base repository: whrjsdnr/opencv_teamproject**, **base: main**, **head repository: 자신의 Fork**, **compare: 작업 브랜치**를 선택해 PR을 만듭니다. 리뷰 피드백은 같은 브랜치에 추가 Commit/Push하면 PR에 반영됩니다. 다른 팀원의 리뷰와 필요한 검증을 마친 뒤 원본 저장소 관리자가 병합합니다.

GitHub CLI를 사용한다면 개인 계정 인증 후 다음처럼 생성할 수 있습니다.

```bash
gh pr create --repo whrjsdnr/opencv_teamproject --base main --head YOUR_GITHUB_ID:feature/detection
```

토큰·비밀번호는 소스, 문서, remote URL에 넣지 않습니다. HTTPS 인증은 GitHub CLI의 `gh auth login` 또는 운영체제의 자격 증명 관리자를 사용합니다.

## 브랜치와 동기화 규칙

브랜치는 담당 기능 기준으로 이름을 지정합니다. 새로운 기능은 항상 최신 main에서 새 브랜치를 생성합니다. 초기 담당 브랜치가 병합된 뒤에는 `feature/detection-boundary`처럼 구체적인 새 이름을 사용합니다.

작업 트리가 깨끗한 상태에서:

```bash
git fetch upstream
git switch main
git merge --ff-only upstream/main
git push origin main
git switch -c feature/detection-boundary
```

위 `git push origin main`은 **개인 Fork main 동기화**이며 원본 main 직접 Push와 다릅니다. `--ff-only`가 실패하면 강제로 덮어쓰지 말고 분기 이력을 확인합니다. 진행 중인 기능 브랜치에 원본 변경을 반영할 때는 해당 브랜치에서 `git merge upstream/main`을 사용하고 충돌을 함께 검토합니다. 원본 main이나 공동 사용 브랜치에 강제 Push하지 않습니다.

## Commit 규칙

`유형: 변경 내용` 형식을 사용합니다.

| 유형 | 의미 |
|---|---|
| feat | 새로운 기능 |
| fix | 오류 수정 |
| docs | 문서 수정 |
| test | 테스트 추가 |
| perf | 성능 최적화 |
| refactor | 기능 변경 없는 코드 개선 |

예: `feat: implement polygon intrusion detection`

최초 저장소 등록 커밋은 `chore: initialize OpenCV safety monitoring project`를 사용합니다.

## PR 작성 템플릿

아래 내용을 PR 본문에 작성합니다. 실행하지 않은 테스트는 미실행 및 사유를 명시합니다.

```markdown
## 작업 목적

## 변경한 파일

## 구현한 기능

## 테스트 결과
- 실행 명령과 결과:
- 미검증 항목:

## 다른 팀원에게 영향을 주는 변경 사항
- 인터페이스·설정·좌표·시간 기준 변경 여부:
- 관련 팀원 합의 내용:

## 스크린샷 또는 실행 결과 (필요한 경우)
```

`src/utils.py`, `config.json`, `README.md`는 공통 파일이므로 변경 내용을 다른 팀원에게 공유합니다. 함수 인터페이스 변경은 관련 팀원과 이유·입출력·호출부 영향을 합의한 후 PR을 생성합니다. 검출 알고리즘을 버전별로 복제하지 않으며, 구현하지 않은 기능이나 측정하지 않은 FPS를 완료 결과로 기록하지 않습니다.

## 커밋 대상 관리

영상·data/samples/·results/·가상환경·캐시·로그·환경 비밀 파일·Zone.Identifier는 .gitignore로 제외합니다. `.env.example`은 실제 비밀값이 없는 예제만 허용합니다. `git diff --cached`로 키·개인정보·불필요한 대용량 파일을 확인하세요. 이미 추적한 제외 대상은 `git rm --cached -- 파일경로`로 추적만 해제하고 로컬 원본은 보존합니다.

## 원본 저장소 관리자 권장 설정

GitHub Settings의 Rulesets 또는 Branch protection에서 main의 PR 필수, 리뷰 승인 1명 이상, 대화 해결, 강제 Push 및 삭제 금지를 설정합니다. 요금제와 저장소 공개 범위에 따라 지원 여부를 확인하세요. CI를 추가한 뒤 실제 존재하는 검사만 필수 status check로 지정합니다. 이번 초기 등록에서는 이러한 원격 정책이나 팀원 계정의 Fork를 자동 생성하지 않습니다.
