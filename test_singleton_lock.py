import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from singleton_lock import SingletonLock


class SingletonLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.bridge.lock'

    def test_reused_pid_does_not_block(self):
        self.path.write_text(str(os.getpid()))
        lock = SingletonLock(self.path)
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquire())

    def test_exclusive_and_reacquirable(self):
        first, second = SingletonLock(self.path), SingletonLock(self.path)
        self.addCleanup(first.release)
        self.addCleanup(second.release)
        self.assertTrue(first.acquire())
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(self.path.exists())
        self.assertTrue(second.acquire())

    def test_process_exit_releases_lock(self):
        code = ('from singleton_lock import SingletonLock; import sys; '
                'lock=SingletonLock(sys.argv[1]); assert lock.acquire(); '
                'print("ready",flush=True); sys.stdin.read()')
        child = subprocess.Popen([sys.executable, '-c', code, str(self.path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 text=True)
        lock = SingletonLock(self.path)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            self.assertFalse(lock.acquire())
            child.kill()
            child.wait(timeout=10)
            self.assertTrue(lock.acquire())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.stdin.close()
            child.stdout.close()
            lock.release()

    def test_io_errors_are_not_silently_ignored(self):
        with self.assertRaises(OSError):
            SingletonLock(Path(self.temp.name) / 'missing' / 'lock').acquire()


if __name__ == '__main__':
    unittest.main()
