# KAsciiDash 운영 가이드

클러스터에 연결한 뒤 상태를 확인하고, 필요한 작업을 수행하고, 결과를 확인하는 흐름을 정리합니다. 세부 키와 옵션은 [README](../README.md), 계정 발급·인증 갱신·권한 회수는 [kubeconfig 접근 관리](kubeconfig-access.md)를 참고하세요.

## 실행과 연결

프로젝트 루트에서 실행합니다. 다른 Python 버전을 사용해야 한다면 인터프리터를 직접 지정합니다.

```sh
./kasciidash
python3.11 kasciidash.py
```

마지막으로 열었던 프로필이 기본 선택됩니다. 다른 클러스터는 `c` 또는 Settings → Profiles에서 선택하거나 다음처럼 시작합니다.

```sh
./kasciidash --profile my-alias
```

상단의 프로필·클러스터·API 주소를 확인한 뒤 작업합니다. 선택한 프로필의 Prometheus 연결과 Kubernetes API 연결은 서로 독립적입니다. Prometheus 연결 성공이 모든 지표의 존재나 클러스터 일치를 보장하지는 않습니다.

프로젝트 디렉터리 전체를 옮겨도 실행 파일 기준으로 `.kube/`와 `.kasciidash/`를 찾습니다. kubeconfig의 외부 인증 파일 경로, exec 인증 플러그인, Prometheus 인증용 환경변수와 사용자 CA 경로는 이동한 환경에서도 유효해야 합니다.

## 상태 확인 순서

1. Dashboard → Overview에서 노드 상태, Pod 장애·Pending 수와 수집 오류를 확인합니다. N/A는 조회 불가이며 정상이나 사용량 0을 의미하지 않습니다.
2. Clusters → Nodes에서 문제 노드를 선택해 상태와 Describe를 확인합니다. 모니터링 차트에 노드가 안 보이면 노드 목록과 함께 비교합니다. 차트는 Prometheus가 반환한 시계열을 기준으로 하므로 누락된 노드를 사용량 0으로 간주하지 않습니다.
3. Event → Warnings에서 `namespace=... object=... reason=...`으로 관련 이벤트를 좁힙니다. 과거 이벤트의 객체가 이미 삭제됐다면 상세 YAML/Describe는 NotFound일 수 있습니다.
4. Resources → Workloads에서 대상과 namespace를 확인합니다. Pod/Deployment는 Enter → 좌우 키로 Diagnostics를 열어 증상·근거·가능한 설명·다음 확인을 봅니다. 필요한 경우 Describe나 Logs를 명시적으로 선택합니다. Logs는 스트리밍이 아닌 스냅샷이므로 `r`로 다시 조회합니다.
5. 소유 관계와 설정 연결이 필요하면 워크로드의 Topology 또는 Namespaces 상세의 TreeView·Topology를 사용합니다. 이 연결도는 실제 네트워크 통신이나 연결 성공을 보장하는 자료가 아닙니다.

Errors는 Kubernetes Error 이벤트 목록이 아니라 앱의 수집 오류 목록입니다. 별도 reason이 없는 오류는 오류 메시지를 reason 필터로 검색합니다. 오류에 대상 정보가 없으면 namespace/object 조건으로 찾을 수 없습니다.

## 읽기 전용 장애 진단

Pod/Deployment 상세의 마지막 Diagnostics 탭에서 `r`로 조회를 갱신합니다. `u/d` 페이지, Esc 목록 복귀, 좌우 탭 전환을 사용합니다. 이 탭에서는 `e/n/D/a/!`가 차단되고 자동 복구·삭제·재시작·scale·exec를 실행하지 않습니다. Warnings/Errors는 기존 이벤트·수집 오류 확인에 계속 사용합니다.

Deployment는 카운터와 observedGeneration, 조건 및 UID로 연결된 ReplicaSet/Pod를 확인합니다. Pod는 예약·노드 배정, init/일반 컨테이너 대기, 현재/이전 종료, 누적 재시작을 확인합니다. 이벤트는 관련 UID의 보존 사유·시각·횟수만 표시하며 참조 PVC와 namespace Quota는 보조 근거입니다. 상세 해석과 필요한 get/list 권한은 [장애 진단 문서](incident-diagnostics.md)를 참고하세요.

`uncollected`·권한 부족·timeout은 정상이나 0이 아닙니다. Deployment의 오래된 관측 세대, 조회별 시각, 누락된 소유 관계를 확인하고 필요하면 갱신합니다. Pending을 자원 부족으로, OOMKilled를 누수로 단정하지 않으며 누적 restartCount를 최근 발생률로 사용하지 않습니다. 서로 다른 API 읽기와 오래된 이벤트가 혼재할 수 있고 실제 클러스터 동작은 아직 미검증입니다. 로그는 명시적으로 Logs 탭에서 조회합니다.

