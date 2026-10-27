"""Build an offline Windows OCR component from a verified local installation."""
import argparse
import json
import sys
import zipfile
from pathlib import Path

from rsci.core.config import Config
from rsci.core.hashing import file_sha256
from rsci.core.project_paths import is_link
from rsci.research.ocr_runtime import status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = Config(args.config_dir)
    runtime = status(config)
    if not runtime["ready"]:
        raise SystemExit("Install and verify the OCR component first")
    root = (config.home / "research-runtime" / runtime["models"]).parent
    output = Path(args.output).resolve()
    if root.resolve() in output.parents:
        raise SystemExit("Output must be outside the runtime")
    output.parent.mkdir(parents=True, exist_ok=True)
    files = {}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in root.rglob("*"):
            if not path.is_file() or is_link(path) or any(is_link(p) for p in path.parents if p != root):
                continue
            relative = path.relative_to(root).as_posix()
            if any(part.startswith('.') or part == '__pycache__' for part in Path(relative).parts):
                continue
            if relative != 'models.json':
                archive.write(path, relative)
                files[relative] = file_sha256(path)
        import hashlib
        models = {n: digest for n, digest in files.items() if n.startswith('models/')}
        model_bytes = json.dumps(models, indent=2).encode()
        archive.writestr('models.json', model_bytes)
        files['models.json'] = hashlib.sha256(model_bytes).hexdigest()
        archive.writestr("component.json", json.dumps({"schema": "rsci.ocr-component/1", "platform": sys.platform,
            "versions": runtime["versions"], "files": files}, indent=2))
    print(json.dumps({"path": str(output), "sha256": file_sha256(output), "files": len(files)}))


if __name__ == "__main__":
    main()
