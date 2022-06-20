from functools import partial

import torch
from torch import nn, einsum
import torch.nn.functional as F
from einops import rearrange

# helper functions

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

# helper classes

def Upsample(dim):
    return nn.ConvTranspose3d(dim, dim, (1, 4, 4), (1, 2, 2), (0, 1, 1))

def Downsample(dim):
    return nn.Conv3d(dim, dim, (1, 4, 4), (1, 2, 2), (0, 1, 1))

# normalization

class LayerNorm(nn.Module):
    def __init__(self, dim, eps = 1e-5):
        super().__init__()
        self.eps = eps
        self.gamma = nn.Parameter(torch.ones(1, dim, 1, 1, 1))

    def forward(self, x):
        var = torch.var(x, dim = 1, unbiased = False, keepdim = True)
        mean = torch.mean(x, dim = 1, keepdim = True)
        return (x - mean) / (var + self.eps).sqrt() * self.gamma

# conv next
# https://arxiv.org/abs/2201.03545

class ConvNextBlock(nn.Module):
    def __init__(
        self,
        dim,
        dim_out,
        *,
        mult = 2,
        norm = True
    ):
        super().__init__()
        self.ds_conv = nn.Conv3d(dim, dim, (1, 7, 7), padding = (0, 3, 3), groups = dim)

        self.net = nn.Sequential(
            LayerNorm(dim) if norm else nn.Identity(),
            nn.Conv3d(dim, dim_out * mult, (1, 3, 3), padding = (0, 1, 1)),
            nn.GELU(),
            nn.Conv3d(dim_out * mult, dim_out, (1, 3, 3), padding = (0, 1, 1))
        )

        self.res_conv = nn.Conv3d(dim, dim_out, 1) if dim != dim_out else nn.Identity()

    def forward(self, x, time_emb = None):
        h = self.ds_conv(x)
        h = self.net(h)
        return h + self.res_conv(x)

# unet

class XUnet(nn.Module):
    def __init__(
        self,
        dim,
        init_dim = None,
        out_dim = None,
        dim_mults=(1, 2, 4, 8),
        channels = 3
    ):
        super().__init__()
        self.channels = channels

        init_dim = default(init_dim, dim)
        self.init_conv = nn.Conv3d(channels, init_dim, (1, 7, 7), padding = (0, 3, 3))

        dims = [init_dim, *map(lambda m: dim * m, dim_mults)]
        in_out = list(zip(dims[:-1], dims[1:]))

        self.downs = nn.ModuleList([])
        self.ups = nn.ModuleList([])

        num_resolutions = len(in_out)
        conv_next = partial(ConvNextBlock)

        # modules for all layers

        for ind, (dim_in, dim_out) in enumerate(in_out):
            is_last = ind >= (num_resolutions - 1)

            self.downs.append(nn.ModuleList([
                conv_next(dim_in, dim_out, norm = ind != 0),
                conv_next(dim_out, dim_out),
                Downsample(dim_out) if not is_last else nn.Identity()
            ]))

        mid_dim = dims[-1]
        self.mid = conv_next(mid_dim, mid_dim)

        for ind, (dim_in, dim_out) in enumerate(reversed(in_out)):
            is_last = ind >= (num_resolutions - 1)

            self.ups.append(nn.ModuleList([
                conv_next(dim_out * 2, dim_in),
                conv_next(dim_in, dim_in),
                Upsample(dim_in) if not is_last else nn.Identity()
            ]))

        out_dim = default(out_dim, channels)
        self.final_conv = nn.Sequential(
            ConvNextBlock(dim * 2, dim),
            nn.Conv3d(dim, out_dim, 3, padding = 1)
        )

    def forward(self, x):
        is_image = x.ndim == 4
        if is_image:
            x = rearrange(x, 'b c h w -> b c 1 h w')

        x = self.init_conv(x)
        r = x.clone()

        h = []

        for convnext, convnext2, downsample in self.downs:
            x = convnext(x)
            x = convnext2(x)
            h.append(x)
            x = downsample(x)

        x = self.mid(x)

        for convnext, convnext2, upsample in self.ups:
            x = torch.cat((x, h.pop()), dim=1)
            x = convnext(x)
            x = convnext2(x)
            x = upsample(x)

        x = torch.cat((x, r), dim = 1)
        out = self.final_conv(x)

        if is_image:
            x = rearrange(x, 'b c 1 h w -> b c h w')

        return x
