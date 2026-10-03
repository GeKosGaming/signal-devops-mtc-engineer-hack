"""Real kernel-lock/process tests; Ubuntu CI executes these, Windows skips them."""
from __future__ import annotations
import os
import pathlib
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import operations


class PlatformContract(unittest.TestCase):
    def test_windows_subprocess_fd_helper_does_not_require_fcntl(self):
        with patch.object(operations.os, 'name', 'nt'):
            self.assertEqual(operations.lock_pass_fds(), ())
            with self.assertRaisesRegex(operations.OperationLockError, 'POSIX'):
                with operations.operation_lock():
                    self.fail('A Windows caller cannot deploy with a simulated flock')


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Real POSIX flock/process tests run on Ubuntu CI')
class ProcessLock(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.hint = patch.dict(os.environ)
        self.hint.start()
        self.addCleanup(self.hint.stop)
        os.environ.pop(operations.LOCK_FD_ENV, None)

    def env(self, *, inherit=False, hint=None):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT/'scripts')
        if not inherit:
            env.pop(operations.LOCK_FD_ENV, None)
        if hint is not None:
            env[operations.LOCK_FD_ENV] = hint
        return env

    def child(self, code=None, *, inherit=False, hint=None, fds=()):
        code = code or 'with operations.operation_lock(root): print("acquired")'
        return subprocess.run([sys.executable, '-c',
                               'import sys,pathlib,operations; root=pathlib.Path(sys.argv[1]);\n'+code,
                               str(self.root)], env=self.env(inherit=inherit, hint=hint),
                              pass_fds=fds, text=True, capture_output=True, timeout=8)

    def assert_busy(self):
        child = self.child()
        self.assertNotEqual(child.returncode, 0)
        self.assertIn('Another SIGNAL operation is running', child.stderr)

    def shell_workspace(self):
        (self.root/'scripts').mkdir(exist_ok=True)
        for name in ('lib.sh', 'operations.py'):
            shutil.copyfile(ROOT/'scripts'/name, self.root/'scripts'/name)
        shutil.copyfile(ROOT/'versions.env', self.root/'versions.env')

    def holder(self, args, *, env=None):
        process = subprocess.Popen(args, cwd=self.root, env=env or self.env(), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def close():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(close)
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, 'The lock-holder process did not become ready')
        self.assertEqual(process.stdout.readline().strip(), 'locked')
        return process

    def test_contention_fails_fast_and_lock_releases_on_context_exit(self):
        with operations.operation_lock(self.root):
            self.assert_busy()
        result = self.child()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exception_releases_lock_and_restores_environment(self):
        with self.assertRaisesRegex(RuntimeError, 'test failure'):
            with operations.operation_lock(self.root):
                raise RuntimeError('test failure')
        self.assertNotIn(operations.LOCK_FD_ENV, os.environ)
        self.assertEqual(self.child().returncode, 0)

    def test_nested_child_keeps_parent_lock_after_child_exit(self):
        with operations.operation_lock(self.root) as parent_fd:
            result = self.child(inherit=True, fds=operations.lock_pass_fds(self.root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.environ[operations.LOCK_FD_ENV], str(parent_fd))
            self.assert_busy()
        self.assertEqual(self.child().returncode, 0)

    def test_nested_context_close_does_not_unlock_parent(self):
        with operations.operation_lock(self.root) as parent_fd:
            with operations.operation_lock(self.root) as nested_fd:
                self.assertNotEqual(parent_fd, nested_fd)
                self.assertEqual(operations.lock_pass_fds(self.root), (nested_fd,))
            self.assert_busy()
        self.assertEqual(self.child().returncode, 0)

    def test_marker_without_an_inherited_descriptor_cannot_bypass_lock(self):
        with operations.operation_lock(self.root):
            for hint in ('true', '2', '-1', '9999999999', '１２', '999999'):
                with self.subTest(hint=hint):
                    result = self.child(hint=hint)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('descriptor', result.stderr)

    def test_descriptor_for_another_file_is_rejected(self):
        other = self.root/'other.lock'
        with other.open('w') as handle:
            with operations.operation_lock(self.root):
                result = self.child(hint=str(handle.fileno()), fds=(handle.fileno(),))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('another file', result.stderr)

    def test_independent_fd_for_same_inode_cannot_bypass_actual_kernel_lock(self):
        with operations.operation_lock(self.root):
            fd = os.open(operations.lock_path(self.root), os.O_RDWR)
            try:
                result = self.child(hint=str(fd), fds=(fd,))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Another SIGNAL operation is running', result.stderr)
            finally:
                os.close(fd)

    def test_sigterm_and_sigkill_release_lock_after_holder_exits(self):
        code = ('import sys,pathlib,operations;\n'
                'with operations.operation_lock(pathlib.Path(sys.argv[1])):\n'
                ' print("locked",flush=True); sys.stdin.read()')
        for terminate in ('terminate', 'kill'):
            with self.subTest(terminate=terminate):
                process = self.holder([sys.executable, '-c', code, str(self.root)])
                self.assert_busy()
                getattr(process, terminate)()
                process.wait(timeout=5)
                self.assertEqual(self.child().returncode, 0)

    def test_shell_nested_in_python_shares_the_lock_without_deadlock(self):
        self.shell_workspace()
        with operations.operation_lock(self.root):
            child = subprocess.run(['bash', '-c', 'source scripts/lib.sh; lock; printf "nested\\n"'],
                                   cwd=self.root, env=self.env(inherit=True),
                                   pass_fds=operations.lock_pass_fds(self.root), text=True,
                                   capture_output=True, timeout=8)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn('nested', child.stdout)
            self.assert_busy()
        self.assertEqual(self.child().returncode, 0)

    def test_python_contends_with_a_real_shell_lock(self):
        self.shell_workspace()
        process = self.holder(['bash', '-c', 'source scripts/lib.sh; lock; printf "locked\\n"; read -r end'])
        self.assert_busy()
        process.stdin.write('done\n')
        process.stdin.flush()
        process.wait(timeout=5)
        self.assertEqual(self.child().returncode, 0)

    def test_shell_rejects_forged_descriptor_before_any_following_command(self):
        self.shell_workspace()
        with operations.operation_lock(self.root):
            child = subprocess.run(['bash', '-c', 'source scripts/lib.sh; lock; touch mutation'],
                                   cwd=self.root, env=self.env(hint='999999'), text=True,
                                   capture_output=True, timeout=8)
            self.assertNotEqual(child.returncode, 0)
            self.assertFalse((self.root/'mutation').exists())

    def test_entrypoints_fail_before_constructing_reports_or_touching_cluster(self):
        with operations.operation_lock(self.root):
            for name, args in (('acceptance', []), ('log_delivery', ['--run'])):
                with self.subTest(entrypoint=name):
                    code = ('import importlib; m=importlib.import_module('+repr(name)+'); m.ROOT=root;\n'
                            'def no_report(*args,**kwargs): raise AssertionError("Report must not start")\n'
                            'm.Report=no_report; sys.argv=['+repr(name)+']+'+repr(args)+'; sys.exit(m.main())')
                    result = self.child(code)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn('Another SIGNAL operation is running', result.stderr)
                    self.assertNotIn('Report must not start', result.stderr)


if __name__ == '__main__':
    unittest.main()
