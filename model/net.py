"""Signal network. Training runs it on CUDA. Live scoring stays on CPU."""

from __future__ import annotations

import torch
from torch import nn

from model.features import N_FEAT, N_SEQ, SEQ_LEN


class SignalNet(nn.Module):
    def __init__(self, n_feat: int = N_FEAT, n_seq: int = N_SEQ, seq_len: int = SEQ_LEN, n_out: int = 5) -> None:
        super().__init__()
        self.n_feat = n_feat
        self.n_seq = n_seq
        self.seq_len = seq_len
        self.n_out = n_out
        width = 128
        self.conv = nn.Sequential(
            nn.Conv1d(n_seq, width, kernel_size=5, padding=2),
            nn.GELU(),
            nn.Conv1d(width, width, kernel_size=5, padding=2),
            nn.GELU(),
            nn.Conv1d(width, width, kernel_size=3, padding=1),
            nn.GELU(),
        )
        self.mlp = nn.Sequential(
            nn.Linear(n_feat + width * 2, 512),
            nn.GELU(),
            nn.Dropout(0.15),
            nn.Linear(512, 256),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(256, n_out),
        )

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        batch = features.shape[0]
        raw = features[:, : self.n_feat]
        seq = features[:, self.n_feat :].reshape(batch, self.n_seq, self.seq_len)
        hidden = self.conv(seq)
        pooled = torch.cat([hidden.mean(dim=-1), hidden[:, :, -1]], dim=-1)
        return self.mlp(torch.cat([raw, pooled], dim=-1))
