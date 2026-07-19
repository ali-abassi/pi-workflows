#!/usr/bin/env node
/** Render an approved brand-deck JSON artifact as an editable PowerPoint deck. */

import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const resolveRoot = process.env.ARTIFACT_TOOL_RESOLVE_ROOT;
const artifactModule = resolveRoot
  ? await import(pathToFileURL(createRequire(path.join(resolveRoot, "package.json")).resolve("@oai/artifact-tool")).href)
  : await import("@oai/artifact-tool");
const { Presentation, PresentationFile } = artifactModule;

const [input, output, previewDir] = process.argv.slice(2);
if (!input || !output) {
  throw new Error("usage: node render_brand_deck.mjs <brand-deck.json> <output.pptx> [preview-dir]");
}

const spec = JSON.parse(await fs.readFile(input, "utf8"));
if (spec.artifact_type !== "brand-deck" || !Array.isArray(spec.slides) || spec.slides.length < 12) {
  throw new Error("input must be a mechanically approved brand-deck artifact");
}

const theme = spec.theme;
const deck = Presentation.create({ slideSize: { width: 1280, height: 720 } });

function addText(slide, name, text, position, style) {
  const shape = slide.shapes.add({ geometry: "textbox", name, position, fill: "none", line: { style: "solid", fill: "none", width: 0 } });
  shape.text = text;
  shape.text.style = style;
  return shape;
}

function addFrame(slide, name, position, fill, radius = "rounded-xl") {
  return slide.shapes.add({ geometry: "roundRect", name, position, fill, line: { style: "solid", fill: theme.primary, width: 1 }, borderRadius: radius });
}

for (const [index, item] of spec.slides.entries()) {
  const slide = deck.slides.add();
  slide.background.fill = theme.background;
  const dark = item.slide_type === "cover" || item.slide_type === "positioning";
  if (dark) slide.background.fill = theme.primary;
  const ink = dark ? theme.surface : theme.text;
  const muted = dark ? theme.background : theme.muted;

  if (item.slide_type === "cover") {
    addText(slide, "cover-eyebrow", item.eyebrow, { left: 82, top: 72, width: 700, height: 34 }, { fontSize: 18, bold: true, color: theme.accent, fontFamily: theme.body_font });
    addText(slide, "cover-title", spec.deck_title, { left: 82, top: 165, width: 900, height: 145 }, { fontSize: 58, bold: true, color: ink, fontFamily: theme.display_font });
    addText(slide, "cover-subtitle", spec.deck_subtitle, { left: 82, top: 336, width: 760, height: 90 }, { fontSize: 25, color: muted, fontFamily: theme.body_font });
    addText(slide, "cover-callout", item.callout || spec.central_takeaway, { left: 82, top: 540, width: 930, height: 60 }, { fontSize: 22, bold: true, color: ink, fontFamily: theme.body_font });
    addFrame(slide, "evidence-spine", { left: 1120, top: 72, width: 18, height: 570 }, theme.accent, "rounded-full");
  } else if (item.slide_type === "color") {
    addText(slide, "slide-eyebrow", item.eyebrow, { left: 72, top: 52, width: 560, height: 28 }, { fontSize: 16, bold: true, color: theme.accent, fontFamily: theme.body_font });
    addText(slide, "slide-title", item.title, { left: 72, top: 96, width: 1040, height: 72 }, { fontSize: 38, bold: true, color: ink, fontFamily: theme.display_font });
    const swatches = [["BACKGROUND", theme.background], ["SURFACE", theme.surface], ["PRIMARY", theme.primary], ["ACCENT", theme.accent], ["TEXT", theme.text], ["MUTED", theme.muted]];
    swatches.forEach(([label, color], swatchIndex) => {
      const left = 72 + (swatchIndex % 3) * 370;
      const top = 220 + Math.floor(swatchIndex / 3) * 180;
      addFrame(slide, `swatch-${label.toLowerCase()}`, { left, top, width: 330, height: 112 }, color);
      addText(slide, `swatch-label-${label.toLowerCase()}`, `${label}  ${color}`, { left, top: top + 126, width: 330, height: 30 }, { fontSize: 16, bold: true, color: ink, fontFamily: theme.body_font });
    });
  } else {
    addText(slide, "slide-eyebrow", item.eyebrow, { left: 72, top: 52, width: 560, height: 28 }, { fontSize: 16, bold: true, color: theme.accent, fontFamily: theme.body_font });
    addText(slide, "slide-title", item.title, { left: 72, top: 96, width: 1030, height: 84 }, { fontSize: 38, bold: true, color: ink, fontFamily: theme.display_font });
    addText(slide, "slide-body", item.body, { left: 72, top: 205, width: 560, height: 110 }, { fontSize: 21, color: muted, fontFamily: theme.body_font });
    addFrame(slide, "principles-surface", { left: 690, top: 205, width: 500, height: 350 }, dark ? theme.text : theme.surface);
    const bullets = (item.bullets || []).slice(0, 5);
    bullets.forEach((bullet, bulletIndex) => {
      addFrame(slide, `bullet-marker-${bulletIndex + 1}`, { left: 730, top: 250 + bulletIndex * 58, width: 12, height: 12 }, theme.accent, "rounded-full");
      addText(slide, `bullet-${bulletIndex + 1}`, bullet, { left: 765, top: 236 + bulletIndex * 58, width: 375, height: 40 }, { fontSize: 19, color: dark ? theme.surface : theme.text, fontFamily: theme.body_font });
    });
    if (item.callout) addText(slide, "slide-callout", item.callout, { left: 72, top: 505, width: 540, height: 60 }, { fontSize: 22, bold: true, color: ink, fontFamily: theme.body_font });
  }
  const footer = item.source_ids?.length ? `Sources: ${item.source_ids.join(", ")}` : spec.central_takeaway;
  addText(slide, "slide-footer", footer, { left: 72, top: 666, width: 1030, height: 22 }, { fontSize: 11, color: muted, fontFamily: theme.body_font });
  addText(slide, "slide-number", String(index + 1).padStart(2, "0"), { left: 1160, top: 660, width: 50, height: 24 }, { fontSize: 12, bold: true, color: theme.accent, fontFamily: theme.body_font });
}

await fs.mkdir(path.dirname(output), { recursive: true });
if (previewDir) {
  await fs.mkdir(previewDir, { recursive: true });
  for (const [index, slide] of deck.slides.items.entries()) {
    const png = await deck.export({ slide, format: "png", scale: 1 });
    await fs.writeFile(path.join(previewDir, `slide-${String(index + 1).padStart(2, "0")}.png`), new Uint8Array(await png.arrayBuffer()));
    const layout = await slide.export({ format: "layout" });
    await fs.writeFile(path.join(previewDir, `slide-${String(index + 1).padStart(2, "0")}.layout.json`), await layout.text());
  }
  const montage = await deck.export({ format: "webp", montage: true, scale: 1 });
  await fs.writeFile(path.join(previewDir, "montage.webp"), new Uint8Array(await montage.arrayBuffer()));
}
const pptx = await PresentationFile.exportPptx(deck);
await pptx.save(output);
