"""Regression: redact_py_file_reads must not destroy legit output (audit r3 F1).

Old behavior: any open('...')/cat match (including write-mode opens and piped
cat) + output > 4000 chars => hard-trim middle with NO recoverable pointer.
New contract:
  - write/append/exclusive opens never trigger trimming;
  - trimming only happens when the file's bytes are VERIFIED present in output;
  - when trimming, the full original is offloaded and the path is in the marker.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kern.constraints import redact_py_file_reads


class _Sess:
    def __init__(self):
        self.offloaded = {}

    def offload(self, tag, content):
        p = f"/tmp/scratch/{tag}-abc.txt"
        self.offloaded[p] = content
        return p


def test_write_mode_open_never_trims():
    s = _Sess()
    long_report = "RESULT LINE\n" * 500          # ~6000 chars, no file bytes
    out, meta = redact_py_file_reads(s, "py",
        "f=open('/tmp/x.json','w'); f.write(r); print(long_report)", long_report)
    assert "RESULT LINE" in out and out.count("RESULT LINE") == 500, \
        "write-mode open trimmed a legit report"
    assert meta == {}, f"constraint fired on write-mode open: {meta}"


def test_piped_cat_without_file_bytes_not_trimmed():
    s = _Sess()
    tmp = Path("/tmp/kern_redact_probe.txt")
    tmp.write_text("UNIQUEFILECONTENT123\n" * 3)
    out, meta = redact_py_file_reads(s, "exec", f"cat {tmp} | grep OTHER",
                                     "grep results\n" * 600)   # no file bytes inside
    assert out.count("grep results") == 600, "piped cat output trimmed despite no file bytes"


def test_true_positive_trims_with_recoverable_pointer(tmp_path):
    s = _Sess()
    f = tmp_path / "data.txt"
    payload = "SECRETFILEBYTES-%d\n" % 7
    f.write_text(payload * 10)
    out, meta = redact_py_file_reads(s, "py", f"print(open('{f}').read())",
                                     payload * 400)            # bytes ARE in output
    assert meta.get("constraint") == "redact_py_file_reads"
    assert "redacted" in out
    assert any(p in out for p in s.offloaded), \
        f"trimmed output lacks recoverable pointer; keys={list(s.offloaded)}"
    # the preserved original must contain the full text
    assert all(payload * 400 in v for v in s.offloaded.values())


def test_short_output_with_real_bytes_gets_hint_only(tmp_path):
    s = _Sess()
    f = tmp_path / "host.txt"
    f.write_text("myhost\n")
    out, meta = redact_py_file_reads(s, "exec", f"cat {f}", "myhost\n")
    assert out.startswith("myhost"), "short output mangled"
    assert "read() tool" in out and "redacted (" not in out, \
        "short true positive must get hint, not trim"


def test_short_output_without_file_bytes_gets_nudge_not_trim():
    s = _Sess()
    out, meta = redact_py_file_reads(s, "exec", "cat /etc/hostname", "unrelated\n")
    assert out.startswith("unrelated\n"), "output mangled"
    assert "read() tool" in out, "nudge missing"
    assert "redacted (" not in out, "trimmed without verified bytes"
    assert meta.get("constraint") == "redact_py_file_reads", \
        "pattern match must still be reported (anti-bypass intent)"


def test_heredoc_and_string_literals_are_not_file_reads():
    """cat/open tokens inside heredoc bodies or string literals are DATA, not
    commands (audit r4-verify F1: a heredoc writing a python file that reads
    /etc/passwd made the constraint fire and truncate the whole exec output)."""
    s = _Sess()
    heredoc = ('bash -c "cat > /tmp/x.py <<\'EOF\'\n'
               'import json\n'
               'data = open(\'/etc/passwd\',\'r\').read()\n'
               'EOF"')
    out, meta = redact_py_file_reads(s, "exec", heredoc, "X" * 6000)
    assert meta == {}, f"heredoc body matched as a file read: {meta}"
    assert out.count("X") == 6000, "output truncated on heredoc false positive"

    out2, meta2 = redact_py_file_reads(s, "exec", 'grep -n "open(" kern/engine.py', "Y" * 6000)
    assert meta2 == {}, f"string-literal pattern matched: {meta2}"
    assert out2.count("Y") == 6000


def test_stdin_cat_and_flag_targets_not_matched():
    s = _Sess()
    out, meta = redact_py_file_reads(s, "exec", "echo hi | cat - > /dev/null", "Z" * 6000)
    assert meta == {}, f"cat - (stdin) matched as file read: {meta}"


def test_true_positives_still_fire_with_clean_targets():
    s = _Sess()
    f = Path("/tmp/kern_redact_tp.txt")
    f.write_text("TPCONTENT\n" * 5)
    out, meta = redact_py_file_reads(s, "exec", f"cat {f},", "TPCONTENT\n" * 500)
    assert meta.get("constraint") == "redact_py_file_reads"
    assert meta.get("redact_target") == str(f), f"target not cleaned: {meta}"
    out2, meta2 = redact_py_file_reads(s, "py", f"print(open('{f}').read())", "TPCONTENT\n" * 500)
    assert meta2.get("redact_target") == str(f), f"py target masked: {meta2}"
