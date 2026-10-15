# KDASH

macOS·Ubuntu·Rocky Linux에서 사용할 수 있는 Python3 ASCII Kubernetes 운영 대시보드. 상단 연결 정보, 왼쪽 고정 메뉴, 중앙 요약 카드와 노드 자원 패널로 구성합니다. 기본 조회와 명시적으로 확인하는 운영 작업을 지원하며 kubeconfig의 current-context는 변경하지 않습니다.

## 빠른 시작

Python **3.9 이상**과 `kubectl`이 PATH에 있어야 합니다. Python 표준 라이브러리만 사용하므로 pip 패키지는 필요하지 않습니다. curses를 지원하는 일반 터미널 또는 SSH 터미널에서 실행합니다. Linux는 코드상 호환되지만 실제 Ubuntu·Rocky 환경에서의 실행 검증은 아직 수행하지 않았습니다.

### OS별 준비

| 환경 | 준비 방법 |
| --- | --- |
| macOS | Python 3.9 이상과 kubectl 설치. Homebrew 사용 시 아래 예시 참고 |
| Ubuntu | 아래 apt 예시로 Python·편집기 설치 후 kubectl 설치 |
| Rocky Linux 9 이상 | 아래 dnf 예시로 Python·편집기 설치 후 kubectl 설치 |
| Rocky Linux 8 또는 Python 3.9 미만 | Python 3.9 이상의 별도 인터프리터로 `kdash.py` 직접 실행 |

macOS에서 Homebrew가 설치되어 있다면:

```sh
brew install python kubectl
```

Ubuntu:

```sh
sudo apt update
sudo apt install -y python3 vim
```

Rocky Linux 9 이상:

```sh
sudo dnf install -y python3 vim-minimal
```

