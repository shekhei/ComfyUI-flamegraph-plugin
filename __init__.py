"""Capture one native flamegraph and Python profile per queued prompt."""

from contextvars import ContextVar
import cProfile
from functools import wraps
from html import escape
import logging
import os
from pathlib import Path
import platform
import re
import select
import signal
import subprocess
from time import monotonic
from uuid import uuid4

from aiohttp import web

import execution
import folder_paths
from server import PromptServer
import torch

log = logging.getLogger(__name__)
FRAME = re.compile(r"^([ +|!:\t]*)(\d+) (.+)$")
PERF_FRAME = re.compile(r"^\s+[0-9a-fA-F]+\s+(.+?)(?:\s+\(|$)")

node_timings = ContextVar("flamegraph_node_timings", default=None)


def frames_to_svg(frames, root_width, output, timings):
    if not root_width:
        raise ValueError("Native profile contains no call stacks")

    max_depth = max(frame[2] for frame in frames)
    width = 1400
    height = 85 + len(timings) * 18 + (1 + max_depth) * 20
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="#fffaf2"/>',
        '<text x="12" y="24" font-family="sans-serif">Workflow node execution · elapsed wall time</text>',
    ]
    for row, (node_id, class_type, seconds) in enumerate(timings):
        label = escape(f"{node_id}  {class_type}  {seconds * 1000:,.1f} ms")
        svg.append(f'<text x="12" y="{44 + row * 18}" font-family="monospace" font-size="13">{label}</text>')
    svg.append(
        f'<text x="12" y="{50 + len(timings) * 18}" font-family="sans-serif">'
        f'Native stacks · {root_width:,} thread samples · hover for details</text>'
    )
    for left, count, depth, label in frames:
        x = left * width / root_width
        w = count * width / root_width
        y = height - 22 - depth * 20
        hue = 12 + sum(map(ord, label)) % 35
        svg.append(
            f'<g><title>{escape(label)} ({count:,} samples)</title>'
            f'<rect x="{x:.3f}" y="{y}" width="{w:.3f}" height="19" fill="hsl({hue},80%,66%)" stroke="#fffaf2" stroke-width=".35"/>'
        )
        if w >= 21:
            limit = int((w - 6) / 6.6)
            displayed = label if len(label) <= limit else label[:limit - 1] + "..."
            svg.append(f'<text x="{x + 3:.3f}" y="{y + 13}" font-family="monospace" font-size="11">{escape(displayed)}</text>')
        svg.append("</g>")
    output.write_text("\n".join([*svg, "</svg>"]), encoding="utf-8")


def write_macos_flamegraph(report, output, timings):
    call_graph = report.split("Call graph:\n", 1)[1].split("\n\n", 1)[0]
    frames = []
    parents = []
    root_width = 0
    for line in call_graph.splitlines():
        match = FRAME.match(line)
        if not match:
            continue
        indent, count, label = match.groups()
        count = int(count)
        while parents and parents[-1][0] >= len(indent):
            parents.pop()
        if parents:
            parent = parents[-1]
            left = parent[1] + parent[2]
            parent[2] += count
        else:
            left = root_width
            root_width += count
        depth = len(parents)
        parents.append([len(indent), left, 0])
        label = re.sub(r" \+ \d.*$", "", label)
        frames.append((left, count, depth, label))
    frames_to_svg(frames, root_width, output, timings)


def write_linux_flamegraph(report, output, timings):
    folded = {}
    stack = []
    for line in report.splitlines():
        if not line.strip():
            if stack:
                folded[tuple(reversed(stack))] = folded.get(tuple(reversed(stack)), 0) + 1
                stack = []
            continue
        match = PERF_FRAME.match(line)
        if match:
            label = re.sub(r"\+0x[0-9a-fA-F]+$", "", match.group(1).strip())
            stack.append(label)
    if stack:
        folded[tuple(reversed(stack))] = folded.get(tuple(reversed(stack)), 0) + 1

    frames = []
    root_width = 0
    for stack, count in sorted(folded.items()):
        left = root_width
        root_width += count
        for depth, label in enumerate(stack):
            frames.append((left, count, depth, label))
    frames_to_svg(frames, root_width, output, timings)


