"""Harbor 0.25 installed-agent integration using Varin's native Host APIs."""
import json
from pathlib import Path
import shlex
import tarfile
import tempfile

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .trajectory import export_trajectory, apply_usage


class VarinAgent(BaseInstalledAgent):
    capabilities = AgentCapabilities(atif=True, native_config=True)

    @staticmethod
    def name() -> str:
        return "varin"

    def __init__(self, logs_dir: Path, *, runtime_bundle: str, config=None, directory: str | None = None, **kwargs):
        if config is None:
            raise ValueError("Pass --ak config=<path> with a Varin evaluation configuration")
        self.bundle = Path(runtime_bundle).resolve(strict=True)
        with tarfile.open(self.bundle) as archive:
            stream = archive.extractfile("bundle.json")
            if stream is None:
                raise ValueError("Missing bundle.json; use scripts/prepare_runtime.py")
            self.provenance = json.load(stream)
        self.directory = directory
        super().__init__(logs_dir=logs_dir, config=config, **kwargs)
        self.configuration = (json.loads(self.config_source.read_text())
                              if isinstance(self.config_source, Path) else self.config_source)
        # --model selects a registered Varin provider/model pair, preserving native naming.
        if self.model_name:
            provider, separator, model = self.model_name.partition("/")
            if not separator or not provider or not model:
                raise ValueError("--model must be a registered Varin provider/model pair")
            self.configuration["model"] = {**self.configuration["model"], "providerId": provider, "modelId": model}
        self.run_env = dict(self.extra_env)
        for channel in self.configuration["models"]["channels"]:
            auth = channel["authentication"]
            if auth["kind"] == "environment":
                variable = auth["variable"]
                value = self._get_env(variable)
                if value is None:
                    raise ValueError(f"Missing configured model credential environment variable: {variable}")
                self.run_env[variable] = value

    def version(self) -> str:
        return self.provenance["varin"]["sourceRevision"]

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(environment, command="mkdir -p /installed-agent/varin")
        await environment.upload_file(self.bundle, "/installed-agent/varin-runtime.tgz")
        await self.exec_as_root(environment, command="tar -xzf /installed-agent/varin-runtime.tgz -C /installed-agent/varin")
        await environment.upload_file(Path(__file__).with_name("run.mjs"), "/installed-agent/varin-run.mjs")
        await environment.upload_file(Path(__file__).with_name("configuration.mjs"), "/installed-agent/configuration.mjs")
        await self.exec_as_root(environment, command="/installed-agent/varin/bin/node --version")

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        instruction = self.render_instruction(instruction)
        # The task image supplies its cwd; never assume every benchmark uses /app.
        directory = self.directory
        if directory is None:
            result = await self.exec_as_agent(environment, command="pwd -P")
            directory = result.stdout.strip()
        output = str(self.environment_logs_dir / "varin")
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "instruction.txt"
            settings = Path(temporary) / "config.json"
            task.write_text(instruction, encoding="utf-8")
            settings.write_text(json.dumps(self.configuration), encoding="utf-8")
            await environment.upload_file(task, "/installed-agent/varin-instruction.txt")
            await environment.upload_file(settings, "/installed-agent/varin-config.json")
        command = shlex.join([
            "/installed-agent/varin/bin/node", "/installed-agent/varin-run.mjs",
            "--runtime", "/installed-agent/varin", "--config", "/installed-agent/varin-config.json",
            "--task", "/installed-agent/varin-instruction.txt", "--workdir", directory,
            "--output", output, "--keep-host",
        ])
        await self.exec_as_agent(environment, command=command, env=self.run_env)

    def populate_context_post_run(self, context: AgentContext) -> None:
        # Harbor syncs the original agent logs before this callback, including on failure.
        directory = self.logs_dir / "varin"
        record_path = directory / "run.json"
        if not record_path.exists():
            return  # An install/transport failure remains Harbor's original failure.
        record = json.loads(record_path.read_text())
        context.metadata = {**(context.metadata or {}), "varin": record}
        usage_path = directory / "usage.json"
        if usage_path.exists():
            apply_usage(context, json.loads(usage_path.read_text()))
        if (directory / "histories.json").exists():
            trajectory = export_trajectory(directory)
            (self.logs_dir / "trajectory.json").write_text(trajectory.model_dump_json(indent=2), encoding="utf-8")
