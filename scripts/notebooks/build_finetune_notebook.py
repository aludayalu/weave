#!/usr/bin/env python3
"""Build the fine tuning notebook from the pipeline scripts.

The scripts are the source of truth. This turns 05 and 07 into a notebook with
one cell per stage, because the training loop is worth being able to read and
tweak in place, and because a job cannot submit a script that only exists in a
notebook.

Cell boundaries come from the marker comments in the source, so the notebook and
the scripts cannot drift: edit the script, re-run this, and they stay in step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARTS = [HERE / "05_build_dataset.py", HERE / "07_train_lora.py"]


def split_on_markers(text: str) -> list[tuple[str, str]]:
    """Chop source into (kind, body) on the # %% NNN comment markers."""
    # the kind is optional, so a bare `# %% section title` is a code boundary
    # and only `# %% [markdown]` is prose. Without the outer ?: the group is
    # mandatory and no plain marker ever matches.
    pattern = re.compile(r"^# %%+ ?(?:\[?(markdown|code)\]?)? ?(.*)$", re.M)
    marks = list(pattern.finditer(text))
    if not marks:
        return [("code", text)]
    out = []
    # anything before the first marker is real code and must not be dropped,
    # which is how `from __future__ import annotations` went missing once
    if marks and marks[0].start() > 0:
        head = text[:marks[0].start()].strip("\n")
        if head:
            out.append(("code", head + "\n"))
    for index, mark in enumerate(marks):
        kind = mark.group(1) or ("markdown" if "markdown" in mark.group(0) else "code")
        start = mark.end()
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        out.append((kind, text[start:end].strip("\n") + "\n"))
    return out


def markdown(body: str) -> dict:
    lines = [f"# {body.strip()}\n"] if not body.lstrip().startswith("#") else body.splitlines(keepends=True)
    return {"cell_type": "markdown", "metadata": {},
            "source": lines if isinstance(lines, list) else [lines]}


def code(body: str) -> dict:
    return {"cell_type": "code", "execution_count": None, "metadata": {},
            "outputs": [], "source": body.splitlines(keepends=True)}


def main() -> int:
    cells = []
    for part in PARTS:
        text = part.read_text()
        doc = text.split('"""', 2)[1].strip()
        cells.append(markdown(f"## {part.stem}\n\n{doc}"))
        for kind, body in split_on_markers(text.split('"""', 2)[2]):
            if not body.strip():
                continue
            cells.append(markdown(body) if kind == "markdown" else code(body))

    # Nothing in the training path is preinstalled. On a Databricks ML runtime
    # torch comes with the image but transformers, peft, trl and datasets do not,
    # and psycopg is needed for the dataset stage even there.
    cells.append(markdown("## install\n\nRun this once, then restart the kernel and Run All. "
                          "`torch` is already in the Databricks ML runtime, so only the "
                          "adapter libraries are installed here."))
    cells.append(code(
        "%pip install -q psycopg[binary] transformers peft trl datasets accelerate\n"
        "# torch is in the ML runtime. If this cluster is CPU only, the training\n"
        "# cell below cannot work at all: LoRA on 8B needs an 80GB GPU.\n"
        "import torch\n"
        "print('torch', torch.__version__, '| cuda', torch.cuda.is_available(),\n"
        "      '|', torch.cuda.device_count(), 'gpu(s)')\n"))

    # training stays a script, because it needs GPU libraries and has to be
    # submitted to a job rather than run in a notebook kernel. The last cell
    # shells out to it rather than inlining 200 lines of trainer.
    cells.append(markdown("## train\n\nThe trainer is `07_train_lora.py`, not a cell. It needs a "
                          "GPU and has to be submitted to a job, so it stays a script. The cell "
                          "below runs it here for a smoke test, or a job runs it for real."))
    # resolved rather than relative, because a notebook kernel's working
    # directory is not reliably the folder the notebook was uploaded to
    cells.append(code(
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "here = Path(globals().get('__file__') or Path.cwd() / '_').resolve().parent\n"
        "trainer = next((p for p in here.glob('*train_lora.py')), None)\n"
        "if trainer is None:\n"
        "    raise SystemExit(f'put 07_train_lora.py next to this notebook; looked in {here}')\n"
        "env = dict(os.environ,\n"
        "           WEAVE_TRAIN_FILE='/tmp/weave-ft/train.jsonl',\n"
        "           WEAVE_VAL_FILE='/tmp/weave-ft/val.jsonl',\n"
        "           WEAVE_OUTPUT_DIR='/tmp/weave-ft/model')\n"
        "done = subprocess.run([sys.executable, str(trainer)], env=env,\n"
        "                      capture_output=True, text=True)\n"
        "print(done.stdout[-4000:])\n"
        "print(done.stderr[-4000:])\n"))

    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python",
                           "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4, "nbformat_minor": 5,
    }
    out = HERE / "08_finetune.ipynb"
    out.write_text(json.dumps(notebook, indent=1))
    code_cells = sum(1 for c in cells if c["cell_type"] == "code")
    print(f"{out.name}: {len(cells)} cells ({code_cells} code), "
          f"{out.stat().st_size:,}b")
    for index, cell in enumerate(cells):
        head = "".join(cell["source"])[:66].replace("\n", " ")
        print(f"  {index:>2} {cell['cell_type'][:4]}  {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
