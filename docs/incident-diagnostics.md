# Incident Diagnostics

Pod 및 Deployment 상세 화면에 기존 탭 뒤로 `Diagnostics` 탭을 추가했습니다.
기존 메뉴 번호와 기본 상세 탭, Warnings/Errors 흐름은 유지합니다.
리소스 목록에서 Enter → 좌/우로 Diagnostics 선택, r 새로 조회, u/d 페이지,
Esc 목록 복귀입니다. 이 탭에서는 편집/생성/작업/exec 단축키를 실행하지 않습니다.
Logs 탭 선택은 명시적인 별도 동작이며 진단 조회는 로그를 읽지 않습니다.

## 관측과 해석

- Deployment: desired, updated, ready, available, total, generation/observedGeneration과 조건을 표시합니다.
  최신 세대 관측 여부를 먼저 판단하고 카운터가 없는 경우 0으로 간주하지 않습니다.
  불완료는 진행 중일 수도 있으므로 장애 확정으로 표현하지 않습니다.
- Deployment → ReplicaSet → Pod는 controller ownerReference의 UID/kind/name으로 연결합니다.
  Job/StatefulSet에는 Deployment 규칙을 적용하지 않습니다. 첫 버전은 Pod/Deployment만 지원합니다.
- Pod: PodScheduled, nodeName, init/일반 컨테이너 waiting, last/current termination,
  누적 restartCount를 표시합니다. 시간별 재시작률은 계산하지 않습니다.
  Pending 원인을 자원 부족으로, OOMKilled 원인을 메모리 누수로 단정하지 않습니다.
- 관련 UID의 보존 이벤트는 사유/시각/count만 표시합니다. 이벤트 원문 메시지, 조건 메시지,
  환경변수, Secret 데이터, 이미지/명령행은 출력하지 않습니다. 알 수 없는 사유는 숨깁니다.
- 참조 PVC의 phase 및 네임스페이스 Quota hard/used는 보조 근거입니다.
  Quota 사용률만으로 해당 워크로드의 실패를 단정하지 않습니다.

## 수집과 한계

기존 timeout이 있는 인자 배열 기반 Client로 namespace 범위 `get -o json`만 사용합니다.
대상 GET 후 events/PVC/Quota, Deployment의 경우 ReplicaSet/Pod 목록을 읽고 대상 UID와
세대를 재검증합니다. Secret/discovery/exec/복구 명령은 호출하지 않습니다.
조회 실패는 권한 부족/timeout/API 미수집으로 표시하고 raw stderr는 출력하지 않습니다.
각 소스의 조회 시각과 출처를 표시하며 실패/미수집을 정상으로 처리하지 않습니다.

API 읽기는 원자적 스냅샷이 아닙니다. 자식 객체는 수집 중 변할 수 있고 보존 이벤트는
오래되거나 누락될 수 있습니다. 재시작 빈도, 실제 메모리 사용량, probe 설정의 유효성,
자원 스케줄링 가능 여부와 원인 확정은 이 버전에서 제공하지 않습니다.
기존 Detail 세대 격리를 사용하므로 닫거나 전환한 화면에 완료된 오래된 응답을 넣지 않습니다.
실행 중 subprocess는 즉시 종료하지 않고 기존 Client timeout까지 완료될 수 있습니다.
namespace 목록 크기에 비례해 응답 크기가 커질 수 있습니다.

## 오프라인 검증

```sh
python3 -m unittest discover -s tests -q
python3 kasciidash.py --demo
```

Demo에서 Workloads → Pod Enter → Diagnostics 탭으로 synthetic 진단을 볼 수 있습니다.
Demo Deployment의 진단 내용도 synthetic Pod 예시이며 실제 Deployment 상태를 의미하지 않습니다.
테스트는 fake client/fixture만 사용합니다. 실제 인증정보나 클러스터 접근 검증은 하지 않았습니다.

공식 판정 근거:
- https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
- https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/
- https://kubernetes.io/docs/concepts/policy/resource-quotas/
