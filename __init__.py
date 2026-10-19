from .nodes import MOSSCreateBoundingBoxesLegacy, MOSSImageLayoutPromptLegacy

NODE_CLASS_MAPPINGS = {
    "MOSSCreateBoundingBoxesLegacy": MOSSCreateBoundingBoxesLegacy,
    "MOSSImageLayoutPromptLegacy": MOSSImageLayoutPromptLegacy,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MOSSCreateBoundingBoxesLegacy": "OGN_MOSS-Image  Layout BOX",
    "MOSSImageLayoutPromptLegacy": "OGN_MOSS-Image Prompt",
}

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
