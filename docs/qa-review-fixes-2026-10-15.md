# 리뷰 결함 수정 QA 기록

검증일: 2026-10-15 (Asia/Seoul)

## 범위와 결과

| 항목 | 확인 내용 |
| --- | --- |
| 프로필 저장 계약 | `profiles.json`의 독립적 경로 검사, 기존 파일 자동 이전, 동일·충돌·손상 파일, 동시 시작, 쓰기·백업 실패와 원본 보존 |
| 기존 YAML 수정 | UID/resourceVersion 별도 보존, 삭제된 필드 복원·변경 값 거부, dry-run 대상 교체 거부, 조건부 replace 및 승인 대기 중 변경 거부, 취소·정상 실행 |
| 초안 정리 | 원자적 rename으로 경로 확보 후 검사, 삭제 직전 새 파일 생성 보존, 내용 변경·소실·정리 실패, 복구 경로와 배포 성공 안내 분리 |
| 리소스 식별자 | Workloads↔API Resources 양방향 조회, Deployment/ReplicaSet/StatefulSet/DaemonSet/Job/CronJob, 기존 축약 폴더, API group·프로필 격리 |
| 기존 기능 | 기존 171개 테스트를 포함해 총 188개 테스트 통과 |

## 실제 실행 환경

- macOS 26.6.2, Apple Silicon.
- Python 3.13.0: 전체 188개 unittest 통과.
- Python 3.14.2: 전체 188개 unittest 통과.
- `sh -n kasciidash` 통과.
- Python 3.9 문법 모드로 전체 Python 소스 파싱 통과. Python 3.9 인터프리터는 설치돼 있지 않아 실제 실행하지 않았습니다.
- 공개 실행 진입점, 패키지 실행과 다른 CWD에서의 실행을 기존 패키지 테스트로 확인했습니다.
- README와 docs의 로컬 상대 링크·anchor 검사를 통과했습니다.

초기에는 새 회귀 테스트를 먼저 추가해 실패를 확인했습니다. API 변경 요청은 모의 클라이언트로 검증했고, 실제 인증·Secret 값을 사용하지 않았습니다. 기존 exec 테스트는 로컬 PTY와 로컬 sh/bash만 실행합니다.

## 확인하지 않은 범위와 운영 시 변경점

실제 Kubernetes·Prometheus 연동, Ubuntu·Rocky 8.10 실행, 네트워크 파일시스템의 rename/잠금 동작은 검증하지 않았습니다. CI는 이번 변경에 추가하지 않았습니다.

기존 YAML 수정은 UID/resourceVersion을 포함한 전체 객체 replace입니다. update 권한이 필요하며, 사용자가 지운 필드가 실제로 제거될 수 있습니다. UI의 `apply` 승인 단어는 유지하지만 서버 요청은 `kubectl replace`이고 강제 삭제·재생성 옵션은 사용하지 않습니다. 관련 동작의 근거는 [Kubernetes 갱신 충돌 문서](https://kubernetes.io/docs/reference/using-api/api-concepts/#updates-to-existing-resources)와 [kubectl replace 문서](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_replace/)입니다.

초안 정리는 원래 공개 경로를 원자적으로 확보한 후 그 파일만 처리합니다. 이후 같은 공개 경로에 생성된 파일은 삭제하지 않고, 변경되거나 정리할 수 없는 파일은 원래 경로 또는 `.draft-claim-*/`에 보존합니다. D 목록에서 복구 파일을 선택할 수 있습니다. 디스크 오류나 강제 프로세스 종료 시 남은 복구 파일도 운영 가이드에 따라 확인하세요.

실제 사용자 프로필은 이번 테스트에서 읽거나 이전하지 않았습니다. 업데이트된 앱의 다음 정상 시작 시 파일 검사·마이그레이션이 수행됩니다. 충돌 시 원본을 보존하고 중단합니다.
