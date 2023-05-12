# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team
import os
import json
import torch
import deepspeed
from deepspeed.inference.engine import InferenceEngine
from deepspeed import OnDevice
from mii.utils import mii_cache_path

from transformers import AutoModelForCausalLM, AutoTokenizer, AutoConfig
from transformers.utils.hub import EntryNotFoundError
from transformers.modeling_utils import get_checkpoint_shard_files
from transformers.utils import WEIGHTS_NAME, WEIGHTS_INDEX_NAME
try:
    from transformers.utils import cached_path, hf_bucket_url
    USE_NEW_HF_CACHE = False
except ImportError:
    from huggingface_hub import snapshot_download
    USE_NEW_HF_CACHE = True
'''
TODO: The following class and functions are non-optimal (i.e., hacky) solutions
to getting the Bloom models working and will be refactored in a future PR
'''


class BloomPipeline(object):
    def __init__(self, model, tokenizer, checkpoint_dict):
        self.model = model
        self.tokenizer = tokenizer
        self.checkpoint_dict = checkpoint_dict

    def __call__(self, inputs, **kwargs):
        local_rank = int(os.getenv('LOCAL_RANK', '0'))
        torch.cuda.set_device(local_rank)
        if isinstance(self.model, InferenceEngine):
            self.model = self.model.module

        # expand proto list into py-list
        inputs = list(inputs)
        tokens = self.tokenizer.batch_encode_plus(inputs,
                                                  return_tensors="pt",
                                                  padding=True)
        for t in tokens:
            if torch.is_tensor(tokens[t]):
                tokens[t] = tokens[t].to(f'cuda:{local_rank}')

        greedy_output = self.model.generate(**tokens, **kwargs)
        outputs = self.tokenizer.batch_decode(greedy_output, skip_special_tokens=True)

        return [[{'generated_text': output}] for output in outputs]


def get_checkpoint_files(model_name, model_path):
    model_file = os.path.join(model_path, WEIGHTS_NAME)
    model_sharded_file = os.path.join(model_path, WEIGHTS_INDEX_NAME)

    if os.path.isfile(model_file):
        resolved_archive_files = [model_file]
    elif os.path.isfile(model_sharded_file):
        resolved_archive_files, sharded_metadata = get_checkpoint_shard_files(
            model_name,
            model_sharded_file,
            cache_dir=model_path,
            revision=None
        )
    else:
        raise FileNotFoundError(f"Could not find checkpoint files for {model_name}")

    return resolved_archive_files


def get_checkpoint_files_old(pretrained_model_name_or_path):
    cache_dir = None
    is_sharded = False
    revision = None
    local_files_only = False

    filename = WEIGHTS_NAME
    archive_file = hf_bucket_url(pretrained_model_name_or_path,
                                 filename=filename,
                                 revision=revision)

    try:
        resolved_archive_file = cached_path(
            archive_file,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
        )
        return [resolved_archive_file]

    except (EntryNotFoundError, FileNotFoundError):
        if filename == WEIGHTS_NAME:
            # Maybe the checkpoint is sharded, we try to grab the index name in this case.
            archive_file = hf_bucket_url(
                pretrained_model_name_or_path,
                filename=WEIGHTS_INDEX_NAME,
                revision=revision,
            )
            resolved_archive_file = cached_path(
                archive_file,
                cache_dir=cache_dir,
                local_files_only=local_files_only,
            )
            is_sharded = True

    if is_sharded:
        # resolved_archive_file becomes a list of files that point to the different checkpoint shards in this case.
        resolved_archive_file, sharded_metadata = get_checkpoint_shard_files(
            pretrained_model_name_or_path,
            resolved_archive_file,
            cache_dir=cache_dir,
            revision=revision
        )

        return resolved_archive_file


def create_checkpoint_dict(model_name, model_path, mii_config):
    if USE_NEW_HF_CACHE:
        model_path = snapshot_download(model_name, cache_dir=model_path)
    if mii_config.checkpoint_dict:
        mii_config.checkpoint_dict['base_dir'] = model_path
        return mii_config.checkpoint_dict
    elif os.path.isfile(os.path.join(model_path, "ds_inference_config.json")):
        with open(os.path.join(model_path, "ds_inference_config.json")) as f:
            data = json.load(f)
        data["base_dir"] = model_path
        return data
    else:
        if USE_NEW_HF_CACHE:
            checkpoint_files = get_checkpoint_files(model_name, model_path)
        else:
            checkpoint_files = get_checkpoint_files_old(model_name)
        return {"type": "BLOOM", "checkpoints": checkpoint_files, "version": 1.0}


def _attempt_load(load_fn, model_name, cache_path, kwargs={}):
    try:
        value = load_fn(model_name, **kwargs)
    except OSError:
        print(f'Attempted load but failed, retrying using cache_dir={cache_path}')
        value = load_fn(model_name, cache_dir=cache_path, **kwargs)
    return value


# TODO: This function is a hack for the Bloom models and will be replaced with a LargeModel provider code path
def load_hf_llm(model_path, model_name, task_name, mii_config):
    deepspeed.init_distributed('nccl')
    local_rank = int(os.getenv('LOCAL_RANK', '0'))
    world_size = int(os.getenv('WORLD_SIZE', '1'))

    cache_path = mii_cache_path()

    tokenizer = _attempt_load(AutoTokenizer.from_pretrained,
                              model_name,
                              cache_path,
                              kwargs={"padding_side": 'left'})
    tokenizer.pad_token = tokenizer.eos_token

    config = _attempt_load(AutoConfig.from_pretrained, model_name, cache_path)

    with OnDevice(dtype=torch.float16, device='meta', enabled=True):
        model = AutoModelForCausalLM.from_config(config, torch_dtype=torch.bfloat16)
    model = model.eval()
    checkpoint_dict = create_checkpoint_dict(model_name, model_path, mii_config)
    torch.distributed.barrier()
    return BloomPipeline(
        model=model, tokenizer=tokenizer, checkpoint_dict=checkpoint_dict
    )
