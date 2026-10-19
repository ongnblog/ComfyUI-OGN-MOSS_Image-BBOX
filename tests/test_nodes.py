import importlib.util
import json
from pathlib import Path
import sys
import unittest

import torch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("moss_layout_legacy_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)


class NodeTests(unittest.TestCase):
    def test_legacy_registration_and_socket_contract(self):
        self.assertEqual(len(package.NODE_CLASS_MAPPINGS), 2)
        editor = package.NODE_CLASS_MAPPINGS["MOSSCreateBoundingBoxesLegacy"]
        formatter = package.NODE_CLASS_MAPPINGS["MOSSImageLayoutPromptLegacy"]
        self.assertEqual(editor.RETURN_TYPES, ("IMAGE", "BOUNDING_BOX", "ARRAY", "INT", "INT"))
        self.assertEqual(formatter.INPUT_TYPES()["required"]["element"][0], "ARRAY")
        self.assertFalse(editor.INPUT_TYPES()["required"]["boxes_json"][1]["dynamicPrompts"])
        self.assertEqual(package.NODE_DISPLAY_NAME_MAPPINGS["MOSSCreateBoundingBoxesLegacy"], "OGN_MOSS-Image  Layout BOX")
        self.assertEqual(package.NODE_DISPLAY_NAME_MAPPINGS["MOSSImageLayoutPromptLegacy"], "OGN_MOSS-Image Prompt")

    def test_editor_to_formatter_workflow(self):
        box = dict(x=0.125, y=0.09375, w=0.75, h=0.125, type="text", desc="Title", text="EXPLORE", palette=["#123456"])
        editor = package.NODE_CLASS_MAPPINGS["MOSSCreateBoundingBoxesLegacy"]()
        preview, bboxes, elements, width, height = editor.create(1024, 1024, json.dumps([box]))["result"]
        self.assertEqual(tuple(preview.shape), (1, 1024, 1024, 3))
        self.assertEqual(preview.dtype, torch.float32)
        self.assertTrue(torch.isfinite(preview).all())
        self.assertGreater(float(preview.max()), float(preview.min()))
        self.assertEqual(bboxes[0][0]["y"], 96)
        output = package.NODE_CLASS_MAPPINGS["MOSSImageLayoutPromptLegacy"]().convert("poster", elements, width, height)
        self.assertEqual(output["result"][0], output["ui"]["text"][0])
        self.assertEqual(json.loads(output["result"][0])["items"][0]["bbox"], [-416, -384, -288, 384])

    def test_large_canvas_preview_is_bounded(self):
        output = package.NODE_CLASS_MAPPINGS["MOSSCreateBoundingBoxesLegacy"]().create(4096, 2048)
        self.assertEqual(tuple(output["result"][0].shape), (1, 512, 1024, 3))
        self.assertEqual(output["result"][2], [])
        self.assertEqual(output["ui"]["dims"], [4096, 2048])

    def test_preview_and_bbox_use_moss_enclosing_edges(self):
        box = dict(x=100.2 / 1024, y=200.2 / 1024, w=300.2 / 1024, h=400.2 / 1024)
        output = package.NODE_CLASS_MAPPINGS["MOSSCreateBoundingBoxesLegacy"]().create(1024, 1024, json.dumps([box]))
        preview, bboxes, elements, width, height = output["result"]
        self.assertEqual(bboxes[0][0], {"x": 100, "y": 200, "width": 301, "height": 401})
        self.assertTrue(torch.allclose(preview[0, 250, 401], torch.tensor([84, 191, 255]) / 255))
        converted = package.NODE_CLASS_MAPPINGS["MOSSImageLayoutPromptLegacy"]().convert("scene", elements, width, height)
        self.assertEqual(json.loads(converted["result"][0])["items"][0]["bbox"], [-312, -412, 89, -111])

    def test_background_preview_uses_canvas_aspect_ratio(self):
        from moss_layout_legacy_test.nodes import render_preview
        preview = render_preview([], 512, 1024, torch.ones((1, 20, 80, 3)))
        self.assertEqual(tuple(preview.shape), (1, 1024, 512, 3))
        self.assertAlmostEqual(float(preview[0, 0, 0, 0]), 0.4, places=6)


if __name__ == "__main__":
    unittest.main()
