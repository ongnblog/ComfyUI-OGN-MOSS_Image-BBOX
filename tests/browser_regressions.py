"""Regression checks against a disposable ComfyUI server with Nodes 2.0 disabled.

python tests/browser_regressions.py --url http://127.0.0.1:8188 --chromium /usr/bin/chromium
The tests create browser workflows and change tabs. Do not use a working session.
"""

import argparse
import json
from pathlib import Path
import sys
import time
import unittest

from playwright.sync_api import sync_playwright


URL = "http://127.0.0.1:8188"
CHROMIUM = "/usr/bin/chromium"
FIELDS = ["prompt", "width", "height", "character_boxes"]
BOX = dict(x=0.2, y=0.2, w=0.4, h=0.4, type="obj", desc="subject", text="", palette=[])


class BrowserRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1600, "height": 1100}, device_scale_factor=2)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(URL, wait_until="networkidle")
        self.page.wait_for_function("window.app?.graph != null")
        self.page.keyboard.press("Escape")
        self.assertFalse(self.page.evaluate("window.app.ui.settings.getSettingValue('Comfy.VueNodes.Enabled')"))
        self.page.evaluate("""() => {
            const app = window.app; app.graph.clear();
            const editor = LiteGraph.createNode('MOSSCreateBoundingBoxesLegacy');
            const formatter = LiteGraph.createNode('MOSSImageLayoutPromptLegacy');
            editor.pos = [120,140]; formatter.pos = [1040,140];
            app.graph.add(editor); app.graph.add(formatter);
            window.editorId = editor.id; window.formatterId = formatter.id;
            editor.connect(2,formatter,formatter.findInputSlot('element'));
            app.canvas.ds.scale = 1; app.canvas.ds.offset = [0,0];
            app.canvas.setDirty(true,true);
        }""")
        self.page.get_by_label("Bounding box canvas").wait_for(state="visible")
        self.load_box()

    def tearDown(self):
        errors = list(self.errors)
        self.context.close()
        self.assertEqual(errors, [], "Browser errors")

    def load_box(self, box=None):
        self.page.evaluate("""(box) => {
            const node = window.app.graph.getNodeById(window.editorId);
            node.widgets.find(w=>w.name==='boxes_json').value = JSON.stringify([box]);
            node.mossEditor.load();
        }""", box or BOX)

    def boxes(self):
        return json.loads(self.page.evaluate("JSON.stringify(window.app.graph.getNodeById(window.editorId).mossEditor.boxes)"))

    def point(self, x, y):
        return self.page.evaluate("""([x,y]) => {
            const ed = window.app.graph.getNodeById(window.editorId).mossEditor;
            ed.draw();
            const rect = ed.canvas.getBoundingClientRect(), v = ed.view;
            return [rect.left+(v.x+x*v.w)*rect.width/ed.canvas.clientWidth,
                    rect.top+(v.y+y*v.h)*rect.height/ed.canvas.clientHeight];
        }""", [x, y])

    def drag(self, start, end):
        a, b = self.point(*start), self.point(*end)
        self.page.mouse.move(*a)
        self.page.mouse.down()
        kind = self.page.evaluate("window.app.graph.getNodeById(window.editorId).mossEditor.drag?.kind")
        self.page.mouse.move(*b, steps=8)
        self.page.mouse.up()
        return kind

    def resize_node(self, node_id, width_delta, height_delta, zoom=0.75):
        # Exercise LiteGraph's real corner gesture, rather than only setSize().
        corner = self.page.evaluate("""([id, zoom]) => {
            const app=window.app, node=app.graph.getNodeById(id);
            node.pos=[180,140];
            for (const other of app.graph.nodes) if (other!==node) other.pos=[2400,140];
            app.canvas.ds.scale=zoom; app.canvas.ds.offset=[0,0];
            app.canvas.setDirty(true,true);
            const rect=app.canvas.canvas.getBoundingClientRect();
            return [rect.left+(node.pos[0]+node.size[0]-2)*zoom,
                    rect.top+(node.pos[1]+node.size[1]-2)*zoom];
        }""", [node_id, zoom])
        self.page.wait_for_timeout(100)
        self.page.mouse.move(*corner)
        self.page.mouse.down()
        self.page.mouse.move(corner[0]+width_delta*zoom, corner[1]+height_delta*zoom, steps=12)
        self.page.mouse.up()
        self.page.wait_for_timeout(150)

    def panel_metrics(self, node_id):
        return self.page.evaluate("""id => {
            const n=window.app.graph.getNodeById(id);
            const controls=n.mossEditor
                ? [n.mossEditor.canvas,n.mossEditor.description,n.mossEditor.text]
                : [n.widgets.find(w=>w.name==='prompt').element,n.mossPromptOutput];
            return {size:Array.from(n.size), controls:controls.map(el=>[el.clientWidth,el.clientHeight])};
        }""", node_id)

    def queue_result(self):
        with self.page.expect_response(lambda r: r.url == URL+"/api/prompt" and r.request.method == "POST") as response:
            self.page.evaluate("async () => { await app.queuePrompt(0,1); }")
        self.assertTrue(response.value.ok, response.value.text())
        prompt_id = response.value.json()["prompt_id"]
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            history = self.page.request.get(URL+"/history/"+prompt_id).json().get(prompt_id)
            if history and history.get("status", {}).get("completed"):
                self.assertEqual(history["status"]["status_str"], "success")
                return history["outputs"]
            self.page.wait_for_timeout(50)
        self.fail(f"Prompt {prompt_id} did not complete: {history}")

    def resize_screenshot(self, name):
        artifacts = Path("test-results")
        artifacts.mkdir(exist_ok=True)
        self.page.screenshot(path=str(artifacts / name))

    def test_editor_node_resize_preserves_geometry_resolution_and_saved_size(self):
        self.page.evaluate("""() => {
            const n=app.graph.getNodeById(window.editorId);
            n.widgets.find(w=>w.name==='width').value=1536;
            n.widgets.find(w=>w.name==='height').value=768;
            n.mossEditor.refresh();
        }""")
        node_id = self.page.evaluate("window.editorId")
        before = self.panel_metrics(node_id)
        boxes = self.boxes()
        api_before = self.page.evaluate("async () => (await app.graphToPrompt()).output")
        readout = self.page.get_by_label("MOSS bbox (abs_centralized yxyx)").inner_text()
        for zoom in [0.5, 0.75, 1]:
            self.resize_node(node_id, 280, 220, zoom)
            enlarged = self.panel_metrics(node_id)
            for old, new in zip(before["controls"], enlarged["controls"]):
                self.assertGreater(new[0], old[0]+40)
                self.assertGreater(new[1], old[1]+30)
            self.assertEqual(self.boxes(), boxes)
            self.assertEqual(self.page.get_by_label("MOSS bbox (abs_centralized yxyx)").inner_text(), readout)
            self.assertEqual(self.page.evaluate("async () => (await app.graphToPrompt()).output"), api_before)
            if zoom == 0.75:
                self.resize_screenshot("editor-node-enlarged.png")
            self.resize_node(node_id, -280, -220, zoom)
            smaller = self.panel_metrics(node_id)
            for old, new in zip(enlarged["controls"], smaller["controls"]):
                self.assertLess(new[0], old[0]-40)
                self.assertLess(new[1], old[1]-30)
            before = smaller

        # Allow shrink below the default size, and preserve it on reload.
        self.resize_node(node_id, -160, -80)
        small = self.panel_metrics(node_id)
        self.resize_screenshot("editor-node-shrunk.png")
        self.assertLess(small["size"][0], 820)
        self.assertLess(small["size"][1], before["size"][1])
        saved = self.page.evaluate("JSON.stringify(app.graph.serialize())")
        self.page.evaluate("async data => { await app.loadGraphData(JSON.parse(data)); }", saved)
        self.assertEqual(self.panel_metrics(node_id)["size"], small["size"])
        self.assertEqual(self.boxes(), boxes)
        self.assertEqual(self.page.evaluate("async () => (await app.graphToPrompt()).output"), api_before)
        # Pointer mapping must follow the resized, non-square display.
        self.assertEqual(self.drag((0.05, 0.05), (0.15, 0.15)), "draw")
        self.assertAlmostEqual(self.boxes()[1]["x"], 0.05, delta=0.01)
        self.assertAlmostEqual(self.boxes()[1]["w"], 0.1, delta=0.01)
        self.page.get_by_role("button", name="↶", exact=True).click()
        self.assertEqual(self.boxes(), boxes)

    def test_prompt_node_resize_grows_both_fields_and_keeps_queue_output(self):
        self.page.evaluate("""() => {
            for (const id of [window.editorId,window.formatterId]) {
                const n=app.graph.getNodeById(id);
                n.widgets.find(w=>w.name==='width').value=1536;
                n.widgets.find(w=>w.name==='height').value=768;
            }
            app.graph.getNodeById(window.formatterId).widgets.find(w=>w.name==='prompt').value='日本のポスター {a|b}';
        }""")
        result_before = self.queue_result()
        self.assertEqual(result_before[str(self.page.evaluate("window.editorId"))]["dims"], [1536, 768])
        self.page.wait_for_function("app.graph.getNodeById(window.formatterId)?.mossPromptOutput?.value.includes('abs_centralized')")
        output = self.page.get_by_label("MOSS layout output").input_value()
        self.assertEqual(json.loads(output)["items"][0]["bbox"], [-231, -461, 77, 154])
        node_id = self.page.evaluate("window.formatterId")
        api_before = self.page.evaluate("async () => (await app.graphToPrompt()).output")
        before = self.panel_metrics(node_id)
        self.resize_node(node_id, 280, 300)
        enlarged = self.panel_metrics(node_id)
        self.resize_screenshot("prompt-node-enlarged.png")
        for old, new in zip(before["controls"], enlarged["controls"]):
            self.assertGreater(new[0], old[0]+200)
            self.assertGreater(new[1], old[1]+100)
        self.resize_node(node_id, -280, -300)
        smaller = self.panel_metrics(node_id)
        self.resize_screenshot("prompt-node-shrunk.png")
        for old, new in zip(enlarged["controls"], smaller["controls"]):
            self.assertLess(new[0], old[0]-200)
            self.assertLess(new[1], old[1]-100)
        self.assertEqual(self.page.get_by_label("MOSS layout output").input_value(), output)
        self.assertEqual(self.page.evaluate("async () => (await app.graphToPrompt()).output"), api_before)
        self.assertEqual(self.queue_result(), result_before)
        self.assertEqual(self.page.get_by_label("MOSS layout output").input_value(), output)

    def test_editor_guidance_scales_and_wraps_with_node_width(self):
        node_id = self.page.evaluate("window.editorId")
        api_before = self.page.evaluate("async () => (await app.graphToPrompt()).output")

        def notices():
            return self.page.evaluate("""() => {
                const root=app.graph.getNodeById(window.editorId).mossEditor.root;
                return [...root.querySelectorAll('[aria-label="BOX editor help"], [role="status"]')].map(el => {
                    const style=getComputedStyle(el);
                    return {font:parseFloat(style.fontSize), line:parseFloat(style.lineHeight),
                        width:el.clientWidth, height:el.clientHeight,
                        contentWidth:el.scrollWidth, contentHeight:el.scrollHeight, text:el.textContent};
                });
            }""")

        original = notices()
        self.assertEqual(len(original), 2)
        self.resize_node(node_id, -480, -60)
        narrow = notices()
        self.resize_screenshot("editor-guidance-narrow.png")
        self.assertGreater(narrow[0]["height"], narrow[0]["line"]*1.5)
        self.resize_node(node_id, 780, 240)
        wide = notices()
        self.resize_screenshot("editor-guidance-wide.png")
        # Growing the canvas must leave the notes visible inside the panel.
        self.assertTrue(self.page.evaluate("""() => {
            const root=app.graph.getNodeById(window.editorId).mossEditor.root;
            return root.scrollHeight <= root.clientHeight+1;
        }"""))
        for initial, small, large in zip(original, narrow, wide):
            self.assertLess(small["font"], initial["font"])
            self.assertGreater(large["font"], initial["font"])
            self.assertLess(small["width"], large["width"])
            self.assertEqual(small["text"], initial["text"])
            self.assertEqual(large["text"], initial["text"])
            for state in [small, large]:
                self.assertLessEqual(state["contentWidth"], state["width"]+1)
                self.assertLessEqual(state["contentHeight"], state["height"]+1)
        self.assertEqual(self.page.evaluate("async () => (await app.graphToPrompt()).output"), api_before)

    def test_all_four_handles_resize_even_in_draw_mode(self):
        for corner in [(0, 0), (1, 0), (0, 1), (1, 1)]:
            with self.subTest(corner=corner):
                self.load_box()
                self.page.get_by_role("button", name="Draw / 描画", exact=True).click()
                start = (0.2 + corner[0] * 0.4, 0.2 + corner[1] * 0.4)
                end = (0.1 if corner[0] == 0 else 0.8, 0.1 if corner[1] == 0 else 0.8)
                self.assertEqual(self.drag(start, end), "resize")
                boxes = self.boxes()
                self.assertEqual(len(boxes), 1)
                self.assertAlmostEqual(boxes[0]["w"], 0.5 if corner[0] == 0 else 0.6, delta=0.01)
                self.assertAlmostEqual(boxes[0]["h"], 0.5 if corner[1] == 0 else 0.6, delta=0.01)

    def test_text_editing_and_export_for_all_box_types(self):
        control = self.page.get_by_label("文字 / text", exact=True)
        kinds = self.page.get_by_label("種類 / type", exact=True)
        previous = ""
        for kind, label in [("character", "character"), ("obj", "object"), ("text", "text")]:
            with self.subTest(kind=kind):
                kinds.select_option(kind)
                self.assertTrue(control.is_enabled())
                self.assertEqual(control.input_value(), previous)
                literal = f'{kind}: HELLO\n日本語 "MOSS" {{a|b}}'
                control.fill(literal)
                self.assertEqual(self.boxes()[0]["text"], literal)
                self.page.evaluate("async () => { await window.app.queuePrompt(0,1); }")
                self.page.wait_for_function("""([literal,label]) => {
                    const output=window.app.graph.getNodeById(window.formatterId)?.mossPromptOutput?.value;
                    if (!output) return false;
                    const item=JSON.parse(output).items[0];
                    return item.text===literal && item.label===label;
                }""", arg=[literal, label])
                previous = literal
        saved = self.page.evaluate("JSON.stringify(window.app.graph.serialize())")
        self.page.evaluate("async data => { await window.app.loadGraphData(JSON.parse(data)); }", saved)
        self.assertEqual(control.input_value(), previous)
        # No active BOX still disables the controls.
        self.page.get_by_role("button", name="削除", exact=True).click()
        self.assertTrue(control.is_disabled())

    def test_resize_and_draw_follow_pointer_at_different_zoom_levels(self):
        for zoom in [0.5, 0.75, 1.5]:
            with self.subTest(zoom=zoom):
                self.load_box()
                self.page.evaluate("""zoom => {
                    window.app.canvas.ds.scale=zoom;
                    window.app.canvas.setDirty(true,true);
                }""", zoom)
                self.page.wait_for_function("""zoom => {
                    const c=window.app.graph.getNodeById(window.editorId).mossEditor.canvas;
                    return Math.abs(c.getBoundingClientRect().width/c.clientWidth-zoom)<0.01;
                }""", arg=zoom)
                self.assertEqual(self.drag((0.6, 0.6), (0.8, 0.8)), "resize")
                self.assertEqual(len(self.boxes()), 1)
                self.assertAlmostEqual(self.boxes()[0]["w"], 0.6, delta=0.01)
                self.assertEqual(self.drag((0.05, 0.05), (0.15, 0.15)), "draw")
                self.assertEqual(len(self.boxes()), 2)
                self.assertAlmostEqual(self.boxes()[1]["x"], 0.05, delta=0.01)
                self.assertAlmostEqual(self.boxes()[1]["w"], 0.1, delta=0.01)

    def test_edge_handle_can_be_grabbed_outside_image(self):
        self.load_box({**BOX, "x": 0, "y": 0, "w": 1, "h": 1})
        self.assertEqual(self.drag((-0.01, -0.01), (0.1, 0.1)), "resize")
        self.assertEqual(len(self.boxes()), 1)
        self.assertAlmostEqual(self.boxes()[0]["x"], 0.1, delta=0.01)

    def test_moss_coordinate_readout_and_renamed_nodes(self):
        self.load_box({**BOX, "x": 100.2 / 1024, "y": 200.2 / 1024, "w": 300.2 / 1024, "h": 400.2 / 1024})
        for key, value in {"x": 100, "y": 200, "w": 301, "h": 401}.items():
            self.assertEqual(self.page.get_by_label(f"box {key} pixels", exact=True).input_value(), str(value))
        readout = self.page.get_by_label("MOSS bbox (abs_centralized yxyx)", exact=True)
        self.assertEqual(readout.inner_text(), "MOSS yxyx: [-312, -412, 89, -111]")
        titles = self.page.evaluate("[window.app.graph.getNodeById(window.editorId).title, window.app.graph.getNodeById(window.formatterId).title]")
        self.assertEqual(titles, ["OGN_MOSS-Image  Layout BOX", "OGN_MOSS-Image Prompt"])
        self.page.evaluate("async () => { await window.app.queuePrompt(0,1); }")
        self.page.wait_for_function("window.app.graph.getNodeById(window.formatterId)?.mossPromptOutput?.value.includes('abs_centralized')")
        result = json.loads(self.page.get_by_label("MOSS layout output").input_value())
        self.assertEqual(result["items"][0]["bbox"], [-312, -412, 89, -111])
        # Editing a displayed pixel width must not reintroduce a hidden fraction.
        width = self.page.get_by_label("box w pixels", exact=True)
        width.fill("300"); width.press("Tab")
        self.assertEqual(width.input_value(), "300")
        self.assertEqual(readout.inner_text(), "MOSS yxyx: [-312, -412, 89, -112]")

    def formatter_values(self):
        return json.loads(self.page.evaluate("""() => JSON.stringify(Object.fromEntries(
            window.app.graph.getNodeById(window.formatterId).widgets
                .filter(w=>['prompt','width','height','character_boxes'].includes(w.name))
                .map(w=>[w.name,w.value])))"""))

    def workflow(self, values, include_old_panel=False, named=False):
        data = json.loads(self.page.evaluate("JSON.stringify(window.app.graph.serialize())"))
        data.pop("id", None)
        node = next(n for n in data["nodes"] if n["type"] == "MOSSImageLayoutPromptLegacy")
        node["widgets_values"] = [values[name] for name in FIELDS] + ([""] if include_old_panel else [])
        for item in data["nodes"]:
            item.pop("widgets_values_named", None)
        if named:
            node["widgets_values_named"] = dict(values)
        return data

    def test_old_positional_workflows_do_not_shift_values(self):
        for include_panel in [True, False]:
            with self.subTest(old_panel=include_panel):
                expected = dict(prompt="scene A", width=768, height=1536, character_boxes="1, 3")
                data = self.workflow(expected, include_old_panel=include_panel)
                for _ in range(3):
                    self.page.evaluate("async data => { await window.app.loadGraphData(data); }", data)
                    self.assertEqual(self.formatter_values(), expected)
                    api = json.loads(self.page.evaluate("async () => JSON.stringify((await window.app.graphToPrompt()).output)"))
                    for name, value in expected.items():
                        self.assertEqual(api[str(self.page.evaluate("window.formatterId"))]["inputs"][name], value)
                    # A normal save must contain exactly the four actual formatter inputs.
                    saved = json.loads(self.page.evaluate("JSON.stringify(window.app.graph.serialize())"))
                    fmt = next(n for n in saved["nodes"] if n["type"] == "MOSSImageLayoutPromptLegacy")
                    self.assertEqual(fmt["widgets_values"], [expected[name] for name in FIELDS])
                    for node in saved["nodes"]:
                        node.pop("widgets_values_named", None)
                    data = saved

    def test_switching_real_workflow_tabs_keeps_independent_values(self):
        cases = {
            "moss-regression-A": dict(prompt="scene A", width=768, height=1536, character_boxes="1, 3"),
            "moss-regression-B": dict(prompt="scene B", width=2048, height=512, character_boxes="2"),
        }
        for name, values in cases.items():
            data = self.workflow(values, include_old_panel=True)
            self.page.evaluate("async ([data,name]) => { await window.app.loadGraphData(data,true,true,name+'.json'); }", [data, name])
        for _ in range(3):
            for name, expected in cases.items():
                self.page.get_by_test_id("workflow-tab").filter(has_text=name).click()
                self.page.wait_for_function("""prompt => window.app.graph.getNodeById(window.formatterId)
                    ?.widgets.find(w=>w.name==='prompt')?.value===prompt""", arg=expected["prompt"])
                self.assertEqual(self.formatter_values(), expected)

    def test_named_values_and_clone_are_preserved(self):
        expected = dict(prompt="日本の街並み", width=1536, height=768, character_boxes="1, 2")
        data = self.workflow(expected, include_old_panel=True, named=True)
        self.page.evaluate("async data => { await window.app.loadGraphData(data); }", data)
        self.assertEqual(self.formatter_values(), expected)
        values = json.loads(self.page.evaluate("""() => {
            const copy=window.app.graph.getNodeById(window.formatterId).clone();
            window.app.graph.add(copy);
            const result=Object.fromEntries(copy.widgets.filter(w=>['prompt','width','height','character_boxes'].includes(w.name)).map(w=>[w.name,w.value]));
            window.app.graph.remove(copy);
            return JSON.stringify(result);
        }"""))
        self.assertEqual(values, expected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--chromium", default=CHROMIUM)
    args, remaining = parser.parse_known_args()
    URL, CHROMIUM = args.url.rstrip("/"), args.chromium
    unittest.main(argv=[sys.argv[0], *remaining])
