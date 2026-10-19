import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EDITOR = "MOSSCreateBoundingBoxesLegacy";
const FORMATTER = "MOSSImageLayoutPromptLegacy";
const EDITOR_WIDTH = 820;
const EDITOR_PANEL_MIN_HEIGHT = 360;
let DescriptionAutoComplete;
const clamp = (v, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));
const clone = (value) => JSON.parse(JSON.stringify(value));

function pixelBox(box, width, height) {
    // Use the same enclosing pixel edges as MOSS's official prompt compiler.
    const stable = (value) => Number(value.toFixed(9));
    const x = Math.floor(stable(box.x * width));
    const y = Math.floor(stable(box.y * height));
    const right = Math.ceil(stable((box.x + box.w) * width));
    const bottom = Math.ceil(stable((box.y + box.h) * height));
    return { x, y, w: right - x, h: bottom - y };
}

function outlineColor(hex) {
    const rgb = [1, 3, 5].map((offset) => parseInt(hex.slice(offset, offset + 2), 16));
    const luminance = rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114;
    const mix = luminance < 150 ? (150 - luminance) / (255 - luminance) : 0;
    return `rgb(${rgb.map((channel) => Math.round(channel + (255 - channel) * mix)).join(",")})`;
}

function element(tag, styles = {}, text) {
    const el = document.createElement(tag);
    Object.assign(el.style, styles);
    if (text !== undefined) el.textContent = text;
    return el;
}

function chain(target, name, callback) {
    const previous = target[name];
    target[name] = function (...args) {
        const result = previous?.apply(this, args);
        callback.apply(this, args);
        return result;
    };
}

function hideDataWidget(widget) {
    widget.hidden = true;
    widget.type = "converted-widget";
    widget.computeSize = () => [0, -4];
    if (widget.inputEl) widget.inputEl.style.display = "none";
    if (widget.element) widget.element.style.display = "none";
}

function preserveInputWidgets(nodeType, names) {
    // Keep display-only DOM widgets out of positional workflow values. In
    // Frontend 1.55, an extra preview value looks like the old forceInput
    // placeholder for `element`, so migration removes the real width value.
    chain(nodeType.prototype, "onSerialize", function (data) {
        const values = names.map((name) => this.widgets.find((w) => w.name === name)?.value);
        data.widgets_values = values;
        data.widgets_values_named = Object.fromEntries(names.map((name, index) => [name, values[index]]));
    });

    const configure = nodeType.prototype.configure;
    nodeType.prototype.configure = function (data, ...args) {
        const named = data.widgets_values_named;
        const positional = data.widgets_values;
        const saved = new Map();
        names.forEach((name, index) => {
            if (named && Object.prototype.hasOwnProperty.call(named, name)) saved.set(name, named[name]);
            else if (Array.isArray(positional) && index < positional.length) saved.set(name, positional[index]);
        });
        // Old releases appended a preview value. Strip it BEFORE ComfyUI's
        // forceInput migration, without mutating the caller's saved workflow.
        const normalized = { ...data };
        if (saved.size === names.length) {
            normalized.widgets_values = names.map((name) => saved.get(name));
            normalized.widgets_values_named = Object.fromEntries(saved);
        }
        const result = configure.call(this, normalized, ...args);
        // Restore by input name as well, for frontends without named restore.
        for (const [name, value] of saved) {
            const widget = this.widgets.find((w) => w.name === name);
            if (widget) widget.value = value;
        }
        this.mossEditor?.load();
        return result;
    };
}

function addPanel(node, name, panel, minHeight) {
    const widget = node.addDOMWidget(name, "moss_legacy_panel", panel, {
        serialize: false, hideOnZoom: false,
        getMinHeight: () => minHeight,
    });
    widget.serialize = false;
    // Legacy Frontend distributes spare node height among DOM widgets.
    // computeSize would make this a fixed-height widget and bypass that layout.
    widget.serializeValue = () => undefined;
    return widget;
}

