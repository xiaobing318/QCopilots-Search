#!/usr/bin/env python3
"""Generate independent Qwen reference vectors from the original BF16 weights.

Run in an isolated Python 3.12 environment with the pinned dependencies below.
All downloads and output stay outside the checkout. Reviewed, small JSON outputs
can subsequently be copied into tests/runtime/fixtures/qwen. No GGUF or project
inference executable is used by this program.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_COMMIT = "393e2978d27852b0d0230d6994f37f9c15bed73c"
SCRIPT_SHA256 = "0e447b6a80e05527f3fd437ab4f1617a6e7d0df102b8164348bbce787a41b267"
SCRIPT_URL = ("https://raw.githubusercontent.com/QwenLM/Qwen3-VL-Embedding/"
              + SCRIPT_COMMIT + "/src/models/qwen3_vl_embedding.py")
DEPENDENCIES = {
    "torch": "2.8.0", "torchvision": "0.23.0", "transformers": "4.57.1",
    "qwen-vl-utils": "0.0.14", "accelerate": "1.11.0",
    "huggingface-hub": "0.36.0", "safetensors": "0.6.2",
    "numpy": "2.2.6", "pillow": "11.3.0",
}
MODELS = {
    "qwen3-vl-embedding-2b": {
        "repo": "Qwen/Qwen3-VL-Embedding-2B",
        "revision": "9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda",
        "dimensions": 2048, "minimumCosine": 0.995,
        "referenceDtype": "float32",
        "weights": [("model.safetensors", 4255140312,
                     "c73fa9caeddeb3ff831d46c085a7a5708343248ca777e90f2d486964464509c1")],
    },
    "qwen3-vl-embedding-8b": {
        "repo": "Qwen/Qwen3-VL-Embedding-8B",
        "revision": "2c4565515e0f265c6511776e7193b22c0968ddc7",
        "dimensions": 4096, "minimumCosine": 0.97,
        "referenceDtype": "bfloat16",
        "weights": [
            ("model-00001-of-00004.safetensors", 4998056552,
             "79ef275ec5f751d5fb59357c00d473268f9fd74abf5e38aa30137d268e7733c4"),
            ("model-00002-of-00004.safetensors", 4915962464,
             "a4da61f512e84fc0f0b80bcb7bcc5137eb3bf25b658a7d55f84f4056078545f0"),
            ("model-00003-of-00004.safetensors", 4915962496,
             "7fb17cf8f06d6fe5aaacf114e85c4e6d8318799f24b517f50d1ec154a8d47007"),
            ("model-00004-of-00004.safetensors", 1459698112,
             "000213b6d1d03ed9023fac23716da51ec4c5be221a04526c2f732d31d8fed1f5"),
        ],
    },
}
SMALL_FILES = (
    "config.json", "preprocessor_config.json", "video_preprocessor_config.json",
    "tokenizer.json", "tokenizer_config.json", "added_tokens.json",
    "special_tokens_map.json", "vocab.json", "merges.txt", "chat_template.jinja",
)
COMMON_METADATA = {
    "added_tokens.json": (707, "c0284b582e14987fbd3d5a2cb2bd139084371ed9acbae488829a1c900833c680"),
    "chat_template.jinja": (5519, "a47e6afb389f86f45be7810f17d2686fd42b2bec7ba6e6958abf85845af258c5"),
    "merges.txt": (1671853, "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"),
    "special_tokens_map.json": (613, "76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd"),
    "tokenizer_config.json": (5404, "355f2b4e5bad7b01f11ef6cb68ebc176f61b95c3276092ea225b1bea0e01e95c"),
    "tokenizer.json": (11423705, "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"),
    "video_preprocessor_config.json": (817, "59c5c9eb52182eb14c06ffb10ca9effd29adce5f238a95de23ca14a38dbd2cb1"),
    "vocab.json": (2776833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
}
METADATA = {
    "qwen3-vl-embedding-2b": {
        **COMMON_METADATA,
        "config.json": (1570, "9172f55b0b9cce70b7f67b10c58a408ccf3ec15c587e6efd4d5f41631237fded"),
        "preprocessor_config.json": (783, "d75c1f9f474add87cb0188d43c4fbf5c232d2af64f156fcefc5311089049100d"),
    },
    "qwen3-vl-embedding-8b": {
        **COMMON_METADATA,
        "config.json": (1539, "1eaa88d8d8824a47304e864c14101fe9efe3286607a7573f27eb723637f0d73d"),
        "preprocessor_config.json": (784, "d6ccdc0fe0f6b49aaeb3c10d2687289a057233ed685b30155202898a0e5e470f"),
        "model.safetensors.index.json": (67737, "f8656aa4a0a666568f6f6befe980a2ba02d9702eb909bd4c211d6f5db1f980ea"),
    },
}
PREFIX = "<|im_start|>system\nRepresent the user's input.<|im_end|>\n<|im_start|>user\n"
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def external_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError("Cache and generated output must be outside the source tree")
    return path


def download(url: str, destination: Path, expected_size: int | None = None,
             expected_sha: str | None = None) -> None:
    if destination.is_file():
        if ((expected_size is None or destination.stat().st_size == expected_size)
                and (expected_sha is None or sha256(destination) == expected_sha)):
            return
        raise ValueError(f"Existing file failed verification: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    for attempt in range(6):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            if expected_size is not None and offset >= expected_size:
                if offset == expected_size and expected_sha is not None and sha256(partial) == expected_sha:
                    partial.replace(destination)
                    return
                partial.unlink()
                offset = 0
            elif offset and expected_sha is not None and sha256(partial) == expected_sha:
                partial.replace(destination)
                return
            headers = {"User-Agent": "qcs-independent-qwen-reference/1"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            # Avoid a previously cached signed URL expiring during a resumed transfer.
            request_url = url + ("&" if "?" in url else "?") + f"qcs={int(time.time())}"
            with urllib.request.urlopen(urllib.request.Request(request_url, headers=headers),
                                        timeout=180) as response:
                if offset and response.status != 206:
                    offset = 0
                elif offset and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise ValueError("Server returned an unexpected byte range")
                with partial.open("ab" if offset else "wb") as stream:
                    shutil.copyfileobj(response, stream, length=8 * 1024 * 1024)
            if expected_size is not None and partial.stat().st_size != expected_size:
                raise ValueError(f"Wrong download size for {destination.name}")
            if expected_sha is not None and sha256(partial) != expected_sha:
                raise ValueError(f"Wrong download SHA-256 for {destination.name}")
            partial.replace(destination)
            return
        except Exception as error:
            print(f"Download retry {attempt + 1}/6 for {destination.name}: {error}", flush=True)
            if attempt == 5:
                raise
            time.sleep(min(2 ** attempt, 30))


def prepare(model_id: str, cache: Path, metadata_only: bool) -> tuple[Path, Path]:
    spec = MODELS[model_id]
    model_dir = cache / "weights" / model_id
    files = list(SMALL_FILES)
    if len(spec["weights"]) > 1:
        files.append("model.safetensors.index.json")
    base = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/"
    for name in files:
        print(f"Prepare {model_id}/{name}", flush=True)
        size, checksum = METADATA[model_id][name]
        download(base + name, model_dir / name, size, checksum)
    official = cache / "upstream" / "qwen3_vl_embedding.py"
    download(SCRIPT_URL, official, expected_size=14682, expected_sha=SCRIPT_SHA256)
    if not metadata_only:
        remaining = 0
        for name, size, _ in spec["weights"]:
            if (model_dir / name).is_file():
                continue
            partial = model_dir / (name + ".part")
            existing = partial.stat().st_size if partial.is_file() else 0
            remaining += size - existing if 0 <= existing <= size else size
        if shutil.disk_usage(cache).free < remaining + 2 * 1024 ** 3:
            raise ValueError("Insufficient disk space for original weights and a 2 GiB reserve")
        def prepare_weight(weight: tuple[str, int, str]) -> None:
            name, size, checksum = weight
            print(f"Prepare {model_id}/{name} ({size} bytes)", flush=True)
            download(base + name, model_dir / name, size, checksum)
        # Separate shard files avoid duplicate caches and keep transfer concurrency bounded.
        with ThreadPoolExecutor(max_workers=2) as transfers:
            list(transfers.map(prepare_weight, spec["weights"]))
    return model_dir, official


def cases() -> list[dict]:
    result = [
        {"id": "query-en", "kind": "query", "textOnly": True,
         "text": "payment declined error"},
        {"id": "query-zh", "kind": "query", "textOnly": True,
         "text": "银行卡付款被拒绝时应该怎么办？"},
        {"id": "query-leading-newline", "kind": "query", "textOnly": True,
         "text": "\n银行卡付款被拒绝时应该怎么办？"},
        {"id": "document-en", "kind": "document", "textOnly": False,
         "title": "Payment troubleshooting.txt",
         "text": "If a card payment is declined, check the card details and available funds, then contact the bank."},
        {"id": "document-zh", "kind": "document", "textOnly": False,
         "title": "银行卡支付故障排查",
         "text": "如果银行卡付款被拒绝，请核对卡号、有效期和账户余额，然后联系发卡银行。"},
        {"id": "unrelated-en", "kind": "document", "textOnly": False,
         "title": "Garden notes", "text": "Sunflowers grow best in full sunlight. Water the soil and remove weeds regularly."},
        {"id": "long-query", "kind": "query", "textOnly": True,
         "text": ("Find the troubleshooting notes for a declined payment and the bank response. " * 90)
                 + "保留完整的查询模板尾部。"},
    ]
    for image, label in (("../photo.jpg", "photo"), ("portrait.png", "portrait")):
        for budget, tokens in ((140, 320), (280, 640), (560, 1280)):
            result.append({"id": f"image-{label}-{tokens}", "kind": "image",
                           "textOnly": False, "image": image, "budget": budget,
                           "visualTokenBudget": tokens})
    result.append({"id": "image-screen-640", "kind": "image", "textOnly": False,
                   "image": "../screen.png", "budget": 280, "visualTokenBudget": 640})
    return result


def generate(args: argparse.Namespace, model_dir: Path, official_path: Path) -> Path:
    actual = {name: importlib.metadata.version(name) for name in DEPENDENCIES}
    for name, required in DEPENDENCIES.items():
        if actual[name].split("+")[0] != required:
            raise ValueError(f"{name} must be {required}, found {actual[name]}")
    import torch
    from PIL import Image
    from qwen_vl_utils.vision_process import process_vision_info
    from transformers.models.qwen3_vl.processing_qwen3_vl import Qwen3VLProcessor

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    module_spec = importlib.util.spec_from_file_location("qcs_official_qwen_reference", official_path)
    upstream = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = upstream
    module_spec.loader.exec_module(upstream)
    print(f"Loading original BF16 weights with {args.dtype} computation on CPU", flush=True)
    model = upstream.Qwen3VLForEmbedding.from_pretrained(
        model_dir, torch_dtype=getattr(torch, args.dtype), attn_implementation="sdpa",
        low_cpu_mem_usage=True, local_files_only=True,
    ).eval()
    model.config.use_cache = False
    processor = Qwen3VLProcessor.from_pretrained(model_dir, padding_side="right", local_files_only=True)
    # Reuse upstream formatting and last-token pooling without its device autodetection
    # or the right truncation that would drop QCS's required assistant suffix.
    formatter = object.__new__(upstream.Qwen3VLEmbedder)
    formatter.default_instruction = "Represent the user's input."
    formatter.min_pixels = 4 * 32 * 32
    formatter.total_pixels = upstream.MAX_TOTAL_PIXELS
    formatter.fps, formatter.max_frames = 1, 64
    inputs = cases()
    for item in inputs:
        started = time.monotonic()
        print(f"Infer {args.model}/{item['id']}", flush=True)
        if item["kind"] == "image":
            image_path = (args.fixtures_root / item["image"]).resolve()
            item["sha256"] = sha256(image_path)
            source_image = Image.open(image_path).convert("RGB")
            item["originalImageSize"] = list(source_image.size)
            formatter.max_pixels = item["visualTokenBudget"] * 32 * 32
            conversation = formatter.format_model_input(image=source_image)
            images, videos, video_kwargs = process_vision_info(
                [conversation], image_patch_size=16,
                return_video_metadata=True, return_video_kwargs=True)
            if videos is not None or not images:
                raise ValueError("Unexpected image preprocessing result")
            item["imageSize"] = list(images[0].size)
            prompt = processor.apply_chat_template([conversation], add_generation_prompt=True, tokenize=False)
            batch = processor(text=prompt, images=images, padding=True, truncation=False,
                              do_resize=False, return_tensors="pt", **video_kwargs)
            item["imageGridThw"] = batch["image_grid_thw"].tolist()
        else:
            text = item["text"]
            item["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            title = item.get("title", "")
            body = (title + "\n" if title else "") + text
            conversation = formatter.format_model_input(text=body)
            prompt = processor.apply_chat_template([conversation], add_generation_prompt=True, tokenize=False)[0]
            if prompt != PREFIX + body + SUFFIX:
                raise ValueError("Upstream chat template no longer matches the reviewed input contract")
            # The official tokenizer's TemplateProcessing appends <|endoftext|>.
            # It has attention_mask=1 and is the actual LAST-pooled token.
            tokens = processor.tokenizer.encode(prompt, add_special_tokens=True)
            capacity = 512 if item["textOnly"] else 2048
            item["untruncatedTokenCount"] = len(tokens)
            if len(tokens) > capacity:
                suffix_ids = processor.tokenizer.encode(SUFFIX, add_special_tokens=True)
                body_ids = processor.tokenizer.encode(PREFIX + body, add_special_tokens=False)
                if tokens != body_ids + suffix_ids:
                    raise ValueError("Cannot safely truncate across template token boundaries")
                tokens = body_ids[:capacity - len(suffix_ids)] + suffix_ids
            batch = {"input_ids": torch.tensor([tokens]), "attention_mask": torch.ones((1, len(tokens)), dtype=torch.long)}
        item["tokenIds"] = batch["input_ids"][0].tolist()
        item["contextLength"] = 512 if item["textOnly"] else 2048
        if len(item["tokenIds"]) > item["contextLength"]:
            raise ValueError("Reference input exceeds the application context")
        with torch.inference_mode():
            output = model(**batch, use_cache=False)
            vector = upstream.Qwen3VLEmbedder._pooling_last(output.last_hidden_state, batch["attention_mask"])
            # Float32 normalization avoids an extra BF16 rounding at the output boundary.
            vector = torch.nn.functional.normalize(vector.float(), p=2, dim=-1)[0]
        item["vector"] = vector.tolist()
        expected = MODELS[args.model]["dimensions"]
        if len(item["vector"]) != expected or not all(math.isfinite(x) for x in item["vector"]):
            raise ValueError("Invalid independent reference vector")
        print(f"Complete {item['id']}: {len(item['tokenIds'])} tokens in {time.monotonic()-started:.1f}s", flush=True)
    spec = MODELS[args.model]
    provenance = {
        "sourceRepo": "https://github.com/QwenLM/Qwen3-VL-Embedding",
        "revision": SCRIPT_COMMIT, "scriptPath": "src/models/qwen3_vl_embedding.py",
        "scriptSha256": SCRIPT_SHA256, "modelRepo": spec["repo"], "modelRevision": spec["revision"],
        "weights": [{"file": name, "bytes": size, "sha256": checksum} for name, size, checksum in spec["weights"]],
        "modelMetadata": {path.name: sha256(path) for path in sorted(model_dir.iterdir()) if path.is_file() and path.suffix != ".safetensors"},
        "dependencies": actual, "python": sys.version.split()[0],
        "allInstalledPackages": dict(sorted((distribution.metadata["Name"], distribution.version)
                                             for distribution in importlib.metadata.distributions())),
        "generatorSha256": sha256(Path(__file__)),
        "weightsDtype": "bfloat16", "dtype": args.dtype, "normalizationDtype": "float32", "device": "cpu",
        "attention": "sdpa", "batchSize": 1, "useCache": False,
        "formatting": "Official ChatML and tokenizer TemplateProcessing terminal <|endoftext|>, title newline body, body-only 512/2048 token truncation preserving full assistant suffix and terminal token",
        "imagePreprocessing": "Official qwen-vl-utils smart_resize, patch 16, merge 2, min_pixels 4096, per-input max_pixels, RGB Pillow bicubic",
    }
    result = {"schemaVersion": 1, "modelId": args.model, "dimensions": spec["dimensions"],
              "minimumCosine": spec["minimumCosine"], "provenance": provenance, "inputs": inputs,
              "rankings": [{"query": "query-en", "preferred": "document-en", "other": "unrelated-en"},
                           {"query": "query-zh", "preferred": "document-zh", "other": "unrelated-en"}]}
    by_id = {item["id"]: item["vector"] for item in inputs}
    for ranking in result["rankings"]:
        cosine = lambda a, b: sum(x * y for x, y in zip(by_id[a], by_id[b]))
        if cosine(ranking["query"], ranking["preferred"]) <= cosine(ranking["query"], ranking["other"]):
            raise ValueError("Independent reference failed the expected semantic ranking")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / (args.model + ".json")
    temporary = output_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=MODELS)
    parser.add_argument("--cache-root", required=True, type=external_path)
    parser.add_argument("--output-dir", required=True, type=external_path)
    parser.add_argument("--fixtures-root", type=Path, default=ROOT / "tests/runtime/fixtures/qwen")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--dtype", choices=("float32", "bfloat16"),
                        help="Reference compute dtype. Defaults to FP32 for 2B and BF16 for 8B.")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    args.dtype = args.dtype or MODELS[args.model]["referenceDtype"]
    if not 1 <= args.threads <= 64:
        parser.error("--threads must be between 1 and 64")
    args.cache_root.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    model_dir, official = prepare(args.model, args.cache_root, args.metadata_only)
    if not args.prepare_only and not args.metadata_only:
        print(generate(args, model_dir, official), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
