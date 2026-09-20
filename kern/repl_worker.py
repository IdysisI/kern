"""Isolated persistent interpreter.

An interrupt/timeout first tries SIGINT (POSIX): the cell aborts with a
KeyboardInterrupt and the namespace SURVIVES (audit r4-gui W2). Only if the
worker fails to answer within the parent's grace period does the parent fall
back to killing the process.
"""
import contextlib
import io
import json
import os
import signal
import sys
import traceback

# Reply framing: every result is written as ONE write() with a unique sentinel
# prefix. redirect_stdout cannot catch output that never touched sys.stdout
# (a C extension writing straight to fd 1), and such stray lines used to land
# before the JSON reply, desyncing the parent's readline()/json.loads protocol
# (audit r4-gui W1). The parent scans for the sentinel line and skips garbage.
# NOTE: the pid is the WORKER's — the parent must format SENTINEL_FMT with
# proc.pid, not import a pid baked at its own import time.
SENTINEL_FMT = '@KERN-REPL-REPLY:%d@ '
SENTINEL = SENTINEL_FMT % os.getpid()


class Capture(io.TextIOBase):
    """Rolling capture that keeps HEAD + TAIL on overflow (audit r4-gui W4).

    Tracebacks and the first lines of progress output matter most — the old
    tail-only cap silently clipped the actual error away.
    """
    CAP = 16000

    def __init__(self):
        self.head = ''
        self.tail = ''
        self.total = 0

    def write(self, text):
        self.total += len(text)
        self.tail += text
        half = self.CAP // 2
        if len(self.tail) > half:
            if not self.head:
                self.head = self.tail[:half]
            self.tail = self.tail[-half:]
        return len(text)

    def render(self):
        if not self.head:
            return self.tail
        dropped = max(0, self.total - len(self.head) - len(self.tail))
        return (self.head + f'\n[... {dropped} characters omitted ...]\n'
                + self.tail)


def main():
    # Seed __name__ so `if __name__ == "__main__":` blocks in pasted snippets
    # don't run by accident — but the name EXISTS (audit r4-gui W3).
    namespace = {'__name__': '__kern__', '__builtins__': __builtins__}

    def _sigint(_signum, _frame):
        # Runs on the main thread between bytecodes: aborts ONLY the current
        # cell; the namespace (variables/imports) survives (W2). C-level calls
        # that ignore signals are covered by the parent's kill-after-grace.
        raise KeyboardInterrupt('py() cell interrupted')

    try:
        signal.signal(signal.SIGINT, _sigint)
    except (ValueError, OSError):
        pass  # non-main-thread or platform quirk: parent kill still applies

    for line in sys.stdin:
        buf = Capture()
        status = 'succeeded'
        try:
            data = json.loads(line)
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                exec(compile(data['code'], '<kern-py>', 'exec'), namespace)
        except BaseException:
            status = 'failed'
            buf.write(traceback.format_exc())
        text = buf.render() or '(no output; state retained)'
        # Single buffered write to the real fd 1: one atomic-ish line the parent
        # can find by sentinel even if the payload itself printed raw bytes.
        frame = SENTINEL + json.dumps({'text': text, 'status': status}) + '\n'
        try:
            os.write(1, frame.encode('utf-8', 'replace'))
        except OSError:
            sys.stdout.write(frame)
            sys.stdout.flush()


if __name__ == '__main__':
    main()
