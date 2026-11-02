import json
import sys

creds = json.loads(sys.stdin.readline())
code = sys.stdin.read()
exec(compile(code, "<remote>", "exec"), {"_": None, "CREDS": creds, "code": code})
