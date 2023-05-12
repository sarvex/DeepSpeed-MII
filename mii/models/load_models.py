# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team
import os
import mii
import json
import torch
import inspect
import deepspeed
from deepspeed.runtime.config import DeepSpeedConfig
from deepspeed.runtime.zero.config import ZeroStageEnum


def load_models(task_name,
                model_name,
                model_path,
                ds_optimize,
                ds_zero,
                provider,
                mii_config,
                ds_config_path=None):
    global generator
    local_rank = int(os.getenv('LOCAL_RANK', '0'))
    world_size = int(os.getenv('WORLD_SIZE', '1'))

    inf_config = {
        "tensor_parallel": {
            "tp_size": world_size,
            "mpu": None
        },
        "dtype": mii_config.dtype,
        "replace_method": "auto",
        "enable_cuda_graph": mii_config.enable_cuda_graph,
        "checkpoint": None,
        "config": None,
        "training_mp_size": 1,
        "replace_with_kernel_inject": mii_config.replace_with_kernel_inject,
        "max_tokens": mii_config.max_tokens
    }

    if provider == mii.constants.ModelProvider.HUGGING_FACE:
        from mii.models.providers.huggingface import hf_provider
        if "bigscience/bloom" in model_name:
            assert mii_config.dtype in [
                torch.half,
                torch.int8,
            ], "Bloom models only support fp16/int8"
            assert mii_config.enable_cuda_graph == False, "Bloom models do no support Cuda Graphs"
        inference_pipeline = hf_provider(model_path, model_name, task_name, mii_config)
    elif provider == mii.constants.ModelProvider.ELEUTHER_AI:
        from mii.models.providers.eleutherai import eleutherai_provider
        assert mii_config.dtype == torch.half, "gpt-neox only support fp16"
        assert mii_config.enable_cuda_graph == False, "Provider EleutherAI not supported with Cuda Graphs"
        from megatron import mpu
        inf_config["tensor_parallel"]["mpu"] = mpu
        inference_pipeline = eleutherai_provider(model_path,
                                                 model_name,
                                                 task_name,
                                                 mii_config)
        inf_config["training_mp_size"] = 2
        inf_config["config"] = inference_pipeline.neox_args
    elif provider == mii.constants.ModelProvider.HUGGING_FACE_LLM:
        from mii.models.providers.llm import load_hf_llm
        assert mii_config.dtype in [
            torch.half,
            torch.int8,
        ], "Bloom models only support fp16/int8"
        assert mii_config.enable_cuda_graph == False, "Bloom models do no support Cuda Graphs"
        inference_pipeline = load_hf_llm(model_path, model_name, task_name, mii_config)
        inf_config["checkpoint"] = inference_pipeline.checkpoint_dict
        if (
            mii_config.dtype == torch.int8
            and "enable_qkv_quantization"
            in inspect.signature(deepspeed.init_inference).parameters
        ):
            inf_config["enable_qkv_quantization"] = True
    elif provider == mii.constants.ModelProvider.DIFFUSERS:
        from mii.models.providers.diffusers import diffusers_provider
        inference_pipeline = diffusers_provider(model_path,
                                                model_name,
                                                task_name,
                                                mii_config)
        inf_config["replace_with_kernel_inject"] = False  #not supported yet
        inf_config["enable_cuda_graph"] = True
    else:
        raise ValueError(f"Unknown model provider {provider}")

    print(
        f"> --------- MII Settings: ds_optimize={ds_optimize}, replace_with_kernel_inject={mii_config.replace_with_kernel_inject}, enable_cuda_graph={mii_config.enable_cuda_graph} "
    )
    if ds_optimize:
        engine = deepspeed.init_inference(getattr(inference_pipeline,
                                                  "model",
                                                  inference_pipeline),
                                          config=inf_config)
        if mii_config.profile_model_time:
            engine.profile_model_time()
        if hasattr(inference_pipeline, "model"):
            inference_pipeline.model = engine

    elif ds_zero:
        ds_config = DeepSpeedConfig(ds_config_path)
        #TODO: don't read ds-config from disk, we should pass this around as a dict instead
        ds_config_dict = json.load(open(ds_config_path, 'r'))
        assert ds_config.zero_optimization_stage == ZeroStageEnum.weights, "DeepSpeed ZeRO inference is only supported for ZeRO-3"

        # initialise Deepspeed ZeRO and store only the engine object
        ds_engine = deepspeed.initialize(model=inference_pipeline.model,
                                         config_params=ds_config_dict)[0]
        ds_engine.module.eval()  # inference
        inference_pipeline.model = ds_engine.module

    if mii_config.load_with_sys_mem:
        inference_pipeline.device = torch.device(f"cuda:{local_rank}")

    return inference_pipeline
