import json
import math
import unittest

from layout import dimensions, make_layout_prompt, parse_regions, regions_to_bboxes, regions_to_elements


def region(x=0.125, y=0.09375, w=0.75, h=0.125, **metadata):
    return dict(x=x, y=y, w=w, h=h, type="obj", desc="", text="", palette=[], **metadata)


class LayoutTests(unittest.TestCase):
    def test_official_userguide_poster_example(self):
        boxes = [region(), region(x=0.21875, y=0.3125, w=0.5625, h=0.5625)]
        boxes[0].update(type="text", text="EXPLORE", desc="Large dark-blue uppercase title centered at the top.")
        boxes[1]["desc"] = "A flat blue mountain illustration in the lower center."
        output = json.loads(make_layout_prompt("A minimalist travel poster.", regions_to_elements(boxes), 1024, 1024))
        self.assertEqual(output["items"], [
            {"label": "text", "caption": boxes[0]["desc"], "text": "EXPLORE", "bbox": [-416, -384, -288, 384]},
            {"label": "object", "caption": boxes[1]["desc"], "bbox": [-192, -288, 384, 288]},
        ])
        self.assertEqual(output["bbox_coord_space"], "abs_centralized")
        self.assertEqual(output["bbox_format"], "yxyx")

    def test_rectangular_canvas_axis_order(self):
        elements = [{"bbox": [0, 250, 500, 1000], "desc": "right side"}]
        item = json.loads(make_layout_prompt("scene", elements, 1536, 768))["items"][0]
        self.assertEqual(item["bbox"], [-384, -384, 0, 768])

    def test_pixel_edges_enclose_fractional_coordinates(self):
        elements = [{"bbox": [100.1, 200.1, 300.1, 400.1]}]
        item = json.loads(make_layout_prompt("scene", elements, 1024, 512))["items"][0]
        self.assertEqual(item["bbox"], [-205, -308, -102, -102])

    def test_exact_pixel_coordinates_do_not_expand_from_float_error(self):
        for width in (512, 1008, 1536, 2048, 4096, 8192, 16384):
            box = region(x=7 / width, y=19 / 512, w=113 / width, h=79 / 512)
            item = json.loads(make_layout_prompt("", regions_to_elements([box]), width, 512))["items"][0]
            self.assertEqual(item["bbox"], [19 - 256, 7 - width // 2, 98 - 256, 120 - width // 2])

    def test_single_pixel_offsets_on_high_resolution_canvases(self):
        for width, height in ((8192, 4096), (16384, 8192), (16384, 16384)):
            box = region(x=1 / width, y=1 / height, w=113 / width, h=79 / height)
            item = json.loads(make_layout_prompt("", regions_to_elements([box]), width, height))["items"][0]
            self.assertEqual(item["bbox"], [1 - height // 2, 1 - width // 2, 80 - height // 2, 114 - width // 2])

    def test_full_canvas_and_empty_layout(self):
        result = make_layout_prompt("  unchanged\n", [], 1024, 1024)
        self.assertEqual(result, "  unchanged\n")
        item = json.loads(make_layout_prompt("", [{"bbox": [0, 0, 1000, 1000]}], 1024, 1024))["items"][0]
        self.assertEqual(item["bbox"], [-512, -512, 512, 512])

    def test_unicode_newlines_and_palette_are_preserved(self):
        box = region()
        box.update(type="text", desc="看板", text='こんにちは\n"MOSS" {a|b}', palette=["#123", "#abcdef"])
        boxes = parse_regions(json.dumps([box]))
        prompt = make_layout_prompt(" 日本の街並み ", regions_to_elements(boxes), 1024, 1024)
        result = json.loads(prompt)
        self.assertIn("こんにちは", prompt)
        self.assertEqual(result["summary"]["caption"], "日本の街並み")
        self.assertEqual(result["items"][0]["text"], box["text"])
        self.assertEqual(result["items"][0]["palette"], ["#112233", "#ABCDEF"])

    def test_character_labels_and_text_precedence(self):
        boxes = [region(), region(), region()]
        boxes[0]["type"] = "character"
        boxes[2].update(type="text", text="title")
        result = json.loads(make_layout_prompt("", regions_to_elements(boxes), 1024, 1024, "2, 3"))
        self.assertEqual([item["label"] for item in result["items"]], ["character", "character", "text"])

    def test_core_elements_are_accepted_without_extensions(self):
        result = json.loads(make_layout_prompt("scene", [{"type": "obj", "desc": "cat", "bbox": [100, 200, 300, 400]}], 1024, 1024, "1"))
        self.assertEqual(result["items"][0]["label"], "character")
        self.assertNotIn("text", result["items"][0])

    def test_nontext_regions_preserve_optional_literal_text_and_labels(self):
        literal = 'HELLO\n日本語 "MOSS" {a|b}'
        for kind, label in [("obj", "object"), ("character", "character")]:
            with self.subTest(kind=kind):
                box = region()
                box.update(type=kind, text=literal)
                elements = regions_to_elements(parse_regions(json.dumps([box])))
                self.assertEqual(elements[0]["text"], literal)
                item = json.loads(make_layout_prompt("scene", elements, 1024, 1024))["items"][0]
                self.assertEqual(item["text"], literal)
                self.assertEqual(item["label"], label)
                box["text"] = ""
                elements = regions_to_elements(parse_regions(json.dumps([box])))
                self.assertNotIn("text", elements[0])
                item = json.loads(make_layout_prompt("scene", elements, 1024, 1024))["items"][0]
                self.assertNotIn("text", item)

    def test_pixel_bbox_frame_shape(self):
        self.assertEqual(regions_to_bboxes([region()], 1024, 1024), [[{"x": 128, "y": 96, "width": 768, "height": 128}]])
        self.assertEqual(regions_to_bboxes([], 1024, 1024), [])

    def test_pixel_bbox_and_moss_enclose_the_same_fractional_region(self):
        box = region(x=100.2 / 1024, y=200.2 / 1024, w=300.2 / 1024, h=400.2 / 1024)
        self.assertEqual(regions_to_bboxes([box], 1024, 1024), [[{"x": 100, "y": 200, "width": 301, "height": 401}]])
        item = json.loads(make_layout_prompt("scene", regions_to_elements([box]), 1024, 1024))["items"][0]
        self.assertEqual(item["bbox"], [-312, -412, 89, -111])

    def test_official_moss_compiler_landscape_example(self):
        # MOSS reference: a cup at xyxy [176,224,800,864], canvas 1536x1024.
        box = region(x=176 / 1536, y=224 / 1024, w=624 / 1536, h=640 / 1024)
        item = json.loads(make_layout_prompt("ceramic campaign", regions_to_elements([box]), 1536, 1024))["items"][0]
        self.assertEqual(item["bbox"], [-288, -592, 352, 32])
        self.assertEqual(regions_to_bboxes([box], 1536, 1024)[0][0], {"x": 176, "y": 224, "width": 624, "height": 640})

    def test_official_moss_compiler_fractional_portrait_example(self):
        # MOSS reference: xyxy [10.9,20.1,100.01,200.99], canvas 768x1344.
        box = region(x=10.9 / 768, y=20.1 / 1344, w=89.11 / 768, h=180.89 / 1344)
        item = json.loads(make_layout_prompt("portrait", regions_to_elements([box]), 768, 1344))["items"][0]
        self.assertEqual(item["bbox"], [-652, -374, -471, -283])
        self.assertEqual(regions_to_bboxes([box], 768, 1344)[0][0], {"x": 10, "y": 20, "width": 91, "height": 181})

    def test_subprecision_boxes_are_rejected_instead_of_exporting_zero_area(self):
        box = region(x=0, y=0, w=1e-14, h=0.1)
        with self.assertRaisesRegex(ValueError, "too small"):
            regions_to_bboxes([box], 1024, 1024)
        with self.assertRaisesRegex(ValueError, "too small"):
            make_layout_prompt("scene", regions_to_elements([box]), 1024, 1024)

    def test_reversed_and_outside_editor_boxes(self):
        boxes = parse_regions(json.dumps([region(x=1.2, y=0.9, w=-0.7, h=-0.5)]))
        self.assertAlmostEqual(boxes[0]["x"], 0.5)
        self.assertAlmostEqual(boxes[0]["w"], 0.5)
        self.assertAlmostEqual(boxes[0]["y"], 0.4)

    def test_invalid_editor_data_is_reported(self):
        for value in ("{", "{}", "[null]", "[{}]", json.dumps([region(w=0)]), json.dumps([region(x=2)]), json.dumps([region(x=math.nan)])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_regions(value)

    def test_invalid_metadata_is_reported(self):
        for key, value in (("palette", ["red"]), ("palette", ["#ffffff"] * 6), ("type", "unsupported"), ("desc", None)):
            box = region()
            box[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                parse_regions(json.dumps([box]))

    def test_invalid_elements_are_reported(self):
        for elements in (None, {}, [None], [{}], [{"bbox": [0, 0, 0, 10]}], [{"bbox": [0, 0, 1001, 10]}], [{"bbox": [0, 0, math.inf, 10]}]):
            with self.subTest(elements=elements), self.assertRaises(ValueError):
                make_layout_prompt("scene", elements, 1024, 1024)

    def test_dimensions_follow_moss_requirements(self):
        self.assertEqual(dimensions(1536, 768), (1536, 768))
        for value in (0, -16, 1023, 16385, True, 1024.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                dimensions(value, 1024)


if __name__ == "__main__":
    unittest.main()
