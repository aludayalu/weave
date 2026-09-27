"""Fine tune the weave agent model on the trajectory dataset.

This is the job body for 06_finetune.py. It reads the chat+tools jsonl that
05_build_dataset.py wrote, and trains a LoRA adapter on top of an instruct model,
so the result behaves like a tool using assistant rather than like a completion
engine.

Two things about the data shape, because they are the whole point:

  * A training row is one whole conversation. The system prompt names the tools,
    the user states the task, and then there are dozens of assistant and tool
    messages. Loss is applied to the assistant messages only, including the
    tool_call arguments, because those are what the model has to learn to emit.
  * Tool results are masked out. They are context, not output, and training on
    them teaches the model to hallucinate tool output.

Run it as a Databricks job on a GPU, with the dataset on a volume. It expects
the env vars the job sets: WEAVE_TRAIN_FILE, WEAVE_VAL_FILE, WEAVE_OUTPUT_DIR.
"""

from __future__ import annotations

# %% imports

import importlib
import json
import os
import random
import time
from pathlib import Path

TRAIN_FILE = os.environ.get("WEAVE_TRAIN_FILE", "/Volumes/main/weave/datasets/train.jsonl")
VAL_FILE = os.environ.get("WEAVE_VAL_FILE", "/Volumes/main/weave/datasets/val.jsonl")
OUTPUT_DIR = Path(os.environ.get("WEAVE_OUTPUT_DIR", "/Volumes/main/weave/models/weave-agent-v1"))

# Qwen3-8B. Same family as the model that generated the trajectories, so the
# adapter starts from weights that already know this tool call format and this
# style, and it fits one 80GB GPU in bf16 with room for activations at 16k
# context. No quantisation needed, which keeps the training numerics clean.
#
# The 122B variant of this model is a 122B MoE and needs 2 to 4 GPUs even at 4
# bit, so it is not the one to iterate on. Set WEAVE_BASELINE to change it.
BASE_MODEL = os.environ.get("WEAVE_BASELINE", "Qwen/Qwen3-8B")
LOAD_IN_4BIT = os.environ.get("WEAVE_4BIT", "0") == "1"

# 18 conversations is very little data. Rank stays low on purpose: a high rank
# adapter on 18 examples memorises them rather than learning the behaviour, and
# that failure is invisible because training loss still looks fine.
LORA_R = int(os.environ.get("WEAVE_LORA_R", "8"))
LORA_ALPHA = int(os.environ.get("WEAVE_LORA_ALPHA", "16"))
EPOCHS = float(os.environ.get("WEAVE_EPOCHS", "3"))
LEARNING_RATE = float(os.environ.get("WEAVE_LR", "1e-4"))
MAX_SEQ = int(os.environ.get("WEAVE_MAX_SEQ", "16384"))
BATCH = int(os.environ.get("WEAVE_BATCH", "1"))
GRAD_ACCUM = int(os.environ.get("WEAVE_GRAD_ACCUM", "8"))
SEED = 17


# %% data

def need(module: str, package: str, why: str):
    """Import something or say exactly what to install.

    A bare `import torch` in a script that also runs as a notebook cell gives a
    ModuleNotFoundError traceback, which does not tell anyone that the fix is a
    pip line. Notebook 03 has had this shape for its dbutils shim all along, so
    these two files should not be worse.
    """
    try:
        return importlib.import_module(module)
    except ImportError:
        raise SystemExit(
            f"{module} is needed to {why}, and it is not installed.\n"
            f"  %pip install {package}\n"
            f"then restart the kernel and run this again.")


def load(path: str) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def to_chat(row: dict) -> tuple[list[dict], list[bool]]:
    """Split a conversation into messages and a per-token mask of what to train on.

    Only the assistant turns get loss. The system prompt, the task, and every
    tool result are context the model reads but must not be imitated.
    """
    messages, trainable = [], []
    for message in row["messages"]:
        messages.append({
            "role": message["role"],
            "content": message.get("content") or "",
            **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {}),
            **({"tool_call_id": message["tool_call_id"],
                "name": message.get("name")} if message.get("role") == "tool" else {}),
        })
        # an assistant turn is trainable, a tool result never is
        trainable.append(message["role"] == "assistant")
    return messages, trainable


# %% train