// No Vue widgets or Nodes 2.0 APIs: this is a normal legacy DOM widget.
export class BoxEditor {
    constructor(node, stateWidget) {
        this.node = node;
        this.stateWidget = stateWidget;
        this.boxes = [];
        this.selected = -1;
        this.undoStack = [];
        this.redoStack = [];
        this.mode = "select";
        this.background = null;
        this.backgroundVersion = 0;
        this.root = element("div", {
            display: "flex", flexDirection: "column", gap: "6px", padding: "8px",
            boxSizing: "border-box", width: "100%", height: "100%", overflow: "auto",
            containerType: "inline-size", minWidth: "0",
            background: "#20242b", color: "#eee", font: "12px sans-serif",
        });
        this.root.className = "moss-legacy-editor";
        // Keep editing gestures inside this node, including Delete and Ctrl+A.
        for (const event of ["pointerdown", "pointerup", "click", "dblclick", "keydown", "wheel"]) {
            this.root.addEventListener(event, (e) => e.stopPropagation());
        }
        const toolbar = element("div", { display: "flex", gap: "4px", flexWrap: "wrap", flexShrink: "0" });
        const button = (label, action) => {
            const btn = element("button", { cursor: "pointer" }, label);
            btn.type = "button";
            btn.addEventListener("click", action);
            toolbar.append(btn);
            return btn;
        };
        this.drawButton = button("Draw / 描画", () => { this.mode = "draw"; this.refresh(); });
        this.selectButton = button("Select / 選択", () => { this.mode = "select"; this.refresh(); });
        this.duplicateButton = button("複製", () => this.duplicate());
        this.deleteButton = button("削除", () => this.remove());
        this.undoButton = button("↶", () => this.undo());
        this.undoButton.title = "Undo (Ctrl/Cmd+Z)";
        this.redoButton = button("↷", () => this.redo());
        this.redoButton.title = "Redo (Ctrl/Cmd+Shift+Z)";
        this.root.append(toolbar);

        const workspace = element("div", {
            display: "grid", gridTemplateColumns: "minmax(0, 1.2fr) minmax(0, 1fr)",
            gridTemplateRows: "minmax(0, 1fr)", gap: "12px", flex: "1 0 260px", minWidth: "0", minHeight: "260px",
        });
        this.canvas = element("canvas", {
            width: "100%", minWidth: "0", height: "100%", minHeight: "0", touchAction: "none",
            outline: "1px solid #526170", borderRadius: "6px",
        });
        this.canvas.tabIndex = 0;
        this.canvas.setAttribute("aria-label", "Bounding box canvas");
        const sidebar = element("div", {
            display: "flex", flexDirection: "column", gap: "8px", minWidth: "0", minHeight: "0",
            boxSizing: "border-box", padding: "10px", background: "#2b3b50", color: "#edf4ff",
            border: "1px solid #627a96", borderRadius: "6px",
        });
        workspace.append(this.canvas, sidebar);
        this.root.append(workspace);
        const inputStyle = {
            width: "100%", boxSizing: "border-box", minWidth: "0", padding: "4px",
            background: "#142235", color: "#f2f6fc", border: "1px solid #7890aa", borderRadius: "3px",
            colorScheme: "dark",
        };
        this.list = element("select", inputStyle);
        this.list.setAttribute("aria-label", "Selected box");
        this.list.addEventListener("change", () => { this.selected = Number(this.list.value); this.refresh(); });
        sidebar.append(this.list);

        const details = element("div", {
            display: "grid", gridTemplateColumns: "70px minmax(0, 1fr)",
            gridTemplateRows: "auto minmax(60px, 1fr) minmax(48px, .75fr) auto",
            gap: "4px", alignItems: "center", flex: "1", minHeight: "0",
        });
        const field = (label, control) => {
            control.setAttribute("aria-label", label);
            Object.assign(control.style, inputStyle);
            details.append(element("label", {}, label), control);
            return control;
        };
        this.kind = field("種類 / type", element("select"));
        for (const [value, label] of [["obj", "object / 物体"], ["character", "character / 人物"], ["text", "text / 文字"]]) {
            const option = element("option", {}, label);
            option.value = value;
            this.kind.append(option);
        }
        this.kind.addEventListener("change", () => this.edit((box) => { box.type = this.kind.value; }));
        this.description = field("説明 / desc", element("textarea", { resize: "none", height: "100%", minHeight: "0" }));
        this.description.rows = 4;
        this.description.addEventListener("input", () => this.edit((box) => { box.desc = this.description.value; }, false));
        if (DescriptionAutoComplete) this.descriptionAutoComplete = new DescriptionAutoComplete(this.description);
        this.text = field("文字 / text", element("textarea", { resize: "none", height: "100%", minHeight: "0" }));
        this.text.rows = 3;
        this.text.addEventListener("input", () => this.edit((box) => { box.text = this.text.value; }, false));
        this.colors = field("色 / palette", element("input"));
        this.colors.placeholder = "#54BFFF, #FFFFFF (max 5)";
        this.colors.addEventListener("change", () => {
            const values = this.colors.value.split(/[\s,]+/).filter(Boolean);
            if (values.length > 5 || values.some((v) => !/^#[\da-f]{3}(?:[\da-f]{3})?$/i.test(v))) {
                this.status.textContent = "色は #RGB または #RRGGBB で最大5色指定してください。";
                this.colors.setAttribute("aria-invalid", "true");
                return;
            }
            this.colors.removeAttribute("aria-invalid");
            this.edit((box) => { box.palette = values.map((v) => (v.length === 4 ? "#" + [...v.slice(1)].map((c) => c + c).join("") : v).toUpperCase()); });
        });
        const coords = element("div", { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "4px", flexShrink: "0" });
        coords.title = "左上原点のピクセル座標。下段に MOSS の画像中心原点・yxyx 座標を表示します。";
        this.coords = {};
        for (const key of ["x", "y", "w", "h"]) {
            const label = element("label", { display: "flex", flexDirection: "column", gap: "2px" }, key);
            const input = element("input", { width: "100%", boxSizing: "border-box", minWidth: "0" });
            input.type = "number";
            input.step = "1";
            input.setAttribute("aria-label", `box ${key} pixels`);
            input.addEventListener("change", () => {
                const value = Math.round(Number(input.value));
                if (!Number.isFinite(value) || input.value === "") { this.refresh(); return; }
                const [width, height] = this.dims();
                const extent = key === "x" || key === "w" ? width : height;
                this.edit((box) => {
                    // Apply edits to the displayed pixel edges, including when
                    // a restored workflow contains fractional pointer geometry.
                    const pixels = pixelBox(box, width, height);
                    Object.assign(box, { x: pixels.x / width, y: pixels.y / height, w: pixels.w / width, h: pixels.h / height });
                    if (key === "x") box.x = clamp(value / extent, 0, 1 - box.w);
                    if (key === "y") box.y = clamp(value / extent, 0, 1 - box.h);
                    if (key === "w") box.w = clamp(value / extent, 1 / width, 1 - box.x);
                    if (key === "h") box.h = clamp(value / extent, 1 / height, 1 - box.y);
                });
            });
            this.coords[key] = input;
            label.append(input);
            coords.append(label);
        }
        this.mossBBox = element("output", { font: "11px monospace", whiteSpace: "pre-wrap", userSelect: "text", flexShrink: "0" });
        this.mossBBox.setAttribute("aria-label", "MOSS bbox (abs_centralized yxyx)");
        this.mossBBox.title = "MOSS 用 [y_min, x_min, y_max, x_max]。画像中心が原点のピクセル座標です。";
        sidebar.append(details);
        this.root.append(coords, this.mossBBox);
        // Size guidance text against this editor, independently of graph zoom.
        // Keep it readable at the limits and wrap long status/error messages.
        const noticeStyle = {
            width: "100%", minWidth: "0", flexShrink: "0",
            fontSize: "clamp(9px, 1.4cqi, 16px)", lineHeight: "1.4",
            whiteSpace: "pre-wrap", overflowWrap: "anywhere",
        };
        const help = element("div", { ...noticeStyle, opacity: "0.8" }, "空白をドラッグ: 新規枠 / 枠内: 移動 / 四隅: サイズ変更 / Shift+ドラッグ: 重ねて描画");
        help.setAttribute("aria-label", "BOX editor help");
        this.status = element("div", { ...noticeStyle, color: "#b9dcff" });
        this.status.setAttribute("role", "status");
        this.root.append(help, this.status);
        this.canvas.addEventListener("pointerdown", (e) => this.pointerDown(e));
        this.canvas.addEventListener("pointermove", (e) => this.pointerMove(e));
        this.canvas.addEventListener("pointerup", (e) => this.pointerEnd(e, false));
        this.canvas.addEventListener("pointercancel", (e) => this.pointerEnd(e, true));
        this.canvas.addEventListener("lostpointercapture", (e) => this.pointerEnd(e, true));
        this.root.addEventListener("keydown", (e) => {
            if (["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(e.target.tagName)) return;
            if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); this.remove(); }
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
                e.preventDefault(); e.shiftKey ? this.redo() : this.undo();
            }
            if (e.key === "Escape" && this.drag) this.pointerEnd({ pointerId: this.drag.pointerId }, true);
        });
        this.resizeObserver = new ResizeObserver(() => this.draw());
        this.resizeObserver.observe(this.canvas);
        this.load();
    }

