"""Structural projections of what the write tools produced.

A write tool's return string says almost nothing — `"Created /tmp/x.pptx with
3 slides"` is true of a deck that is completely wrong. What has to be compared
is the package it left behind, and comparing it byte-for-byte is useless
because python-pptx's own serialisation is not part of any contract.

So each write case records a projection: a small dict of the fields that
actually carry the behaviour, pulled out of the XML.

**Read the XML directly, never through python-pptx or python-docx.** Using
python-pptx to compute the expected value would bake its placeholder
inheritance and its element ordering into the oracle, and the TypeScript side
has neither — only a zip reader and an XML parser. A projection the port
cannot independently compute is a projection that proves nothing.

Parts are found through the real relationship graph rather than by assuming
`sheet1.xml` or `slide1.xml`. The assumption happens to hold for these
fixtures, which is exactly why relying on it would hide a port that relied on
it too.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}

_R_ID = "{%s}id" % NS["r"]
_W_VAL = "{%s}val" % NS["w"]


def _xml(zf: zipfile.ZipFile, name: str) -> etree._Element:
    return etree.fromstring(zf.read(name))


def _rels_for(zf: zipfile.ZipFile, part: str) -> dict[str, str]:
    """Relationship id -> resolved part name, for the part's own .rels file."""
    directory, _, filename = part.rpartition("/")
    rels_name = f"{directory}/_rels/{filename}.rels" if directory else f"_rels/{filename}.rels"
    if rels_name not in zf.namelist():
        return {}
    root = _xml(zf, rels_name)
    resolved: dict[str, str] = {}
    for relationship in root.findall("rel:Relationship", NS):
        target = relationship.get("Target", "")
        if target.startswith("/"):
            resolved[relationship.get("Id", "")] = target.lstrip("/")
        else:
            base = directory
            while target.startswith("../"):
                target = target[3:]
                base = base.rpartition("/")[0]
            resolved[relationship.get("Id", "")] = f"{base}/{target}" if base else target
    return resolved


# --------------------------------------------------------------------------
# xlsx
# --------------------------------------------------------------------------


def _sheet_part(zf: zipfile.ZipFile, sheet_name: str | None) -> tuple[str, str]:
    workbook = _xml(zf, "xl/workbook.xml")
    rels = _rels_for(zf, "xl/workbook.xml")
    for sheet in workbook.findall("s:sheets/s:sheet", NS):
        if sheet_name is None or sheet.get("name") == sheet_name:
            return sheet.get("name", ""), rels[sheet.get(_R_ID, "")]
    raise KeyError(f"no sheet named {sheet_name!r}")


def project_xlsx_cell(path: Path, *, sheet: str | None = None, ref: str = "A1") -> dict[str, Any]:
    """The written cell's raw XML, plus what the write cost the rest of the sheet.

    `raw_xml` is the load-bearing field: it shows whether the value went in as
    an inline string, a shared-string index or a number, which the tool's
    return string does not say. `formula_cells_losing_cached_v` is the other
    one — openpyxl's round-trip drops the cached `<v>` from every formula cell
    it did not touch, and a port that edits the sheet XML in place (which the
    rewrite does deliberately) will not.
    """
    with zipfile.ZipFile(path) as zf:
        name, part = _sheet_part(zf, sheet)
        root = _xml(zf, part)
        cell = root.find(f's:sheetData/s:row/s:c[@r="{ref}"]', NS)
        projection: dict[str, Any] = {
            "sheet": name,
            "part": part,
            "found": cell is not None,
            "shared_strings_part_present": "xl/sharedStrings.xml" in zf.namelist(),
        }
        if cell is not None:
            projection["raw_xml"] = etree.tostring(cell, encoding="unicode").strip()
            projection["t"] = cell.get("t")
            projection["s"] = cell.get("s")
            value = cell.find("s:v", NS)
            formula = cell.find("s:f", NS)
            inline = cell.find("s:is/s:t", NS)
            projection["v"] = value.text if value is not None else None
            projection["f"] = formula.text if formula is not None else None
            projection["inline_t"] = inline.text if inline is not None else None

        losing: list[str] = []
        for other in root.iterfind("s:sheetData/s:row/s:c", NS):
            if other.find("s:f", NS) is None:
                continue
            value = other.find("s:v", NS)
            if value is None or value.text in (None, ""):
                losing.append(other.get("r", "?"))
        projection["formula_cells_losing_cached_v"] = sorted(losing)

        workbook = _xml(zf, "xl/workbook.xml")
        projection["sheet_states"] = {
            sheet.get("name", ""): sheet.get("state", "visible")
            for sheet in workbook.findall("s:sheets/s:sheet", NS)
        }
        # Charts and pivot tables are what the deliberate in-place-edit fix
        # exists to preserve; record whether they survived.
        projection["chart_parts"] = sorted(
            n for n in zf.namelist() if n.startswith("xl/charts/")
        )
        projection["pivot_parts"] = sorted(
            n for n in zf.namelist() if n.startswith("xl/pivotTables/")
        )
    return projection


