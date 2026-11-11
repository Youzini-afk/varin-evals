import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from varin_evals.harbor import VarinAgent


class AdapterConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "runtime.tgz"
        body = json.dumps({"varin": {"sourceRevision": "fixture-revision"}}).encode()
        with tarfile.open(self.bundle, "w:gz") as archive:
            metadata = tarfile.TarInfo("bundle.json")
            metadata.size = len(body)
            archive.addfile(metadata, io.BytesIO(body))

    def test_registered_channel_and_full_model_identifier_survive_harbor_selection(self):
        config = {"model": {"providerId": "old", "modelId": "old", "thinkingLevel": "high"}, "models": {"channels": []}}
        agent = VarinAgent(self.root, runtime_bundle=str(self.bundle), config=config,
                           model_name="my-channel/vendor/model-v1")
        self.assertEqual(agent.configuration["model"], {"providerId": "my-channel", "modelId": "vendor/model-v1", "thinkingLevel": "high"})
        self.assertEqual(agent.version(), "fixture-revision")

    def test_missing_declared_credential_fails_before_environment_execution(self):
        config = {"model": {}, "models": {"channels": [{"authentication": {"kind": "environment", "variable": "VARIN_EVALS_TEST_MISSING_CREDENTIAL"}}]}}
        with self.assertRaisesRegex(ValueError, "Missing configured model credential"):
            VarinAgent(self.root, runtime_bundle=str(self.bundle), config=config)


if __name__ == "__main__":
    unittest.main()
