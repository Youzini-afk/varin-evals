"""Load private connection values into the declared native Varin configuration."""
import json
import os
from pathlib import Path


def load_configuration(path: Path, private_provider: Path | None = None,
                       private_aux_provider: Path | None = None) -> tuple[dict, dict[str, str]]:
    config = json.loads(path.read_text())
    environment = dict(os.environ)
    if private_provider:
        private = json.loads(private_provider.read_text())
        environment["VARIN_EVAL_BASE_URL"] = private["baseUrl"]
        environment["VARIN_EVAL_API_KEY"] = private["apiKey"]
        previous = config["model"]
        provider_id, previous_model = previous["providerId"], previous["modelId"]
        model_id = private["model"]
        effort = private.get("thinkingLevel", previous.get("thinkingLevel"))
        for channel in config["models"]["channels"]:
            if channel["id"] == provider_id:
                for model in channel["models"]:
                    if model["id"] == previous_model:
                        model["id"] = model_id
        config["model"] = {**previous, "modelId": model_id, "thinkingLevel": effort}
        settings = config.get("agentSettings", {})
        if (settings.get("defaultProvider"), settings.get("defaultModel")) == (provider_id, previous_model):
            settings.update(defaultModel=model_id, defaultThinkingLevel=effort)
        utility = settings.get("utilityModel")
        if utility and (utility["providerId"], utility["modelId"]) == (provider_id, previous_model):
            utility["modelId"] = model_id
            settings["utilityThinkingLevel"] = effort
        for binding in settings.get("harness", {}).get("models", {}).values():
            if (binding.get("providerId"), binding.get("modelId")) == (provider_id, previous_model):
                binding["modelId"] = model_id
                binding.setdefault("agent", {}).setdefault("modelSettings", {})["thinkingLevel"] = effort
        selector = settings.get("harness", {}).get("delegationModelSelection", {})
        if selector.get("model") == {"providerId": provider_id, "modelId": previous_model}:
            selector.update(model={"providerId": provider_id, "modelId": model_id}, thinkingLevel=effort)
    if private_aux_provider:
        private = json.loads(private_aux_provider.read_text())
        environment["VARIN_AUX_BASE_URL"] = private["baseUrl"]
        environment["VARIN_AUX_API_KEY"] = private["apiKey"]
    return config, environment


def model_bindings(config: dict) -> dict:
    settings = config.get("agentSettings", {})
    harness = settings.get("harness", {})
    return {
        "primary": config["model"],
        "utility": {"model": settings.get("utilityModel"), "thinkingLevel": settings.get("utilityThinkingLevel")},
        "roles": {role: {"providerId": binding.get("providerId"), "modelId": binding.get("modelId"),
                          "thinkingLevel": binding.get("agent", {}).get("modelSettings", {}).get("thinkingLevel")}
                  for role, binding in harness.get("models", {}).items()},
        "embedding": harness.get("embedding"),
        "fastDecision": harness.get("fastDecision"),
    }
