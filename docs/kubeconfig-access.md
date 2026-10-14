# KDASH용 kubeconfig 발급과 Kubernetes 권한

[README의 kubeconfig와 프로필 관리로 돌아가기](../README.md#kubeconfig와-프로필-관리)

## 먼저 알아둘 것

**프로필 등록에 슈퍼 관리자 권한이 필수인 것은 아닙니다.** 등록은 kubeconfig 파일을 읽고 접속 설정을 저장하는 로컬 작업입니다. 이후 화면 조회와 변경 작업은 kubeconfig에 들어 있는 사용자 또는 ServiceAccount의 Kubernetes 권한으로 수행합니다. 프로필을 등록하거나 alias를 바꿔도 권한이 추가되지 않습니다.

전체 클러스터 조회에는 클러스터 범위의 권한이 필요합니다. 전체 namespace에서 생성·수정·삭제·exec·RBAC 관리까지 수행하려면 폭넓은 운영 권한이 필요하며, `cluster-admin`은 이를 제공하는 선택지입니다. 계정과 RBAC를 처음 발급하는 담당자는 해당 계정 생성·권한 부여 권한이 있어야 합니다. 이미 허용된 범위를 넘어서는 Role이나 Binding은 일반 계정으로 만들 수 없습니다. [Kubernetes RBAC 공식 문서](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)

이 문서의 예시는 **클러스터 관리자가 직접 실행할 발급 절차**입니다. KDASH가 자동으로 권한을 부여하지 않으며, 아래 명령도 문서 작성 과정에서 실제 클러스터에 실행하지 않았습니다. 이름은 예시이므로 기존 계정·Binding과 겹치지 않게 정합니다.

## 1. 어떤 계정을 사용할지 결정하기

| 사용 목적 | 권한 구성 | KDASH 동작 |
| --- | --- | --- |
| 프로필 등록 | 유효한 kubeconfig 파일 | 등록 가능. API 접근/권한은 실제 조회 시 확인 |
| 클러스터 조회 | 명시한 리소스의 get/list + 클러스터 범위 Binding | Overview, Nodes, Namespaces, 리소스 목록·YAML·Describe |
| 로그 조회 | pods/log get + Pod/컨트롤러 조회 | 지원되는 워크로드의 로그 확인 |
| 노드 CPU/메모리 | metrics.k8s.io의 nodes get/list | Metrics API가 설치되어 있어야 사용량 표시 |
| 노드 디스크 사용량 | nodes/proxy get | 현재 KDASH는 kubelet Summary API 사용. 고위험 권한이므로 별도 검토 |
| 특정 namespace 운영 | 해당 namespace에 생성·수정·삭제·scale·exec 권한 추가 | 허용된 namespace의 작업 가능 |
| 전체 클러스터 운영 | cluster-admin을 ClusterRoleBinding으로 연결 | 전체 리소스 운영. admission 정책 등 별도 통제는 계속 적용 |
| Prometheus 차트 | Prometheus endpoint 접근·별도 인증 | Kubernetes RBAC만으로 연결되지 않음 |

현재 KDASH는 Pod·워크로드 목록을 모든 namespace에서 조회합니다. namespace 한 곳의 RoleBinding만 가진 계정은 `-A` 조회가 Forbidden일 수 있습니다. 권한이 없는 기능은 N/A 또는 Errors로 표시되며 앱을 통해 권한을 우회하지 않습니다. API Resources에 타입이 보이는 것과 해당 타입의 조회·생성 권한은 별개입니다.

### cluster-admin과 슈퍼 관리자는 어떻게 다른가?

- `ClusterRole`은 권한의 정의, `ClusterRoleBinding`은 그 권한을 클러스터 전체에서 계정에 연결하는 객체입니다. Role 이름만 있다고 권한이 생기는 것은 아닙니다.
- 기본 `cluster-admin`을 ClusterRoleBinding으로 연결하면 모든 namespace와 클러스터 리소스를 관리할 수 있습니다.
- 인증서의 `system:masters` 그룹은 RBAC 검사를 우회하는 비상용 슈퍼 관리자입니다. 일반 운영 계정에 이 그룹을 넣지 않습니다. Binding 삭제만으로 해당 인증서의 권한을 회수할 수 없습니다. [RBAC 운영 지침](https://kubernetes.io/docs/concepts/security/rbac-good-practices/)

## 2. 이미 발급받은 kubeconfig가 있는 경우

클러스터 담당자에게 아래를 요청합니다.

1. 대상 클러스터의 API 주소와 CA가 포함된 개인별 kubeconfig.
2. 사용할 context 이름과 계정 종류: 개인 인증서, OIDC/exec 인증 또는 ServiceAccount.
3. 조회용인지 전체 운영용인지, 허용된 namespace와 작업 범위.
4. 인증서·토큰 만료 시점과 갱신 방법.
5. 클러스터 외부에서 API 서버에 접근하는 데 필요한 VPN/방화벽 조건.

클라우드 관리형 Kubernetes는 공급자의 CLI·SSO 절차로 kubeconfig를 발급받고, 공급자의 IAM/접근 정책과 Kubernetes 권한을 함께 확인합니다. kubeconfig를 복사했다고 접근 권한이 새로 생기지는 않습니다.

신뢰할 수 있는 kubeconfig만 사용합니다. kubeconfig의 exec 인증 설정은 로컬 프로그램을 실행할 수 있습니다. [kubeconfig 공식 안내](https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/)

기존 설정에서 한 context만 인증서 포함 사본으로 내보내는 예시입니다. 경로와 context를 실제 값으로 바꿉니다. 새 파일 경로를 사용해 기존 파일을 덮어쓰지 마세요.

```sh
umask 077
kubectl --kubeconfig /path/to/issued.conf --context issued-context \
  config view --minify --raw --flatten > /path/to/my-cluster.conf
chmod 600 /path/to/my-cluster.conf
```

`--raw` 출력에는 인증 정보가 포함되므로 파일로만 저장하고 터미널·채팅·이슈에 붙여넣지 않습니다. exec 플러그인의 프로그램·SSO 세션·외부 tokenFile 등은 별도로 준비해야 합니다. `--flatten`이 모든 외부 의존성을 없애는 것은 아닙니다.

### kubeadm의 관리자 파일

kubeadm 클러스터의 control-plane에는 보통 `/etc/kubernetes/admin.conf`가 있습니다. 최근 kubeadm의 이 파일은 `kubeadm:cluster-admins` 그룹을 사용하는 관리자 인증서입니다. `/etc/kubernetes/super-admin.conf`는 `system:masters` 비상용 인증서입니다. 이전 버전이나 배포 방식에 따라 파일·그룹 구성이 다를 수 있습니다.

Kubernetes 공식 문서는 이 파일들을 다른 사용자에게 공유하지 않고 별도 계정을 발급하도록 안내합니다. `/etc/kubernetes/kubelet.conf`나 클러스터 CA 개인키를 사용자 접속 파일 대신 가져오지 않습니다. [kubeadm 인증서와 사용자 발급](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-certs/)

## 3. 전용 ServiceAccount 발급

아래 절차는 Kubernetes의 TokenRequest와 `kubectl create token`을 사용할 수 있는 클러스터용입니다. Kubernetes 1.24 이상 기준이며, 실제 kubectl은 클러스터와 호환되는 버전을 사용합니다.

이 문서는 조회 계정을 먼저 만들고 필요하면 별도 Binding으로 전체 운영 권한을 추가합니다. 사람별 장기 사용은 조직의 SSO/개인 계정 발급 방식이 있다면 그 방식을 우선 사용합니다.

### 3-1. 발급 담당자의 kubeconfig 지정

아래 경로는 **이미 권한을 가진 발급 담당자**의 파일입니다. 발급 담당자의 credential을 새 계정에 복사하는 방식이 아닙니다.

```sh
export KDASH_ISSUER_CONFIG=/path/to/administrator.conf
kubectl --kubeconfig "$KDASH_ISSUER_CONFIG" config current-context
kubectl --kubeconfig "$KDASH_ISSUER_CONFIG" get nodes
```

대상 클러스터가 맞는지 확인한 뒤 다음 명령을 실행합니다.

### 3-2. 조회 계정과 권한 만들기

아래는 KDASH 기본 메뉴에 필요한 리소스를 명시한 예시입니다. Secret 조회와 nodes/proxy는 포함하지 않습니다. logs와 ConfigMap에는 민감 정보가 들어 있을 수 있으므로 이 조회 계정도 배포 범위를 관리합니다.

```sh
kubectl --kubeconfig "$KDASH_ISSUER_CONFIG" apply -f - <<'YAML'
apiVersion: v1
kind: Namespace
metadata:
  name: kdash-access
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: kdash-reader
  namespace: kdash-access
automountServiceAccountToken: false
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: kdash-reader
rules:
  - apiGroups: [""]
    resources: ["nodes", "namespaces", "pods", "events", "services", "configmaps", "resourcequotas", "limitranges", "persistentvolumeclaims", "persistentvolumes", "serviceaccounts"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
    verbs: ["get", "list"]
  - apiGroups: ["batch"]
    resources: ["jobs", "cronjobs"]
    verbs: ["get", "list"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses", "networkpolicies"]
    verbs: ["get", "list"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["get", "list"]
  - apiGroups: ["storage.k8s.io"]
    resources: ["storageclasses", "volumeattachments"]
    verbs: ["get", "list"]
  - apiGroups: ["rbac.authorization.k8s.io"]
    resources: ["roles", "rolebindings", "clusterroles", "clusterrolebindings"]
    verbs: ["get", "list"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["nodes", "pods"]
    verbs: ["get", "list"]
  - nonResourceURLs: ["/api", "/api/*", "/apis", "/apis/*", "/version"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: kdash-reader
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: kdash-reader
subjects:
  - kind: ServiceAccount
    name: kdash-reader
    namespace: kdash-access
YAML
```

이 계정에서는 Secret 메뉴, 노드 디스크 사용량, 생성·수정·삭제·exec가 제한됩니다. 임의 CRD의 get/list도 자동으로 허용하지 않으므로 필요한 API group/resource만 추가합니다. metrics-server가 설치되지 않았다면 Metrics API 권한을 줘도 사용량이 나오지 않습니다. 기본 discovery·SelfSubjectAccessReview 접근을 별도로 제한한 클러스터는 관리자에게 추가 확인합니다.

### 3-3. 전체 운영이 필요한 경우: cluster-admin 연결

**이 단계는 전체 클러스터 변경 권한이 필요한 운영자에게만 적용합니다.** 같은 ServiceAccount를 관리자 권한으로 올리는 예시이며, 아래 Binding을 추가하면 더 이상 조회 전용 계정이 아닙니다. 실제 운영에서는 사용자·용도별로 별도 계정과 식별 가능한 Binding 이름을 사용하세요.

```sh
kubectl --kubeconfig "$KDASH_ISSUER_CONFIG" create clusterrolebinding kdash-full-operator \
  --clusterrole=cluster-admin \
  --serviceaccount=kdash-access:kdash-reader
```

`cluster-admin`에는 Secret 조회, 워크로드 배포·삭제, exec, RBAC 변경, nodes/proxy 등 광범위한 권한이 포함됩니다. 서비스 중단과 데이터 손실에 직접 영향을 줄 수 있습니다. KDASH의 입력 확인은 실수 방지용이며 Kubernetes 권한 통제를 대체하지 않습니다. [기본 ClusterRole과 Binding](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#user-facing-roles)

특정 namespace의 운영만 필요하다면 위 Binding 대신 해당 namespace에 필요한 리소스·동작만 가진 Role/RoleBinding을 추가합니다. scale은 `deployments/scale` 등 하위 리소스 권한, exec는 `pods/exec` 권한이 필요하고, YAML 수정은 apply 및 diff의 서버 dry-run 권한도 확인합니다. 현재 KDASH가 수행하는 전역 목록 조회를 위해 조회 범위와 변경 범위를 따로 설계할 수 있습니다.

### 3-4. 만료형 토큰으로 kubeconfig 만들기

토큰은 `kubectl create token`으로 발급합니다. `--duration=8h`는 요청한 수명이며 실제 유효 기간은 API 서버 설정에 따라 달라질 수 있습니다. 토큰을 내장한 파일은 KDASH가 자동 갱신하지 않습니다. 장기 토큰 Secret을 기본 방식으로 만들지 않습니다. [토큰 발급 명령](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_create/kubectl_create_token/), [ServiceAccount 토큰 관리](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/)

아래 스크립트는 Python 표준 라이브러리만 사용합니다. 발급 담당자의 현재 context에서 **클러스터 주소·CA 설정만** 가져오고, 새 ServiceAccount 토큰으로 별도 kubeconfig를 만듭니다. 발급 담당자의 사용자 인증서는 결과에 포함하지 않습니다. 토큰을 셸 명령 인자나 터미널에 출력하지 않습니다.

프로젝트 디렉토리에서 실행합니다. 기존 파일이 있으면 덮어쓰지 않고 실패합니다.

```sh
export KDASH_NEW_CONFIG="$PWD/.kube/my-cluster.conf"
python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess

issuer = os.environ['KDASH_ISSUER_CONFIG']
base = ['kubectl', '--kubeconfig', issuer]
def run(*args):
    result = subprocess.run(base + list(args), check=True, text=True, capture_output=True)
    return result.stdout

source = json.loads(run('config', 'view', '--minify', '--raw', '--flatten', '-o', 'json'))
cluster = source['clusters'][0]['cluster']
if cluster.get('insecure-skip-tls-verify'):
    raise SystemExit('Use a kubeconfig with a trusted CA; TLS verification must be enabled.')
# The issuer must be allowed to create serviceaccounts/token in kdash-access.
token = run('create', 'token', 'kdash-reader', '-n', 'kdash-access', '--duration=8h').strip()
config = {
    'apiVersion': 'v1', 'kind': 'Config',
    'clusters': [{'name': 'my-cluster', 'cluster': cluster}],
    'users': [{'name': 'kdash-reader', 'user': {'token': token}}],
    'contexts': [{'name': 'kdash-my-cluster', 'context': {
        'cluster': 'my-cluster', 'user': 'kdash-reader', 'namespace': 'default'}}],
    'current-context': 'kdash-my-cluster',
}
path = Path(os.environ['KDASH_NEW_CONFIG']).expanduser()
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(path.parent, 0o700)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump(config, output, indent=2)
    output.write('\n')
print('Saved kubeconfig:', path)
PY
```

JSON 형식의 kubeconfig도 kubectl에서 읽을 수 있으므로 확장자는 KDASH 규칙대로 `.conf`를 사용합니다.

발급한 파일은 API 서버의 `server` 주소에 접근 가능한 컴퓨터에서 사용합니다. control-plane 내부 IP만 들어 있다면 관리자가 외부 접속 가능하고 인증서 SAN과 일치하는 주소로 설정해야 합니다. `insecure-skip-tls-verify`로 우회하지 않습니다.

## 4. kubeadm으로 개인용 인증서를 발급하는 방법

kubeadm을 사용하고 CA 서명이 가능한 control-plane에서 관리자가 실행하는 별도 방법입니다. 외부 CA 구성이나 관리형 서비스에서는 이 명령을 그대로 사용할 수 없습니다.

발급 컴퓨터의 작업 디렉토리에서 실행합니다. 결과는 `.kube/kdash-operator.conf`에 저장됩니다. KDASH 프로젝트 안에서 실행하면 `.gitignore`로 제외됩니다. 저장소 밖에 발급하려면 `KDASH_NEW_CONFIG`를 별도의 보호 디렉토리 아래 절대 경로로 변경하세요. 갱신할 때는 먼저 `.kube/kdash-operator-renewed.conf`처럼 다른 이름으로 발급합니다.

```sh
export KDASH_NEW_CONFIG="$PWD/.kube/kdash-operator.conf"
python3 - <<'PY'
import os
from pathlib import Path
import subprocess
import tempfile

path = Path(os.environ['KDASH_NEW_CONFIG']).expanduser()
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(path.parent, 0o700)
if path.exists():
    raise SystemExit('Output exists; choose a different protected path: ' + str(path))

def run(*args):
    return subprocess.run(list(args), check=True, text=True, capture_output=True).stdout

# Failed commands never open or truncate the output kubeconfig.
with tempfile.TemporaryDirectory(prefix='kdash-issuance-', dir=path.parent) as folder:
    cluster_config = run('sudo', 'kubectl', '--kubeconfig', '/etc/kubernetes/admin.conf',
                         '-n', 'kube-system', 'get', 'configmap', 'kubeadm-config',
                         '-o', 'jsonpath={.data.ClusterConfiguration}')
    if not cluster_config.strip():
        raise SystemExit('Empty kubeadm ClusterConfiguration; no output written.')
    config_path = Path(folder) / 'kubeadm-config.yaml'
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(cluster_config)
    kubeconfig = run('sudo', 'kubeadm', 'kubeconfig', 'user', '--config', str(config_path),
                     '--client-name', 'kdash-operator', '--validity-period', '24h')
    if not kubeconfig.strip():
        raise SystemExit('Empty kubeconfig; no output written.')
    # Exclusive creation also protects a file appearing during issuance.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(kubeconfig)
print('Saved kubeconfig:', path)
PY
```

토큰·개인키를 stdout에 출력하지 않고, 명령이 실패하면 새 kubeconfig를 생성하지 않습니다. `.kube`는 권한 700, 생성 파일은 600입니다. `.gitignore`는 이미 추적된 파일이나 과거 커밋의 인증 정보를 제거하지 않습니다.

이 단계에서는 사용자 자격 증명만 발급되었으며 관리 권한이 부여된 것은 아닙니다. 전체 운영 권한을 부여하려면 관리자가 다음을 실행합니다. `--org system:masters`는 지정하지 않습니다.

```sh
sudo kubectl --kubeconfig /etc/kubernetes/admin.conf \
  create clusterrolebinding kdash-certificate-operator \
  --clusterrole=cluster-admin --user=kdash-operator
```

인증서 유효 기간은 24시간의 예시입니다. 갱신 시 재발급하고 만료 시점과 API 주소를 확인합니다. Binding 삭제로 이 사용자에게 부여한 권한을 제거할 수 있지만 다른 Binding이나 그룹 경유 권한이 있으면 남습니다. 사용자마다 다른 `--client-name`을 사용하세요. [kubeadm 사용자 발급 공식 안내](https://kubernetes.io/docs/tasks/administer-cluster/kubeadm/kubeadm-certs/#generating-kubeconfig-files-for-additional-users)

## 5. 접속과 권한 검증

새 계정의 파일로 실행합니다. 발급 담당자의 kubeconfig로 검사하면 권한을 잘못 판단할 수 있습니다.

```sh
export KDASH_CHECK_CONFIG=/absolute/path/to/my-cluster.conf
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" config current-context
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" get nodes
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" get pods -A
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" auth can-i list pods --all-namespaces
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" auth can-i list deployments.apps --all-namespaces
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" auth can-i get pods --subresource=log -n aifactory
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" auth can-i create pods --subresource=exec -n aifactory
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" auth can-i patch deployments.apps -n aifactory
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" auth can-i update deployments.apps --subresource=scale -n aifactory
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" auth can-i get nodes --subresource=proxy
kubectl --kubeconfig "$KDASH_CHECK_CONFIG" auth can-i --list
```

`aifactory`는 실제 작업 namespace로 바꿉니다. 조회 계정은 변경·exec·nodes/proxy 검사에서 `no`가 나오는 것이 의도된 결과입니다. `--list`는 현재 namespace의 요약이므로 필요한 작업·namespace를 위처럼 각각 검사합니다. `can-i`는 인가 여부를 확인하며, admission 정책·API 설치 여부·접속 성공·토큰 만료까지 보장하지는 않습니다. [권한 검사 공식 안내](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_auth/kubectl_auth_can-i/)

## 6. KDASH 프로필 등록

1. 생성한 `.kube/my-cluster.conf`(ServiceAccount) 또는 `.kube/kdash-operator.conf`(kubeadm)를 KDASH 컴퓨터의 보호 디렉토리로 안전하게 전달합니다. 파일 권한 600을 유지합니다.
2. `./kdash.sh`를 실행합니다. 프로필이 없으면 등록 안내가 나옵니다.
3. 기존 프로필이 있으면 `c` 또는 `Settings → Profiles`에 진입합니다.
4. `a`를 선택하고 `.conf`의 절대 경로를 입력합니다.
5. alias를 입력합니다. context는 파일 안의 클러스터·사용자·기본 namespace 조합이며 파일명이 아닙니다.
6. 등록한 프로필 번호를 선택해 접속합니다.

`.kdash/profiles.json`이 없는 최초 실행은 `.kube/*.conf`를 파일명 순서로 자동 등록하고 **기존 파일 자체를 직접 참조**합니다. 별도 사본은 만들지 않습니다. 이후에는 JSON에 저장된 프로필을 사용하며, 추가 파일은 `a`로 등록합니다. 별도 선택이 없으면 첫 프로필에 연결합니다.

`a`에서 아직 등록되지 않은 경로를 가져오면 `.kube/<이름>-<식별자>.conf`에 인증서를 포함한 별도 사본을 저장합니다. **이 가져오기 방식에서는 외부 원본을 갱신해도 관리 사본은 바뀌지 않습니다.** 입력 경로가 이미 등록된 `.kube` 파일 자체이면 기존 프로필을 반환하고 alias 입력이나 새 복사를 진행하지 않습니다. 외부 원본 경로는 관리 사본과 다르므로 같은 외부 경로도 다른 alias로 다시 가져올 수 있습니다.

토큰/인증서 갱신은 다음 두 방식 중 하나를 사용합니다.

- **현재 프로필 유지:** KDASH를 종료하고 현재 프로필의 `.kube` 참조 파일 경로를 확인합니다. 갱신 파일을 별도의 보호 경로에 발급하여 새 계정으로 접속·권한 검증을 마친 뒤, 권한 600의 완성된 파일을 같은 파일시스템에서 원자적으로 이동해 해당 참조 파일을 교체하고 재실행합니다. 자동 등록은 원래 `.kube/*.conf`, 명시적 가져오기는 `.kube/<이름>-<식별자>.conf`를 교체해야 합니다. 외부 원본만 교체하면 가져온 프로필에는 반영되지 않습니다. 클러스터와 context가 같은지 확인하세요.
- **새 프로필로 검증 후 전환:** 현재 등록된 참조 파일과 **다른 경로**에 갱신 파일을 발급하고 `a`로 가져옵니다. 예: `.kube/kdash-operator-renewed.conf`. 기존 alias와 다른 임시 alias를 사용하고, 새 프로필을 선택해 접속·권한을 확인합니다. 새 프로필의 Prometheus 설정은 따로 연결합니다. 이후 이전 프로필을 삭제하고 필요하면 새 alias를 변경합니다.

인증 파일의 경로는 `.kdash/profiles.json`의 `kubeconfig` 필드로 확인할 수 있습니다. 인증 파일 내용은 터미널이나 이슈에 출력하지 않습니다.

프로필 삭제는 인증 파일이나 Kubernetes 계정·RBAC를 삭제하지 않습니다. Prometheus 연결도 별도 설정이며 `p`에서 Service/포트를 선택합니다. [README의 프로필 관리](../README.md#kubeconfig와-프로필-관리)

## 7. 운영 시 주의사항과 권한 회수

### 노드 디스크 조회 권한

현재 KDASH의 디스크 값은 `/api/v1/nodes/<node>/proxy/stats/summary`에서 조회합니다. `nodes/proxy get`은 이 한 경로만 허용하는 권한이 아니며 kubelet의 명령 실행 등에도 접근할 수 있어 단순 읽기 권한으로 취급할 수 없습니다. 필요하지 않으면 부여하지 않고 Disk N/A를 유지하세요. [Kubernetes의 kubelet API 접근 위험](https://kubernetes.io/docs/concepts/security/api-server-bypass-risks/)

### 인증 정보와 변경 작업

- Secret의 `get`뿐 아니라 `list`도 값을 노출할 수 있습니다. KDASH 목록에서 값이 숨겨져 있어도 계정이 가진 권한은 그대로입니다.
- Pod/워크로드 생성과 exec도 namespace의 Secret·서비스 계정 등에 접근할 수 있는 강한 권한입니다. namespace와 admission 정책을 함께 관리합니다. [RBAC 권한 설계 지침](https://kubernetes.io/docs/concepts/security/rbac-good-practices/)
- kubeconfig는 인증 토큰이나 개인키를 포함할 수 있습니다. 파일은 권한 600, 저장 디렉토리는 제한된 사용자만 접근하게 유지합니다. 실제 파일을 GitHub·메신저·이슈·실행 로그에 올리지 않습니다.
- `.gitignore`는 이미 추적 중인 파일이나 과거 커밋을 지우지 않습니다. 유출되었다면 저장소에서 파일을 제거하는 것과 별도로 해당 자격 증명을 회수·재발급합니다.
- `.kdash/drafts`의 Secret YAML, ConfigMap, exec/logs 출력도 민감 정보가 될 수 있습니다. 초안은 자동 삭제되지 않습니다.
- `cluster-admin`이라도 admission 정책과 별도 보안 통제로 작업이 거부될 수 있습니다. 운영 전 대상 클러스터·namespace·객체를 확인합니다.

### 전체 운영 권한만 회수

위 ServiceAccount 예시의 추가 관리자 Binding만 제거합니다. 조회 Binding은 남습니다.

```sh
kubectl --kubeconfig "$KDASH_ISSUER_CONFIG" delete clusterrolebinding kdash-full-operator
```

이 계정이 다른 Binding으로 관리자 권한을 받았다면 그것도 확인합니다. `system:masters` 인증서는 이 방법으로 회수되지 않습니다.

### 전용 ServiceAccount 접속까지 폐기

아래는 이 문서에서 만든 전용 리소스만 제거하는 예시입니다. 실제 존재 여부와 다른 사용자를 확인한 뒤 실행합니다.

```sh
kubectl --kubeconfig "$KDASH_ISSUER_CONFIG" delete clusterrolebinding kdash-reader
kubectl --kubeconfig "$KDASH_ISSUER_CONFIG" -n kdash-access delete serviceaccount kdash-reader
kubectl --kubeconfig "$KDASH_ISSUER_CONFIG" delete clusterrole kdash-reader
```

추가 `kdash-full-operator` Binding이 있다면 먼저 제거합니다. ServiceAccount 삭제는 그 계정에 발급한 토큰의 인증에도 영향을 줍니다. namespace 자체는 공유될 수 있으므로 자동 삭제하지 않습니다. 개인 인증서 예시는 해당 `kdash-certificate-operator` Binding을 제거하고 다른 권한 경로도 확인합니다.

## 8. 오류별 확인

| 오류/현상 | 확인할 내용 |
| --- | --- |
| Unauthorized | 토큰 만료, 인증서 만료, 인증 플러그인/SSO 상태 |
| Forbidden | 접속한 계정, API group/resource/verb/namespace 권한 |
| x509 오류 | API 서버 주소, 인증서 SAN, CA, 로컬 시간 |
| 접속 시간 초과 | API 서버 주소·포트, VPN/방화벽/라우팅 |
| 전체 Pod 목록 Forbidden | namespace RoleBinding만 있는지, 클러스터 범위 list 권한 여부 |
| 디스크 N/A | nodes/proxy 권한, 노드 kubelet/Summary API 상태 |
| CPU/Memory N/A | metrics-server와 Metrics API 권한 |
| 발급한 토큰이 있는데 계속 Unauthorized | KDASH가 원본이 아니라 이전 관리 사본을 사용하는지 확인 |
| API Resources에 보이지만 조회/배포 실패 | discovery는 성공했지만 해당 리소스의 권한·필수 필드가 부족할 수 있음 |
