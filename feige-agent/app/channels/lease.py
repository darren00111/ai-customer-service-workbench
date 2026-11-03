"""OS released, nonblocking ownership of one store connection."""
import hashlib
import os
from pathlib import Path


class ConnectionLease:
    def __init__(self, workspace, spec):
        key = '|'.join((spec.merchant_id, spec.platform, spec.store_id))
        self.path = Path(workspace.path).parent / 'connection-locks' / (hashlib.sha256(key.encode()).hexdigest()+'.lock')
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b'0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ValueError('此店铺已在另一个窗口或程序中连接，请先停止原连接') from None
        self.handle = handle

    def release(self):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()
