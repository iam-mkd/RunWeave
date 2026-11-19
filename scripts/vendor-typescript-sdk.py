"""Copy the built TypeScript SDK into the `runweave init --language typescript` template.

The SDK is not on npm yet, so new TypeScript projects get this copy. Build it first:
    npm --prefix sdk/typescript ci && npm --prefix sdk/typescript run build
    python scripts/vendor-typescript-sdk.py
tests/test_eval_cli.py fails if the copy drifts from the build.
"""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk" / "typescript"
TARGET = ROOT / "src" / "runweave" / "templates" / "typescript" / "vendor" / "runweave-agent"


def main() -> None:
    built = SDK / "dist"
    if not (built / "index.js").is_file():
        raise SystemExit("Build sdk/typescript first (npm run build)")
    if TARGET.exists():
        shutil.rmtree(TARGET)
    (TARGET / "dist").mkdir(parents=True)
    for path in sorted(built.iterdir()):
        if path.suffix in {".js", ".ts"}:
            shutil.copyfile(path, TARGET / "dist" / path.name)
    source = json.loads((SDK / "package.json").read_text(encoding="utf-8"))
    manifest = {
        key: source[key]
        for key in (
            "name",
            "version",
            "description",
            "license",
            "type",
            "engines",
            "exports",
            "types",
        )
    }
    manifest["files"] = ["dist"]
    (TARGET / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(ROOT / "LICENSE", TARGET / "LICENSE")
    print(f"Copied the SDK into {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
