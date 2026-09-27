#!/usr/bin/env python3
"""Turn each notebook .py into a .ipynb, using the `# %%` markers as cells.

Databricks wants .ipynb; the .py stays the source of truth because it is
readable, diffable and runnable outside Databricks. A `# %% [markdown]` marker
becomes a markdown cell, anything else becomes a code cell.

Usage: python3 build_notebooks.py
"""
import ast
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
SOURCES = ["00_recon.py", "01_three_dumps_to_lakebase.py", "02_silver_clean.py"]


def strip_comment(text):
    """Turn a block of # comments into markdown."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            lines.append(stripped[1:].strip())
        elif not stripped:
            lines.append("")
        else:
            lines.append(stripped)
    return "\n".join(lines).strip()


def split_cells(source):
    """Yield (kind, text) for each `# %%` block, docstring first as markdown."""
    if source.startswith('"""'):
        end = source.index('"""', 3) + 3
        yield "markdown", source[:end].strip('"').strip()
        source = source[end:]

    # one capture group only: re.split returns it between the text blocks.
    # [ \t] and [^\n] keep the match on one line, so the line after a
    # marker is never swallowed.
    blocks = re.split(r"^#[ \t]*%%[ \t]*(\[[a-z]+\])?[ \t]*[^\n]*$", source, flags=re.M)
    for index in range(1, len(blocks), 2):
        kind = (blocks[index] or "").strip("[]") or "code"
        body = blocks[index + 1].strip("\n")
        if not body.strip():
            continue
        if kind == "markdown":
            yield "markdown", strip_comment(body)
        else:
            yield "code", body.strip("\n") + "\n"


def to_source(text):
    """Notebooks store source as a list of lines, each keeping its newline."""
    return text.splitlines(keepends=True)


def verify(name, source, cells):
    """Every real line of the .py must survive into the notebook.

    A cell-splitting bug once deleted the line after each marker, which only
    showed up as a NameError on the cluster, so it is checked here instead.
    """
    joined = "\n".join("".join(cell["source"]) for cell in cells)
    missing = []
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith('"""'):
            continue
        if line not in joined:
            missing.append(f"  line {number}: {stripped[:70]}")
    if missing:
        raise SystemExit(f"{name}: {len(missing)} lines lost building the notebook:\n"
                         + "\n".join(missing[:10]))
    return len(joined)


def build(name):
    source = (HERE / name).read_text()
    cells = []
    for kind, text in split_cells(source):
        if kind == "code":
            ast.parse(text)  # a cell that cannot parse must not ship
        cells.append({
            "cell_type": kind,
            "metadata": {},
            "source": to_source(text),
            **({} if kind == "code" else {}),
        } if kind == "markdown" else {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": to_source(text),
        })

    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    kept = verify(name, source, cells)
    target = HERE / (name[:-3] + ".ipynb")
    target.write_text(json.dumps(notebook, indent=1) + "\n")

    code_cells = sum(1 for cell in cells if cell["cell_type"] == "code")
    print(f"{target.name}: {len(cells)} cells ({code_cells} code), "
          f"{target.stat().st_size / 1024:.1f} KB, all source lines kept")


if __name__ == "__main__":
    for filename in SOURCES:
        build(filename)
