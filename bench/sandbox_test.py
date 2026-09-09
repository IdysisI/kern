import os
os.environ["KERN_SANDBOX"] = "1"
from kern.syscalls import FS, tool_exec
fs = FS("/home/marty/kern-playground")
print(tool_exec(fs, "echo inside > sbx.txt && cat sbx.txt")[0])
print(tool_exec(fs, "touch /usr/local/bin/nope 2>&1 || echo BLOCKED")[0])
print(tool_exec(fs, "curl -s -m 5 -o /dev/null -w \"%{http_code}\" https://pypi.org 2>&1 || echo NETBLOCKED")[0])