## 신규 YAML 초안과 기존 리소스 수정

| 작업 | 조작 | 로컬 파일 처리 |
| --- | --- | --- |
| 새 리소스 작성 | 해당 리소스 목록에서 `n` | `.kasciidash/<alias>/<resource>/<name>.yaml` 저장 |
| 저장된 초안 이어서 작업 | 같은 프로필·리소스 화면에서 `D` | 선택 후 편집·검증·생성 확인 |
| 기존 객체 수정 | 행 선택 → Enter → `e` | 임시 편집 파일을 종료 시 정리 |

신규 초안은 `create` 성공 시 삭제하고 취소·실패 시 보존합니다. 승인 대기 중 외부에서 초안이 바뀌면 승인한 내용만 제출하고 더 최신 파일은 남깁니다. 신규 생성은 기존 객체를 덮어쓰는 apply가 아닙니다. AlreadyExists라면 기존 객체를 조회해 수정할지, 새 이름으로 생성할지 결정합니다.

같은 alias·resource·name의 초안은 namespace가 달라도 하나만 저장할 수 있습니다. YAML의 namespace를 확인하세요. 별명을 바꾸면 이전 별명 폴더의 초안은 자동 이동하지 않습니다.

신규 초안을 다른 객체로 바꾸려면 해당 리소스의 `n`으로 새로 시작하세요. 기존 객체 수정은 apiVersion·kind·name·namespace를 유지해야 하며, 선택 시 UID/resourceVersion을 보존한 조건부 replace의 서버 dry-run과 로컬 diff를 확인한 뒤 `apply`를 입력합니다. 실제 요청은 `kubectl replace`이며 `--force`를 사용하지 않습니다. update 권한이 필요하고 생략한 필드가 제거될 수 있습니다. 충돌 시 대상을 다시 조회합니다. 제출 성공은 요청 수락을 뜻하므로 목록·상태·이벤트에서 후속 결과를 확인합니다.

## 변경 후 확인과 복구

- 재시작 후에는 워크로드 상태와 새 Pod의 Ready 여부를 확인합니다. 요청이 성공했어도 이미지·스토리지·설정 문제로 실행은 실패할 수 있습니다.
- 정지는 replica 0, 시작은 입력한 양수 replica로 변경합니다. 이전 replica 수를 자동 복원하지 않으므로 필요한 수를 별도로 확인하세요. HPA나 operator가 다시 변경할 수 있습니다.
- 충돌이나 대상 교체가 표시되면 목록을 갱신하고 대상을 다시 선택합니다. 오래된 상세 화면에서 반복 실행하지 않습니다.
- 잘못된 YAML 변경은 클러스터의 현재 상태와 버전 관리 중인 원본 manifest를 비교해 수정합니다. KAsciiDash는 로컬 배포 이력이나 자동 롤백을 제공하지 않습니다.
- 삭제는 일반 삭제 요청입니다. 자동 복구 기능이 아니며, 컨트롤러 소유 Pod는 다시 만들어질 수 있습니다. 상위 워크로드와 연결 자원을 함께 확인하세요.

## 문제 해결

| 증상 | 확인할 항목과 다음 조치 |
| --- | --- |
| 명령 실행 불가 | 실행 권한, PATH의 python3, Python 3.9 이상인지 확인. 다른 버전은 `python3.11 kasciidash.py`처럼 실행 |
| Unauthorized | 계정·토큰·인증서 만료와 프로필이 참조하는 실제 `.kube` 파일 확인. 외부 원본만 갱신해도 관리 사본은 바뀌지 않음 |
| Forbidden | Errors의 조회 대상과 필요한 RBAC 확인. 조회 권한과 수정·logs·exec 권한은 별개 |
| NotFound / UID 충돌 | 과거 이벤트 또는 삭제·교체된 객체인지 확인하고 현재 목록에서 다시 선택 |
| Prometheus 연결됐지만 N/A | exporter·지표 존재, 필터와 cluster 라벨, 인증·접근 오류 확인. 혼합 클러스터를 집계하지 못할 때는 전용 endpoint 또는 일관된 cluster 라벨 사용 |
| exec에서 입력 불가 | 실제 터미널에서 실행하고 컨테이너에 sh/bash가 있는지 확인. `Ctrl+]`로 로컬 연결 종료, `exit` 또는 `Ctrl+D`로 쉘 종료 |
| YAML 에디터가 바로 닫힘 | `$VISUAL`, `$EDITOR` 확인. GUI 편집기는 저장까지 기다리는 옵션 사용: `EDITOR='code --wait' ./kasciidash` |
| 프로필 저장 충돌 | 다른 프로세스의 설정 변경을 덮어쓰지 않도록 중단한 상태. 앱을 다시 열어 최신 설정을 읽음 |