# --------------------------------------------------------------------------
# docx
# --------------------------------------------------------------------------


def project_docx_paragraphs(path: Path) -> dict[str, Any]:
    """Body-level paragraphs only — `./w:body/w:p`, direct children.

    That restriction is the whole point: a `w:p` inside a `w:tbl` is not a body
    child, which is why `read_word_text` excludes table text. A port that walks
    every `w:p` in the document passes every other assertion and still returns
    the wrong text.
    """
    with zipfile.ZipFile(path) as zf:
        root = _xml(zf, "word/document.xml")
        body = root.find("w:body", NS)
        paragraphs: list[dict[str, Any]] = []
        for para in body.findall("w:p", NS):
            style = para.find("w:pPr/w:pStyle", NS)
            runs: list[dict[str, Any]] = []
            breaks: list[str] = []
            for run in para.findall("w:r", NS):
                text_parts: list[str] = []
                for child in run:
                    local = etree.QName(child).localname
                    if local == "t":
                        text_parts.append(child.text or "")
                    elif local in ("br", "cr", "tab", "noBreakHyphen"):
                        breaks.append(local)
                run_props = run.find("w:rPr", NS)
                runs.append({
                    "text": "".join(text_parts),
                    "rPr_keys": sorted(
                        etree.QName(c).localname for c in run_props
                    ) if run_props is not None else [],
                })
            paragraphs.append({
                "styleId": style.get(_W_VAL) if style is not None else None,
                "text": "".join(r["text"] for r in runs),
                "runs": runs,
                "breaks": breaks,
            })

        styles_present: list[str] = []
        if "word/styles.xml" in zf.namelist():
            styles = _xml(zf, "word/styles.xml")
            styles_present = sorted(
                s.get("{%s}styleId" % NS["w"], "")
                for s in styles.findall("w:style", NS)
            )

        table_paragraph_count = len(body.findall(".//w:tbl//w:p", NS))

    return {
        "paragraphs": paragraphs,
        # Proves "Heading 1" resolved to the Heading1 styleId rather than being
        # written through verbatim.
        "styles_present": styles_present,
        "body_paragraph_count": len(paragraphs),
        "table_paragraph_count": table_paragraph_count,
    }


# --------------------------------------------------------------------------
# pptx
# --------------------------------------------------------------------------


def _slide_parts(zf: zipfile.ZipFile) -> list[str]:
    """Slide parts in p:sldIdLst order, which is presentation order."""
    presentation = _xml(zf, "ppt/presentation.xml")
    rels = _rels_for(zf, "ppt/presentation.xml")
    return [
        rels[entry.get(_R_ID, "")]
        for entry in presentation.findall("p:sldIdLst/p:sldId", NS)
    ]


def _shape_text(element: etree._Element) -> str:
    """Concatenate a:t, rendering a:br the way python-pptx does (\\v)."""
    pieces: list[str] = []
    for node in element.iter():
        local = etree.QName(node).localname
        if local == "t":
            pieces.append(node.text or "")
        elif local == "br":
            pieces.append("\v")
    return "".join(pieces)


def _xfrm(element: etree._Element) -> dict[str, Any] | None:
    xfrm = element.find(".//a:xfrm", NS)
    if xfrm is None:
        return None
    off = xfrm.find("a:off", NS)
    ext = xfrm.find("a:ext", NS)
    return {
        "off": {"x": int(off.get("x", 0)), "y": int(off.get("y", 0))} if off is not None else None,
        "ext": {"cx": int(ext.get("cx", 0)), "cy": int(ext.get("cy", 0))} if ext is not None else None,
    }