Ubuntu·Rocky의 kubectl은 [Kubernetes 공식 Linux 설치 안내](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/)를 따라 설치합니다. 클러스터 버전과 호환되는 kubectl을 사용하세요. Python 버전은 설치 후 아래 명령으로 확인합니다. Ubuntu의 제공 버전은 [Ubuntu Python 안내](https://ubuntu.com/developers/docs/reference/availability/python/), Rocky 9의 기본 Python 3.9는 [Rocky 공식 릴리스 안내](https://docs.rockylinux.org/releases/release_notes/9_0/)를 참고하세요.

```sh
python3 --version
python3 -c 'import sys, curses; assert sys.version_info >= (3, 9), "Python 3.9+ required"; print("Python/curses OK")'
kubectl version --client
```

`python3`가 3.9 미만이면 별도로 설치한 Python 3.9 이상의 실행 파일을 사용합니다. 예를 들어 `python3.11`이 설치되어 있다면:

```sh
python3.11 -c 'import curses; print("curses OK")'
python3.11 kdash.py --demo
python3.11 kdash.py
```

`kdash.sh`는 PATH의 `python3`를 실행하므로 다른 버전은 위와 같이 직접 지정합니다.

### 프로젝트 배치와 첫 실행

프로젝트 디렉토리 전체를 실행할 컴퓨터로 복사합니다. `kdash.py` 하나만 복사하면 함께 사용하는 모듈을 불러올 수 없습니다. 아래 `/path/to/k8s-dashcli`는 실제 프로젝트 경로로 바꿉니다.

```sh
cd /path/to/k8s-dashcli
chmod +x kdash.sh
mkdir -p .kube
./kdash.sh --demo  # 접속 정보 없이 화면 확인, q로 종료

# 실제 kubeconfig를 .kube/<k8s-cluster-name>.conf 형태로 저장
cp /path/to/your/kubeconfig .kube/my-cluster.conf
chmod 600 .kube/my-cluster.conf
./kdash.sh
```

외부 kubeconfig에 상대 경로 인증서가 있다면 단순 복사 대신 `./kdash.sh` 실행 후 등록 화면에서 **원본 파일의 절대 경로**를 입력하세요. 앱이 인증서를 포함한 사본으로 가져옵니다.

실제 접속 전에 kubeconfig 자체가 동작하는지 확인할 수 있습니다:

```sh
kubectl --kubeconfig /path/to/your/kubeconfig get nodes
```

첫 대시보드 연결 뒤에는 `↑/↓`로 소메뉴를 고르고 `Enter`로 진입합니다. 화면 안에서 `←/→`로 탭을 바꾸고, `Esc`로 돌아갑니다. `q`로 종료합니다.

### kubeconfig와 프로필 관리

프로필 등록 전 [kubeconfig 발급·클러스터 권한·운영 주의사항](docs/kubeconfig-access.md)을 참고하세요. 등록 자체에는 슈퍼 관리자 권한이 필요하지 않으며, 조회용 계정과 전체 운영용 계정의 발급 방법·권한 검증·갱신 및 회수 절차를 설명합니다.

최초 실행 시 `.kube/*.conf`를 파일명 순서로 프로필에 등록하여 **기존 파일을 직접 참조**합니다. 자동 등록은 사본을 만들지 않습니다. 이후에는 `.kdash/profiles.json`에 등록된 순서를 사용합니다. 실행할 때마다 첫 프로필에 자동 연결합니다. 프로필이 없으면 등록 화면으로 안내합니다. `Settings → Profiles` 또는 `c`에서 등록·조회·별명 수정·고급 접속 설정 변경·삭제·선택 및 Prometheus 설정을 관리합니다. 시작 전 kubeconfig 선택 질문은 표시하지 않습니다.

외부 kubeconfig는 프로필 등록에서 경로를 입력합니다. `kubectl config view --raw --flatten`으로 인증서를 포함한 관리용 복사본을 `.kube/<이름>-<식별자>.conf`에 저장하므로 상대 인증서 경로가 깨지지 않습니다. 원본은 수정하지 않습니다. 외부 원본을 갱신해도 관리 사본은 자동 갱신되지 않습니다. 이미 등록된 `.kube` 참조 파일 경로를 다시 입력하면 기존 프로필을 반환합니다. exec 인증 플러그인은 해당 CLI가 KDASH를 실행하는 컴퓨터에 설치되어 있어야 합니다. 프로필의 context를 변경하면 Prometheus 설정을 초기화합니다.

인증 갱신 시에는 KDASH를 종료하고 등록 방식에 맞는 `.kube` 참조 파일을 검증된 갱신 파일로 원자적으로 교체하거나, 현재 등록 파일과 다른 보호 경로의 갱신 파일을 임시 alias로 가져와 검증 후 전환합니다. 새 프로필은 Prometheus도 다시 설정합니다. 프로필 삭제는 파일이나 Kubernetes 계정·RBAC를 삭제하지 않습니다. 자세한 절차는 [프로필 등록·인증 갱신](docs/kubeconfig-access.md#6-kdash-프로필-등록)을 참고하세요.

인증 파일은 `.kube` 또는 저장소 밖의 보호 디렉토리에 보관합니다. `.kube/*`, `.kdash/`, 루트의 `*.conf`·`*.kubeconfig`는 Git에서 제외합니다. 이미 추적된 파일과 과거 커밋에는 소급 적용되지 않습니다.

```sh
./kdash.sh --demo                       # 실제 접속 없는 샘플 화면
./kdash.sh --demo --once --width 140     # ASCII 화면을 텍스트로 출력
./kdash.sh --profile my-alias           # 등록된 alias/ID 선택
./kdash.sh --config /path/dev.conf --context dev-admin  # 일회성 직접 연결; 등록/복사하지 않음
./kdash.sh --interval 15 --timeout 8
./kdash.sh --config .kube/dev.conf --once
```

### 실행 옵션

| 옵션 | 용도 / 기본값 |
| --- | --- |
| `--demo` | 클러스터 호출 없이 샘플 화면 확인 |
| `--once` | 한 번 조회해 텍스트 출력 후 종료. 비대화형 환경에서도 사용 가능 |
| `--width 140` | `--once` 출력 폭. 기본 120열 |
| `--profile ALIAS` | 등록된 프로필의 alias 또는 ID로 연결 |
| `--config PATH` | 지정한 kubeconfig로 일회성 연결. 프로필로 등록하지 않음 |
| `--context NAME` | kubeconfig 내부의 접속 설정 이름 지정 |
| `--interval 15` | Kubernetes 객체 갱신 간격. 기본 30초 |
| `--metrics-interval 2` | Prometheus 조회 완료 후 갱신 간격. 기본 5초 |
| `--timeout 8` | kubectl 요청별 제한 시간. 기본 8초 |
| `--scroll-step 10` | 마우스 휠 이동량. 기본 5줄 |

`--config`와 `--profile`은 동시에 지정할 수 없습니다. 전체 옵션은 `./kdash.sh --help`로 확인합니다.

### Prometheus 연결 순서

1. `c` 또는 `Settings → Profiles`로 들어갑니다.
2. `p`를 선택하고 설정할 프로필을 고릅니다.
3. 선택 프로필에서 조회한 Service 목록에서 Prometheus의 **서비스/포트 번호**를 선택합니다.
4. 주소가 여러 개라면 노드 또는 LoadBalancer 주소를 선택합니다. NodePort는 노드 IP와 NodePort로 URL을 구성합니다.
5. 표시된 주소를 확인하고 Enter로 연결 테스트·저장합니다. 인증이 필요한 경우 `a`에서 환경변수 이름이나 CA 파일을 설정합니다.
6. 대시보드로 돌아와 `Dashboard → Monitoring`에 진입합니다.

연결 설정은 프로필별로 저장되어 다음 실행에서도 사용합니다. 실행 컴퓨터에서 Prometheus 주소에 접근할 수 있어야 합니다. 연결 확인 후에도 특정 지표가 N/A이면 해당 exporter·수집 설정을 확인하세요. 상세 설정과 지표별 조건은 아래 Prometheus 섹션에 정리되어 있습니다.

### 로컬 파일 위치

아래 앱 관리 파일은 현재 셸 위치가 아니라 **프로젝트 디렉토리 기준**으로 저장합니다. `--config`나 등록 시 입력하는 외부 파일 경로는 절대 경로 사용을 권장합니다.

| 경로 | 내용 |
| --- | --- |
| `.kube/*.conf` | 클러스터 kubeconfig 사본 |
| `.kdash/profiles.json` | 별명·kubeconfig 경로·context·프로필별 Prometheus 연결 정보 |
| `.kdash/drafts/*.yaml` | 생성·수정한 YAML 초안. 성공·취소 후에도 보존 |

다른 컴퓨터에서 사용할 때는 kubeconfig 인증 플러그인·인증 환경변수·사용자 CA 파일도 해당 컴퓨터에 준비해야 합니다. 프로필에 저장된 외부 파일의 절대 경로는 새 환경에 맞게 다시 설정합니다.

### 자주 발생하는 실행 문제

| 증상 | 확인 방법 |
| --- | --- |
| `Permission denied`로 실행되지 않음 | 프로젝트에서 `chmod +x kdash.sh` 실행 |
| `python3: command not found` | Python 설치 후 PATH 확인 |
| `is_relative_to`, `removesuffix`, `cancel_futures` 관련 오류 | Python 3.9 이상인지 확인 |
| `No module named curses` / `_curses` | curses 포함 Python 패키지를 설치하고 `python3 -c 'import curses'`로 확인 |
| `kubectl is required in PATH` | kubectl 설치 후 `kubectl version --client` 실행 |
| `Interactive dashboard requires a terminal` | 일반 터미널에서 실행. SSH는 `ssh -t user@host`로 TTY 할당. 텍스트 출력은 `--once` 사용 |
| 터미널 초기화 오류 | `$TERM`과 terminfo 설치 확인. 터미널 종류에 맞는 TERM 사용 |
| 목록이 N/A / Forbidden | `Event → Errors`에서 원인 확인 후 kubeconfig·RBAC 권한 확인 |
| Monitoring 연결 오류 | 실행 컴퓨터에서 endpoint 접근 가능 여부, HTTP/HTTPS·포트·인증·CA 확인 |
| 휠이 동작하지 않음 | 터미널/SSH/tmux의 마우스 이벤트 지원 확인. `j/k`, `u/d`로도 조작 가능 |

터미널 최소 크기는 60열 x 18행, 권장 크기는 140열 x 45행 이상입니다. 좁은 화면에서는 카드가 재배치되고 자원 수치가 줄바꿈됩니다. 85열 미만에서는 메뉴를 상단으로 옮깁니다. 창 크기를 변경하면 다시 렌더링합니다.

## 메뉴

```text
Dashboard
  Overview       [Summary | Nodes]
  Monitoring     [Cluster & Nodes | Namespace Capacity | Service Traffic | Workload Health]
Clusters
  Nodes          [Status | YAML | Describe]
  Namespaces     [Overview | Resources | Topology | YAML | Describe]
Resources
  Workloads      [Pods | Deployments | ReplicaSets | StatefulSets]
  Network        [Services | EndpointSlices | Ingresses | NetworkPolicies]
  Config         [ConfigMaps | Secrets | ResourceQuotas | LimitRanges]
  Storage        [PVCs | PVs | StorageClasses | VolumeAttachments]
  Access / RBAC  [ServiceAccounts | Roles | RoleBindings | ClusterRoles | ClusterRoleBindings]
  API Resource   [API 종류 선택 -> 객체 목록]
Event
  Warnings
  Errors
Settings
  Profiles       [등록 / 조회 / 수정 / 삭제 / 선택 / Prometheus]
```

메뉴 앞 번호 없이 모든 대메뉴와 소메뉴를 펼쳐 표시합니다. 화면 높이가 부족하면 선택한 항목이 보이도록 메뉴 표시 범위가 이동합니다. 대메뉴는 들여쓰기를 줄인 구분 제목이며 선택하지 않습니다. 소메뉴에는 `-`를 붙이고, 위/아래 키로 그룹 경계를 넘어 소메뉴를 바로 선택합니다. 첫 메뉴 Overview에서 위로 이동하면 마지막 Profiles로, 마지막에서 아래로 이동하면 Overview로 순환합니다. j/k도 동일하게 동작합니다. Enter로 진입하고 화면 안에서는 좌/우로 탭을 이동합니다. Esc는 메뉴 선택으로 돌아가며, 메뉴에서 다시 누르면 현재 선택을 유지합니다. 화면별 선택 탭을 유지합니다. Errors는 KDASH의 조회/렌더링 오류이며 Kubernetes Warning 이벤트와 구분합니다.


| 키 | 기능 |
| --- | --- |
| 1 + Enter | 메인 대시보드: 요약 카드와 모든 노드의 자원 현황 |
| 2 + Enter | Overview의 Nodes 탭으로 바로 이동하는 기존 단축키 |
| m + Enter | Prometheus 실시간 차트와 메트릭 |
| N + Enter | Clusters / Nodes 목록 (대문자 N) |
| Z + Enter | Clusters / Namespaces 목록 (대문자 Z) |
| 3 + Enter | 워크로드 화면, 좌/우로 Pods / Deployments / ReplicaSets / StatefulSets 탭 전환 |
| 4 + Enter | 보관 중인 Warning 이벤트의 사유, 대상, 메시지, 발생 횟수 |
| 5 + Enter | 데이터 소스별 조회 실패 원인 |
| 6 + Enter | Network: Services / EndpointSlices / Ingresses / NetworkPolicies |
| 7 + Enter | Configuration: ConfigMaps / Secrets / ResourceQuotas / LimitRanges |
| 8 + Enter | Storage: PVCs / PVs / StorageClasses / VolumeAttachments |
| 9 + Enter | Access/RBAC: ServiceAccounts / Roles / RoleBindings / ClusterRoles / ClusterRoleBindings |
| 0 + Enter | API Resources: 클러스터의 API/CRD 목록 검색 및 선택 조회 |
| r | 수동 새로고침 |
| c 또는 s + Enter | Settings / Profiles 관리 화면 |
| q | 종료 |
| 위/아래 또는 j/k | 소메뉴 선택, 목록에서는 행 선택, 상세/차트에서는 한 줄 스크롤 |
| Enter | 선택한 메뉴 진입 |
| Esc | 화면에서 메뉴 선택으로 복귀 |
| 좌/우 | Overview, Monitoring, Workloads, 카테고리 화면에서 탭 이동 (끝에서 처음으로 순환) |
| / | Workloads·카테고리·API 객체 목록 필터 입력, API 종류 목록에서는 검색 |
| x | 현재 목록 필터 또는 API Resources 검색 초기화 |
| 마우스 휠 | 기본 5줄씩 위 / 아래 이동 |
| u / d | 중앙 패널 한 페이지 위 / 아래 (PgUp / PgDn도 지원) |

처음에는 Overview를 표시하며 키보드 초점은 Overview 소메뉴에 있습니다. 숫자 키와 m/s는 해당 소메뉴를 바로 선택하는 단축키이며 Enter로 진입합니다. 페이지 키와 휠 스크롤은 화면 진입 후 동작합니다.

워크로드 탭 이름과 상태 집계는 상단에 고정됩니다. Pod 탭은 Total/Healthy/Issues, Running/Pending/Failed/Succeeded/Unknown, NotReady/CrashLoopBackOff/Terminating 및 ImagePullBackOff/ErrImagePull/CreateContainerConfigError 수를 표시합니다. Healthy는 Running이면서 현재 이슈가 없는 Pod입니다. phase 집계와 장애 원인 집계는 겹칠 수 있으므로 합산하지 않습니다. 예를 들어 init container가 CrashLoopBackOff인 Pending Pod는 Pending·Issues·CrashLoopBackOff에 각각 포함됩니다. 장애 원인은 Terminating을 우선하고, Succeeded는 정상 완료로 처리한 뒤 init container·일반 컨테이너 순서로 현재 waiting/실패 사유를 검사하고, PodInitializing/ContainerCreating 같은 대기 사유보다 실제 장애 사유를 우선하여 대표 원인 하나를 표시·집계합니다. 성공적으로 완료한 init container는 장애로 세지 않습니다. 컨트롤러 탭은 전체/정상/이슈 객체 수와 desired/ready replica 합계를 표시합니다. 집계 범위는 모든 namespace입니다. 조회 실패 시 N/A를 표시합니다. 아주 작은 화면에서는 상단 집계 일부가 생략될 수 있으므로 140열 x 35행 이상을 권장합니다.

휠 이동량은 `./kdash.sh --scroll-step 10`처럼 조절합니다. `j/k`와 방향키는 한 줄씩 유지합니다. 터미널이 마우스 이벤트를 전달해야 휠 이동이 동작합니다. 앱이 마우스 입력을 활성화하므로 터미널에 따라 텍스트 선택에는 Shift 키가 필요할 수 있습니다.

기본 30초마다 새 데이터를 수집합니다. 백그라운드 수집 중에는 이전 스냅샷과 메뉴를 계속 표시합니다. Snapshot은 수집 시작 시각이며 여러 API를 조회하므로 값들은 단일 원자적 스냅샷이 아닙니다. 종료 또는 파일 전환 시 진행 중인 조회가 timeout까지 마무리될 수 있습니다.

## Clusters / Nodes

`[Clusters] → Nodes`에서 현재 프로필의 노드를 표로 조회합니다. 목록에는 노드명·역할·Ready/Pressure/Cordoned 상태·내부 IP·Kubelet 버전·CPU/메모리 capacity·배정된 Pod 수를 표시합니다. Pod 수는 현재 보관 중인 객체 수로, 완료된 Pod도 포함합니다. `/`로 `name=worker status=Cordoned`처럼 복합 필터를 적용하고 `x`로 해제합니다.

`↑/↓` 또는 `j/k`로 노드를 선택하고 `Enter`로 상세를 엽니다. 좌우 키로 다음 탭을 전환합니다.

- **Status**: Ready 및 Pressure 조건·변경 시각·메시지, 스케줄링 가능 여부, 주소·Pod CIDR, capacity/allocatable(확장 자원 포함), CPU/메모리 실제 사용량, 루트 파일시스템 사용량, OS·커널·런타임·Kubelet 정보, taint·label, 배정된 Pod의 상태·Ready·재시작 횟수.
- **YAML**: 해당 노드의 원본 YAML.
- **Describe**: 해당 노드에 대한 `kubectl describe` 결과.

`r`은 현재 목록 또는 상세 탭을 갱신하고, `u/d`·휠은 목록 이동/상세 스크롤에 사용합니다. `Esc`는 상세 → 기존 목록 → 메뉴 순서로 돌아갑니다. 이 메뉴는 조회 전용입니다. 기존 Overview의 노드 자원 요약과 Monitoring 차트는 그대로 사용할 수 있습니다.

Status는 필요할 때 해당 노드만 조회합니다. CPU/메모리는 metrics.k8s.io, 디스크는 kubelet Summary API를 사용합니다. 값은 원본 Kubernetes quantity 또는 명시된 bytes 단위로 표시하며, 디스크는 루트 파일시스템만 포함합니다. 추가 조회가 거부되거나 수집원이 없으면 해당 값은 N/A와 오류 사유를 표시하고 나머지 노드 정보는 유지합니다. 전체 노드 목록은 기존 스냅샷을 재사용하며 상세에서는 노드·메트릭·배정 Pod·디스크의 실제 최신 조회 결과를 표시합니다.

## Clusters / Namespaces

`[Clusters] → Namespaces`에서 이름·Active/Terminating 상태·Pod 수·생성 후 경과 시간을 표로 조회합니다. Pod 수는 기존 전체 Pod 스냅샷 기준이며 완료된 Pod도 포함합니다. `/`로 `name=demo status=Active`처럼 복합 필터를 적용하고 `x`로 해제합니다. `↑/↓` 또는 `j/k`로 선택하고 `Enter`로 상세를 엽니다.

좌우 키로 **Overview → Resources → Topology → YAML → Describe** 탭을 전환합니다.

- **Overview:** 상태, UID, 생성/삭제 시각, spec·metadata finalizer, labels·annotations·conditions, 자원 종류별 개수, Pod phase별 개수, ResourceQuota의 hard/used와 LimitRange 제한.
- **Resources:** 선택 namespace에 존재하는 객체의 이름·종류·상태·생성 후 경과 시간을 자원 종류별 표로 표시합니다. `api-resources`로 발견한 namespaced API/CRD도 조회합니다. 빈 자원 종류는 생략하고, 조회 실패는 N/A 및 사유를 표시합니다.
- **Topology:** 워크로드 소유 관계, Ingress의 Service 참조, Service의 selector 후보 Pod 및 EndpointSlice 대상, Pod의 PVC/ConfigMap/Secret/ServiceAccount 참조와 NetworkPolicy가 선택하는 Pod를 ASCII 관계도로 표시합니다.
- **YAML / Describe:** 해당 namespace의 원본 YAML과 `kubectl describe` 결과.

토폴로지 예시(`--demo`의 합성 데이터):

```text
OWNERSHIP
+- Deployment/web
   +- ReplicaSet/web-abc
      +- Pod/web-abc-1 [Running; node=worker-01]

INGRESS / SERVICE / POD
+- Ingress/web
   +- example.invalid/ -> Service/web port=80
+- Service/web
   +- [selector candidate] Pod/web-abc-1 [Running; node=worker-01]

POD RESOURCE REFERENCES
+- Pod/web-abc-1 [Running; node=worker-01]
   +- ConfigMap/web-config
   +- PersistentVolumeClaim/web-data
   +- Secret/web-secret
   +- ServiceAccount/default
```

이 관계도는 **Kubernetes 설정과 참조 관계**입니다. 실제 트래픽 흐름이나 통신 허용 여부를 측정하지 않습니다. selector 후보에는 Ready가 아닌 Pod도 포함될 수 있으므로 EndpointSlice의 ready 상태를 함께 확인합니다. 소유 관계는 이름 대신 UID로 연결하며, UID가 바뀌었거나 조회할 수 없는 참조는 unavailable/unresolved로 표시합니다. selector 없는 Service에 모든 Pod를 연결하지 않습니다. ExternalName은 외부 DNS 이름을 표시합니다. 실제 트래픽 그래프는 Monitoring의 Service Traffic에서 확인합니다. [소유 관계](https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/), [Service](https://kubernetes.io/docs/concepts/services-networking/service/), [EndpointSlice](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/)

상세에 진입할 때 선택 namespace 범위에서 최대 6개 자원 종류를 병렬 조회하며, Overview·Resources·Topology는 같은 조회 결과를 공유합니다. 여러 API의 결과이므로 원자적인 스냅샷은 아닙니다. `r`은 현재 상세를 재조회하며 이 세 탭의 공유 결과를 함께 갱신합니다. YAML/Describe의 `r`은 해당 탭만 재조회합니다. `u/d`·휠·방향키로 스크롤하고 `Esc`는 상세 → 목록 → 메뉴 순서로 돌아갑니다. 토폴로지는 최대 2,500줄, 소유 관계 깊이는 12단계로 표시하며 제한에 도달하면 안내합니다. Resources 목록은 토폴로지 표시 제한과 별도로 유지합니다.

이 Clusters 메뉴는 조회 전용입니다. namespace 조회에는 namespaces get/list, 내부 자원에는 각 namespaced 리소스의 list 권한이 필요합니다. discovery나 일부 API의 권한이 없어도 공통 자원 조회와 다른 결과는 유지합니다. Secret/ConfigMap의 내용과 annotations는 목록·토폴로지 캐시에 남기지 않으며 참조에는 이름만 표시합니다. Secret list 권한 자체는 민감 정보를 읽을 수 있는 권한입니다. Prometheus는 필요하지 않습니다.

## Resources 상세 · 로그 · 운영 작업

Workloads, Network, Config, Storage, Access/RBAC와 API Resources 목록에서 `↑/↓` 또는 `j/k`로 행을 선택하고 `Enter`로 상세 화면을 엽니다. 필터가 적용된 목록의 선택 행을 사용하며 namespace가 다른 동명 리소스는 구분합니다. 자동 갱신 시 동일 객체를 계속 선택합니다.

| 상세 화면 키 | 동작 |
| --- | --- |
| 좌/우 | YAML → Describe → Logs 탭 전환. Logs는 지원하는 워크로드에서만 표시 |
| Esc | 상세 → 기존 목록. API Resources에서는 다음 Esc가 카탈로그로 이동 |
| u/d, 휠, ↑/↓ | 상세 내용 스크롤 |
| r | 현재 상세 탭 다시 조회 |
| p | Logs에서 현재/이전 종료 인스턴스 로그 전환 |
| e | 현재 객체의 YAML을 편집하고 재배포 |
| n | 현재 탭/API Resource의 YAML 작성·배포. 빈 목록에서도 사용 가능 |
| a | 지원되는 시작·정지·재시작·삭제 작업 메뉴 |
| ! | Pod/지원 워크로드에서 Pod·컨테이너를 선택해 exec |

YAML은 선택 객체를 `kubectl get -o yaml`로 새로 조회하므로 ConfigMap/Secret 내용도 포함됩니다. Describe는 `kubectl describe` 결과이며, 선택 객체가 없을 때 이름 접두사가 같은 다른 객체를 대신 조회하지 않습니다. RBAC 오류와 삭제된 객체의 NotFound는 상세 화면에 표시하고 `r`로 다시 조회할 수 있습니다. 조회 중에도 Esc로 빠져나올 수 있습니다.

Logs는 Pod 및 Deployment·ReplicaSet·StatefulSet, API Resources의 DaemonSet·Job에서 지원합니다. 모든 컨테이너의 최근 **200줄, 컨테이너당 최대 256 KiB**를 조회하고 Pod/컨테이너 이름과 타임스탬프를 붙입니다. 컨트롤러는 해당 selector와 일치하는 Pod들의 로그를 조회합니다. 실시간 스트리밍이 아닌 스냅샷이며 `r`로 갱신합니다. 이전 로그가 없거나 일부 컨테이너가 대기 중이면 오류와 이미 받은 로그를 함께 표시합니다.

### 시작·정지·재시작·삭제

- Deployment·StatefulSet·ReplicaSet: 시작은 입력한 양수 replica 수로 scale, 정지는 replica 0으로 scale합니다. 이전 replica 수를 자동 복원하는 방식은 아닙니다. autoscaler/operator가 이후 replica 수를 다시 변경할 수 있습니다.
- Deployment·StatefulSet·DaemonSet: Pod 템플릿의 restartedAt annotation을 변경하는 rolling restart를 지원합니다. 실제 Pod 교체는 컨트롤러의 업데이트 전략에 따릅니다.
- 조회 가능한 객체: 일반 삭제를 지원합니다. 강제 삭제 옵션은 사용하지 않으며, 컨트롤러가 관리하는 Pod는 다시 생성될 수 있습니다.

현재 프로필·namespace·대상·실행 명령을 먼저 보여주고 `start`, `stop`, `restart`, `delete`를 직접 입력해야 실행합니다. 단순 Enter나 Esc는 취소입니다. 선택 이후 객체 교체나 동시 수정은 서버에서 사전 조건으로 거부합니다. 삭제는 UID/resourceVersion을 가진 DeleteOptions, 재시작은 UID/resourceVersion test를 포함한 JSON Patch, scale은 현재 replicas와 resourceVersion 조건을 사용합니다. 충돌 시 새 목록에서 다시 선택합니다. 삭제 요청은 기본 유예 기간과 background 전파 정책을 사용하며 완료를 기다리지 않습니다. 재시작에는 리소스 patch 권한, scale에는 scale 조회·update 권한이 필요합니다. [scale 조건](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_scale/), [삭제 조건](https://kubernetes.io/docs/reference/kubernetes-api/definitions/preconditions-v1-meta/)

### YAML 작성·수정 후 배포

`n`은 현재 메뉴·탭의 리소스를 바로 생성합니다. Workloads > Pods에서는 Pod, Network > Services에서는 Service 템플릿이 열립니다. API Resources에서는 카탈로그에서 선택한 타입 또는 진입한 목록의 타입을 사용합니다. 목록이 비어 있거나 필터 결과가 없어도 생성할 수 있으며, 기존 객체를 선택할 필요가 없습니다.

선택 프로필의 [Kubernetes Discovery API](https://kubernetes.io/docs/concepts/overview/kubernetes-api/)에서 Kind·API 버전·namespace 범위·create 지원 여부를 확인합니다. namespace는 선택 행의 namespace를 기본값으로 제안하고, 선택 행이 없으면 default를 제안합니다. 클러스터 범위 리소스는 namespace를 묻지 않습니다. 생성 화면에는 기존 행의 객체명 대신 생성할 리소스 타입을 표시합니다.

Pod, Deployment, ReplicaSet, StatefulSet, DaemonSet, Service, ConfigMap, Secret, PVC, Job, Namespace, Ingress 기본 템플릿을 제공합니다. 그 외 API/CRD는 실제 apiVersion·kind·metadata를 넣은 최소 템플릿을 제공하고 `kubectl explain` 명령을 안내합니다. 해당 리소스의 필수 필드를 편집기에서 채워야 합니다. 이미지·selector·호스트·StorageClass, StatefulSet의 headless Service 등은 클러스터에 맞게 조정합니다. 서버 dry-run에서 선택한 타입·이름·namespace를 유지하는지 확인한 뒤 배포합니다.

편집기는 `$VISUAL`, `$EDITOR`, `vi` 순으로 사용합니다. 예: `EDITOR='code --wait' ./kdash.sh`. 편집기가 파일 저장까지 기다리도록 설정해야 합니다. vi에서는 `i`로 수정하고 `Esc` 후 `:wq`로 저장·종료합니다.

초안은 `.kdash/drafts/<식별자>.yaml`에 권한 600으로 저장하며, 성공·취소·오류 후에도 남겨 둡니다. Secret 편집 초안에는 민감한 값이 포함될 수 있습니다. 기존 객체를 편집할 때 status·managedFields 등 서버 관리 필드는 제거하고 resourceVersion은 동시 수정 충돌 감지를 위해 유지합니다.

저장 후 YAML 전문과 서버 dry-run 결과를 표시합니다. 기존 객체 수정은 diff도 표시하며 이름·namespace·kind·apiVersion 변경을 막습니다. 신규는 `create`, 수정은 `apply`를 직접 입력해야 실제 배포합니다. 신규는 이미 존재하는 객체를 덮어쓰지 않습니다. 미리보기·서버 dry-run·diff·최종 제출은 메모리에 보관한 동일한 YAML 내용을 stdin으로 전달합니다. 승인 대기 중 보관용 초안 파일을 외부에서 바꿔도 제출 내용은 바뀌지 않습니다. 변경한 초안을 반영하려면 `e`로 다시 편집하여 검증·승인을 반복합니다. 검증에 실패하면 `e`로 다시 편집할 수 있습니다.

### Exec

Pod에서는 컨테이너를, 워크로드에서는 selector와 일치하는 Running Pod와 실행 중인 컨테이너를 선택합니다. 기본 명령은 `/bin/sh`이며 직접 다른 명령을 입력할 수 있습니다. 로컬 shell을 거치지 않고 인자를 전달하므로 파이프 등은 `sh -c "..."` 형태로 입력합니다. 대상과 명령을 확인하고 `exec`를 입력하면 터미널을 연결합니다. 컨테이너에 shell이 없는 이미지에서는 해당 이미지에 존재하는 명령을 지정해야 합니다. `exit`로 종료하면 대시보드로 돌아갑니다.

권한은 기존 kubeconfig를 그대로 사용합니다. 로그는 `pods/log` 조회, exec는 `pods/exec` 생성 권한이 필요합니다. 변경 작업에는 해당 리소스의 create/patch/update/delete 및 scale 서브리소스 권한 등이 필요합니다. `--demo`에서는 조회 예제만 제공하고 변경·exec는 실행하지 않습니다.

## 이벤트 목록과 상세 보기

`Event → Warnings`는 최근 발생 시간, namespace, 대상 객체, 사유, 횟수, 메시지를 표로 표시합니다. `Event → Errors`는 Kubernetes/API 리소스/디스커버리 조회 오류의 출처와 메시지를 표시합니다. Errors는 기존처럼 대시보드 수집 오류 목록입니다.

목록의 각 항목은 한 줄이며 긴 값은 `...`로 줄입니다. 좁은 터미널에서는 Warnings의 일부 열을 숨기며 상세 화면에서 모두 확인할 수 있습니다. 표 머리글은 스크롤해도 상단에 유지됩니다.

- `↑/↓` 또는 `j/k`: 행 선택. 선택 행은 `>`와 반전 색으로 표시합니다.
- `Enter`: 선택한 행의 상세 내용. 메시지는 생략 없이 줄바꿈하며 Warning의 첫 탭은 원본 **Event JSON**입니다.
- `←/→`: **Event JSON → Describe → YAML** 탭 전환. Describe/YAML은 이벤트가 참조하는 대상 리소스를 조회합니다. `involvedObject`와 `regarding`을 지원하며 `fieldPath`가 특정 컨테이너를 가리켜도 대상 Pod 전체를 조회합니다.
- `e`: 이벤트 대상의 YAML을 편집합니다. 기존 편집기·서버 dry-run·diff·`apply` 입력 확인 절차를 그대로 사용합니다. Kubernetes 권한과 리소스의 수정 가능 필드 제한이 적용됩니다.
- `Esc`: 상세 → 목록 → 메뉴 순서로 돌아갑니다.
- `u/d`: 목록에서 한 페이지 분량의 행 이동, 상세에서 페이지 스크롤. 휠도 목록에서는 행을 이동하고 상세에서는 내용을 스크롤합니다.
- `r`: Describe/YAML에서는 대상 리소스를 다시 조회합니다. Event JSON/Error Details에서는 목록을 갱신하고 열어 둔 원본 상세는 유지합니다.

자동 갱신 시 동일한 이벤트의 선택을 유지합니다. Event JSON은 진입 시점의 스냅샷으로 고정되며 새 이벤트 값은 목록으로 돌아가 다시 열면 확인할 수 있습니다. Describe/YAML은 조회 시점의 대상 리소스 정보입니다. `Esc`로 돌아가도 기존 목록의 선택 행을 유지합니다. 빈 목록과 조회 불가는 별도로 표시합니다.

대상은 이벤트에 기록된 `apiVersion`, `kind`, 이름, UID로 식별하고 API discovery에서 실제 리소스 이름과 namespace 범위를 확인합니다. CRD와 클러스터 범위 객체도 지원합니다. 이벤트의 resourceVersion은 과거 값일 수 있으므로 현재 객체의 UID로 동일성을 검증합니다. YAML은 검증한 GET 결과를 변환하고, Describe는 조회 전후 UID를 확인합니다. 삭제·권한 거부·같은 이름의 객체 교체가 확인되면 대상 조회/편집을 중단하고 원본 이벤트를 유지합니다. 현재 객체를 다루려면 Resources 목록에서 다시 선택합니다.

**Errors는 KDASH의 수집·렌더링·discovery 오류입니다.** 오류 문자열에서 대상 이름을 추측하지 않습니다. 대상 ObjectReference가 명시된 오류는 `Error Details / Describe / YAML`을 제공하고, 대상 참조나 UID 등 식별 정보가 없으면 원본 오류 상세만 표시합니다. 대상 조회에는 해당 객체의 get 권한, 편집에는 기존 YAML 편집에 필요한 권한이 적용됩니다. `--demo`에서는 합성 조회 화면을 제공하고 편집·배포는 실행하지 않습니다.

## 수치의 의미

- **Total / Allocatable**: Node status의 capacity와 allocatable. CPU는 cores, 메모리/디스크는 GiB입니다.
- **Request**: 해당 노드에 배정된 비종료 Pod의 요청량 합계. 일반 컨테이너, init container, restartable init sidecar, Pod overhead와 Pod-level requests를 반영합니다. 아직 노드가 정해지지 않은 Pending Pod는 노드 요청 합계에서 제외합니다.
- **CPU / Memory Used**: `metrics.k8s.io/v1beta1/nodes` 사용량. CPU는 metrics-server 수집 구간 값이며 메모리는 Metrics API 값입니다. metrics-server가 없거나 값이 누락되면 N/A입니다.
- **Disk**: Total/Allocatable은 ephemeral-storage이고 Request는 ephemeral-storage 요청 합계입니다. Used는 kubelet Summary API의 node.fs.usedBytes이며 사용률은 같은 node.fs.capacityBytes를 분모로 계산합니다. node rootfs 용량을 별도로 표시합니다. PVC나 여러 디스크의 총 용량을 의미하지 않으며 imagefs가 별도인 경우 해당 사용량은 포함하지 않습니다.
- **Pod Issues**: Pending/Failed/Unknown, Terminating, Running이지만 NotReady, waiting 상태 또는 실패한 컨테이너가 있는 Pod 수입니다. Pending은 Issues에도 포함됩니다. 누적 restart 횟수는 상세 목록에 표시합니다.
- **Controller Issues**: desired 대비 Ready 부족, 아직 반영되지 않은 generation, Deployment rollout/availability 문제, StatefulSet RollingUpdate 지연입니다. Deployment가 소유한 ReplicaSet은 각각 집계되므로 서로 합산해 고유 장애 수로 해석하지 않습니다.
- **Warning events**: Kubernetes에서 현재 보관 중인 Warning 이벤트 객체 수와 그 객체의 누적 발생 횟수 합계입니다. Prometheus/Alertmanager 알람이나 전체 과거 알람 기록은 아닙니다. 이미 해결된 문제의 이벤트도 보관 기간 동안 남을 수 있습니다.
- **N/A**: 조회 실패 또는 수집되지 않은 값. 권한 부족을 0으로 표시하지 않습니다. Errors 메뉴에서 실패 원인을 확인할 수 있습니다. Nodes API 상태는 노드 목록 조회 성공 여부이며 전체 클러스터 건강도를 의미하지 않습니다.

## 필요한 조회 권한

- core API: nodes 조회, 모든 namespace의 pods/events 목록
- apps API: 모든 namespace의 deployments/replicasets/statefulsets 목록
- metrics.k8s.io API: nodes 목록
- 디스크 통계: `/api/v1/nodes/<node>/proxy/stats/summary` 조회 (`nodes/proxy` get 권한)

조회 권한을 자동으로 설치하거나 변경하지 않습니다. 디스크 조회가 거부되면 나머지 정보는 계속 표시합니다. `nodes/proxy` 권한은 범위가 넓으므로 기존 운영 정책에 맞는 계정으로 실행하세요.

`.kube/*.conf`는 gitignore에 포함됩니다. 기본 설정 파일이나 인증 정보는 제공하지 않습니다. 실제 클러스터 연결 검증에는 사용자의 kubeconfig가 필요합니다.

지표 참고: [Node metrics](https://kubernetes.io/docs/reference/instrumentation/node-metrics/), [Resource requests](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/), [Sidecar requests](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/).

## 개발 검증

```sh
python3 -m unittest discover -v
```

테스트는 데모·모의 kubectl 응답으로 검증하며 실제 클러스터에 생성·수정·삭제 명령을 보내지 않습니다. OS별 실제 터미널 동작과 클러스터 권한은 실행 환경에서 별도로 확인합니다.


## 목록 검색 필터

Workloads, Network, Configuration, Storage, Access/RBAC 또는 API Resources에서 선택한 객체 목록에 Enter로 진입하고 `/`를 누릅니다. 화면 맨 아래 `Filter>`에 입력한 뒤 Enter로 적용합니다. 예:

```text
name=web ns=kubeflow status=running
namespace=monitoring status=CrashLoopBackOff
status=issues
status=healthy
name=pipeline
status="Ready 0/2"
```

이름/namespace/상태는 대소문자 구분 없이 부분 문자열로 검색하며 조건은 AND로 결합합니다. `namespace`는 `ns`, `state`는 `status`의 별칭입니다. 필드명이 없는 입력은 이름 검색입니다. 예를 들어 `Running`만 입력하면 이름을 검색하므로 상태 검색은 `status=Running`으로 입력합니다. Workloads의 status는 Pod phase, 현재 장애 사유 및 `issues`/`healthy`/`ok`를 대상으로 합니다. Healthy는 Running이면서 현재 이슈가 없는 Pod이며 정상 컨트롤러도 Healthy에 해당합니다. `ok`는 이슈가 없다는 뜻으로 Succeeded Pod도 포함합니다. 컨트롤러의 Ready 부족 사유처럼 공백이 있는 검색어는 따옴표로 감쌉니다.

Enter로 적용, Esc로 취소, Backspace로 삭제, Ctrl+U로 입력 전체 삭제합니다. 입력 중 q/c/숫자 등은 검색어로 처리합니다. 필터는 탭별로 유지되며 `x`로 현재 탭 필터를 초기화합니다. `Matches / Total`은 검색 결과 수와 해당 리소스 전체 수이고, 그 아래 상태 집계는 검색 결과 기준입니다. 필터는 클러스터 전체 조회 결과에 로컬에서 적용하며 서버의 RBAC 범위를 바꾸지 않습니다.

카테고리와 API 객체 목록도 `Filter` 및 `Matches / Total`을 표시합니다. 같은 리소스의 필터는 카테고리/API 화면 사이에서도 유지되며, API 종류 검색과 객체 목록 필터는 별도로 저장합니다. 일반 리소스의 status는 API가 제공한 phase/state/reason/conditions 및 연결 상태를 검색합니다(예: `status=Bound`, `status=Ready=False`, `status=Attached=False`). 상태 필드가 없는 리소스는 이름과 namespace로 검색하세요. 클러스터 범위 리소스에는 namespace가 없으므로 `ns=...` 조건을 적용하면 결과가 없습니다.

## 운영 리소스 카테고리

네트워크는 Service IP/포트/selector, EndpointSlice의 ready endpoint 수와 주소, Ingress의 클래스/호스트/주소/TLS 개수, NetworkPolicy의 Pod selector/정책 종류/규칙 개수를 표시합니다. 네트워크 정책 규칙 수나 Service 객체 존재 여부는 실제 통신 성공을 의미하지 않습니다.

설정은 ConfigMap/Secret 키 개수와 종류, ResourceQuota의 hard/used, LimitRange 제한을 표시합니다. Secret/ConfigMap 값과 원문이 들어갈 수 있는 annotations·managedFields는 목록 캐시에 보관하지 않습니다. API Resources에서 조회해도 동일하게 처리합니다. 목록에는 이름·namespace·UID·resourceVersion·생성/삭제 시간 등의 필요한 메타데이터와 키 개수를 남깁니다. API 응답 자체에는 민감 값이 포함될 수 있으므로 list 권한은 여전히 민감한 조회 권한입니다. 행을 선택해 YAML 상세를 열거나 편집하면 실제 값을 조회합니다. 목록 조회에는 해당 리소스의 list 권한이 필요합니다.

스토리지는 PVC/PV 상태/용량/접근 모드/StorageClass, StorageClass의 provisioner와 reclaim/binding 정책, VolumeAttachment의 노드/PV/연결 상태/오류를 표시합니다. 접근 권한은 ServiceAccount, Role/ClusterRole 규칙 수, RoleBinding/ClusterRoleBinding의 roleRef와 subjects를 표시합니다.

각 카테고리의 선택한 탭만 백그라운드에서 조회합니다. 최초 진입 시 Loading, 실패 시 N/A와 오류를 표시합니다. 다른 탭 전환과 키보드 탐색은 조회 중에도 가능합니다. 이미 조회한 탭은 마지막 결과를 표시하며 선택된 탭은 기본 새로고침 주기에 다시 조회합니다. `r`로 현재 탭을 다시 조회할 수 있습니다. 모든 namespace를 조회하므로 namespace별 접근만 허용된 계정은 Forbidden이 발생할 수 있습니다.

## API Resources 탐색

`0` 선택 후 Enter로 진입하면 아래 두 discovery 조회 결과를 합쳐 목록을 만듭니다.

```sh
kubectl api-resources --verbs=list --namespaced=true --cached=false -o name
kubectl api-resources --verbs=list --namespaced=false --cached=false -o name
```

선택한 kubeconfig/context를 그대로 사용합니다. list를 지원하는 실제 API와 CRD를 표시하며, discovery에서 제공된다는 사실이 현재 계정의 list 권한을 보장하지는 않습니다. discovery 일부가 실패하면 나머지 목록과 오류를 함께 표시합니다.

`/`로 리소스 이름/API group을 검색하고 위/아래 또는 j/k로 선택한 뒤 Enter로 객체 목록을 조회합니다. namespace 범위 리소스에는 `-A`를 적용하고, cluster 범위에는 적용하지 않습니다. Esc 한 번은 API 리소스 목록으로 돌아가고, 그 상태에서 Esc를 누르면 네비게이션으로 돌아갑니다. `r`은 discovery와 현재 선택한 리소스를 새로 조회합니다.

카테고리에 있는 리소스는 해당 전용 열로 표시하고 그 외 CRD 등은 namespace/name/kind/apiVersion/status/age로 표시합니다. CRD의 임의 spec 필드는 목록 열로 해석하지 않습니다. 행을 선택하고 Enter로 YAML·Describe 상세를 볼 수 있으며, `n`으로 선택한 API Resource의 YAML을 작성할 수 있습니다.

참고: [kubectl api-resources](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_api-resources/), [kubectl get](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_get/).

## Prometheus Monitoring

저장된 프로필의 Prometheus 연결로 실제 시계열을 조회합니다. `Dashboard → Monitoring`에 들어가면 선택한 탭만 백그라운드에서 조회하며 기본 5초마다 갱신합니다. 기존 Kubernetes 객체 조회는 기본 30초 주기를 유지합니다. 외부 Python 패키지는 필요하지 않습니다.

| 탭 | 표시 내용 | 필터 예시 |
| --- | --- | --- |
| Cluster & Nodes | 합산 CPU cores·메모리 GiB, 디스크 읽기/쓰기·IOPS, 호스트 RX/TX와 노드별 표 | `node=k8s-w1` |
| Namespace Capacity | CPU·메모리 사용량, 선언된 requests/limits와 namespace별 표 | `ns=kubeflow` |
| Service Traffic | Istio destination 요청률·HTTP 5xx. 해당 지표가 없으면 namespace별 Pod RX/TX와 서비스 지표 부재 안내 | `ns=default service=web` |
| Workload Health | CPU·메모리, CFS throttling 비율, 5분간 재시작 증가, Pending·Failed·마지막 OOM 상태 및 상위 Pod 표 | `ns=kubeflow name=pipeline` |

- `t`: 조회 구간 5분 → 15분 → 1시간. `p`: 자동 조회 일시정지/재개. 진행 중인 조회는 완료될 수 있습니다.
- `r`: 즉시 갱신. 일시정지 중에도 수동 조회합니다. `u/d`, 마우스 휠: 아래의 차트·상위 20개 표 스크롤.
- `/`: 탭별 복합 필터 입력, `x`: 초기화. `cluster`는 대소문자를 구분하는 **정확 일치**입니다. 예: `cluster=prod`는 `prod-east`에 매칭되지 않습니다. 나머지 필터는 정규식이 아닌 부분 문자열입니다. node 필터는 노드명/IP에서 검색하며 나머지 필터는 대소문자를 구분합니다. 특정 노드/namespace를 선택하면 그래프도 해당 범위로 좁혀집니다.
- `./kdash.sh --metrics-interval 2`: 조회 완료 후 2초 간격으로 다시 조회. 차트 해상도는 5분=5초, 15분=15초, 1시간=60초이며 실제 원본 정밀도는 Prometheus scrape 주기에 좌우됩니다. CPU·I/O 등의 rate는 직전 2분 평균입니다.

차트와 지표별 표는 터미널 폭에 맞춰 여러 열로 자동 배치합니다. 넓어지면 한 줄에 더 많이 표시하고, 좁아지면 다음 줄로 넘깁니다. 표는 배정된 칸의 너비를 채우며, 긴 Pod 이름이 있는 Workload Health 표는 더 넓은 최소 폭을 사용합니다. 창 크기를 바꾸면 즉시 재배치합니다.

차트는 ASCII `*`로 표시하며 Y축은 해당 조회 구간의 최소/최대에 맞춥니다. 각 카드의 최신값과 표 단위는 동일합니다. 데이터가 없거나 NaN이면 `N/A`, 조회 실패는 `ERROR`, 가장 최신인 원본 샘플도 120초 이상 오래됐으면 `STALE`로 표시합니다. `Newest sample age`는 해당 지표에서 가장 최신인 원본 샘플의 나이로, 모든 대상의 정상 수집을 보장하지 않습니다. 부분적으로 사라진 대상은 합산에서 빠질 수 있습니다. 구간의 샘플 누락은 0으로 채우지 않습니다.

집계 범위는 **선택한 프로필에 저장된 Prometheus endpoint**입니다. 서로 다른 클러스터가 같은 endpoint를 공유하면 `cluster=<라벨>`로 범위를 좁혀야 합니다. 차트에 사용하는 모든 지표 수집원과 연산 피연산자를 조회 기간 및 rate/increase의 추가 5분 구간에서 검사합니다. 서로 다른 라벨이 발견되면 집계를 멈추고 정확한 필터를 안내합니다. 단일 라벨을 자동 감지한 경우에도 실제 차트 쿼리를 그 라벨에 고정하여 조회 중 다른 클러스터가 들어오지 않도록 합니다. exporter마다 라벨이 다르면 수집 설정을 통일해야 합니다. 지정한 cluster 라벨이 없는 지표는 혼합하지 않고 N/A로 남습니다. cluster 라벨이 없는 서로 다른 클러스터의 데이터는 CLI가 구분할 수 없으므로 전용 endpoint를 사용하거나 Prometheus 수집 설정에 식별 가능한 cluster 라벨을 추가해야 합니다.

node-exporter는 노드 지표, kubelet/cAdvisor는 컨테이너 지표, kube-state-metrics는 요청량·상태 지표를 제공합니다. CPU는 사용 cores, 노드 메모리는 MemTotal-MemAvailable, 컨테이너 메모리는 working set입니다. 디스크 I/O는 sd/vd/xvd/nvme/mmc 물리 장치만 포함하며 파티션과 dm 장치를 제외합니다. 호스트 네트워크는 loopback·일반적인 Pod 가상 인터페이스를 제외하지만 bond와 하위 NIC의 중복은 구성에 따라 발생할 수 있습니다.

Namespace requests/limits는 일반 컨테이너의 선언값이며 init/overhead를 포함한 스케줄러 유효 요청량이나 ResourceQuota 사용량이 아닙니다. CFS throttling은 throttled periods / all periods의 가중 비율입니다. OOM은 마지막 종료 원인이 OOMKilled인 컨테이너 수이며 새로 발생한 이벤트 수가 아닙니다.

Service별 관측에는 추가 수집원이 필요합니다. 현재 자동 지원하는 서비스 수집원은 `istio_requests_total`의 destination reporter입니다. 없을 때 표시하는 Pod 네트워크는 Service별 트래픽이 아니며 hostNetwork Pod는 호스트 트래픽과 겹칠 수 있습니다. 이 상태에서 `service=` 필터를 지정하면 서비스 데이터 부재를 표시합니다. Ingress/Hubble 어댑터, p95 latency, ResourceQuota 차트는 아직 지원하지 않습니다.

조회에는 프로필의 인증 환경변수·사용자 CA·URL prefix 설정을 그대로 사용합니다. 리디렉션을 따라가지 않으며 프로필 변경 시 조회기와 캐시를 새로 만듭니다. 탭·필터·기간별 캐시는 최근 12개까지 유지합니다. `--demo`는 Prometheus에 연결하지 않습니다.

참고: [Prometheus 조회 API](https://prometheus.io/docs/prometheus/latest/querying/api/), [cAdvisor 지표](https://github.com/google/cadvisor/blob/master/docs/storage/prometheus.md), [Istio 지표](https://istio.io/latest/docs/reference/config/metrics/).

## 클러스터 프로필과 Prometheus 설정

프로필은 `.kdash/profiles.json`에 관리합니다. 예시 구조:

```json
{
  "version": 1,
  "profiles": [
    {
      "id": "example-profile-id",
      "alias": "production",
      "kubeconfig": ".kube/production-ab12cd34.conf",
      "context": "production-admin",
      "prometheus": {
        "endpoint": "https://prometheus.example.com",
        "token_env": "KDASH_PROD_PROM_TOKEN",
        "username": "",
        "password_env": "",
        "ca_file": "",
        "verified_at": null
      }
    }
  ]
}
```

`.kdash/`는 gitignore 대상입니다. JSON과 가져온 kubeconfig는 파일 권한 600으로 저장하며 JSON은 임시 파일을 통한 교체로 저장합니다. 여러 실행에서 동시에 설정을 수정한 경우 뒤의 저장을 거부해 앞의 변경을 보존합니다. 잘못된 JSON은 자동 초기화하지 않고 오류를 알립니다.

Profiles 목록에는 별명과 `CONF FILE`(conf 파일명)을 표시합니다. context는 파일명이 아니라 conf 안의 클러스터·계정·기본 namespace를 묶은 접속 설정 이름입니다. 접속 설정이 하나이면 자동으로 사용하고, 여러 개이며 기본값이 없으면 클러스터·계정·API 주소를 보고 번호를 선택합니다.

Profiles 화면에서 `a`는 파일 경로로 등록, `v`는 경로와 연결 설정 상세 조회, `e`는 별명 변경, `p`는 Prometheus 설정, `d`는 등록 삭제입니다. 번호 입력은 해당 프로필로 연결합니다. Enter는 현재 프로필로 돌아가며 현재 프로필이 삭제됐다면 첫 프로필을 선택합니다. `q`는 종료합니다. `e`는 접속 설정을 묻거나 클러스터에 조회하지 않습니다. 같은 conf 안의 다른 접속 설정을 선택하려면 고급 옵션 `k`를 사용합니다. 접속 설정을 바꾸면 기존 Prometheus 연결은 해제됩니다. 프로필 목록의 `*`는 현재 선택을 뜻합니다. 선택한 프로필은 현재 실행에만 적용되며 다음 실행은 다시 첫 프로필입니다. `--profile <alias 또는 ID>`로 시작 프로필을 지정할 수 있습니다.

삭제는 JSON 등록만 제거하고 kubeconfig 파일은 보존합니다. 초기 파일 자동 등록은 JSON이 없을 때 한 번만 수행하므로 삭제한 프로필이 다음 실행에서 다시 나타나지 않습니다. 나중에 `.kube`에 추가한 파일은 `a`에서 해당 경로를 입력해 등록합니다. 가져오기는 새 파일명으로 저장하므로 기존 파일을 덮어쓰지 않습니다.

Prometheus 설정에서는 다음을 지원합니다.

- 설정 진입 즉시 선택 프로필의 kubeconfig/context로 Service와 Node를 조회합니다. TCP NodePort/LoadBalancer Service와 이름·라벨에 prometheus가 포함된 Service의 포트를 목록으로 표시합니다. 먼저 Service/포트 번호를 선택하고, 주소가 여러 개이면 노드 또는 LoadBalancer 주소 번호를 선택합니다. Service port / targetPort / NodePort를 구분해 보여주며 NodePort 연결에는 노드 주소와 NodePort를 사용합니다. HTTPS 포트 이름은 HTTPS URL의 초깃값으로 사용합니다. `r`로 목록을 다시 조회합니다.
- `u`: NodePort/LoadBalancer/Ingress/직접 URL 입력. 필요한 경우 base path도 포함합니다.
- `j`: 별도의 Prometheus JSON 파일 경로로 설정 가져오기. 파일 내용은 위 예시의 `prometheus` 객체만 사용하며 검증 후 프로필 JSON에 복사 저장합니다. 이후 원본 JSON 변경을 자동 추적하지 않습니다.
- `t`: 저장된 설정으로 다시 연결 확인.
- `x`: 해당 프로필의 Prometheus 연결 설정 해제.

연결 주소는 선택 결과로 자동 생성합니다. Enter로 API 연결 확인 후 저장하며, 기본 흐름에서는 IP/URL·인증·CA 파일을 입력하지 않습니다. 인증이 필요한 경우에만 `a`에서 고급 인증을 설정하고, `h`로 HTTP/HTTPS를 전환합니다. 새 주소로 바꾸면 기존 주소의 인증 정보를 자동 전달하지 않습니다. 입력 중 Esc는 Enter 없이 즉시 취소하며 Backspace와 Ctrl+U도 지원합니다. 직접 URL 입력은 `u`의 보조 기능이며 scheme과 port를 포함한 완전한 URL(예: `http://10.10.12.135:30900`)을 입력합니다. 잘못된 URL은 인증 단계로 진행하지 않고 Service 목록으로 돌아갑니다. ClusterIP 주소는 일반적으로 클러스터 내부용이므로 KDASH 실행 컴퓨터에서의 라우팅 여부에 따라 연결되지 않을 수 있습니다. Service 조회 권한이 없어도 URL 직접 입력은 가능합니다. 이 기능은 Service를 생성하거나 NodePort로 변경하지 않습니다.

연결 테스트는 `/api/v1/query?query=vector(1)` 응답 형식을 확인하고 성공한 설정만 저장합니다. URL 리다이렉트는 자동으로 따라가지 않으므로 최종 API base URL을 입력합니다. 이 테스트는 API 접근 확인이며 노드/컨테이너 지표 수집 여부나 대상 클러스터 일치 여부를 보증하지 않습니다. Monitoring은 지표별 수집 상태와 cluster 라벨을 추가로 확인합니다.

인증은 없음, Bearer 토큰 환경변수, Basic 인증 username+password 환경변수를 지원합니다. 토큰/비밀번호 값은 JSON에 저장하지 않습니다. 필요한 환경변수를 설정한 셸에서 실행하세요. HTTPS는 기본 인증서 검증을 사용하며 사설 CA 파일을 지정할 수 있습니다. 인증서 검증 우회 옵션은 제공하지 않습니다.

공식 동작 참고: [Prometheus 조회 API](https://prometheus.io/docs/prometheus/latest/querying/api/), [Kubernetes Service 유형](https://kubernetes.io/docs/concepts/services-networking/service/).
