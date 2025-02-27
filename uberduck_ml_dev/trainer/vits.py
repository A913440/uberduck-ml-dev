# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/trainer.vits.ipynb (unless otherwise specified).

__all__ = ['feature_loss', 'discriminator_loss', 'generator_loss', 'kl_loss', 'VITSTrainer']

# Cell
import json
import os
from pathlib import Path
from pprint import pprint

import torch
from torch.cuda.amp import autocast, GradScaler
import torch.distributed as dist
from torch.nn import functional as F
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.optim.lr_scheduler import ExponentialLR
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import time

from ..models.common import MelSTFT
from ..utils.plot import (
    plot_attention,
    plot_gate_outputs,
    plot_spectrogram,
)
from ..text.util import text_to_sequence, random_utterance
from ..text.symbols import symbols_with_ipa
from .base import TTSTrainer

from ..models.vits import (
    DEFAULTS,
    MultiPeriodDiscriminator,
    SynthesizerTrn,
)
from ..data_loader import (
    TextAudioSpeakerLoader,
    TextAudioSpeakerCollate,
    DistributedBucketSampler,
)
from ..vendor.tfcompat.hparam import HParams
from ..utils.plot import save_figure_to_numpy, plot_spectrogram
from ..utils.utils import slice_segments, clip_grad_value_

# Cell


def feature_loss(fmap_r, fmap_g):
    loss = 0
    for dr, dg in zip(fmap_r, fmap_g):
        for rl, gl in zip(dr, dg):
            rl = rl.float().detach()
            gl = gl.float()
            loss += torch.mean(torch.abs(rl - gl))

    return loss * 2


def discriminator_loss(disc_real_outputs, disc_generated_outputs):
    loss = 0
    r_losses = []
    g_losses = []
    for dr, dg in zip(disc_real_outputs, disc_generated_outputs):
        dr = dr.float()
        dg = dg.float()
        r_loss = torch.mean((1 - dr) ** 2)
        g_loss = torch.mean(dg ** 2)
        loss += r_loss + g_loss
        r_losses.append(r_loss.item())
        g_losses.append(g_loss.item())

    return loss, r_losses, g_losses


def generator_loss(disc_outputs):
    loss = 0
    gen_losses = []
    for dg in disc_outputs:
        dg = dg.float()
        l = torch.mean((1 - dg) ** 2)
        gen_losses.append(l)
        loss += l

    return loss, gen_losses


def kl_loss(z_p, logs_q, m_p, logs_p, z_mask):
    """
    z_p, logs_q: [b, h, t_t]
    m_p, logs_p: [b, h, t_t]
    """
    z_p = z_p.float()
    logs_q = logs_q.float()
    m_p = m_p.float()
    logs_p = logs_p.float()
    z_mask = z_mask.float()

    kl = logs_p - logs_q - 0.5
    kl += 0.5 * ((z_p - m_p) ** 2) * torch.exp(-2.0 * logs_p)
    kl = torch.sum(kl * z_mask)
    l = kl / torch.sum(z_mask)
    return l

# Cell


