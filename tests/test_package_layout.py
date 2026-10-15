"""Package boundaries and public launchers; no live cluster calls."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kasciidash_app.ui import dashboard

ROOT = Path(__file__).resolve().parents[1]


class PackageLayoutTests(unittest.TestCase):
    def test_project_state_stays_at_repository_root(self):
        self.assertEqual(dashboard.BASE, ROOT)
        self.assertEqual(sorted(p.name for p in ROOT.glob('*.py')), ['kasciidash.py'])

    def test_public_launchers_from_another_directory(self):
        commands = ([sys.executable, str(ROOT / 'kasciidash.py')],
                    [str(ROOT / 'kasciidash')])
        with tempfile.TemporaryDirectory() as directory:
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(command + ['--demo', '--once'], cwd=directory,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('KAsciiDash', result.stdout)

    def test_module_launcher(self):
        result = subprocess.run([sys.executable, '-m', 'kasciidash_app', '--demo', '--once'],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('KAsciiDash', result.stdout)
