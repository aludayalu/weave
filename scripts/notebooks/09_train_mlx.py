#!/usr/bin/env python3
"""Fine tune the weave agent locally on Apple Silicon, through MLX.

This is the local path. 07_train_lora.py is the Databricks GPU path; this one
runs on this machine, in the same venv setup_local_mlx.sh builds.

Unsloth on a Mac does not use the torch or CUDA path at all. It routes through
MLX, with the same API shape:

    FastMLXModel.from_pretrained(...)   instead of  FastModel / AutoModelForCausalLM
    model.get_peft_model(...)           instead of  get_peft_model
    MLXTrainer                          instead of  SFTTrainer

and it carries its own advantages: 4 bit quantization, chunked cross entropy that
saves roughly 4GB a step on a long sequence, and elementwise gradient clipping
which costs nothing where global norm clipping would cost gigabytes.

Two things are deliberate:

  * train_on_responses_only, so loss lands on the assistant turns and the tool
    results stay masked. Training on a tool result teaches the model to invent
    its own tool output, which is the classic way a tool agent dataset goes bad.
  * 4 bit. A 4B model in bf16 is 8GB of weights before any activations, and this
    machine has 16GB shared between the model, the optimiser and everything
    else.

Run:
    ~/.venvs/weave-mlx/bin/python 09_train_mlx.py
    ~/.venvs/weave-mlx/bin/python 09_train_mlx.py --epochs 1     # faster smoke
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import signal
import time
from pathlib import Path

MODEL = os.environ.get("WEAVE_MODEL", "Qwen/Qwen3-4B")
TRAIN_FILE = Path(os.environ.get("WEAVE_TRAIN_FILE", "/tmp/weave-ft/train.jsonl"))
VAL_FILE = Path(os.environ.get("WEAVE_VAL_FILE", "/tmp/weave-ft/val.jsonl"))
OUTPUT_DIR = Path(os.environ.get("WEAVE_OUTPUT_DIR", "/tmp/weave-ft/mlx-adapter"))

MAX_SEQ = int(os.environ.get("WEAVE_MAX_SEQ", "2048"))
LORA_R = int(os.environ.get("WEAVE_LORA_R", "8"))
LORA_ALPHA = int(os.environ.get("WEAVE_LORA_ALPHA", "16"))
LR = float(os.environ.get("WEAVE_LR", "1e-4"))
BATCH = int(os.environ.get("WEAVE_BATCH", "1"))
GRAD_ACCUM = int(os.environ.get("WEAVE_GRAD_ACCUM", "8"))
SEED = 17


def load(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"{path} does not exist. Run 05_build_dataset.py first.")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if not rows:
        raise SystemExit(f"{path} is empty")
    return rows


def strip_to_accepted(row: dict) -> dict:
    """Drop the fields the chat template will not take.

    The dataset rows carry metadata and a title, which are not conversation.
    Passing them through makes the tokenizer silently produce a different
    template than the one the trajectories were generated with.
    """
    messages = []
    for message in row["messages"]:
        clean = {"role": message["role"], "content": message.get("content") or ""}
        if message.get("tool_calls"):
            clean["tool_calls"] = [
                {"type": "function",
                 "function": {"name": call["function"]["name"],
                              # arguments must be a json string in a tool call
                              "arguments": call["function"]["arguments"]
                              if isinstance(call["function"]["arguments"], str)
                              else json.dumps(call["function"]["arguments"])}}
                for call in message["tool_calls"]]
        if message.get("role") == "tool":
            clean["tool_call_id"] = message.get("tool_call_id")
            if message.get("name"):
                clean["name"] = message["name"]
        messages.append(clean)
    return {"messages": messages}


def template_markers(tokenizer) -> tuple[str, str]:
    """The markers that open a user turn and an assistant turn, from the template.

    Qwen uses ChatML, so they are <|im_start|>user and <|im_start|>assistant, but
    the point of probing is that guessing wrong here silently masks the wrong
    span, and training then looks fine while teaching nothing.
    """
    probe = tokenizer.apply_chat_template(
        [{"role": "user", "content": "x"},
         {"role": "assistant", "content": "y"}], tokenize=False)
    for instruction, response in (
            ("<|im_start|>user", "<|im_start|>assistant"),
            ("<|start_header_id|>user", "<|start_header_id|>assistant"),
            ("### Human:", "### Assistant:"),
            ("<|user|>", "<|assistant|>")):
        if instruction in probe and response in probe:
            return instruction, response
    raise SystemExit(
        f"could not find the chat markers in this template, so the loss mask "
        f"would be wrong. Template looked like: {probe[:300]}")


def main() -> int:
    # MLX will sit at low cpu and flat memory when it is blocked rather than
    # busy, and there is no other way to tell those apart from outside. This
    # dumps the actual python stack on demand:  kill -USR1 <pid>
    faulthandler.enable()
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-seq", type=int, default=MAX_SEQ)
    parser.add_argument("--dry-run", action="store_true",
                        help="check the data and the imports, load no model")
    args = parser.parse_args()

    print("=" * 68)
    print(f"weave agent, local MLX fine tune\n  model  {args.model}\n  data   {TRAIN_FILE}")
    print("=" * 68)

    from unsloth_zoo.mlx.loader import FastMLXModel
    from unsloth_zoo.mlx.trainer import (MLXTrainer, MLXTrainingConfig,
                                        train_on_responses_only)
    import mlx.core as mx

    print(f"\nmlx device: {mx.default_device()}")

    train = [strip_to_accepted(r) for r in load(TRAIN_FILE)]
    val = [strip_to_accepted(r) for r in load(VAL_FILE)] if VAL_FILE.exists() else []
    print(f"  {len(train)} train, {len(val)} val conversations")
    for row in train[:1]:
        roles = [m["role"] for m in row["messages"]]
        print(f"  example shape: {len(roles)} messages, "
              f"{roles.count('assistant')} assistant, {roles.count('tool')} tool")

    if args.dry_run:
        print("\ndry run: imports and data are fine, no model loaded")
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()

    print(f"\nloading {args.model} in 4 bit, max_seq {args.max_seq}")
    # from_pretrained returns (model, tokenizer). It is a tuple, not a model,
    # which is what the first version of this assumed and crashed on.
    model, tokenizer = FastMLXModel.from_pretrained(
        args.model, max_seq_length=args.max_seq, load_in_4bit=True)
    print(f"  loaded in {time.time() - started:.0f}s")

    model = FastMLXModel.get_peft_model(
        model, r=LORA_R, lora_alpha=LORA_ALPHA, lora_dropout=0.05,
        target_modules="all-linear", random_state=SEED,
        max_seq_length=args.max_seq)
    # no trainable-parameter count here: mlx Modules have no named_parameters,
    # and get_peft_model already reports the figure

    # The tool list travels in the system prompt, which is where the trajectories
    # put it, so the model is trained on exactly the prompt shape it will be
    # served with. There is no tools argument on MLXTrainer.
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "platinum", Path(__file__).resolve().parent / "04_gold_to_platinum.py")
        platinum = _ilu.module_from_spec(spec)
        spec.loader.exec_module(platinum)
        print(f"  {len(platinum.TOOLS)} tool schemas defined by 04, and named "
              f"in the system prompt")
    except Exception as error:                          # noqa: BLE001
        print(f"  note: 04 not loaded ({type(error).__name__})")

    # MLXTrainingConfig is a dataclass that rejects unknown fields, so these are
    # the real names, not the transformers spelling.
    config = MLXTrainingConfig(
        output_dir=str(OUTPUT_DIR),
        learning_rate=LR,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=BATCH,
        gradient_accumulation_steps=GRAD_ACCUM,
        max_seq_length=args.max_seq,
        seed=SEED,
        gradient_checkpointing=True,
        logging_steps=1,
        report_to=[],
        # mx.compile on the first step stalled this at 2% cpu while the machine
        # swapped 2.3GB. Compiling a 4B graph is not worth that here, and eager
        # mode steps fine on a dataset this small.
        compile=False,
        # leave headroom for the OS. The default guard sized itself at 10.8GB of
        # a 16GB machine, which is what drove it into swap.
        memory_limit_gb=7.5,
    )
    trainer = MLXTrainer(model=model, tokenizer=tokenizer, train_dataset=train,
                         eval_dataset=val or None, args=config)

    # Loss on assistant turns only. This is a function applied to a constructed
    # trainer, not a config field, and it needs the template markers for this
    # tokenizer, which is why the template is probed rather than assumed.
    markers = template_markers(tokenizer)
    train_on_responses_only(
        trainer, instruction_part=markers[0], response_part=markers[1],
        tokenizer=tokenizer, force_match=False)
    print(f"  loss masked to assistant turns, markers {markers}")

    print(f"\ntraining: {args.epochs} epoch(s), batch {BATCH} x accum "
          f"{GRAD_ACCUM}, lr {LR}, seq {args.max_seq}, compile off, "
          f"memory guard 7.5GB\n")

    result = trainer.train()
    elapsed = time.time() - started
    print(f"\ntrained in {elapsed:.0f}s")

    for name in ("save_pretrained", "save_lora_adapters", "save_pretrained_merged"):
        saver = getattr(model, name, None)
        if callable(saver):
            try:
                target = str(OUTPUT_DIR / "adapter")
                saver(target) if name != "save_pretrained_merged" else saver(
                    target, save_method="adapter")
                print(f"  saved with {name} -> {target}")
                break
            except Exception as error:                  # noqa: BLE001
                print(f"  {name} failed: {type(error).__name__}: {error}")
    else:
        trainer.save(str(OUTPUT_DIR / "adapter"))
        print(f"  saved with trainer.save -> {OUTPUT_DIR / 'adapter'}")

    (OUTPUT_DIR / "weave_manifest.json").write_text(json.dumps({
        "model": args.model, "backend": "mlx", "lora_r": LORA_R,
        "lora_alpha": LORA_ALPHA, "epochs": args.epochs, "lr": LR,
        "max_seq": args.max_seq, "batch": BATCH, "grad_accum": GRAD_ACCUM,
        "seed": SEED, "train_conversations": len(train),
        "val_conversations": len(val), "seconds": int(elapsed),
        "loss": {k: v for k, v in (result or {}).items()
                 if isinstance(v, (int, float))},
    }, indent=2, default=str))
    print(f"  manifest -> {OUTPUT_DIR / 'weave_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
