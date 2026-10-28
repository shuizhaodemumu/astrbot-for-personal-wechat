"""Create private configuration for an independently installed native bridge."""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path(".runtime"))
    args = parser.parse_args()
    root = args.runtime_dir.expanduser().resolve()
    if any((root / name).exists() for name in ("server.toml", "client.toml")):
        parser.error("Configuration already exists; refusing to overwrite it")
    os.umask(0o077)
    private = root / "private"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "server.toml").write_text(
        'schemaVersion = 1\nlisten = "127.0.0.1:6174"\n'
        f"dataDir = {json.dumps(str(private), ensure_ascii=False)}\n"
    )
    (root / "client.toml").write_text(
        'schemaVersion = 1\nactiveServer = "local"\n\n'
        '[servers.local]\nurl = "http://127.0.0.1:6174"\n'
        f"tokenFile = {json.dumps(str(private / 'api-token'), ensure_ascii=False)}\n"
    )
    print(f"Private configuration created in {root}")


if __name__ == "__main__":
    main()
