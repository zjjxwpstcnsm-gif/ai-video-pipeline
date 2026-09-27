from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_video.director.continuity import REQUIRED_LEDGER_FIELDS, validate_ledger_entry
from ai_video.director.project import load_shot, load_yaml
from ai_video.metadata import MetadataError, validate_metadata
from ai_video.media.ffmpeg import concat_mp4, require_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import private_check
import public_boundary


class BoundaryTests(unittest.TestCase):
    def test_allow_public_source(self):
        public_boundary.inspect('src/ai_video/__init__.py', b'x = 1\n')

    def test_private_paths(self):
        for path in ['assets/a.yaml', 'projects/a.json', '.env', '../README.md', '/README.md', '.git/config']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                public_boundary.inspect(path, b'hello')

    def test_binary(self):
        with self.assertRaises(ValueError):
            public_boundary.inspect('README.md', b'x\0y')

    def test_secret(self):
        with self.assertRaises(ValueError):
            public_boundary.inspect('README.md', ('github_' + 'pat_' + 'a' * 40).encode())

    def test_embedded_media(self):
        with self.assertRaises(ValueError):
            public_boundary.inspect('README.md', ('data:image/png;base64,' + 'A' * 70).encode())

    def test_broken_python(self):
        with self.assertRaises(SyntaxError):
            public_boundary.inspect('src/ai_video/__init__.py', b'def broken(')

    def test_oversize(self):
        with self.assertRaises(ValueError):
            public_boundary.inspect('README.md', b'a' * 512001)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'projects' / 'demo').mkdir(parents=True)
        self.config = self.root / 'projects' / 'demo' / 'project.yaml'
        self.config.write_text('name: synthetic\nshots: []\n')

    def test_yaml_json(self):
        (self.root / 'projects' / 'index.json').write_text('{"projects": []}')
        self.assertEqual(validate_metadata(self.root), 2)

    def test_no_media_bytes(self):
        (self.root / 'projects' / 'demo' / 'test.mp4').write_bytes(b'\0' * 100)
        self.assertEqual(validate_metadata(self.root), 1)

    def test_no_private_code_execution(self):
        (self.root / 'projects' / 'demo' / 'side_effect.py').write_text('raise RuntimeError("private code")')
        self.assertEqual(validate_metadata(self.root), 1)

    def test_empty(self):
        self.config.unlink()
        with self.assertRaises(MetadataError):
            validate_metadata(self.root)

    def test_bad_yaml(self):
        self.config.write_text('x: [')
        with self.assertRaises(Exception):
            validate_metadata(self.root)

    def test_bad_json(self):
        (self.root / 'projects' / 'broken.json').write_text('{')
        with self.assertRaises(Exception):
            validate_metadata(self.root)

    def test_symlink(self):
        (self.root / 'projects' / 'link.yaml').symlink_to(self.config)
        with self.assertRaises(MetadataError):
            validate_metadata(self.root)

    def test_root_symlink(self):
        (self.root / 'assets').symlink_to(self.root / 'projects', target_is_directory=True)
        with self.assertRaises(MetadataError):
            validate_metadata(self.root)

    def test_scalar(self):
        self.config.write_text('just a string')
        with self.assertRaises(MetadataError):
            validate_metadata(self.root)

    def test_unsafe_yaml_tag(self):
        self.config.write_text('x: !!python/object:builtins.object {}')
        with self.assertRaises(Exception):
            validate_metadata(self.root)

    def test_alias_limit(self):
        self.config.write_text('x: &x 1\ny: [' + ','.join(['*x'] * 33) + ']')
        with self.assertRaises(MetadataError):
            validate_metadata(self.root)

    def test_file_size(self):
        with patch('ai_video.metadata.MAX_BYTES', 2), self.assertRaises(MetadataError):
            validate_metadata(self.root)

    def test_public_cli_suppresses_paths(self):
        self.config.write_text('x: [TOP_SECRET_SENTINEL')
        result = subprocess.run([sys.executable, '-I', '-m', 'ai_video.metadata', str(self.root)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('TOP_SECRET_SENTINEL', result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)


class PrivateTransportTests(unittest.TestCase):
    def test_repository_allowlist_format(self):
        self.assertEqual(private_check.repository_name('owner/data'), 'owner/data')
        for name in ['', '../data', 'owner/data?x=1', 'https://example.com/a', 'owner/data/extra']:
            with self.assertRaises(private_check.CheckError):
                private_check.repository_name(name)

    def test_environment_strips_secrets(self):
        with patch.dict(os.environ, {'ASSETS_PAT': 'private-value', 'AGNES_API_KEY': 'private-value',
                                     'PYTHONPATH': '/private', 'GITHUB_TOKEN': 'private-value'}):
            env = private_check.clean_environment(Path('/tmp/test'))
        for key in ['ASSETS_PAT', 'AGNES_API_KEY', 'GITHUB_TOKEN', 'PYTHONPATH']:
            self.assertNotIn(key, env)

    def test_no_redirect(self):
        self.assertIsNone(private_check.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))

    def test_suppressed_process_output(self):
        with patch('private_check.subprocess.run') as run:
            run.return_value.returncode = 1
            with self.assertRaises(private_check.CheckError):
                private_check.run_quiet(['private-op'], cwd=Path('/tmp'), env={})
            self.assertIs(run.call_args.kwargs['stdout'], subprocess.DEVNULL)
            self.assertIs(run.call_args.kwargs['stderr'], subprocess.DEVNULL)

    def test_missing_token(self):
        with patch.dict(os.environ, {'ASSETS_REPOSITORY': 'owner/data', 'ASSETS_PAT': ''}):
            with self.assertRaises(private_check.CheckError):
                private_check.main()

    def test_public_assets_rejected(self):
        opener = unittest.mock.MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(b'{"private": false}')
        with patch.dict(os.environ, {'ASSETS_REPOSITORY': 'owner/data', 'ASSETS_PAT': 'test-value'}), \
             patch('private_check.urllib.request.build_opener', return_value=opener), \
             patch('private_check.run_quiet') as run:
            with self.assertRaises(private_check.CheckError):
                private_check.main()
            run.assert_not_called()

    def test_success_isolated_execution(self):
        opener = unittest.mock.MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(b'{"private":true,"default_branch":"main"}')
        records = []
        def fake_run(args, **kwargs):
            records.append((args, dict(kwargs['env'])))
            if 'clone' in args:
                Path(args[-1]).mkdir()
        with patch.dict(os.environ, {'ASSETS_REPOSITORY': 'owner/data', 'ASSETS_PAT': 'test-value'}), \
             patch('private_check.urllib.request.build_opener', return_value=opener), \
             patch('private_check.run_quiet', side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(private_check.main(), 0)
        self.assertIn('--filter=blob:none', records[0][0])
        self.assertEqual(records[-1][0][1:4], ['-I', '-m', 'ai_video.metadata'])
        self.assertNotIn('ASSETS_PAT', records[-1][1])


class MigratedUtilityTests(unittest.TestCase):
    def test_continuity(self):
        self.assertEqual(len(validate_ledger_entry({})), len(REQUIRED_LEDGER_FIELDS))
        self.assertEqual(validate_ledger_entry(dict.fromkeys(REQUIRED_LEDGER_FIELDS, 'synthetic')), [])

    def test_shot_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'shot.yaml'
            p.write_text('shot_id: s1\nprompt: synthetic\n')
            self.assertEqual(load_shot(p).duration, 5)
            self.assertEqual(load_shot(p).aspect_ratio, '9:16')
            p.write_text('- not-a-mapping')
            with self.assertRaises(ValueError):
                load_yaml(p)

    def test_require_ffmpeg(self):
        with patch('ai_video.media.ffmpeg.shutil.which', return_value=None), self.assertRaises(RuntimeError):
            require_ffmpeg()

    def test_empty_concat(self):
        with patch('ai_video.media.ffmpeg.require_ffmpeg', return_value='ffmpeg'), self.assertRaises(ValueError):
            concat_mp4([], 'out.mp4')

    def test_concat_rejects_output_input_collision(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'in.mp4'
            p.touch()
            with self.assertRaises(ValueError):
                concat_mp4([p], p)

    def test_concat_rejects_newline(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'in\nfile.mp4'
            p.touch()
            with self.assertRaises(ValueError):
                concat_mp4([p], Path(temp) / 'out.mp4')

    def test_real_ffmpeg_apostrophe_and_space(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            if os.environ.get('REQUIRE_FFMPEG') == '1':
                self.fail('CI must have ffmpeg and ffprobe')
            self.skipTest('ffmpeg unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            clip = root / "a'b clip.mp4"
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                            '-i', 'color=s=64x64:r=10:d=0.5', '-c:v', 'mpeg4', str(clip)], check=True)
            result = concat_mp4([clip, clip], root / 'result.mp4')
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format',
                                                       '-of', 'json', str(result)]))
            self.assertAlmostEqual(float(probe['format']['duration']), 1.0, delta=0.15)
            self.assertFalse(result.with_suffix('.concat.txt').exists())


if __name__ == '__main__':
    unittest.main()
