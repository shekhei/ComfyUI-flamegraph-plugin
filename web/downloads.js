import { api } from "../../scripts/api.js";
import { app } from "../../scripts/app.js";

app.registerExtension({
    name: "run_flamegraph.controls",
    setup() {
        const storageKey = "comfy.runFlamegraph";
        let saved = {};
        try {
            saved = JSON.parse(localStorage.getItem(storageKey)) ?? {};
        } catch {
            // Continue with defaults when browser storage is unavailable.
        }

        const panel = document.createElement("details");
        panel.open = true;
        panel.style.cssText = "position:fixed;left:16px;bottom:16px;z-index:1000;"
            + "padding:10px 14px;border-radius:8px;background:var(--comfy-menu-bg,#202020);"
            + "color:var(--fg-color,#eee);font:14px sans-serif;box-shadow:0 2px 12px #0005";
        const heading = document.createElement("summary");
        heading.textContent = "Run Flamegraph";
        heading.style.cursor = "pointer";
        const form = document.createElement("div");
        form.style.cssText = "display:grid;gap:8px;margin-top:10px";

        const enabled = document.createElement("input");
        enabled.type = "checkbox";
        enabled.checked = saved.enabled !== false;
        const enabledLabel = document.createElement("label");
        enabledLabel.append(enabled, " Enable profiling");

        const prefix = document.createElement("input");
        prefix.type = "text";
        prefix.maxLength = 48;
        prefix.placeholder = "flamegraph";
        prefix.value = typeof saved.prefix === "string" ? saved.prefix : "flamegraph";
        const prefixLabel = document.createElement("label");
        prefixLabel.style.cssText = "display:grid;gap:4px";
        prefixLabel.append("Filename prefix", prefix);

        form.append(enabledLabel, prefixLabel);
        panel.append(heading, form);
        document.body.append(panel);

        const settings = () => ({ enabled: enabled.checked, prefix: prefix.value });
        const save = () => {
            try {
                localStorage.setItem(storageKey, JSON.stringify(settings()));
            } catch {
                // Current controls still apply to queued runs.
            }
        };
        enabled.addEventListener("change", save);
        prefix.addEventListener("input", save);

        const fetchApi = api.fetchApi.bind(api);
        api.fetchApi = (route, options) => {
            if (route === "/prompt" && options?.method === "POST" && typeof options.body === "string") {
                const body = JSON.parse(options.body);
                body.extra_data = { ...body.extra_data, run_flamegraph: settings() };
                return fetchApi(route, { ...options, body: JSON.stringify(body) });
            }
            return fetchApi(route, options);
        };
    },
});
