"""Extract the installed application from an official OCI image, without a daemon.

Requires Google's `crane` CLI. The benchmark's task image is never replaced.
"""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time


def prepare(image: str, platform: str, output: Path, download_retries: int = 2) -> None:
    if output.exists():
        raise FileExistsError(output)
    digest = subprocess.check_output(
        ["crane", "digest", "--platform", platform, image], text=True
    ).strip()
    repository = image.split("@", 1)[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    pinned = f"{repository}@{digest}"
    image_config = json.loads(subprocess.check_output(["crane", "config", "--platform", platform, pinned], text=True))
    image_path = next(value[5:] for value in image_config["config"]["Env"] if value.startswith("PATH="))
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = output.parent / "image-cache"
    cache.mkdir(exist_ok=True)
    image_archive = cache / f"{digest.removeprefix('sha256:')}.tar"
    if not image_archive.exists():
        pending = image_archive.with_suffix(".pending.tar")
        for attempt in range(download_retries + 1):
            result = subprocess.run(["crane", "pull", "--platform", platform, "--cache_path", str(cache / "layers"), pinned, str(pending)])
            if result.returncode == 0:
                pending.replace(image_archive)
                break
            if attempt == download_retries:
                result.check_returncode()
            time.sleep(2 ** attempt)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        root = Path(temporary)
        archive = root / "rootfs.tar"
        with image_archive.open("rb") as source:
            subprocess.run(["crane", "export", "-", str(archive)], stdin=source, check=True)
        bundle = root / "bundle"
        bundle.mkdir()
        with tarfile.open(archive) as source:
            prefix = "home/varin/app/"
            for member in source:
                original = member.name.removeprefix("./")
                if not original.startswith(prefix) or original == prefix:
                    continue
                # Keep relative dependency symlinks; reject links escaping the bundle.
                member = copy.copy(member)
                member.name = "app/" + original[len(prefix):]
                if member.islnk():
                    member.linkname = "app/" + member.linkname.removeprefix("./").removeprefix(prefix)
                source.extract(member, bundle, filter="data")
            binaries = {member.name.removeprefix("./"): member for member in source.getmembers()}
            for name in ("node", "bun"):
                member = next((binaries.get(f"{directory.strip('/')}/{name}")
                               for directory in image_path.split(":")
                               if f"{directory.strip('/')}/{name}" in binaries), None)
                if member is None:
                    raise FileNotFoundError(f"Image PATH has no {name} executable")
                stream = source.extractfile(member)
                if stream is None:
                    raise ValueError(f"Cannot read {name} from image")
                destination = bundle / "bin" / name
                destination.parent.mkdir(exist_ok=True)
                with stream, destination.open("wb") as target:
                    import shutil
                    shutil.copyfileobj(stream, target)
                destination.chmod(0o755)
        runtime = json.loads((bundle / "app/cloud-runtime.json").read_text())
        provenance = {"schemaVersion": 1, "image": pinned, "platform": platform, "varin": runtime}
        (bundle / "bundle.json").write_text(json.dumps(provenance, indent=2) + "\n")
        with tarfile.open(output, "w:gz", dereference=False) as target:
            for child in bundle.iterdir():
                target.add(child, arcname=child.name)
    print(json.dumps({"archive": str(output), **provenance}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download-retries", type=int, default=2)
    args = parser.parse_args()
    prepare(args.image, args.platform, args.output, args.download_retries)
