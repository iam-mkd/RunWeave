"""Run native startup verification against locally built immutable worker images."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from runweave.core.settings import Settings
from runweave.security import LOCAL_PRINCIPAL, ContentPolicy
from runweave.tools.browser_fixtures import ORIGIN
from runweave.tools.isolated import BrowserRead, IsolatedTools, PythonProgram


async def check(python_image: str, browser_image: str) -> dict[str, object]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        model_provider="fake",
        auth_mode="local",
        isolated_tools_enabled=True,
        sandbox_python_image=python_image,
        sandbox_browser_image=browser_image,
        browser_allowed_origins=(ORIGIN,),
        sandbox_fixture_credential=SecretStr("synthetic-broker-private-canary"),
    )
    tools = IsolatedTools(settings, ContentPolicy(("synthetic-broker-private-canary",)))
    try:
        await tools.start()
        calculated = await tools.python(
            PythonProgram(code="result=sum(input_data)", data=[1, 2, 3]), LOCAL_PRINCIPAL
        )
        page = await tools.browser(BrowserRead(url=ORIGIN + "/"), LOCAL_PRINCIPAL)
        return {
            **tools.proof,
            "python_demo": calculated.model_dump(mode="json"),
            "browser_demo": page.model_dump(mode="json"),
            "cleanup_confirmed": not tools.runtime.names,
        }
    finally:
        await tools.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-image", required=True)
    parser.add_argument("--browser-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    proof = asyncio.run(check(args.python_image, args.browser_image))
    root = Path(__file__).resolve().parents[1]
    proof["verified_at_utc"] = datetime.now(UTC).isoformat()
    proof["source_sha256"] = {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(
            [*root.joinpath("sandbox").glob("*.py"), *root.joinpath("sandbox").glob("Dockerfile.*")]
            + [
                root / "src/runweave/tools" / name
                for name in (
                    "container_runtime.py",
                    "isolated.py",
                    "isolation_checks.py",
                    "python_policy.py",
                    "browser_fixtures.py",
                )
            ]
        )
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Native code and browser startup checks passed")


if __name__ == "__main__":
    main()
