from kern.syscalls import is_safe_readonly, redact

tests = [
    ("ls -la", True), ("rg foo src/", True), ("git status", True),
    ("git push origin main", False), ("ls > out.txt", False),
    ("cat f.py | grep def", True), ("echo $(rm -rf /)", False),
    ("sed -i s/a/b/ f", False), ("python3 -c \"x=1\"", False),
    ("find . -name \"*.py\" | head", True), ("git log --oneline | head", True),
]
bad = 0
for cmd, want in tests:
    got = is_safe_readonly(cmd)
    if got != want:
        bad += 1
        print("FAIL", repr(cmd), "got", got, "want", want)
print("matcher:", "ALL OK" if not bad else f"{bad} FAILURES")
print(redact('api_key = "sk-abcdef1234567890abcdef" and AKIAIOSFODNN7EXAMPLE'))
print(redact('-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----\nafter'))
