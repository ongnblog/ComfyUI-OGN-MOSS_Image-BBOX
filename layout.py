"""Layout conversion independent of ComfyUI and its frontend API."""

import json
import math
import re


def dimensions(width, height):
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 16384 or value % 16:
            raise ValueError(f"{name} must be a multiple of 16 between 16 and 16384")
    return width, height


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def palette(colors):
    if not isinstance(colors, list):
        raise ValueError("palette must be an array of hexadecimal colors")
    result = []
    for color in colors:
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", color):
            raise ValueError("palette colors must use #RGB or #RRGGBB")
        if len(color) == 4:
            color = "#" + "".join(c * 2 for c in color[1:])
        result.append(color.upper())
    if len(result) > 5:
        raise ValueError("Use at most five colors per box")
    return result


def text(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def parse_regions(boxes_json):
    try:
        regions = json.loads(boxes_json)
    except (ValueError, TypeError) as exc:
        raise ValueError("Box editor data is not valid JSON") from exc
    if not isinstance(regions, list):
        raise ValueError("Box editor data must be an array")
    normalized = []
    for i, region in enumerate(regions, 1):
        if not isinstance(region, dict):
            raise ValueError(f"Box {i} must be an object")
        x, y, w, h = (number(region.get(k), f"Box {i}: {k}") for k in ("x", "y", "w", "h"))
        x0, x1 = sorted((max(0, min(1, x)), max(0, min(1, x + w))))
        y0, y1 = sorted((max(0, min(1, y)), max(0, min(1, y + h))))
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"Box {i} must have a nonzero area inside the canvas")
        kind = region.get("type", "obj")
        if kind not in ("obj", "character", "text"):
            raise ValueError(f"Box {i}: unsupported type {kind!r}")
        normalized.append({
            "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
            "type": kind,
            "desc": text(region.get("desc", ""), f"Box {i}: desc"),
            "text": text(region.get("text", ""), f"Box {i}: text"),
            "palette": palette(region.get("palette", [])),
        })
    return normalized


def regions_to_elements(regions):
    elements = []
    for region in regions:
        element = {
            "type": "text" if region["type"] == "text" else "obj",
            "bbox": [max(0, min(1000, v * 1000)) for v in (
                region["y"], region["x"], region["y"] + region["h"], region["x"] + region["w"],
            )],
            "desc": region["desc"],
        }
        if region["type"] == "character":
            element["label"] = "character"
        if region["type"] == "text" or region["text"]:
            element["text"] = region["text"]
        if region["palette"]:
            element["color_palette"] = region["palette"]
        elements.append(element)
    return elements


def enclosing_pixel_edges(bbox, width, height, scale=1):
    """Top-left pixel edges in yxyx order, using MOSS's export rounding."""
    y0, x0, y1, x1 = bbox
    edges = [
        math.floor(round(y0 * height / scale, 9)),
        math.floor(round(x0 * width / scale, 9)),
        math.ceil(round(y1 * height / scale, 9)),
        math.ceil(round(x1 * width / scale, 9)),
    ]
    if edges[0] >= edges[2] or edges[1] >= edges[3]:
        raise ValueError("Bounding box is too small at MOSS export precision")
    return edges


def regions_to_bboxes(regions, width, height):
    boxes = []
    for region in regions:
        y0, x0, y1, x1 = enclosing_pixel_edges([
            region["y"], region["x"], region["y"] + region["h"], region["x"] + region["w"],
        ], width, height)
        boxes.append({"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
    return [boxes] if boxes else []


def make_layout_prompt(prompt, elements, width, height, character_boxes=""):
    """Export MOSS abs_centralized/yxyx pixel coordinates, keeping literal text."""
    dimensions(width, height)
    text(prompt, "prompt")
    if not isinstance(elements, list):
        raise ValueError("element must be an ARRAY from a bounding-box editor")
    characters = {int(i) for i in re.findall(r"\d+", character_boxes)}
    items = []
    for i, element in enumerate(elements, 1):
        if not isinstance(element, dict):
            raise ValueError(f"Element {i} must be an object")
        box = element.get("bbox")
        if not isinstance(box, (tuple, list)) or len(box) != 4:
            raise ValueError(f"Element {i}: bbox must be [y_min, x_min, y_max, x_max] in 0–1000 coordinates")
        y0, x0, y1, x1 = [number(v, f"Element {i}: bbox") for v in box]
        if not (0 <= y0 < y1 <= 1000 and 0 <= x0 < x1 <= 1000):
            raise ValueError(f"Element {i}: bbox must have positive area within 0–1000")
        kind = element.get("type", "obj")
        label = "text" if kind == "text" else (
            "character" if i in characters or kind == "character" or element.get("label") == "character" else "object"
        )
        item = {"label": label, "caption": text(element.get("desc", ""), f"Element {i}: desc")}
        if label == "text" or element.get("text"):
            item["text"] = text(element.get("text", ""), f"Element {i}: text")
        if element.get("color_palette"):
            item["palette"] = palette(element["color_palette"])
        edges = enclosing_pixel_edges(box, width, height, scale=1000)
        item["bbox"] = [edge - offset for edge, offset in zip(edges, [height // 2, width // 2] * 2)]
        items.append(item)
    if not items:
        return prompt
    return json.dumps({
        "summary": {"caption": prompt.strip()}, "items": items,
        "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx",
    }, ensure_ascii=False, separators=(",", ":"))
