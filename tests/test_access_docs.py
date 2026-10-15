"""Execute credential examples with mocked commands; never contact Kubernetes."""
import copy
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from kasciidash_app.config import profiles
from tests.test_profiles import CONFIG

BASE = Path(__file__).resolve().parents[1]


class AccessDocTests(unittest.TestCase):
    def scripts(self):
        text = (BASE / 'docs/kubeconfig-access.md').read_text()
        return re.findall(r"python3 - <<'PY'\n(.*?)\nPY", text, re.S)

    def test_credential_output_paths_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.gitignore').write_text((BASE / '.gitignore').read_text())
            subprocess.run(['git', 'init', '-q', directory], check=True, capture_output=True)
            for path in ('.kube/my-cluster.conf', '.kube/kasciidash-operator.conf', '.kasciidash/profiles.json', 'kasciidash-operator.conf'):
                with self.subTest(path=path):
                    result = subprocess.run(['git', '-C', directory, 'check-ignore', '-q', path])
                    self.assertEqual(result.returncode, 0)
            self.assertEqual(subprocess.run(['git', '-C', directory, 'check-ignore', '-q', '.kube/.gitkeep']).returncode, 1)

    def test_issuance_examples_permissions_success_collision_and_failure(self):
        scripts = self.scripts()
        self.assertEqual(len(scripts), 2)
        for script in scripts:
            for scenario in ('success', 'existing', 'issuer-failure', 'issuance-failure'):
                with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                    parent = Path(directory) / '.kube'
                    parent.mkdir(mode=0o755)
                    output = parent / 'fixture.conf'
                    if scenario == 'existing':
                        output.write_text('VALID_EXISTING_FIXTURE')

                    def run(args, **kwargs):
                        issuance = 'kubeadm' in args or 'token' in args
                        if scenario == 'issuer-failure' or (scenario == 'issuance-failure' and issuance):
                            raise subprocess.CalledProcessError(1, args)
                        if 'kubeadm' in args:
                            data = 'apiVersion: v1\nkind: Config\n# FAKE_ONLY_CERTIFICATE\n'
                        elif 'token' in args:
                            data = 'FAKE_ONLY_TOKEN\n'
                        elif 'config' in args:
                            data = json.dumps(CONFIG)
                        else:
                            data = 'apiVersion: kubeadm.k8s.io/v1beta4\nkind: ClusterConfiguration\n'
                        return Mock(stdout=data, returncode=0)

                    environment = {'KASCIIDASH_NEW_CONFIG': str(output), 'KASCIIDASH_ISSUER_CONFIG': '/synthetic/issuer.conf'}
                    with patch.dict(os.environ, environment), patch('subprocess.run', side_effect=run), patch('builtins.print'):
                        if scenario == 'success':
                            exec(compile(script, '<documentation-fixture>', 'exec'), {})
                        else:
                            with self.assertRaises((FileExistsError, SystemExit, subprocess.CalledProcessError)):
                                exec(compile(script, '<documentation-fixture>', 'exec'), {})
                    if scenario == 'success':
                        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
                        self.assertTrue(output.read_text())
                    elif scenario == 'existing':
                        self.assertEqual(output.read_text(), 'VALID_EXISTING_FIXTURE')
                    else:
                        self.assertFalse(output.exists())
                    self.assertEqual(list(parent.glob('kasciidash-issuance-*')), [])

    def test_github_relative_document_links(self):
        readme = (BASE / 'README.md').read_text()
        doc = (BASE / 'docs/kubeconfig-access.md').read_text()
        self.assertIn('(docs/kubeconfig-access.md)', readme)
        self.assertIn('(../README.md#kubeconfig와-프로필-관리)', doc)
        self.assertIn('### kubeconfig와 프로필 관리', readme)
        for path in (BASE / 'README.md', BASE / 'docs/kubeconfig-access.md'):
            for target in re.findall(r'\]\(([^)]+)\)', path.read_text()):
                if target.startswith(('http:', 'https:', '#')):
                    continue
                self.assertTrue((path.parent / target.split('#')[0]).exists(), target)


class ProfileRenewalTests(unittest.TestCase):
    def test_bootstrap_tracks_file_and_same_path_keeps_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.kube').mkdir()
            source = root / '.kube/cluster.conf'
            source.write_text('FAKE_OLD_CERT')
            store = profiles.Store(root)
            item = store.items[0]
            self.assertEqual(store.config_path(item), source.resolve())
            source.write_text('FAKE_RENEWED_CERT')
            self.assertEqual(store.config_path(item).read_text(), 'FAKE_RENEWED_CERT')
            factory = Mock()
            with patch('builtins.input', return_value=str(source)), patch.object(profiles, 'clear'), patch('builtins.print'):
                self.assertEqual(profiles.register_prompt(store, factory)['id'], item['id'])
            factory.assert_not_called()
            self.assertEqual(len(store.items), 1)

    def test_import_copy_is_independent_and_new_alias_can_renew(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'external.yaml'
            source.write_text('FAKE_OLD_CERT')
            store = profiles.Store(root)
            first = store.register(source, 'original', CONFIG)
            source.write_text('FAKE_RENEWED_CERT')
            self.assertNotIn('FAKE_RENEWED_CERT', store.config_path(first).read_text())
            renewed = copy.deepcopy(CONFIG)
            renewed['users'][0]['user']['client-certificate-data'] = 'FAKE_RENEWED_CERT'
            second = store.register(source, 'temporary-renewal', renewed)
            self.assertNotEqual(store.config_path(first), store.config_path(second))
            self.assertIn('FAKE_RENEWED_CERT', store.config_path(second).read_text())
            old_path = store.config_path(first)
            store.remove(first['id'])
            self.assertTrue(old_path.exists())


if __name__ == '__main__':
    unittest.main()
