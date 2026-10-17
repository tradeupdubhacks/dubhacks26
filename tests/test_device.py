import torch

from model.net import SignalNet
from model.train import training_device


def test_fit_device_is_cuda_when_the_gpu_is_visible():
    device = training_device()
    if torch.cuda.is_available():
        assert device.type == "cuda"
    else:
        assert device.type == "cpu"
    network = SignalNet().to(device)
    output = network(torch.ones(8, 4, device=device))
    assert output.shape == (8,)
    assert output.device.type == device.type