def start_macos_sample(report):
    process = subprocess.Popen(
        ["/usr/bin/sample", str(os.getpid()), "86400", "2", "-file", str(report)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if select.select([process.stderr], [], [], 5)[0]:
        status = process.stderr.readline().decode(errors="replace")
        if status.startswith("Sampling process "):
            return process
        log.warning("Native profiler could not attach: %s", status.strip())
    else:
        log.warning("Native profiler did not start within five seconds")
    process.kill()
    process.communicate()
    return None


def finish_macos_sample(process, report, timings):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        _, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    if process.returncode or not report.is_file():
        raise RuntimeError(stderr.decode(errors="replace").strip())
    graph = report.with_suffix(".svg")
    write_macos_flamegraph(report.read_text(encoding="utf-8", errors="replace"), graph, timings)
    return graph


def start_linux_perf(report):
    data = report.with_suffix(".perf.data")
    return subprocess.Popen(
        [
            "perf",
            "record",
            "-F",
            "99",
            "-g",
            "--call-graph",
            "dwarf",
            "-p",
            str(os.getpid()),
            "-o",
            str(data),
            "--",
            "sleep",
            "86400",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def finish_linux_perf(process, report, timings):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        _, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    data = report.with_suffix(".perf.data")
    if process.returncode or not data.is_file():
        raise RuntimeError(stderr.decode(errors="replace").strip())

    script = subprocess.run(
        ["perf", "script", "-i", str(data)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    if script.returncode:
        raise RuntimeError(script.stderr.strip())

    report.write_text(script.stdout, encoding="utf-8")
    graph = report.with_suffix(".svg")
    write_linux_flamegraph(script.stdout, graph, timings)
    return graph


def start_native_profile(report):
    system = platform.system()
    if system == "Darwin":
        process = start_macos_sample(report)
        if process is None:
            return None
        return ("macos-sample", process)
    if system == "Linux":
        return ("linux-perf", start_linux_perf(report))
    log.warning("Native profiler is not supported on %s", system)
    return None


def finish_native_profile(native_profile, report, timings):
    kind, process = native_profile
    if kind == "macos-sample":
        return finish_macos_sample(process, report, timings)
    if kind == "linux-perf":
        return finish_linux_perf(process, report, timings)
    raise RuntimeError(f"Unknown native profiler {kind}")


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

        prefix = settings.get("prefix")
        prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", prefix[:48]).strip("-_") if isinstance(prefix, str) else ""
        prefix = prefix or "flamegraph"

        try:
            directory = Path(folder_paths.get_output_directory()) / "profiles"
            directory.mkdir(parents=True, exist_ok=True)
            # Use a generated suffix even if a caller reuses a prompt ID.
            stem = directory / f"{prefix}-{uuid4().hex}"
            report = stem.with_suffix(".txt")
            log.info("Profiling prompt %s in %s", prompt_id, stem)
            native_profile = start_native_profile(report)
        except Exception:
            log.exception("Could not initialize prompt profiling")
            return original(self, prompt, prompt_id, extra_data, execute_outputs)

        python_profile = cProfile.Profile()
        timings = []
        token = node_timings.set(timings)
        try:
            return python_profile.runcall(original, self, prompt, prompt_id, extra_data, execute_outputs)
        finally:
            node_timings.reset(token)
            try:
                if native_profile is not None:
                    graph = finish_native_profile(native_profile, report, timings)
                    artifact = {
                        "filename": graph.name,
                        "download_url": f"/run-flamegraph/{graph.name}",
                    }
                    self.history_result["flamegraph"] = artifact
                    self.history_result["outputs"]["run_flamegraph"] = {
                        "flamegraphs": [{**artifact, "type": "output", "subfolder": "profiles"}],
                    }

            except Exception:
                log.exception("Could not save native profile for prompt %s", prompt_id)
            try:
                python_profile.dump_stats(stem.with_suffix(".pstats"))
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


@PromptServer.instance.routes.get("/run-flamegraph/{filename:(?:[A-Za-z0-9_-]{1,48}-)?[0-9a-f]{32}\\.svg}")
async def download_flamegraph(request):
    filename = request.match_info["filename"]
    graph = Path(folder_paths.get_output_directory()) / "profiles" / filename
    if not graph.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(
        graph,
        headers={
            "Content-Type": "image/svg+xml",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


WEB_DIRECTORY = "web"
install()
NODE_CLASS_MAPPINGS = {"NativeProfileSmokeTest": NativeProfileSmokeTest, "RunFlamegraph": RunFlamegraph}
NODE_DISPLAY_NAME_MAPPINGS = {"NativeProfileSmokeTest": "Native Profile Smoke Test", "RunFlamegraph": "Run Flamegraph"}
