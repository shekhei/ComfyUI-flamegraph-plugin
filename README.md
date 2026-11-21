# ComfyUI-flamegraph-plugin
a plugin to add flamegraph of the run to output

Profiling uses `pyinstrument` and writes pyinstrument's HTML profile into the ComfyUI queue output for each profiled run.

Use the **Run Flamegraph Output** node as the workflow output anchor. Connect any upstream value to its `anything` input, such as the image coming out of `VAEDecode`. When profiling is enabled in the overlay, the finished queue item includes pyinstrument HTML and Speedscope JSON reports. The separate **Enable Torch profiler** switch adds a Chrome trace JSON, with CPU events and CUDA events when available.

Install dependencies from the plugin directory:

```bash
pip install -r requirements.txt
```
