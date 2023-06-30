import os

import pytest
import torch

import colossalai
from colossalai.logging import disable_existing_loggers
from colossalai.testing import assert_hf_output_close, clear_cache_before_run, rerun_if_address_is_in_use, spawn
from tests.kit.model_zoo import model_zoo
from tests.test_shardformer.test_model._utils import build_model, run_forward

os.environ['TRANSFORMERS_NO_ADVISORY_WARNINGS'] = 'true'


def check_forward_backward(org_model, sharded_model, data_gen_fn, output_transform_fn, loss_fn):
    org_output, org_loss, shard_output, shard_loss = run_forward(org_model, sharded_model, data_gen_fn,
                                                                 output_transform_fn, loss_fn)

    # forward check
    assert_hf_output_close(org_output, shard_output, ignore_keys=['past_key_values'], rtol=1e-4)

    # run backward
    org_loss.backward()
    shard_loss.backward()

    # check grad
    if hasattr(org_model, 'model'):
        llama_model = org_model.model
        shard_llama_model = sharded_model.model
    else:
        llama_model = org_model
        shard_llama_model = sharded_model

    org_grad = llama_model.layers[0].self_attn.q_proj.weight.grad
    shard_grad = shard_llama_model.layers[0].self_attn.q_proj.weight.grad
    shard_grad_list = [torch.zeros([*shard_grad.shape]).to('cuda') for _ in range(4)]
    shard_grad = torch.distributed.all_gather(shard_grad_list, shard_grad)
    all_shard_grad = torch.cat(shard_grad_list, dim=0)

    assert torch.allclose(org_loss, shard_loss,
                          atol=1e-5), f"shard model loss is not equal to orgin model loss\n{org_loss}\n{shard_loss}"
    assert torch.allclose(org_grad, all_shard_grad,
                          atol=1e-5), f"shard model grad is not equal to orgin model grad\n{org_grad}\n{shard_grad}"


def check_llama(rank, world_size, port):
    disable_existing_loggers()
    colossalai.launch(config={}, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')

    sub_model_zoo = model_zoo.get_sub_registry('transformers_llama')

    for name, (model_fn, data_gen_fn, output_transform_fn, loss_fn, _) in sub_model_zoo.items():
        org_model, sharded_model = build_model(model_fn)
        check_forward_backward(org_model, sharded_model, data_gen_fn, output_transform_fn, loss_fn)

    torch.cuda.empty_cache()


@pytest.mark.dist
@rerun_if_address_is_in_use()
@clear_cache_before_run()
def test_llama():
    spawn(check_llama, 4)


if __name__ == "__main__":
    test_llama()