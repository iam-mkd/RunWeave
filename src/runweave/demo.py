import argparse
import asyncio

from runweave.agents.contracts import IncidentRequest, IncidentResult
from runweave.core.runtime import open_runtime
from runweave.core.settings import Settings
from runweave.deadline import RUN_DEADLINE, deadline_scope


async def run_demo(incident: IncidentRequest, settings: Settings) -> IncidentResult:
    async with open_runtime(settings) as runtime:
        deadline = asyncio.get_running_loop().time() + settings.run_timeout_seconds
        async with deadline_scope(deadline, RUN_DEADLINE):
            return await runtime.runner.run(incident)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one offline synthetic incident triage")
    parser.add_argument("--service", default="payments")
    parser.add_argument("--description", default="Elevated response latency")
    args = parser.parse_args()
    incident = IncidentRequest(service=args.service, description=args.description)
    result = asyncio.run(run_demo(incident, Settings()))
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
