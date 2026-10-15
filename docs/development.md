# KAsciiDash 개발 가이드

Python 3.9 이상과 표준 라이브러리를 사용합니다. 코드 구조는 [README](../README.md#프로젝트-구조), 운영 중 문제 확인은 [운영 가이드](operations.md)를 참고하세요.

## 실행 진입점과 로컬 상태

`kasciidash`는 같은 디렉터리의 `kasciidash.py`를 실행하고, Python 진입점은 `kasciidash_app.ui.dashboard.main()`을 호출합니다. 다음 세 방식은 같은 앱을 실행합니다.

```sh
./kasciidash --demo
python3 kasciidash.py --demo
python3 -m kasciidash_app --demo
```

실제 클러스터 연결 없이 화면 텍스트를 확인하려면 다음을 사용합니다.

```sh
./kasciidash --demo --once --width 140
```

루트 경로는 `kasciidash_app/ui/dashboard.py`의 위치에서 계산합니다. 패키지 깊이를 바꿀 때는 `.kube/`와 `.kasciidash/`의 기준 경로도 함께 검증해야 합니다. 내부 모듈을 파일 경로로 직접 실행하지 않습니다.

## 변경 위치

| 변경하려는 기능 | 주로 확인할 위치 |
| --- | --- |
| 메뉴·탭·키·갱신·curses 렌더링 | `kasciidash_app/ui/dashboard.py` |
| 프로필·kubeconfig 가져오기·연결 설정 | `kasciidash_app/config/profiles.py` |
| Prometheus 조회·집계·모니터링 렌더링 | `kasciidash_app/monitoring/prometheus.py` |
| 리소스 목록·상세·작업·이벤트·노드·namespace | `kasciidash_app/resources/` |
| 워크로드 범위·연결 그래프 | `kasciidash_app/topology/` |
| exec의 PTY·입출력·터미널 복원 | `kasciidash_app/terminal/exec_session.py` |
| 회귀 테스트 | `tests/` |

화면·수집·조작이 완전히 분리된 구조는 아닙니다. 특히 dashboard.py에 조회와 화면 처리가 함께 있으므로 함수의 호출자와 기존 테스트를 확인하고 변경합니다. 외부 에디터·exec로 나갔다 돌아오는 코드는 curses 모드 복원까지 검증합니다.

## 검증 명령

프로젝트 루트에서 실행합니다.

```sh
python3 -B -m unittest discover -s tests -t . -v
sh -n kasciidash
git diff --check
```

특정 기능을 확인할 때는 모듈을 지정할 수 있습니다.

```sh
python3 -B -m unittest tests.test_yaml_storage tests.test_event_filters -v
```

테스트는 모의 Kubernetes·Prometheus 응답과 합성 fixture를 사용합니다. exec 테스트 일부는 로컬 PTY와 설치된 sh/bash를 실제로 실행하므로 터미널·OS 기능이 필요합니다. Python 문법 검사와 모의 응답 테스트만으로 모든 Python 버전·macOS·Ubuntu·Rocky 및 실제 클러스터 연동을 검증했다고 표현하지 않습니다.

## 회귀 테스트에서 지켜야 할 동작

- 목록의 선택 대상과 상세·조작 대상이 일치하고, 필터·자동 갱신·객체 교체 뒤에도 다른 객체로 잘못 작업하지 않아야 합니다.
- 기존 YAML은 선택 시 UID/resourceVersion을 별도 보존하고 조건부 replace에 포함합니다. YAML 미리보기·검증·로컬 diff·최종 제출은 같은 보호된 스냅샷을 사용합니다. 확인 대기 중 편집 파일을 다시 읽어 제출 내용을 바꾸지 않습니다.
- 신규 초안 성공 삭제·실패 보존·재개와 기존 편집 임시 파일 정리를 검증합니다.
- Prometheus의 클러스터 범위를 확정하지 못하면 혼합 집계값을 표시하지 않습니다. exporter별 라벨과 fallback 경로도 확인합니다.
- 좁은 터미널과 리사이즈, 빈 목록, RBAC 오류, null 필드, 누락된 지표를 함께 다룹니다.
- 인증·Secret 원문을 실제 환경에서 가져와 fixture로 사용하지 않습니다. 데모와 테스트는 실제 클러스터의 생성·삭제·재시작·scale을 호출하지 않아야 합니다.

사용자 조작이나 로컬 경로가 바뀌면 README와 관련 docs도 함께 갱신합니다. 문서에 아직 제공하지 않는 기능을 사용 가능한 것으로 쓰지 않습니다.

## 저장 계약 회귀 테스트

- `tests.test_profile_registry_contract`: `.kasciidash/profiles.json`을 독립적인 기대 경로로 사용해 잘못된 파일명으로의 회귀를 잡습니다. 이전 파일·동일 파일·충돌·손상·동시 시작·쓰기 실패를 포함합니다.
- `tests.test_edit_preconditions`: 보호 필드 제거·변경, UID/RV 교체, 승인 대기 중 변경에 대한 조건부 요청을 검증합니다. 실제 Kubernetes API에 보내는 테스트는 아닙니다.
- `tests.test_draft_transactions`: 파일 경로의 원자적 확보 후 정리, 새 경로 생성·변경·소실·정리 실패와 리소스 별칭을 검증합니다. 이전 초안·복구 파일은 사라지지 않아야 합니다.

프로필은 canonical 및 기존 파일의 lock을 획득한 뒤 이전합니다. 초안 신규 작성/편집은 정규 리소스별 lock으로 앱 인스턴스 간 중복 편집을 직렬화합니다. 파일 정리는 별도 경로로 rename한 후 그 파일만 검사·삭제하며, 변경됐으면 원래 경로를 덮어쓰지 않고 복구합니다. 이 검증은 로컬 POSIX 파일시스템 기준이며 네트워크 파일시스템의 잠금·rename 특성은 별도 확인이 필요합니다.

이번 변경의 실제 실행 환경과 제한은 [리뷰 수정 QA 기록](qa-review-fixes-2026-10-15.md)에 정리했습니다.
