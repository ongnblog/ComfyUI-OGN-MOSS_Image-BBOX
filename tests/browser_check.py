"""Real ComfyUI integration check; run against a disposable, local test server.

Requires Playwright and Chromium. This creates and clears the browser's workflow.
    python tests/browser_check.py --url http://127.0.0.1:8188 --chromium /usr/bin/chromium
"""

import argparse
import io
import json
from pathlib import Path
import time

from PIL import Image
from playwright.sync_api import sync_playwright


def run(url, chromium, artifacts):
    artifacts.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium, headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="networkidle")
        page.wait_for_function("window.app?.graph != null")
        page.keyboard.press("Escape")
        assert page.evaluate("window.app.ui.settings.getSettingValue('Comfy.VueNodes.Enabled')") is False
        page.evaluate("""() => {
            const app = window.app;
            app.graph.clear();
            const editor = LiteGraph.createNode('MOSSCreateBoundingBoxesLegacy');
            const formatter = LiteGraph.createNode('MOSSImageLayoutPromptLegacy');
            editor.pos = [120,140]; formatter.pos = [1040,140];
            app.graph.add(editor); app.graph.add(formatter);
            window.editorId = editor.id; window.formatterId = formatter.id;
            const widget = formatter.widgets.find(w => w.name === 'prompt');
            widget.value = 'A travel poster.';
            for (const [slot, name] of [[2,'element'],[3,'width'],[4,'height']]) {
                let index = formatter.findInputSlot(name);
                if (index < 0) {
                    const w = formatter.widgets.find(w => w.name === name);
                    formatter.addInput(name, 'INT', {widget:{name}});
                    w.type = 'converted-widget'; w.computeSize = () => [0,-4];
                    index = formatter.findInputSlot(name);
                }
                editor.connect(slot,formatter,index);
            }
            app.canvas.ds.scale = 1; app.canvas.ds.offset = [0,0];
            app.canvas.setDirty(true,true);
        }""")
        canvas = page.get_by_label("Bounding box canvas")
        canvas.wait_for(state="visible")

        def editor_state():
            return json.loads(page.evaluate("JSON.stringify(window.app.graph.getNodeById(window.editorId).mossEditor.boxes)"))

        def point(x, y):
            return page.evaluate("""([x,y]) => {
                const editor = window.app.graph.getNodeById(window.editorId).mossEditor;
                editor.draw();
                const r = editor.canvas.getBoundingClientRect(), v = editor.view;
                return [r.left+v.x+x*v.w, r.top+v.y+y*v.h];
            }""", [x, y])

        def drag(start, end):
            a, b = point(*start), point(*end)
            page.mouse.move(*a); page.mouse.down()
            page.mouse.move(*b, steps=8); page.mouse.up()

        # Mouse drawing, movement, all-corner resize implementation, and numeric editing.
        drag((0.1, 0.1), (0.5, 0.3))
        assert len(editor_state()) == 1
        drag((0.3, 0.2), (0.4, 0.3))
        assert abs(editor_state()[0]["x"] - 0.2) < 0.01
        box = editor_state()[0]
        drag((box["x"] + box["w"], box["y"] + box["h"]), (0.8, 0.5))
        assert abs(editor_state()[0]["w"] - 0.6) < 0.01
        for key, value in {"x": 128, "y": 96, "w": 768, "h": 128}.items():
            control = page.get_by_label(f"box {key} pixels", exact=True)
            control.fill(str(value)); control.press("Tab")
        page.get_by_label("種類 / type", exact=True).select_option("text")
        page.get_by_label("説明 / desc", exact=True).fill("Large dark-blue title.")
        page.get_by_label("文字 / text", exact=True).fill('EXPLORE\n日本 "MOSS" {a|b}')
        colors = page.get_by_label("色 / palette", exact=True)
        colors.fill("#123456, #fff"); colors.press("Tab")
        expected = editor_state()
        assert expected[0]["palette"] == ["#123456", "#FFFFFF"]
        assert expected[0]["text"].endswith('{a|b}')
        page.get_by_role("button", name="複製", exact=True).click()
        assert len(editor_state()) == 2
        page.get_by_role("button", name="削除", exact=True).click()
        assert len(editor_state()) == 1
        page.get_by_role("button", name="↶", exact=True).click()
        assert len(editor_state()) == 2
        page.get_by_role("button", name="↷", exact=True).click()
        assert editor_state() == expected

        # Clone and actual ComfyUI workflow serialization/restoration.
        saved = page.evaluate("JSON.stringify(window.app.graph.serialize())")
        page.evaluate("""() => {
            const original = window.app.graph.getNodeById(window.editorId);
            const copy = original.clone();
            window.app.graph.add(copy);
            if (copy.mossEditor.boxes.length !== original.mossEditor.boxes.length) throw new Error('clone lost boxes');
            window.app.graph.remove(copy);
        }""")
        page.evaluate("async (data) => { await window.app.loadGraphData(JSON.parse(data)); }", saved)
        assert editor_state() == expected
        # Reload via actual workflow loader, ensuring the hidden STRING becomes API input.
        api_prompt = json.loads(page.evaluate("async () => JSON.stringify(await window.app.graphToPrompt())"))
        data = api_prompt["output"][str(page.evaluate("window.editorId"))]["inputs"]["boxes_json"]
        assert json.loads(data) == expected
        response = page.request.post(url + "/prompt", data={"prompt": api_prompt["output"]})
        assert response.ok, response.text()
        prompt_id = response.json()["prompt_id"]
        # Poll the HTTP result directly: an async browser predicate can resolve
        # before the worker has published the history entry on some runners.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            history = page.request.get(url + "/history/" + prompt_id).json().get(prompt_id)
            if history and history.get("status", {}).get("completed"):
                break
            page.wait_for_timeout(50)
        else:
            raise AssertionError(f"Prompt {prompt_id} did not complete: {history}")
        assert history["status"]["status_str"] == "success", history["status"]
        result = json.loads(history["outputs"][str(page.evaluate("window.formatterId"))]["text"][0])
        assert result["items"][0]["bbox"] == [-416, -384, -288, 384], result
        assert result["items"][0]["text"] == expected[0]["text"]
        # Use ComfyUI's own WebSocket execution path to populate the output DOM widget.
        page.evaluate("async () => { await window.app.queuePrompt(0, 1); }")
        page.wait_for_function("document.querySelector('[aria-label=\"MOSS layout output\"]')?.value.includes('abs_centralized')")
        page.screenshot(path=str(artifacts / "legacy-editor.png"))
        (artifacts / "workflow.json").write_text(saved)
        (artifacts / "prompt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))

        # Optional IMAGE input uses ComfyUI's real upload, LoadImage and temp-image routes.
        image_bytes = io.BytesIO()
        Image.new("RGB", (128, 64), (255, 128, 64)).save(image_bytes, format="PNG")
        uploaded = page.request.post(url + "/upload/image", multipart={
            "image": {"name": "moss-layout-browser-test.png", "mimeType": "image/png", "buffer": image_bytes.getvalue()},
            "overwrite": "true",
        })
        assert uploaded.ok, uploaded.text()
        upload = uploaded.json()
        filename = "/".join(v for v in [upload.get("subfolder"), upload["name"]] if v)
        page.evaluate("""(filename) => {
            const image = LiteGraph.createNode('LoadImage');
            window.app.graph.add(image); window.backgroundNodeId = image.id;
            image.widgets.find(w => w.name === 'image').value = filename;
            const editor = window.app.graph.getNodeById(window.editorId);
            image.connect(0, editor, editor.findInputSlot('background'));
        }""", filename)
        page.evaluate("async () => { await window.app.queuePrompt(0,1); }")
        page.wait_for_function("window.app.graph.getNodeById(window.editorId).mossEditor.background?.naturalWidth === 128")
        page.evaluate("""() => {
            window.app.graph.remove(window.app.graph.getNodeById(window.backgroundNodeId));
        }""")
        page.evaluate("async () => { await window.app.queuePrompt(0,1); }")
        page.wait_for_function("window.app.graph.getNodeById(window.editorId).mossEditor.background === null")

        sample = Path(__file__).resolve().parents[1] / "examples" / "moss_layout_workflow.json"
        page.evaluate("async data => { await window.app.loadGraphData(JSON.parse(data)); }", sample.read_text())
        page.wait_for_function("window.app.graph.getNodeById(1)?.mossEditor?.boxes.length === 2")
        page.evaluate("async () => { await window.app.queuePrompt(0,1); }")
        page.wait_for_function("window.app.graph.getNodeById(2)?.mossPromptOutput?.value.includes('abs_centralized')")
        example_result = json.loads(page.get_by_label("MOSS layout output").input_value())
        assert example_result["items"][0]["bbox"] == [-416, -384, -288, 384]
        assert example_result["items"][1]["bbox"] == [-192, -288, 384, 288]
        page.screenshot(path=str(artifacts / "sample-workflow.png"))
        assert not errors, errors
        browser.close()
    print("PASS: classic UI draw/move/resize/edit, duplicate/delete/undo/redo, clone, save/load, API/browser Queue, background load/clear")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    parser.add_argument("--artifacts", type=Path, default=Path("test-results"))
    args = parser.parse_args()
    run(args.url.rstrip("/"), args.chromium, args.artifacts)
