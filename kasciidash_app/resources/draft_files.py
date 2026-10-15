"""Local draft identity and atomic name ownership during cleanup."""
import os
from pathlib import Path
import stat
import tempfile
from urllib.parse import quote


GROUPS = {'deployments': 'apps', 'replicasets': 'apps', 'statefulsets': 'apps',
          'daemonsets': 'apps', 'controllerrevisions': 'apps', 'jobs': 'batch', 'cronjobs': 'batch',
          'ingresses': 'networking.k8s.io', 'networkpolicies': 'networking.k8s.io',
          'endpointslices': 'discovery.k8s.io', 'storageclasses': 'storage.k8s.io',
          'volumeattachments': 'storage.k8s.io', 'roles': 'rbac.authorization.k8s.io',
          'rolebindings': 'rbac.authorization.k8s.io', 'clusterroles': 'rbac.authorization.k8s.io',
          'clusterrolebindings': 'rbac.authorization.k8s.io'}


def canonical_resource(resource):
    return resource + '.' + GROUPS[resource] if resource in GROUPS else resource


def component(value):
    text = str(value or 'unregistered')
    return quote(text, safe='-_') if text not in ('.', '..') else text.replace('.', '%2E')


def directory(base, alias, resource):
    root = Path(base) / '.kasciidash'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root
    for part in (alias, canonical_resource(resource)):
        path = path / component(part)
        if path.is_symlink():
            raise ValueError('YAML storage directory must not be a symbolic link: ' + str(path))
        path.mkdir(exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    return path


def directories(base, alias, resource):
    current = directory(base, alias, resource)
    result = [current]
    plural, _, group = canonical_resource(resource).partition('.')
    if GROUPS.get(plural) == group:
        old = current.parent / component(plural)
        if old.is_symlink():
            raise ValueError('Legacy draft directory must not be a symbolic link.')
        if old.is_dir():
            result.append(old)
    return result


def restore(claimed, original):
    """Publish without replacing an editor's newer pathname."""
    try:
        os.link(claimed, original, follow_symlinks=False)
    except FileExistsError:
        return claimed
    try:
        claimed.unlink()
    except OSError:
        pass  # The published link still retains the complete contents.
    return original


def cleanup(path, payload):
    """Claim the directory entry, inspect it privately, remove only that entry.

    Editors opening/replacing the public path after rename cannot have their new
    file unlinked. A changed or unremovable claimed file remains discoverable.
    """
    path = Path(path)
    claim_dir = None
    claimed = None
    try:
        if path.is_symlink():
            return 'Draft kept: symbolic links are not cleaned up.'
        claim_dir = Path(tempfile.mkdtemp(prefix='.draft-claim-', dir=path.parent))
        claimed = claim_dir / path.name
        try:
            path.rename(claimed)
        except FileNotFoundError:
            return 'Draft already absent; deployment does not need to be repeated.'
        if claimed.is_symlink() or not stat.S_ISREG(claimed.lstat().st_mode):
            kept = restore(claimed, path)
            return 'Draft kept: unexpected file type at ' + str(kept)
        if claimed.read_bytes() != payload:
            kept = restore(claimed, path)
            return 'Changed draft kept: ' + str(kept)
        claimed.unlink()
        return 'Submitted draft removed.'
    except OSError as exc:
        kept = claimed if claimed is not None and claimed.exists() else path
        return 'Local draft cleanup failed; deployment does not need to be repeated. Draft kept at %s (%s)' % (kept, exc)
    finally:
        if claim_dir is not None:
            try:
                claim_dir.rmdir()
            except OSError:
                pass  # Recovery files are offered in the D picker.
