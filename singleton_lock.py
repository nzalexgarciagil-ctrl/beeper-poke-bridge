"""Process-owned locks that survive stale PID files without blocking startup."""
import errno
import os


class SingletonLock:
    def __init__(self, path):
        self.path = path
        self._file = None

    def acquire(self):
        if self._file is not None:
            return True
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        self._file = handle
        return True

    def release(self):
        if self._file is None:
            return
        handle, self._file = self._file, None
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        # Never unlink: a waiter may already hold the same inode open.
