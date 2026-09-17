"""Isolated persistent interpreter. A timeout kills this process, not a thread."""
import contextlib
import io
import json
import sys
import traceback


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
        print(json.dumps({'text': text, 'status': status}), flush=True)


if __name__ == '__main__':
    main()
