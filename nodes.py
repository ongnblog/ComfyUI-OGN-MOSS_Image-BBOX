import numpy as np
import torch
from PIL import Image, ImageDraw

from .layout import dimensions, make_layout_prompt, parse_regions, regions_to_bboxes, regions_to_elements


def render_preview(regions, width, height, background=None):
    scale = min(1, 1024 / max(width, height))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    if background is not None:
        if background.ndim != 4 or background.shape[0] < 1 or background.shape[-1] not in (1, 3, 4):
            raise ValueError("background must be a nonempty ComfyUI IMAGE batch")
        array = (background[0].detach().cpu().numpy().clip(0, 1) * 255).astype(np.uint8)
        if array.shape[-1] == 1:
            array = array[..., 0]
        base = Image.fromarray(array).convert("RGB").resize(size, Image.Resampling.LANCZOS)
        base = Image.blend(Image.new("RGB", size), base, 0.4)
    else:
        base = Image.new("RGB", size, (32, 32, 32))
    draw = ImageDraw.Draw(base)
    boxes = regions_to_bboxes(regions, width, height)
    for i, (region, box) in enumerate(zip(regions, boxes[0] if boxes else []), 1):
        color = region["palette"][0] if region["palette"] else "#54BFFF"
        x, y = box["x"] * size[0] / width, box["y"] * size[1] / height
        x1 = (box["x"] + box["width"]) * size[0] / width
        y1 = (box["y"] + box["height"]) * size[1] / height
        draw.rectangle((x, y, x1, y1), outline=color, width=2)
        draw.text((x + 4, y + 3), str(i), fill=color)
    return torch.from_numpy(np.asarray(base, dtype=np.float32).copy() / 255).unsqueeze(0)


class MOSSCreateBoundingBoxesLegacy:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "width": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}),
                "height": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}),
                "boxes_json": ("STRING", {"default": "[]", "multiline": True, "dynamicPrompts": False}),
            },
            "optional": {"background": ("IMAGE",)},
        }

    RETURN_TYPES = ("IMAGE", "BOUNDING_BOX", "ARRAY", "INT", "INT")
    RETURN_NAMES = ("preview", "bboxes", "elements", "width", "height")
    FUNCTION = "create"
    CATEGORY = "MOSS-Image/Legacy"
    OUTPUT_NODE = True
    DESCRIPTION = "Draw layout boxes in the classic node display. Pixel bounds enclose the drawn region using MOSS rounding; the editor also shows the centered MOSS yxyx coordinates. Connect elements, width and height to OGN_MOSS-Image Prompt."

    def create(self, width, height, boxes_json="[]", background=None):
        dimensions(width, height)
        regions = parse_regions(boxes_json)
        ui = {"dims": [width, height]}
        if background is not None:
            # ComfyUI's existing temporary-image route supplies the editor background.
            from nodes import PreviewImage
            ui["background_images"] = PreviewImage().save_images(background[:1], "moss.legacy.background")["ui"]["images"]
        return {
            "ui": ui,
            "result": (render_preview(regions, width, height, background),
                       regions_to_bboxes(regions, width, height), regions_to_elements(regions), width, height),
        }


class MOSSImageLayoutPromptLegacy:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "prompt": ("STRING", {"default": "", "multiline": True, "dynamicPrompts": False}),
            "element": ("ARRAY", {"forceInput": True}),
            "width": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}),
            "height": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}),
            "character_boxes": ("STRING", {"default": ""}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    FUNCTION = "convert"
    CATEGORY = "MOSS-Image/Legacy"
    OUTPUT_NODE = True
    DESCRIPTION = "Build a MOSS-Image layout JSON prompt: abs_centralized pixel coordinates in yxyx order. Connect normalized elements and image dimensions from the box editor. character_boxes optionally labels 1-based box numbers as characters, e.g. 1, 3."

    def convert(self, prompt, element, width, height, character_boxes=""):
        result = make_layout_prompt(prompt, element, width, height, character_boxes)
        return {"ui": {"text": [result]}, "result": (result,)}