def _sp_tree_shapes(sp_tree: etree._Element) -> list[dict[str, Any]]:
    shapes: list[dict[str, Any]] = []
    for index, child in enumerate(sp_tree):
        local = etree.QName(child).localname
        if local in ("nvGrpSpPr", "grpSpPr"):
            continue
        name_node = child.find(".//p:cNvPr", NS)
        placeholder = child.find(".//p:nvSpPr/p:nvPr/p:ph", NS)
        shapes.append({
            "tag": local,
            "index": index,
            "name": name_node.get("name") if name_node is not None else None,
            "id": int(name_node.get("id")) if name_node is not None and name_node.get("id") else None,
            "ph": {
                "type": placeholder.get("type"),
                "idx": placeholder.get("idx"),
            } if placeholder is not None else None,
            "xfrm": _xfrm(child),
            "text": _shape_text(child),
        })
    return shapes


def project_pptx_package(path: Path) -> dict[str, Any]:
    """Parts, content types, and every slide's layout, shapes and notes.

    `parts` plus `content_types` is what catches a port that forgot to declare
    a notesSlide it added, which opens fine in some readers and not in
    PowerPoint.
    """
    with zipfile.ZipFile(path) as zf:
        slides: list[dict[str, Any]] = []
        for part in _slide_parts(zf):
            rels = _rels_for(zf, part)
            root = _xml(zf, part)
            layout_part = next(
                (target for target in rels.values() if "slideLayout" in target), None
            )
            layout_name = None
            if layout_part:
                layout_name = _xml(zf, layout_part).find("p:cSld", NS).get("name")
            notes_part = next(
                (target for target in rels.values() if "notesSlide" in target), None
            )
            notes_text = None
            if notes_part:
                notes_root = _xml(zf, notes_part)
                body = notes_root.find(
                    './/p:sp/p:nvSpPr/p:nvPr/p:ph[@type="body"]/../../..', NS
                )
                notes_text = _shape_text(body if body is not None else notes_root)
            slides.append({
                "part": part,
                "layout": {"part": layout_part, "name": layout_name},
                "shapes": _sp_tree_shapes(root.find("p:cSld/p:spTree", NS)),
                "notes": {"part": notes_part, "text": notes_text} if notes_part else None,
            })

        content_types = _xml(zf, "[Content_Types].xml")
        overrides = {
            node.get("PartName"): node.get("ContentType")
            for node in content_types.findall("ct:Override", NS)
        }
        return {
            "parts": sorted(zf.namelist()),
            "content_type_overrides": dict(sorted(overrides.items())),
            "slide_count": len(slides),
            "slides": slides,
        }


def _find_sp(root: etree._Element, shape_name: str) -> etree._Element:
    sp_tree = root.find("p:cSld/p:spTree", NS)
    for child in sp_tree:
        name_node = child.find(".//p:cNvPr", NS)
        if name_node is None:
            continue
        if (name_node.get("name") or "").lower() == shape_name.lower():
            return child
    raise KeyError(f"no shape named {shape_name!r}")


def project_pptx_text_shape(path: Path, *, slide_index: int, shape_name: str) -> dict[str, Any]:
    """bodyPr, autofit and paragraph formatting for one filled text shape.

    `autofit_child_count` is here because `_clear_autofit` has to remove BOTH
    the `a:spAutoFit` that add_textbox ships and any stale `a:normAutofit` left
    by a previous fill. Recording only the winning element would let a port
    that removes one of the two pass.
    """
    with zipfile.ZipFile(path) as zf:
        root = _xml(zf, _slide_parts(zf)[slide_index])
        shape = _find_sp(root, shape_name)
        body_pr = shape.find(".//a:bodyPr", NS)

        autofit = None
        autofit_children = 0
        if body_pr is not None:
            for child in body_pr:
                local = etree.QName(child).localname
                if local in ("noAutofit", "normAutofit", "spAutoFit"):
                    autofit_children += 1
                    autofit = {
                        "tag": local,
                        "fontScale": child.get("fontScale"),
                        "lnSpcReduction": child.get("lnSpcReduction"),
                    }

        paragraphs: list[dict[str, Any]] = []
        for para in shape.findall(".//a:p", NS):
            props = para.find("a:pPr", NS)
            def _spc(tag: str) -> str | None:
                if props is None:
                    return None
                node = props.find(f"a:{tag}/a:spcPts", NS)
                if node is not None:
                    return node.get("val")
                node = props.find(f"a:{tag}/a:spcPct", NS)
                return node.get("val") if node is not None else None

            runs = []
            for run in para.findall("a:r", NS):
                run_props = run.find("a:rPr", NS)
                latin = run_props.find("a:latin", NS) if run_props is not None else None
                text_node = run.find("a:t", NS)
                runs.append({
                    "text": text_node.text if text_node is not None else "",
                    "sz": run_props.get("sz") if run_props is not None else None,
                    "b": run_props.get("b") if run_props is not None else None,
                    "latin": latin.get("typeface") if latin is not None else None,
                    "ea_present": (
                        run_props.find("a:ea", NS) is not None
                        if run_props is not None else False
                    ),
                })
            paragraphs.append({
                "lnSpc": _spc("lnSpc"),
                "spcBef": _spc("spcBef"),
                "spcAft": _spc("spcAft"),
                "runs": runs,
                "br_count": len(para.findall("a:br", NS)),
                "endParaRPr": para.find("a:endParaRPr", NS) is not None,
                "pPr_children": [
                    etree.QName(c).localname for c in props
                ] if props is not None else [],
            })

        return {
            "bodyPr_attrs": dict(body_pr.attrib) if body_pr is not None else None,
            "autofit": autofit,
            "autofit_child_count": autofit_children,
            "paragraph_count": len(paragraphs),
            "paragraphs": paragraphs,
        }


