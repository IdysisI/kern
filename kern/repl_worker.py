"""Isolated persistent interpreter. A timeout kills this process, not a thread."""
import contextlib
import io
import json
import os
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
    def __init__(self):
        self.text = ''
        self.dropped = 0

    def write(self, text):
        self.text += text
        if len(self.text) > 16000:
            self.dropped += len(self.text) - 16000
            self.text = self.text[-16000:]
        return len(text)


def main():
    namespace = {}
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
        text = buf.text or '(no output; state retained)'
        if buf.dropped:
            text = f'[{buf.dropped} characters omitted]\n' + text
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
