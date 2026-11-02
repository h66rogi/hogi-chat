#!/usr/bin/env python3
"""run_remote.py <script.py> [ssh-timeout]

Reads the read-only archive credential from a secret manager in-process, makes sure the tiny bootstrap
`remote_boot.py` (no secrets in it) exists on the analysis host, then opens one SSH session and
pipes: one JSON credential line, then the given script. The bootstrap executes the script
in memory.

The remote argv is one plain here-doc command whose only variable part is a file path, so no
quoting layer can mangle it, and the credential never touches a file, argv, the environment,
the remote disk, or this process's stdout/stderr.
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import config  # noqa: E402  (repo root)
import json, os, subprocess, sys

OP = os.path.expanduser("~/.local/bin/op-safe")
VAULT = os.environ.get("HOGI_OP_VAULT", "Engineering")
TITLE = os.environ.get("HOGI_OP_ITEM", "chat-archive read-only")
HERE = os.path.dirname(os.path.abspath(__file__))
BOOT_SRC = open(os.path.join(HERE, "remote_boot.py")).read()
BOOT_REMOTE = "$HOME/hogi-chat/archive/bin/boot.py"


def place(remote_secs):
    return ("mkdir -p $HOME/hogi-chat/archive/bin && cat > " + BOOT_REMOTE + " <<'BOOTEOF'\n" +
            BOOT_SRC.rstrip("\n") + "\nBOOTEOF\n" +
            "chmod 644 " + BOOT_REMOTE + " && timeout -k 30 %d nice -n 19 ionice -c 3 $HOME/hogi-chat/archive/.venv/bin/python " % remote_secs +
            BOOT_REMOTE)

SSH = ["ssh", "-i", os.path.expanduser(os.environ.get("HOGI_SSH_KEY", "~/.ssh/id_ed25519")),
       "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
       "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6",
       os.environ.get("HOGI_SSH_HOST", "ubuntu@example-host")]


def read_cred():
    env = dict(os.environ, OP_SAFE_TIMEOUT="120")
    r = subprocess.run([OP, "item", "list", "--vault", VAULT, "--format", "json"],
                       capture_output=True, text=True, timeout=180, env=env)
    if r.returncode != 0:
        raise SystemExit("secret manager list failed rc=%d" % r.returncode)
    ids = [i["id"] for i in json.loads(r.stdout) if i.get("title") == TITLE]
    if len(ids) != 1:
        raise SystemExit("expected exactly 1 item titled %r, found %d" % (TITLE, len(ids)))
    g = subprocess.run([OP, "item", "get", ids[0], "--vault", VAULT, "--format", "json"],
                       capture_output=True, text=True, timeout=180, env=env)
    if g.returncode != 0:
        raise SystemExit("secret manager read failed rc=%d" % g.returncode)
    d = json.loads(g.stdout)
    f = {x.get("id"): x.get("value") for x in d.get("fields", [])}
    ak, sk = (f.get("username") or "").strip(), (f.get("credential") or "").strip()
    if not ak or not sk:
        raise SystemExit("credential fields missing in secret item")
    return {"ak": ak, "sk": sk,
            "endpoint": "https://36875e4c357ab3a6fcfabe48f617dfb7.r2.cloudflarestorage.com",
            "bucket": os.environ.get("HOGI_R2_BUCKET", "")}


def main():
    src_path = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 3600
    creds = read_cred()
    with open(src_path) as fh:
        script = fh.read()
    payload = json.dumps(creds) + "\n" + script
    remote_secs = max(60, timeout - 60)
    proc = subprocess.Popen(SSH + [place(remote_secs)], stdin=subprocess.PIPE, stdout=None, stderr=None, text=True)
    try:
        proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        left = subprocess.run(SSH + ["pgrep -c -f hogi-chat/archive/bin/boot.py || true"],
                              capture_output=True, text=True, timeout=60).stdout.strip()
        print("REMOTE TIMEOUT after %ss (remote timeout %ss); leftover remote procs: %s"
              % (timeout, remote_secs, left or "0"), file=sys.stderr)
        raise SystemExit(124)
    sys.exit(proc.returncode or 0)


if __name__ == "__main__":
    main()