def project_pptx_table_shape(path: Path, *, slide_index: int, shape_name: str) -> dict[str, Any]:
    """The rebuilt table, and where it landed in the z-order.

    `spTree_index` is the field that matters. fill_pptx_table removes the old
    element and APPENDS the new graphicFrame, so it moves to the end of the
    z-order. A port that replaced it in place would match every other field
    here and produce a different deck.
    """
    with zipfile.ZipFile(path) as zf:
        root = _xml(zf, _slide_parts(zf)[slide_index])
        sp_tree = root.find("p:cSld/p:spTree", NS)
        shape = _find_sp(root, shape_name)
        name_node = shape.find(".//p:cNvPr", NS)
        table = shape.find(".//a:tbl", NS)

        cells: list[list[str]] = []
        grid_cols: list[int] = []
        row_heights: list[int] = []
        table_style_id = None
        tbl_pr_attrs: dict[str, str] = {}
        if table is not None:
            grid_cols = [
                int(node.get("w", 0)) for node in table.findall("a:tblGrid/a:gridCol", NS)
            ]
            for row in table.findall("a:tr", NS):
                row_heights.append(int(row.get("h", 0)))
                cells.append([_shape_text(cell) for cell in row.findall("a:tc", NS)])
            tbl_pr = table.find("a:tblPr", NS)
            if tbl_pr is not None:
                tbl_pr_attrs = dict(tbl_pr.attrib)
                style_node = tbl_pr.find("a:tableStyleId", NS)
                table_style_id = style_node.text if style_node is not None else None

        return {
            "tag": etree.QName(shape).localname,
            "spTree_index": list(sp_tree).index(shape),
            "spTree_child_count": len(list(sp_tree)),
            "name": name_node.get("name") if name_node is not None else None,
            "id": int(name_node.get("id")) if name_node is not None and name_node.get("id") else None,
            "max_id_on_slide": max(
                (int(n.get("id")) for n in root.iter("{%s}cNvPr" % NS["p"]) if n.get("id")),
                default=None,
            ),
            "xfrm": _xfrm(shape),
            "gridCols": grid_cols,
            "rowHeights": row_heights,
            "tblPr": tbl_pr_attrs,
            "tableStyleId": table_style_id,
            "cells": cells,
        }


# --------------------------------------------------------------------------
# notebook
# --------------------------------------------------------------------------


def project_notebook(path: Path) -> dict[str, Any]:
    """The notebook as written, plus the two things the writer changes silently.

    `edit_notebook_cell` rewrites source as
    `[line + "\\n" for line in source.splitlines()] or [""]`, which ADDS a
    trailing newline to a source that lacked one. That is lossy and worth
    pinning.
    """
    text = path.read_text(encoding="utf-8")
    return {
        "notebook": json.loads(text),
        "trailing_newline": text.endswith("\n"),
        "indent_two": "\n  \"cells\"" in text or '\n  "cells"' in text,
    }


PROJECTORS = {
    "xlsx_cell": project_xlsx_cell,
    "docx_paragraphs": project_docx_paragraphs,
    "pptx_package": project_pptx_package,
    "pptx_text_shape": project_pptx_text_shape,
    "pptx_table_shape": project_pptx_table_shape,
    "notebook": project_notebook,
}