exec는 첫 출력이 없는 쉘 시작을 최대 20초 기다립니다. 로컬 연결을 종료했다고 원격에서 시작한 명령까지 중단됐다고 판단하지 마세요.

## 로컬 상태 보관과 이동

앱을 종료한 상태에서 `.kube/`와 `.kasciidash/`를 함께 보호된 저장소에 보관하면 프로필·Prometheus 설정·미배포 초안을 함께 유지할 수 있습니다. 복원할 때도 두 디렉터리를 함께 옮기고 디렉터리 700, 인증·설정·초안 파일 600 권한을 유지합니다. 다른 환경에서는 인증 플러그인, 환경변수와 외부 CA·인증 경로를 다시 준비합니다.

두 디렉터리에는 인증 정보나 Secret YAML이 포함될 수 있으므로 Git 커밋·공개 첨부에 사용하지 않습니다. 프로필 삭제는 로컬 등록 삭제이며 kubeconfig 파일 삭제나 Kubernetes 권한 회수가 아닙니다. 자격 증명 유출 시 조치는 [접근 관리 문서](kubeconfig-access.md)를 따릅니다.

## 레지스트리 이전과 초안 복구

정규 프로필 파일은 `.kasciidash/profiles.json`입니다. 이전의 잘못된 `*_app.config.profiles.json`은 데이터가 충돌하지 않는 경우 자동 이전하고 `.kasciidash/registry-backups/`에 원본을 보관합니다. 서로 다른 레지스트리가 발견되면 대화형 실행에서 복구 선택 화면이 나옵니다. 프로필과 Prometheus 설정 건수를 비교해 사용할 파일 번호를 입력합니다. 원본은 모두 백업하고 선택한 설정을 `profiles.json`으로 사용합니다. 취소는 `q`/Esc이며 기본 선택은 없습니다. `--once`에서는 복구를 묻지 않으므로 `./kasciidash`를 터미널에서 실행합니다. 손상 파일은 앱을 종료하고 보호된 사본을 기준으로 복구합니다. 원본을 백업 없이 삭제하거나 재등록해서 해결하지 않습니다.

Workloads의 축약 이름과 API Resources의 group 포함 이름은 같은 초안을 사용합니다. 이전 축약 폴더도 `D`에서 조회합니다. 같은 이름의 파일이 둘 이상이면 경로를 보고 명시적으로 선택하며, 이전 시 목적지 파일을 덮어쓰지 않습니다.

배포 성공 후 로컬 정리 실패가 표시되면 성공한 배포를 다시 실행하지 말고 목록에서 상태를 조회합니다. 정리하지 못한 파일은 `.draft-claim-*/`에 보존되며 `D`에서 선택할 수 있습니다. 원래 경로에 새 파일이 있으면 기존 복구 파일로 덮어쓰지 않습니다.

## 개별 리소스 알람 확인

목록의 ALARMS는 Warnings + Errors의 고유 기록 수이며, 상세의 두 탭으로 내역을 확인합니다. Deployment는 소유 Pod, Service는 selector가 선택한 Pod까지 포함합니다. Service의 선택 관계는 통신 성공의 증거가 아닙니다. Node는 할당 Pod, Namespace는 해당 namespace 기록을 포함합니다. 같은 이벤트가 500회 발생했더라도 보관 Event가 하나이면 알람 한 건입니다.

Pod phase/Ready 및 장애 수, Prometheus alert 수는 별도로 확인합니다. 대상 reference 없는 수집 오류는 개별 행에 붙이지 않으므로 Event → Errors도 함께 봅니다. 이벤트 수집 실패 시 N/A는 0이나 정상 상태를 뜻하지 않습니다. 알람 탭은 스냅샷이며 `r`로 갱신할 수 있습니다.

메뉴 이동 지연을 줄이기 위해 반복 필터링·정렬과 커서 이동 중 전체 표 재포맷을 재사용합니다. 실제 클러스터의 API 지연·큰 Namespace/Topology 전체 인벤토리 조회까지 없어지는 것은 아니므로, 수집 완료 시각과 QUERY ERROR를 함께 확인하세요.

알람의 하위 자원 범위는 현재 수집된 객체·ownerReferences 기준입니다. 수집하지 않은 중간 CRD/Job 등은 연결이 누락될 수 있습니다. Pod 또는 ReplicaSet 조회가 실패한 관련 목록은 N/A/부분 값으로 표시하며, 모든 클러스터 기록을 영구 보관하거나 완전한 장애 건수를 계산하는 기능은 아닙니다.
