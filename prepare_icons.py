#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Build GTK assets from the user-supplied icon masters without changing them."""

from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

from PIL import Image


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "pyrobolic-icon-set"
OUTPUT = ROOT / "pyrobolic/icons/hicolor"
NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", NS)


def symbolic(source, target):
    original = ET.parse(source).getroot()
    with Image.open(SOURCE / "actions/png/light/64" / f"{source.stem}.png") as preview:
        left, top, right, bottom = preview.getchannel("A").getbbox()
    # Normalize the artwork's optical size, not its original 64px export canvas.
    # Otherwise the small '+' occupies only about six pixels in a 16px GTK icon.
    size = max(right - left, bottom - top) / 0.875
    x, y = (left + right - size) / 2, (top + bottom - size) / 2
    svg = ET.Element(f"{{{NS}}}svg", width="16", height="16", viewBox=f"{x:g} {y:g} {size:g} {size:g}")
    defs = ET.SubElement(svg, f"{{{NS}}}defs")
    mask = ET.SubElement(defs, f"{{{NS}}}mask", id="glyph", maskUnits="userSpaceOnUse",
                         x="0", y="0", width="64", height="64")
    # White interior details in the source become transparent cutouts, not a
    # hard-coded background color that would fail on selected/disabled controls.
    for shape in original:
        if shape.tag.rsplit("}", 1)[-1] in {"title", "defs"}:
            continue
        for element in shape.iter():
            for attribute in ("fill", "stroke"):
                color = element.get(attribute)
                if color and color != "none":
                    if color.startswith("url("):
                        raise ValueError(f"Unexpected gradient in monochrome master: {source}")
                    element.set(attribute, "#000000" if color.upper() == "#FFFFFF" else "#FFFFFF")
        mask.append(shape)
    ET.SubElement(svg, f"{{{NS}}}rect", width="64", height="64", fill="#2e3436", mask="url(#glyph)")
    target.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(svg).write(target, encoding="utf-8", xml_declaration=True)


def main():
    app_id = "io.github.woodpile72.Pyrobolic"
    app_dir = OUTPUT / "scalable/apps"
    app_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE / "app/svg/dark/pyrobolic.svg", app_dir / f"{app_id}.svg")
    for style, suffix in (("symbol", "-color"), ("symbol-black", "-symbolic")):
        svg = ET.parse(SOURCE / f"app/svg/{style}/pyrobolic-small.svg").getroot()
        svg.set("viewBox", "40 50 410 410")
        svg.set("width", "24")
        svg.set("height", "24")
        if suffix == "-symbolic":
            svg.set("viewBox", "54 61 384 362")
            svg.set("preserveAspectRatio", "none")
            defs = svg.find(f"{{{NS}}}defs")
            for element in list(defs):
                if element.tag.endswith("linearGradient"):
                    defs.remove(element)
            for path in svg.iter(f"{{{NS}}}path"):
                path.set("fill", "#2e3436")
                path.set("stroke", "#2e3436")
                path.set("stroke-width", "5")
                path.set("stroke-linejoin", "round")
        ET.ElementTree(svg).write(app_dir / f"{app_id}-tray{suffix}.svg", encoding="utf-8", xml_declaration=True)
    # Pixel-only hosts cannot theme symbolic icons. Keep a light monochrome
    # fallback, while hosts with an installed theme use the genuine SVG.
    fallback_dir = ROOT / "pyrobolic/tray-icons"
    fallback_dir.mkdir(exist_ok=True)
    for size in (16, 24, 32, 48):
        with Image.open(SOURCE / "app/png/symbol-black/128/pyrobolic.png") as source:
            alpha = source.convert("RGBA").getchannel("A")
            alpha = alpha.crop(alpha.getbbox())
            alpha = alpha.resize((size, size), Image.Resampling.LANCZOS)
            fallback = Image.new("RGBA", (size, size), (240, 240, 240, 255))
            fallback.putalpha(alpha)
            fallback.save(fallback_dir / f"pyrobolic-tray-fallback-{size}.png")
        with Image.open(SOURCE / "app/png/symbol/128/pyrobolic.png") as source:
            source.convert("RGBA").crop((10, 12, 113, 115)).resize(
                (size, size), Image.Resampling.LANCZOS).save(fallback_dir / f"pyrobolic-tray-color-fallback-{size}.png")
    for size in (16, 32, 48, 64, 128, 256, 512):
        directory = OUTPUT / f"{size}x{size}/apps"
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / f"app/png/dark/{size}/pyrobolic.png", directory / f"{app_id}.png")
    for source in sorted((SOURCE / "actions/svg/light").glob("*.svg")):
        symbolic(source, OUTPUT / "scalable/actions" / f"pyrobolic-{source.stem}-symbolic.svg")
    directories = ["scalable/apps", "scalable/actions"] + [f"{size}x{size}/apps" for size in (16, 32, 48, 64, 128, 256, 512)]
    index = "[Icon Theme]\nName=Hicolor\nComment=Pyrobolic application icons\nDirectories=" + ",".join(directories) + "\n"
    for directory in directories:
        size = 16 if directory.startswith("scalable") else int(directory.split("x", 1)[0])
        index += f"\n[{directory}]\nSize={size}\nContext={'Actions' if directory.endswith('actions') else 'Applications'}\n"
        index += "Type=Scalable\nMinSize=8\nMaxSize=512\n" if directory.startswith("scalable") else "Type=Fixed\n"
    (OUTPUT / "index.theme").write_text(index, encoding="utf-8")
    print(f"Prepared app icons and symbolic controls in {OUTPUT}")


if __name__ == "__main__":
    main()