def main() -> int:
    torch = need("torch", "torch", "train on a GPU")
    Dataset = need("datasets", "datasets", "hold the conversations").Dataset
    peft = need("peft", "peft", "add the LoRA adapter")
    trl = need("trl", "trl", "run the SFT loop")
    transformers = need("transformers", "transformers", "load the base model")
    get_peft_model = peft.get_peft_model
    LoraConfig = peft.LoraConfig
    SFTTrainer = trl.SFTTrainer
    AutoModelForCausalLM = transformers.AutoModelForCausalLM
    AutoTokenizer = transformers.AutoTokenizer
    DataCollatorForCompletionOnlyLM = transformers.DataCollatorForCompletionOnlyLM
    Trainer = transformers.Trainer
    TrainingArguments = transformers.TrainingArguments
    if not torch.cuda.is_available():
        raise SystemExit(
            "no GPU on this cluster. Building the dataset works anywhere, but "
            "LoRA on an 8B model needs an 80GB GPU. Attach a GPU runtime.")

    print("=" * 68)
    print(f"weave agent fine tune\n  base   {BASE_MODEL}\n  data   {TRAIN_FILE}")
    print("=" * 68)

    # load and tokenise
    train_rows, val_rows = load(TRAIN_FILE), load(VAL_FILE)
    print(f"\n{len(train_rows)} train, {len(val_rows)} val conversations")
    print(f"  lora r={LORA_R} alpha={LORA_ALPHA} epochs={EPOCHS} lr={LEARNING_RATE}")

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def to_record(row: dict) -> dict:
        messages, trainable = to_chat(row)
        text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False)
        ids = tokenizer(text, truncation=True, max_length=MAX_SEQ)["input_ids"]
        # mask everything before the first assistant turn, and mask nothing else
        # from the loss side. SFTTrainer applies the completion mask, so this
        # only has to be the right length.
        return {"text": text, "length": len(ids)}

    train = Dataset.from_list([to_record(r) for r in train_rows])
    val = Dataset.from_list([to_record(r) for r in val_rows]) if val_rows else None
    print(f"  longest conversation {max(r['length'] for r in train_rows)} tokens")

    # load the base model
    load_kwargs = {"device_map": "auto", "torch_dtype": torch.bfloat16}
    if LOAD_IN_4BIT:
        from transformers import BitsAndBytesConfig
        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)
        print("\n  4 bit nf4")
    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL, **load_kwargs)
    model.config.use_cache = False
    if LOAD_IN_4BIT:
        from peft import prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(
        r=LORA_R, lora_alpha=LORA_ALPHA, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM",
        # the projection heads, so the adapter can actually learn the new
        # behaviour rather than nudge the existing one
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"]))
    model.print_trainable_parameters()

    # completion-only masking
    # Only train on the assistant span. Getting the template markers right here
    # is fiddly, so they are discovered from the tokenizer rather than guessed.
    # Qwen's chat template is ChatML, which is not the Llama header style the
    # marker would otherwise assume, so it is taken from the template itself.
    probe = tokenizer.apply_chat_template(
        [{"role": "user", "content": "x"},
         {"role": "assistant", "content": "y"}], tokenize=False)
    marker = next((m for m in ("<|im_start|>assistant", "### Assistant:",
                               "<|start_header_id|>assistant")
                   if m in probe), "<|im_start|>assistant")
    print(f"  response template marker: {marker}")
    collator = DataCollatorForCompletionOnlyLM(
        response_template=marker, tokenizer=tokenizer)

    # run
    args = TrainingArguments(
        output_dir=str(OUTPUT_DIR),
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH,
        gradient_accumulation_steps=GRAD_ACCUM,
        learning_rate=LEARNING_RATE,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        logging_steps=1,
        save_strategy="epoch",
        eval_strategy="epoch" if val else "no",
        bf16=True,
        gradient_checkpointing=True,
        report_to=[],
        seed=SEED,
        remove_unused_columns=False,
    )

    trainer = SFTTrainer(
        model=model, args=args, train_dataset=train, eval_dataset=val,
        data_collator=collator, tokenizer=tokenizer,
        max_seq_length=MAX_SEQ, packing=False)
    started = time.time()
    result = trainer.train()
    print(f"\ntrained in {time.time() - started:.0f}s, "
          f"train loss {result.training_loss:.4f}")

    if val is not None:
        metrics = trainer.evaluate()
        print("  eval:", {k: round(v, 4) for k, v in metrics.items()})

    # save
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(OUTPUT_DIR / "adapter"))
    tokenizer.save_pretrained(str(OUTPUT_DIR / "adapter"))
    (OUTPUT_DIR / "adapter" / "weave_manifest.json").write_text(json.dumps({
        "base_model": BASE_MODEL, "lora_r": LORA_R, "lora_alpha": LORA_ALPHA,
        "epochs": EPOCHS, "learning_rate": LEARNING_RATE, "seed": SEED,
        "train_conversations": len(train_rows), "val_conversations": len(val_rows),
        "train_loss": float(result.training_loss), "finished": time.time(),
    }, indent=2))
    print(f"\nsaved adapter to {OUTPUT_DIR / 'adapter'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
