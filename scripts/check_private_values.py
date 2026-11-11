"""Check tracked/unignored source files against a private provider file. Never prints values."""
import argparse
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--private-provider", required=True, type=Path)
args = parser.parse_args()
private = json.loads(args.private_provider.read_text())
url = urlsplit(private["baseUrl"])
values = {private["apiKey"].encode(), private["baseUrl"].encode(), url.netloc.encode()}
files = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]).split(b"\0")
leaks = []
for name in set(files) - {b""}:
    file = Path(name.decode())
    if file.is_file() and any(value in file.read_bytes() for value in values if value):
        leaks.append(str(file))
print(json.dumps({"filesWithPrivateValues": leaks, "passed": not leaks}))
raise SystemExit(bool(leaks))