    dims() {
        return ["width", "height"].map((name, i) => {
            const input = this.node.inputs?.find((input) => input.name === name);
            const value = this.node.widgets?.find((widget) => widget.name === name)?.value;
            return Math.max(16, Number(input?.link != null ? this.executedDims?.[i] ?? value : value) || 1024);
        });
    }

    load() {
        try {
            const boxes = JSON.parse(this.stateWidget.value || "[]");
            if (!Array.isArray(boxes)) throw new Error("配列ではありません");
            for (const box of boxes) {
                if (!box || !["x", "y", "w", "h"].every((key) => typeof box[key] === "number" && Number.isFinite(box[key])) ||
                    box.x < 0 || box.y < 0 || box.w <= 0 || box.h <= 0 || box.x + box.w > 1.000000001 || box.y + box.h > 1.000000001 ||
                    !["obj", "character", "text"].includes(box.type) || typeof box.desc !== "string" || typeof box.text !== "string" ||
                    !Array.isArray(box.palette) || box.palette.length > 5 || box.palette.some((c) => !/^#[\da-f]{6}$/i.test(c))) {
                    throw new Error("枠データの形式が不正です");
                }
            }
            this.boxes = clone(boxes);
            this.selected = this.boxes.length ? 0 : -1;
            this.loadError = null;
            this.undoStack = [];
            this.redoStack = [];
            this.refresh();
        } catch (error) {
            // Preserve malformed saved data instead of silently overwriting a workflow.
            this.loadError = String(error);
            this.status.textContent = `枠データを読み込めません: ${error}`;
        }
    }

    sync() {
        this.stateWidget.value = JSON.stringify(this.boxes);
        this.node.setDirtyCanvas?.(true, true);
        this.node.graph?.change?.();
    }

    commit(before, refresh = true) {
        if (before !== JSON.stringify(this.boxes)) {
            this.undoStack.push(before);
            if (this.undoStack.length > 100) this.undoStack.shift();
            this.redoStack = [];
            this.sync();
        }
        if (refresh) this.refresh();
        else {
            this.undoButton.disabled = !this.undoStack.length;
            this.redoButton.disabled = !this.redoStack.length;
            const box = this.boxes[this.selected];
            const option = this.list.options[this.selected];
            if (option && box) option.textContent = `${this.selected + 1}: ${box.type} ${box.desc.slice(0, 50)}`;
            this.draw();
        }
    }

    edit(action, refresh = true) {
        if (this.loadError || this.selected < 0) return;
        const before = JSON.stringify(this.boxes);
        action(this.boxes[this.selected]);
        this.commit(before, refresh);
    }

    remove() {
        if (this.loadError || this.selected < 0) return;
        const before = JSON.stringify(this.boxes);
        this.boxes.splice(this.selected, 1);
        this.selected = Math.min(this.selected, this.boxes.length - 1);
        this.commit(before);
    }

    duplicate() {
        if (this.loadError || this.selected < 0) return;
        const before = JSON.stringify(this.boxes);
        const box = clone(this.boxes[this.selected]);
        box.x = Math.min(1 - box.w, box.x + 0.02);
        box.y = Math.min(1 - box.h, box.y + 0.02);
        this.boxes.push(box);
        this.selected = this.boxes.length - 1;
        this.commit(before);
    }

    undo() {
        if (!this.undoStack.length) return;
        this.redoStack.push(JSON.stringify(this.boxes));
        this.boxes = JSON.parse(this.undoStack.pop());
        this.selected = Math.min(this.selected, this.boxes.length - 1);
        this.sync(); this.refresh();
    }

    redo() {
        if (!this.redoStack.length) return;
        this.undoStack.push(JSON.stringify(this.boxes));
        this.boxes = JSON.parse(this.redoStack.pop());
        this.selected = this.boxes.length - 1;
        this.sync(); this.refresh();
    }

    refresh() {
        const [width, height] = this.dims();
        const box = this.boxes[this.selected];
        this.list.replaceChildren();
        if (!this.boxes.length) this.list.append(element("option", {}, "枠を描画してください"));
        this.boxes.forEach((b, i) => {
            const option = element("option", {}, `${i + 1}: ${b.type} ${b.desc.slice(0, 50)}`);
            option.value = String(i);
            this.list.append(option);
        });
        this.list.value = String(this.selected);
        this.kind.value = box?.type ?? "obj";
        this.description.value = box?.desc ?? "";
        this.text.value = box?.text ?? "";
        this.colors.value = box?.palette.join(", ") ?? "";
        this.colors.removeAttribute("aria-invalid");
        for (const control of [this.kind, this.description, this.text, this.colors, ...Object.values(this.coords)]) control.disabled = !box;
        const pixels = box ? pixelBox(box, width, height) : null;
        for (const [key, control] of Object.entries(this.coords)) control.value = pixels ? String(pixels[key]) : "";
        this.duplicateButton.disabled = this.deleteButton.disabled = !box;
        this.undoButton.disabled = !this.undoStack.length;
        this.redoButton.disabled = !this.redoStack.length;
        this.drawButton.style.background = this.mode === "draw" ? "#276283" : "";
        this.selectButton.style.background = this.mode === "select" ? "#276283" : "";
        this.status.textContent = this.loadError ? `枠データを読み込めません: ${this.loadError}` : `${width} × ${height} px · ${this.boxes.length} boxes · 背景は入力を接続して Queue 後に表示`;
        this.draw();
    }

    draw() {
        const [width, height] = this.dims();
        const box = this.boxes[this.selected];
        if (box) {
            const p = pixelBox(box, width, height);
            const bbox = [p.y - height / 2, p.x - width / 2, p.y + p.h - height / 2, p.x + p.w - width / 2];
            this.mossBBox.textContent = `MOSS yxyx: [${bbox.join(", ")}]`;
        } else this.mossBBox.textContent = "MOSS yxyx: —";
        const cw = this.canvas.clientWidth, ch = this.canvas.clientHeight;
        if (!cw || !ch) return;
        const dpr = window.devicePixelRatio || 1;
        this.canvas.width = Math.round(cw * dpr);
        this.canvas.height = Math.round(ch * dpr);
        const ctx = this.canvas.getContext("2d");
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.fillStyle = "#12151b";
        ctx.fillRect(0, 0, cw, ch);
        const scale = Math.min((cw - 16) / width, (ch - 16) / height);
        const view = this.view = { x: (cw - width * scale) / 2, y: (ch - height * scale) / 2, w: width * scale, h: height * scale };
        ctx.fillStyle = "#30353e";
        ctx.fillRect(view.x, view.y, view.w, view.h);
        if (this.background) {
            ctx.globalAlpha = 0.45;
            ctx.drawImage(this.background, view.x, view.y, view.w, view.h);
            ctx.globalAlpha = 1;
        }
        ctx.strokeStyle = "#59616d"; ctx.lineWidth = 1;
        ctx.strokeRect(view.x, view.y, view.w, view.h);
        this.boxes.forEach((box, i) => {
            const x = view.x + box.x * view.w, y = view.y + box.y * view.h;
            const w = box.w * view.w, h = box.h * view.h;
            const color = box.palette[0] || "#54BFFF";
            ctx.fillStyle = color; ctx.globalAlpha = i === this.selected ? 0.2 : 0.08;
            ctx.fillRect(x, y, w, h); ctx.globalAlpha = 1;
            ctx.strokeStyle = outlineColor(color); ctx.lineWidth = i === this.selected ? 2 : 1;
            ctx.strokeRect(x, y, w, h);
            ctx.font = "12px sans-serif"; ctx.fillStyle = outlineColor(color);
            ctx.fillText(`${i + 1} ${box.type}`, x + 4, y + 14);
            if (i === this.selected) {
                for (const [hx, hy] of [[x, y], [x + w, y], [x, y + h], [x + w, y + h]]) ctx.fillRect(hx - 3, hy - 3, 6, 6);
            }
        });
    }

    point(event) {
        if (!this.view) return null;
        const rect = this.canvas.getBoundingClientRect(), v = this.view;
        if (!rect.width || !rect.height) return null;
        // DOM widgets inherit graph zoom through CSS transforms. Pointer
        // coordinates are in screen pixels; the drawn canvas uses local pixels.
        const scaleX = rect.width / this.canvas.clientWidth;
        const scaleY = rect.height / this.canvas.clientHeight;
        return {
            x: ((event.clientX - rect.left) / scaleX - v.x) / v.w,
            y: ((event.clientY - rect.top) / scaleY - v.y) / v.h,
            screenWidth: v.w * scaleX, screenHeight: v.h * scaleY,
        };
    }

    pointerDown(event) {
        if (event.button !== 0 || this.loadError || this.drag) return;
        const p = this.point(event);
        if (!p) return;
        let corner = null;
        const selected = this.boxes[this.selected];
        // Visible handles always resize, including in Draw mode and just
        // outside an image edge. Shift-drag explicitly forces a new box.
        if (selected && !event.shiftKey) {
            corner = [[0, 0], [1, 0], [0, 1], [1, 1]].find(([x, y]) =>
                Math.abs(p.x - selected.x - selected.w * x) * p.screenWidth <= 8 && Math.abs(p.y - selected.y - selected.h * y) * p.screenHeight <= 8);
        }
        if (!corner && (p.x < 0 || p.y < 0 || p.x > 1 || p.y > 1)) return;
        event.preventDefault();
        this.canvas.focus();
        const before = JSON.stringify(this.boxes);
        const hit = this.mode === "draw" || event.shiftKey ? -1 : this.boxes.findLastIndex((b) => p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + b.h);
        let kind;
        if (corner) kind = "resize";
        else if (hit >= 0) { this.selected = hit; kind = "move"; }
        else {
            this.boxes.push({ x: p.x, y: p.y, w: 0, h: 0, type: "obj", desc: "", text: "", palette: [] });
            this.selected = this.boxes.length - 1;
            kind = "draw";
        }
        this.drag = { pointerId: event.pointerId, p, before, kind, corner, original: clone(this.boxes[this.selected]) };
        this.canvas.setPointerCapture(event.pointerId);
        this.refresh();
    }

    pointerMove(event) {
        const drag = this.drag;
        if (!drag || drag.pointerId !== event.pointerId) return;
        const pointer = this.point(event);
        if (!pointer) return;
        const p = { x: clamp(pointer.x), y: clamp(pointer.y) };
        const box = this.boxes[this.selected], original = drag.original;
        if (drag.kind === "move") {
            box.x = clamp(original.x + p.x - drag.p.x, 0, 1 - box.w);
            box.y = clamp(original.y + p.y - drag.p.y, 0, 1 - box.h);
        } else {
            const anchor = drag.kind === "draw" ? drag.p : {
                x: original.x + original.w * (1 - drag.corner[0]),
                y: original.y + original.h * (1 - drag.corner[1]),
            };
            box.x = Math.min(anchor.x, p.x); box.y = Math.min(anchor.y, p.y);
            box.w = Math.abs(p.x - anchor.x); box.h = Math.abs(p.y - anchor.y);
        }
        this.draw();
    }

    pointerEnd(event, cancel) {
        if (!this.drag || this.drag.pointerId !== event.pointerId) return;
        const drag = this.drag, box = this.boxes[this.selected];
        this.drag = null;
        if (this.canvas.hasPointerCapture(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId);
        if (cancel || box.w * this.view.w < 2 || box.h * this.view.h < 2) {
            this.boxes = JSON.parse(drag.before);
            this.selected = Math.min(this.selected, this.boxes.length - 1);
            this.refresh();
        } else this.commit(drag.before);
    }

    executed(message) {
        if (message?.dims) this.executedDims = message.dims;
        const imageInfo = message?.background_images?.[0];
        const version = ++this.backgroundVersion;
        this.background = null;
        if (imageInfo) {
            const image = new Image();
            image.onload = () => { if (this.backgroundVersion === version) { this.background = image; this.draw(); } };
            image.onerror = () => { if (this.backgroundVersion === version) this.status.textContent = "背景画像を読み込めません。入力を確認して再度 Queue してください。"; };
            image.src = api.apiURL(`/view?${new URLSearchParams({ ...imageInfo, type: imageInfo.type || "temp" })}`);
        }
        this.refresh();
    }

    dispose() {
        this.backgroundVersion++;
        this.resizeObserver.disconnect();
        this.descriptionAutoComplete?.dropdown.remove();
        this.root.remove();
    }
}

app.registerExtension({
    name: "MOSS.LegacyLayout",
    async init() {
        const extensionName = "pysssss.AutoCompleter";
        if (!app.extensions.some((extension) => extension.name === extensionName)) return;
        if (app.ui.settings.getSettingValue("Comfy.Extension.Disabled", []).includes(extensionName)) return;
        try {
            // Discover the installed URL so renaming the custom-node directory
            // still shares the same module, word lists and settings as pysssss.
            const paths = await api.getExtensions();
            const source = paths.find((path) => new URL(path, location.href).pathname.endsWith("/autocompleter.js"));
            if (!source) return;
            const url = new URL("./common/autocomplete.js", new URL(api.fileURL(source), location.href));
            ({ TextAreaAutoComplete: DescriptionAutoComplete } = await import(url.href));
        } catch (error) {
            console.warn("MOSS: Could not connect desc tag autocomplete; using plain text input.", error);
        }
    },
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name === EDITOR) {
            preserveInputWidgets(nodeType, ["width", "height", "boxes_json"]);
            chain(nodeType.prototype, "onNodeCreated", function () {
                const data = this.widgets.find((widget) => widget.name === "boxes_json");
                if (!data) return;
                hideDataWidget(data);
                const editor = this.mossEditor = new BoxEditor(this, data);
                addPanel(this, "moss_box_canvas", editor.root, EDITOR_PANEL_MIN_HEIGHT);
                for (const widget of this.widgets.filter((w) => w.name === "width" || w.name === "height")) {
                    chain(widget, "callback", () => editor.refresh());
                }
                this.setSize([Math.max(EDITOR_WIDTH, this.size[0]), Math.max(640, this.size[1])]);
            });
            chain(nodeType.prototype, "onExecuted", function (message) { this.mossEditor?.executed(message); });
            chain(nodeType.prototype, "onRemoved", function () { this.mossEditor?.dispose(); });
        }
        if (nodeData.name === FORMATTER) {
            preserveInputWidgets(nodeType, ["prompt", "width", "height", "character_boxes"]);
            chain(nodeType.prototype, "onNodeCreated", function () {
                const output = element("textarea", { width: "100%", height: "100%", boxSizing: "border-box", resize: "none", font: "12px monospace" });
                output.readOnly = true;
                output.placeholder = "Queue 後に MOSS-Image の出力プロンプトを表示します。";
                output.setAttribute("aria-label", "MOSS layout output");
                for (const type of ["pointerdown", "keydown", "wheel"]) output.addEventListener(type, (e) => e.stopPropagation());
                this.mossPromptOutput = output;
                addPanel(this, "moss_prompt_preview", output, 160);
                this.setSize([Math.max(400, this.size[0]), Math.max(430, this.size[1])]);
            });
            chain(nodeType.prototype, "onExecuted", function (message) {
                if (this.mossPromptOutput) this.mossPromptOutput.value = message?.text?.[0] ?? "";
            });
        }
    },
});
