"""Check desc completion with real ComfyUI-Custom-Scripts on a disposable server.

Install both custom nodes in the test server, with Nodes 2.0 disabled.
The browser supplies a small tag dictionary without changing the installed one.
    python tests/browser_autocomplete.py --url http://127.0.0.1:8188
"""

import argparse
import json

from playwright.sync_api import sync_playwright


def run(url, chromium):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium, headless=True, args=["--no-sandbox"])
        for installed in [True, False]:
            context = browser.new_context(viewport={"width": 1600, "height": 1100})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/pysssss/autocomplete", lambda route: route.fulfill(
                status=200, content_type="text/plain", body="black_hair\nblue_eyes\nsmile\n"
            ))
            if not installed:
                def without_autocomplete(route):
                    response = route.fetch()
                    paths = response.json()
                    roots = [path.rsplit("/", 1)[0] + "/" for path in paths if path.endswith("/autocompleter.js")]
                    route.fulfill(response=response, json=[path for path in paths if not any(path.startswith(root) for root in roots)])
                page.route("**/extensions", without_autocomplete)
            page.goto(url, wait_until="networkidle")
            page.wait_for_function("window.app?.graph != null")
            page.keyboard.press("Escape")
            assert page.evaluate("app.ui.settings.getSettingValue('Comfy.VueNodes.Enabled')") is False
            page.evaluate("""() => {
                app.graph.clear();
                const node=LiteGraph.createNode('MOSSCreateBoundingBoxesLegacy');
                app.graph.add(node); node.pos=[120,140]; window.editorId=node.id;
                node.widgets.find(w=>w.name==='boxes_json').value=JSON.stringify([
                    {x:.1,y:.1,w:.3,h:.6,type:'character',desc:'',text:'',palette:[]},
                    {x:.6,y:.1,w:.3,h:.6,type:'character',desc:'second person',text:'',palette:[]}
                ]);
                node.mossEditor.load();
                app.canvas.ds.scale=1; app.canvas.ds.offset=[0,0]; app.canvas.setDirty(true,true);
            }""")
            desc = page.get_by_label("説明 / desc", exact=True)
            desc.wait_for(state="visible")
            assert page.evaluate("!!app.graph.getNodeById(window.editorId).mossEditor.descriptionAutoComplete") is installed
            if installed:
                # Real parser, real dropdown and actual keyboard/mouse selection.
                for prefix, tag, key in [("bla", "black_hair", "Tab"), ("blue", "blue_eyes", "Enter"), ("smi", "smile", None)]:
                    desc.press_sequentially(prefix, delay=30)
                    candidate = page.locator(".pysssss-autocomplete-item").filter(has_text=tag)
                    candidate.wait_for(state="visible")
                    if key:
                        desc.press(key)
                    else:
                        candidate.click()
                expected = "black_hair, blue_eyes, smile, "
                assert desc.input_value() == expected
                assert page.evaluate("app.graph.getNodeById(window.editorId).mossEditor.boxes[0].desc") == expected
                page.locator(".pysssss-autocomplete").wait_for(state="hidden")
                # The shared class reflects the extension's settings, rather
                # than maintaining a separate dictionary or enable switch.
                page.evaluate("app.graph.getNodeById(window.editorId).mossEditor.descriptionAutoComplete.constructor.enabled=false")
                desc.fill("")
                desc.press_sequentially("bla", delay=30)
                assert page.locator(".pysssss-autocomplete-item:visible").count() == 0
                page.evaluate("app.graph.getNodeById(window.editorId).mossEditor.descriptionAutoComplete.constructor.enabled=true")
                desc.fill(expected)
            else:
                expected = "black hair, smiling 日本語"
                desc.fill(expected)

            # Switching boxes must keep the completed description on its owner.
            page.get_by_label("Selected box", exact=True).select_option("1")
            assert desc.input_value() == "second person"
            page.get_by_label("Selected box", exact=True).select_option("0")
            assert desc.input_value() == expected
            saved = page.evaluate("JSON.stringify(app.graph.serialize())")
            assert len(json.loads(saved)["nodes"][0]["widgets_values"]) == 3
            page.evaluate("async data => { await app.loadGraphData(JSON.parse(data)); }", saved)
            assert desc.input_value() == expected
            prompt = page.evaluate("async () => (await app.graphToPrompt()).output")
            boxes = json.loads(prompt[str(page.evaluate("window.editorId"))]["inputs"]["boxes_json"])
            assert boxes[0]["desc"] == expected
            assert boxes[1]["desc"] == "second person"
            assert page.get_by_label("文字 / text", exact=True).is_enabled()
            if installed:
                desc.fill("")
                desc.press_sequentially("bla", delay=30)
                page.locator(".pysssss-autocomplete-item").filter(has_text="black_hair").wait_for(state="visible")
                page.evaluate("app.graph.remove(app.graph.getNodeById(window.editorId))")
                assert page.locator(".pysssss-autocomplete-item:visible").count() == 0
            assert not errors, errors
            context.close()
        browser.close()
    print("PASS: desc Tab/Enter/click completion, shared enable setting, BOX switching, save/load, API data, popup cleanup, and plain input without extension")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--chromium", default="/usr/bin/chromium")
    args = parser.parse_args()
    run(args.url.rstrip("/"), args.chromium)