class VITSTrainer(TTSTrainer):
    REQUIRED_HPARAMS = [
        "betas",
        "c_kl",
        "c_mel",
        "eps",
        "lr_decay",
        "segment_size",
        "training_audiopaths_and_text",
        "val_audiopaths_and_text",
        "warm_start_name_g",
        "warm_start_name_d",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_interval = 10
        for param in self.REQUIRED_HPARAMS:
            if not hasattr(self, param):
                raise Exception(f"VITSTrainer missing a required param: {param}")
        self.mel_stft = MelSTFT(
            device=self.device,
            rank=self.rank,
            padding=(self.filter_length - self.hop_length) // 2,
        )

    def init_distributed(self):
        if not self.distributed_run:
            return
        if self.rank is None or self.world_size is None:
            raise Exception(
                "Rank and wrld size must be provided when distributed training"
            )
        dist.init_process_group(
            "nccl",
            init_method="tcp://localhost:54321",
            rank=self.rank,
            world_size=self.world_size,
        )
        torch.cuda.set_device(self.rank)

    def _log_training(self, scalars, images):
        print("log training placeholder...")
        if self.rank != 0 or self.global_step % self.log_interval != 0:
            return
        for k, v in scalars.items():
            pieces = k.split("_")
            key = "/".join(pieces)
            self.log(key, self.global_step, scalar=v)
        for k, v in images.items():
            pieces = k.split("_")
            key = "/".join(pieces)
            self.log(key, self.global_step, image=v)

    def _log_validation(self):
        print("log validation...")
        pass

    def save_checkpoint(self, checkpoint_name, model, optimizer, learning_rate, epoch):
        if self.rank != 0:
            return
        if hasattr(model, "module"):
            state_dict = model.module.state_dict()
        else:
            state_dict = model.state_dict()
        os.makedirs(self.checkpoint_path, exist_ok=True)
        torch.save(
            {
                "model": model,
                "global_step": self.global_step,
                "optimizer": optimizer.state_dict(),
                "learning_rate": learning_rate,
                "epoch": epoch,
            },
            os.path.join(self.checkpoint_path, f"{checkpoint_name}.pt"),
        )

    def warm_start(self, net_g, net_d, optim_g, optim_d):
        if not (self.warm_start_name_g and self.warm_start_name_d):
            return net_g, net_d, optim_g, optim_d, 0
        if self.warm_start_name_g:
            checkpoint = torch.load(self.warm_start_name_g)
            net_g.load_state_dict(checkpoint["model"])
            optim_g.load_state_dict(checkpoint["optimizer"])
        if self.warm_start_name_d:
            checkpoint = torch.load(self.warm_start_name_d)
            net_d.load_state_dict(checkpoint["model"])
            optim_d.load_state_dict(checkpoint["optimizer"])
        self.global_step = checkpoint["global_step"]
        self.learning_rate = checkpoint["learning_rate"]
        start_epoch = checkpoint["epoch"]
        return net_g, net_d, optim_g, optim_d, start_epoch

    def _batch_to_device(self, *args):
        ret = []
        if self.device == "cuda":
            for arg in args:
                arg = arg.cuda(self.rank, non_blocking=True)
                ret.append(arg)
            return ret
        else:
            return args

    def _evaluate(self, generator, val_loader):
        print("Validation ...")
        generator.eval()
        with torch.no_grad():
            for batch_idx, batch in enumerate(val_loader):
                (
                    x,
                    x_lengths,
                    spec,
                    spec_lengths,
                    y,
                    y_lengths,
                    speakers,
                ) = self._batch_to_device(*batch)
                x = x[:1]
                x_lengths = x_lengths[:1]
                spec = spec[:1]
                spec_lengths[:1]
                y = y[:1]
                y_lengths = y_lengths[:1]
                speakers = speakers[:1]
                break
            if self.distributed_run:
                y_hat, attn, mask, *_ = generator.module.infer(
                    x, x_lengths, speakers, max_len=1000
                )
            else:
                y_hat, attn, mask, *_ = generator.infer(
                    x, x_lengths, speakers, max_len=1000
                )
            y_hat_lengths = mask.sum([1, 2]).long() * self.hparams.hop_length
            mel = self.mel_stft.spec_to_mel(spec)
            y_hat_mel = self.mel_stft.mel_spectrogram(y_hat.squeeze(1).float())
        self.log(
            "Val/mel_gen",
            self.global_step,
            image=save_figure_to_numpy(plot_spectrogram(y_hat_mel[0].data.cpu())),
        )
        self.log(
            "Val/mel_gt",
            self.global_step,
            image=save_figure_to_numpy(plot_spectrogram(mel[0].data.cpu())),
        )
        self.log(
            "Val/audio_gen", self.global_step, audio=y_hat[0, :, : y_hat_lengths[0]]
        )
        self.log("Val/audio_gt", self.global_step, audio=y[0, :, : y_lengths[0]])
        generator.train()

    def _train_and_evaluate(
        self, epoch, nets, optims, schedulers, scaler: GradScaler, loaders
    ):
        net_g, net_d = nets
        optim_g, optim_d = optims
        scheduler_g, scheduler_d = schedulers
        train_loader, val_loader = loaders
        train_loader.batch_sampler.set_epoch(epoch)
        net_g.train()
        net_d.train()
        # TODO (zach): remove when you want to.
        # self._evaluate(net_g, val_loader)
        for batch_idx, batch in enumerate(train_loader):
            print(f"global step: {self.global_step}")
            print(f"batch idx: {batch_idx}")
            (
                x,
                x_lengths,
                spec,
                spec_lengths,
                y,
                y_lengths,
                speakers,
            ) = self._batch_to_device(*batch)

            with autocast(enabled=self.fp16_run):
                (
                    y_hat,
                    l_length,
                    attn,
                    ids_slice,
                    x_mask,
                    z_mask,
                    (z, z_p, m_p, logs_p, m_q, logs_q),
                ) = net_g(x, x_lengths, spec, spec_lengths, speakers)
                mel = self.mel_stft.spec_to_mel(spec)
                # NOTE(zach): slight difference from the original VITS
                # implementation due to padding differences in the spectrograms
                y_mel = slice_segments(
                    mel, ids_slice, self.segment_size // self.hop_length
                )
                y_hat_mel = self.mel_stft.mel_spectrogram(y_hat.squeeze(1))
                y = slice_segments(y, ids_slice * self.hop_length, self.segment_size)

                # Discriminator
                y_d_hat_r, y_d_hat_g, _, _ = net_d(y, y_hat.detach())
                with autocast(enabled=False):
                    loss_disc, losses_disc_r, losses_disc_g = discriminator_loss(
                        y_d_hat_r, y_d_hat_g
                    )
                    loss_disc_all = loss_disc
            optim_d.zero_grad()
            scaler.scale(loss_disc_all).backward()
            scaler.unscale_(optim_d)
            scaler.step(optim_d)

            with autocast(enabled=self.fp16_run):
                # Generator
                y_d_hat_r, y_d_hat_g, fmap_r, fmap_g = net_d(y, y_hat)
                with autocast(enabled=False):
                    loss_dur = torch.sum(l_length.float())
                    loss_mel = F.l1_loss(y_mel, y_hat_mel) * self.c_mel
                    loss_kl = kl_loss(z_p, logs_q, m_p, logs_p, z_mask) * self.c_kl

                    loss_fm = feature_loss(fmap_r, fmap_g)
                    loss_gen, losses_gen = generator_loss(y_d_hat_g)
                    loss_gen_all = loss_gen + loss_fm + loss_mel + loss_dur + loss_kl
            optim_g.zero_grad()
            scaler.scale(loss_gen_all).backward()
            scaler.unscale_(optim_g)
            scaler.step(optim_g)
            scaler.update()

            if self.rank == 0 and self.global_step % self.log_interval == 0:
                grad_norm_g = clip_grad_value_(net_g.parameters(), None)
                grad_norm_d = clip_grad_value_(net_d.parameters(), None)
                self._log_training(
                    scalars=dict(
                        loss_g_total=loss_gen_all,
                        loss_d_total=loss_disc_all,
                        gradnorm_d=grad_norm_d,
                        gradnorm_g=grad_norm_g,
                        loss_g_fm=loss_fm,
                        loss_g_dur=loss_dur,
                        loss_g_mel=loss_mel,
                        loss_g_kl=loss_kl,
                    ),
                    images=dict(
                        slice_mel_org=save_figure_to_numpy(
                            plot_spectrogram(y_mel[0].data.cpu())
                        ),
                        slice_mel_gen=save_figure_to_numpy(
                            plot_spectrogram(y_hat_mel[0].data.cpu())
                        ),
                        all_mel=save_figure_to_numpy(
                            plot_spectrogram(mel[0].data.cpu())
                        ),
                        all_attn=save_figure_to_numpy(
                            plot_attention(attn[0, 0].data.cpu())
                        ),
                    ),
                )
            self.global_step += 1
        if self.rank == 0:
            self._evaluate(net_g, val_loader)

    def train(self):
        if self.distributed_run:
            self.init_distributed()
        train_dataset = TextAudioSpeakerLoader(
            self.training_audiopaths_and_text,
            self.hparams,
            debug=self.debug,
            debug_dataset_size=self.debug_dataset_size,
        )
        train_sampler = DistributedBucketSampler(
            train_dataset,
            self.batch_size,
            [32, 300, 400, 500, 600, 700, 800, 900, 1000],
            num_replicas=self.world_size,
            rank=self.rank,
            shuffle=True,
        )
        collate_fn = TextAudioSpeakerCollate()
        train_loader = DataLoader(
            train_dataset,
            num_workers=0,
            shuffle=False,
            pin_memory=True,
            collate_fn=collate_fn,
            batch_sampler=train_sampler,
        )
        val_dataset, val_loader = None, None
        if self.rank == 0:
            val_dataset = TextAudioSpeakerLoader(
                self.val_audiopaths_and_text,
                self.hparams,
                debug=self.debug,
                debug_dataset_size=self.debug_dataset_size,
            )
            val_loader = DataLoader(
                val_dataset,
                num_workers=0,
                shuffle=False,
                batch_size=self.batch_size,
                pin_memory=True,
                drop_last=False,
                collate_fn=collate_fn,
            )

        model_kwargs = {k: v for k, v in DEFAULTS.values().items() if hasattr(self, k)}
        net_g = SynthesizerTrn(
            len(symbols_with_ipa),
            self.filter_length // 2 + 1,
            self.segment_size // self.hop_length,
            n_speakers=self.n_speakers,
            **model_kwargs,
        )
        net_d = MultiPeriodDiscriminator(self.use_spectral_norm)
        if self.device == "cuda":
            net_g = net_g.cuda(self.rank)
            net_d = net_d.cuda(self.rank)

        optim_g = torch.optim.AdamW(
            net_g.parameters(),
            self.learning_rate,
            betas=self.betas,
            eps=self.eps,
        )
        optim_d = torch.optim.AdamW(
            net_d.parameters(), self.learning_rate, betas=self.betas, eps=self.eps
        )
        if self.distributed_run:
            net_g = DDP(net_g, device_ids=[self.rank])
            net_d = DDP(net_d, device_ids=[self.rank])

        start_epoch = 0
        net_g, net_d, optim_g, optim_d, start_epoch = self.warm_start(
            net_g,
            net_d,
            optim_g,
            optim_d,
        )

        scheduler_g = ExponentialLR(
            optim_g, gamma=self.lr_decay, last_epoch=start_epoch - 1
        )
        scheduler_d = ExponentialLR(
            optim_d, gamma=self.lr_decay, last_epoch=start_epoch - 1
        )
        scaler = GradScaler(enabled=self.fp16_run)

        for epoch in range(start_epoch, self.epochs):
            self._train_and_evaluate(
                epoch,
                [net_g, net_d],
                [optim_g, optim_d],
                [scheduler_g, scheduler_d],
                scaler,
                [train_loader, val_loader],
            )
            if epoch % self.epochs_per_checkpoint == 0:
                self.save_checkpoint(
                    f"vits_G_{self.global_step}",
                    net_g,
                    optim_g,
                    self.learning_rate,
                    epoch,
                )
                self.save_checkpoint(
                    f"vits_D_{self.global_step}",
                    net_d,
                    optim_d,
                    self.learning_rate,
                    epoch,
                )