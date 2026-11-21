"""Capture one pyinstrument HTML profile per queued prompt."""

from contextvars import ContextVar
from functools import wraps
import logging
from pathlib import Path
import re
from time import monotonic
from uuid import uuid4

import execution
import folder_paths
from comfy.comfy_types.node_typing import IO
import torch

log = logging.getLogger(__name__)

node_timings = ContextVar("flamegraph_node_timings", default=None)


def start_python_profile(prompt_id):
    from pyinstrument import Profiler

    profiler = Profiler(async_mode="enabled")
    profiler.start(target_description=f"ComfyUI prompt {prompt_id}")
    return profiler


def write_pyinstrument_profile_html(session, output):
    from pyinstrument import renderers

    with open(output, "w") as f:
        f.write(renderers.HTMLRenderer(timeline=True, show_all=True).render(session))
    return output

def write_pyinstrument_profile_speedscope(session, output):
    from pyinstrument import renderers

    with open(output, "w") as f:
        f.write(renderers.SpeedscopeRenderer(timeline=True, show_all=True).render(session))
    return output


def flamegraph_output_node_ids(prompt):
    return [
        str(node_id)
        for node_id, node in prompt.items()
        if node.get("class_type") == "RunFlamegraphOutput"
    ]


def make_artifacts(profile, profile_speedscope, torch_trace=None):
    artifact_html = {
        "filename": profile.name,
        "profile_kind": "python",
    }
    artifact_speedscope = {
        "filename": profile_speedscope.name,
        "profile_kind": "speedscope",
    }
    output_artifacts = [
        {**artifact_html, "type": "output", "subfolder": "profiles"},
        {**artifact_speedscope, "type": "output", "subfolder": "profiles"},
    ]
    if torch_trace is not None:
        output_artifacts.append({
            "filename": torch_trace.name,
            "profile_kind": "torch",
            "type": "output",
            "subfolder": "profiles",
        })
    return artifact_html, artifact_speedscope, output_artifacts


def start_torch_profile(prompt_id):
    from torch.profiler import ProfilerActivity, profile

    activities = [ProfilerActivity.CPU]
    if torch.cuda.is_available():
        activities.append(ProfilerActivity.CUDA)
    profiler = profile(activities=activities, record_shapes=True, profile_memory=True)
    profiler.__enter__()
    return profiler


def install():
    original = execution.PromptExecutor.execute
    original_node = execution.execute

    @wraps(original_node)
    def timed_node(server, dynprompt, caches, current_item, *args, **kwargs):
        timings = node_timings.get()
        if timings is None or caches.outputs.get(current_item) is not None:
            return original_node(server, dynprompt, caches, current_item, *args, **kwargs)

        class_type = dynprompt.get_node(current_item)["class_type"]
        started = monotonic()
        try:
            return original_node(server, dynprompt, caches, current_item, *args, **kwargs)
        finally:
            timings.append((current_item, class_type, monotonic() - started))

    @wraps(original)
    def profiled(self, prompt, prompt_id, extra_data=None, execute_outputs=()):
        extra_data = extra_data or {}
        settings = extra_data.get("run_flamegraph")
        if not isinstance(settings, dict):
            # Honor older saved workflows when queued outside the overlay.
            settings = {"enabled": any(
                prompt[str(node_id)]["class_type"] == "RunFlamegraph"
                and prompt[str(node_id)]["inputs"].get("enabled") is True
                for node_id in execute_outputs
            )}
        if settings.get("enabled") is not True:
            return original(self, prompt, prompt_id, extra_data, execute_outputs)

        torch_enabled = settings.get("torch_enabled") is True

        prefix = settings.get("prefix")
        prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix[:48]).strip("-_") if isinstance(prefix, str) else ""
        prefix = prefix or "flamegraph"

        try:
            directory = Path(folder_paths.get_output_directory()) / "profiles"
            directory.mkdir(parents=True, exist_ok=True)
            # Use a generated suffix even if a caller reuses a prompt ID.
            stem = directory / f"{prefix}-{uuid4().hex}"
            log.info("Profiling prompt %s in %s", prompt_id, stem)
            python_profile = start_python_profile(prompt_id)
            torch_profile = start_torch_profile(prompt_id) if torch_enabled else None
        except Exception:
            log.exception("Could not initialize prompt profiling")
            return original(self, prompt, prompt_id, extra_data, execute_outputs)

        timings = []
        token = node_timings.set(timings)
        try:
            return original(self, prompt, prompt_id, extra_data, execute_outputs)
        finally:
            node_timings.reset(token)
            try:
                python_profile.stop()
                torch_trace = None
                if torch_profile is not None:
                    torch_profile.__exit__(None, None, None)
                    torch_trace = stem.with_name(f"{stem.name}-torch.json")
                    torch_profile.export_chrome_trace(str(torch_trace))
                last_session = python_profile.last_session
                profile = write_pyinstrument_profile_html(python_profile.last_session, stem.with_suffix(".html"))
                profile_speedscope = write_pyinstrument_profile_speedscope(last_session, stem.with_suffix(".json"))
                artifact_html, artifact_speedscope, output_artifacts = make_artifacts(profile, profile_speedscope, torch_trace)

                self.history_result["flamegraph"] = artifact_html
                self.history_result["flamegraph.speedscope"] = artifact_speedscope
                output_node_ids = flamegraph_output_node_ids(prompt)
                if not output_node_ids:
                    output_node_ids = ["profile.html_report"]
                for node_id in output_node_ids:
                    self.history_result["outputs"][node_id] = {"flamegraphs": output_artifacts}

            except Exception:
                log.exception("Could not save Python profile for prompt %s", prompt_id)

    execution.execute = timed_node
    execution.PromptExecutor.execute = profiled


class RunFlamegraph:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"enabled": ("BOOLEAN", {"default": True})}}

    @classmethod
    def VALIDATE_INPUTS(cls, input_types):
        if "enabled" in input_types:
            return "Use the enabled switch directly so profiling can start before execution."
        return True

    RETURN_TYPES = ()
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "profiling"

    def run(self, enabled):
        return ()


class RunFlamegraphOutput:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "anything": (IO.ANY, {"forceInput": True}),
            }
        }

    @classmethod
    def VALIDATE_INPUTS(cls, input_types):
        return True

    RETURN_TYPES = (IO.ANY,)
    RETURN_NAMES = ("anything",)
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "profiling"

    def run(self, anything):
        return {
            "ui": {
                "flamegraphs": [{
                    "filename": "Profile will be attached when the workflow finishes.",
                    "profile_kind": "pending",
                }]
            },
            "result": (anything,),
        }


class NativeProfileSmokeTest:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    @classmethod
    def IS_CHANGED(cls):
        return float("nan")

    RETURN_TYPES = ()
    FUNCTION = "run"
    OUTPUT_NODE = True
    CATEGORY = "profiling"

    def run(self):
        matrix = torch.rand((256, 256), device="cpu")
        stop = monotonic() + 0.35
        while monotonic() < stop:
            torch.mm(matrix, matrix)
        return ()


WEB_DIRECTORY = "web"
install()
NODE_CLASS_MAPPINGS = {
    "NativeProfileSmokeTest": NativeProfileSmokeTest,
    "RunFlamegraph": RunFlamegraph,
    "RunFlamegraphOutput": RunFlamegraphOutput,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "NativeProfileSmokeTest": "Native Profile Smoke Test",
    "RunFlamegraph": "Run Flamegraph",
    "RunFlamegraphOutput": "Run Flamegraph Output",
}
