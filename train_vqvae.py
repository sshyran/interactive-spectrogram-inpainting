from datetime import datetime
import uuid
import argparse
import pathlib
import pickle
import json
from typing import Optional
from tqdm import tqdm
import numpy as np
import os


import torch
from torch import nn, optim
from torch.utils.data import DataLoader
from torchvision import datasets, transforms, utils
from torch.utils.tensorboard.writer import SummaryWriter

from vqvae import VQVAE, InferenceVQVAE
from scheduler import CycleScheduler

from nsynth_dataset import NSynthH5Dataset
from GANsynth_pytorch.pytorch_nsynth_lib.nsynth import (
    NSynth, WavToSpectrogramDataLoader)
import GANsynth_pytorch.utils.plots as gansynthplots

import matplotlib as mpl
# use matplotlib without an X server
# on desktop, this avoids matplotlib windows from popping around
mpl.use('Agg')

DIRPATH = os.path.dirname(os.path.abspath(__file__))


HOP_LENGTH = 512
N_FFT = 2048
FS_HZ = 16000


def train(epoch: int, loader: DataLoader, model: nn.Module,
          optimizer: optim.Optimizer,
          scheduler: optim.lr_scheduler._LRScheduler,
          device: str,
          inference_vqvae: InferenceVQVAE,
          run_id: str,
          enable_image_dumps: bool = False,
          tensorboard_writer: Optional[SummaryWriter] = None,
          tensorboard_scalar_interval_epochs: int = 1,
          tensorboard_audio_interval_epochs: int = 5,
          tensorboard_num_audio_samples: int = 10,
          dry_run: bool = False
          ) -> None:
    loader = tqdm(loader, position=1)
    status_bar = tqdm(total=0, position=0, bar_format='{desc}')

    criterion = nn.MSELoss()

    latent_loss_weight = 0.25
    image_dump_sample_size = 25

    mse_sum = 0
    mse_n = 0
    num_samples_seen = 0

    model.train()
    for i, (img, _) in enumerate(loader):
        model.zero_grad()

        img = img.to(device)

        out, latent_loss, perplexity_t, perplexity_b, *_ = model(img)
        recon_loss = criterion(out, img)
        latent_loss = latent_loss.mean()
        loss = recon_loss + latent_loss_weight * latent_loss
        loss.backward()

        if scheduler is not None:
            scheduler.step()
        optimizer.step()

        batch_size = img.shape[0]  # could vary if e.g. drop-last=False in loader
        mse_batch = recon_loss.item()
        mse_sum += mse_batch * batch_size
        num_samples_seen += batch_size

        lr = optimizer.param_groups[0]['lr']

        batch_reconstruction_mse = recon_loss.item()
        batch_latent_loss = latent_loss.item()
        status_bar.set_description_str(
            (
                f'epoch: {epoch + 1}; '
                f'avg mse: {mse_sum / num_samples_seen:.4f}; mse: {mse_batch:.4f}; latent: {batch_latent_loss:.4f}; '
                f'perpl_bottom: {perplexity_b.mean():.4f}; perpl_top: {perplexity_t.mean():.4f}'
            )
        )
        if tensorboard_writer is not None:
            # add scalar summaries
            num_batches_seen = epoch*len(loader) + i
            num_samples_seen = num_batches_seen * img.shape[0]

            tensorboard_writer.add_scalar('training/reconstruction_mse',
                                          batch_reconstruction_mse,
                                          num_samples_seen)
            tensorboard_writer.add_scalar('training/latent_loss',
                                          batch_latent_loss,
                                          num_samples_seen)
            tensorboard_writer.add_scalar('training/perplexity_top',
                                          perplexity_t.mean(),
                                          num_samples_seen)
            tensorboard_writer.add_scalar('training/perplexity_bottom',
                                          perplexity_b.mean(),
                                          num_samples_seen)

        if enable_image_dumps and i % 100 == 0:
            model.eval()

            sample = img[:image_dump_sample_size]
            sample_out = out[:image_dump_sample_size]

            channel_dim = 1
            for channel_index, channel_name in enumerate(
                    ['spectrogram', 'instantaneous_frequency']):
                sample_channel = sample.select(channel_dim, channel_index
                                               ).unsqueeze(channel_dim)
                out_channel = sample_out.select(channel_dim, channel_index
                                                ).unsqueeze(channel_dim)
                utils.save_image(
                    torch.cat([sample_channel, out_channel,
                               (sample_channel-out_channel).abs()], 0),
                    os.path.join(DIRPATH, f'samples/{run_ID}/',
                                 f'{str(epoch + 1).zfill(5)}_{str(i).zfill(5)}_{channel_name}.png'),
                    nrow=image_dump_sample_size,
                    # normalize=True,
                    # range=(-1, 1),
                    # scale_each=True,
                )

            model.train()
        if dry_run:
            break

    if tensorboard_writer is not None:
        tensorboard_writer.flush()


def evaluate(loader: DataLoader, model: nn.Module, device: str,
             dry_run: bool = False):
    with torch.no_grad():
        loader = tqdm(loader, desc='validation')

        criterion = nn.MSELoss()

        latent_loss_weight = 0.25

        mse_sum = 0
        perplexity_t_sum = 0
        perplexity_b_sum = 0
        mse_n = 0
        latent_loss_total = 0

        model.eval()
        for i, (img, _) in enumerate(loader):
            img = img.to(device)

            out, latent_loss, perplexity_t, perplexity_b, *_ = model(img)
            recon_loss = criterion(out, img)
            latent_loss = latent_loss.mean()
            loss = recon_loss + latent_loss_weight * latent_loss

            mse_sum += recon_loss * img.shape[0]
            perplexity_t_sum += perplexity_t.mean()
            perplexity_b_sum += perplexity_b.mean()
            mse_n += img.shape[0]
            latent_loss_total += latent_loss
            if args.dry_run:
                break

        mse_average = mse_sum.item() / mse_n
        latent_loss_average = latent_loss_total.item() / len(loader)
        perplexity_t_average = perplexity_t_sum.item() / len(loader)
        perplexity_b_average = perplexity_b_sum.item() / len(loader)

        return (mse_average, latent_loss_average,
                perplexity_t_average, perplexity_b_average)


if __name__ == '__main__':
    class StoreDictKeyPair(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            my_dict = {}
            for kv in values.split(","):
                k, v = kv.split("=")
                my_dict[str(k)] = int(v)
            setattr(namespace, self.dest, my_dict)

    parser = argparse.ArgumentParser()
    parser.add_argument('--size', type=int, default=256)
    # parser.add_argument('--strides', nargs='+', type=int, default=[2, 4],
    #                     choices=[2, 4, 8, 16])
    parser.add_argument('--resolution_factors', action=StoreDictKeyPair,
                        default={'top': 2, 'bottom': 2})
    parser.add_argument('--num_embeddings', type=int, default=512)
    parser.add_argument('--num_hidden_channels', type=int, default=128)
    parser.add_argument('--num_residual_channels', type=int, default=32)
    parser.add_argument('--epoch', type=int, default=560)
    parser.add_argument('--lr', type=float, default=3e-4)
    parser.add_argument('--dataset', type=str, choices=['nsynth', 'imagenet'])
    parser.add_argument('--dataset_type', choices=['hdf5', 'wav'],
                        default='wav')
    parser.add_argument('--normalize_input_images', action='store_true')
    parser.add_argument('--groups', type=int, default=1)
    parser.add_argument('--sched', type=str)
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--num_workers', type=int, default=4,
                        help='Number of workers for the Dataloaders')
    parser.add_argument('--train_dataset_path', type=str)
    parser.add_argument('--validation_dataset_path', type=str)
    parser.add_argument('--enable_image_dumps', action='store_true',
                        help=('Dump png pictures of the spectrograms during training.'
                              'WARNING: Takes up a lot of space!'))
    parser.add_argument('--disable_writes_to_disk', action='store_true')
    parser.add_argument('--disable_tensorboard', action='store_true')
    parser.add_argument('--dry_run', action='store_true',
                        help=('Test run performing only one step of training'
                              'and evaluation'))
    parser.add_argument('--input_normalization', action='store_true')
    parser.add_argument('--precomputed_normalization_statistics', type=str,
                        default=None,
                        help=('Path to a pickle file containing the values'
                              'for the GANSynth_pytorch.DataNormalizer object')
                        )
    parser.add_argument('--corrupt_codes', choices=['bottom', 'top', 'both'],
                        type=str,
                        help='Whether to corrupt codes using random +/- 1 noise')
    parser.add_argument('--embeddings_initial_variance', type=float, default=1)
    parser.add_argument('--resume_training_from', type=str,
                        help='Path to a checkpoint to resume training from')

    args = parser.parse_args()

    perform_input_normalization = (args.input_normalization
                                   or args.precomputed_normalization_statistics
                                   )

    run_ID = datetime.now().strftime('%Y%m%d-%H%M%S-') + str(uuid.uuid4())[:6]

    print(args)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    train_dataset_path = pathlib.Path(args.train_dataset_path)
    validation_dataset_path = pathlib.Path(args.validation_dataset_path)
    dataset_name = args.dataset
    print("Loading dataset: ", dataset_name)
    vqvae_decoder_activation = None
    if dataset_name == 'imagenet':
        def make_resize_transform(target_size: Union[int, Sequence[int]],
                                  normalize: bool):
            transformations = [
                transforms.Resize(target_size),
                transforms.CenterCrop(target_size),
                transforms.ToTensor()
            ]
            if normalize:
                transformations.append(transforms.Normalize([0.5, 0.5, 0.5],
                                                            [0.5, 0.5, 0.5]))
            return transforms.Compose(transformations)

        transform = make_resize_transform(args.size, args.normalize_images)

        train_dataset = datasets.ImageFolder(train_dataset_path,
                                             transform=transform)
        validation_dataset = datasets.ImageFolder(validation_dataset_path,
                                                  transform=transform)
        loader = DataLoader(train_dataset, batch_size=args.batch_size,
                            shuffle=True, num_workers=args.num_workers)
        validation_loader = DataLoader(validation_dataset,
                                       batch_size=args.batch_size,
                                       shuffle=True,
                                       num_workers=args.num_workers)
        dataloader_for_gansynth_normalization = None
        normalizer_statistics = None
        in_channel = 3

    elif dataset_name == 'nsynth':
        # class to use for building the dataloaders
        dataloader_class = DataLoader
        if args.dataset_type == 'wav':
            valid_pitch_range = [24, 84]
            # converts wavforms to spectrograms on-the-fly on GPU
            from functools import partial
            dataloader_class = partial(WavToSpectrogramDataLoader,
                                       device=device,
                                       n_fft=N_FFT, hop_length=HOP_LENGTH)

            nsynth_dataset = NSynth(
                root=str(train_dataset_path),
                valid_pitch_range=valid_pitch_range,
                categorical_field_list=[],
                squeeze_mono_channel=True)

            if args.validation_dataset_path:
                nsynth_validation_dataset = NSynth(
                    root=str(validation_dataset_path),
                    valid_pitch_range=valid_pitch_range,
                    categorical_field_list=[],
                    squeeze_mono_channel=True)

        elif args.dataset_type == 'hdf5':
            nsynth_dataset = NSynthH5Dataset(
                root_path=train_dataset_path,
                use_mel_frequency_scale=True)
            if args.validation_dataset_path:
                nsynth_validation_dataset = NSynthH5Dataset(
                    root_path=validation_dataset_path,
                    use_mel_frequency_scale=True)
        else:
            assert False

        loader = dataloader_class(
            nsynth_dataset, batch_size=args.batch_size,
            num_workers=args.num_workers, shuffle=True,
            pin_memory=True)

        validation_loader = dataloader_class(nsynth_validation_dataset,
                                             batch_size=args.batch_size,
                                             num_workers=args.num_workers,
                                             shuffle=True, pin_memory=True
                                             )

        in_channel = 2

        dataloader_for_gansynth_normalization = None
        normalizer_statistics = None
        if args.precomputed_normalization_statistics is not None:
            with open(args.precomputed_normalization_statistics, 'rb') as f:
                normalizer_statistics = pickle.load(f)
        elif args.input_normalization:
            dataloader_for_gansynth_normalization = loader
    else:
        raise ValueError("Unrecognized dataset name: ",
                         dataset_name)

    print("Initializing model")

    corruption_weights_base = [0.1, 0.8, 0.1]

    corruption_weights = {
        'top': None,
        'bottom': None
    }
    if args.corrupt_codes is not None:
        if args.corrupt_codes == 'both':
            corruption_weights = {
                'bottom': corruption_weights_base,
                'top': corruption_weights_base
            }
        elif args.corrupt_codes == 'top' or args.corrupt_codes == 'bottom':
            corruption_weights[args.corrupt_codes] = corruption_weights_base
        else:
            assert False, "Not permitted by argparse parameters"

    vqvae_parameters = {'in_channel': in_channel,
                        'groups': args.groups,
                        'num_embeddings': args.num_embeddings,
                        'num_hidden_channels': args.num_hidden_channels,
                        'num_residual_channels': args.num_residual_channels,
                        'corruption_weights': corruption_weights,
                        'embeddings_initial_variance':
                            args.embeddings_initial_variance,
                        # 'resume_training_from': args.resume_training_from
                        'resolution_factors': args.resolution_factors
                        }

    def print_resolution_summary(loader, resolution_factors):
        sample = next(iter(loader))[0][0]
        print(f"Input images shape: {sample.shape}")

        num_channels, height, width = sample.shape
        total_resolution_factor = 1
        for layer_name, resolution_factor in resolution_factors.values():
            total_resolution_factor *= resolution_factor
            print(f"Layer {layer_name}: ")
            print(f"\nAdditional downsampling factor {resolution_factor}")
            C, H, W = sample.shape
            layer_height = height // total_resolution_factor
            layer_width = width // total_resolution_factor
            print(f"\nResolution H={layer_height}, W={layer_width}")

    print_resolution_summary(loader, args.resolution_factors)

    vqvae = VQVAE(dataloader_for_gansynth_normalization=dataloader_for_gansynth_normalization,
                  normalizer_statistics=normalizer_statistics,
                  **vqvae_parameters
                  )
    if dataloader_for_gansynth_normalization is not None:
        # store normalization parameters
        data_normalizer = vqvae.data_normalizer
        normalization_statistics_path = path / '../normalization_statistics.pkl'
        data_normalizer.dump_statistics(normalization_statistics_path)

    model = nn.DataParallel(vqvae).to(device)

    start_epoch = 0
    if args.resume_training_from is not None:
        import re
        checkpoint_path = pathlib.Path(args.resume_training_from)
        epoch_find_regex = '\d+\.pt'
        start_epoch = int(re.search(epoch_find_regex, checkpoint_path.name
                                    )[0][:3])
        model.module.load_state_dict(torch.load(checkpoint_path,
                                                map_location=device)
                                     )

    inference_vqvae = InferenceVQVAE(model, device,
                                     hop_length=HOP_LENGTH, n_fft=N_FFT)

    optimizer = optim.Adam(model.parameters(), lr=args.lr)
    scheduler = None
    if args.sched == 'cycle':
        scheduler = CycleScheduler(
            optimizer, args.lr, n_iter=len(loader) * args.epoch, momentum=None
        )

    MAIN_DIR = pathlib.Path(DIRPATH)
    checkpoints_dir_path = MAIN_DIR / f'checkpoints/{run_ID}/'
    if not (args.dry_run or args.disable_writes_to_disk):
        os.makedirs(checkpoints_dir_path, exist_ok=True)

        vqvae.store_instantiation_parameters(
            checkpoints_dir_path / 'model_parameters.json')

        os.makedirs(MAIN_DIR / f'samples/{run_ID}/', exist_ok=True)

    tensorboard_writer = None
    if not (args.disable_tensorboard or args.disable_writes_to_disk):
        tensorboard_dir_path = MAIN_DIR / f'runs/{run_ID}/'
        os.makedirs(tensorboard_dir_path, exist_ok=True)
        tensorboard_writer = SummaryWriter(tensorboard_dir_path)

    print("Starting training")
    for epoch_index in range(start_epoch, args.epoch):
        train(epoch_index, loader, model, optimizer, scheduler, device,
              run_id=run_ID,
              enable_image_dumps=args.enable_image_dumps,
              tensorboard_writer=tensorboard_writer,
              tensorboard_audio_interval_epochs=3,
              tensorboard_num_audio_samples=5,
              inference_vqvae=inference_vqvae,
              dry_run=args.dry_run)

        if args.disable_writes_to_disk:
            pass
        else:
            checkpoint_filename = (f'vqvae_{dataset_name}_'
                                   f'{str(epoch_index + 1).zfill(3)}.pt')
            torch.save(
                    model.module.state_dict(),
                    checkpoints_dir_path / checkpoint_filename
            )

        # eval on validation set
        with torch.no_grad():
            (mse_validation, latent_loss_validation,
             perplexity_t_validation, perplexity_b_validation) = evaluate(
                 validation_loader, model, device, args.dry_run)

        if tensorboard_writer is not None and not (
                args.dry_run or args.disable_writes_to_disk):
            tensorboard_writer.add_scalar('validation/reconstruction_mse',
                                          mse_validation,
                                          global_step=epoch_index)
            tensorboard_writer.add_scalar('validation/latent_loss',
                                          latent_loss_validation,
                                          global_step=epoch_index)
            tensorboard_writer.add_scalar('validation/perplexity_top',
                                          perplexity_t_validation,
                                          global_step=epoch_index)
            tensorboard_writer.add_scalar('validation/perplexity_bottom',
                                          perplexity_b_validation,
                                          global_step=epoch_index)

            # if i+1 % tensorboard_audio_interval_epochs == 0:
            if dataset_name == 'nsynth':
            # add audio summaries

            samples, reconstructions = inference_vqvae.sample_reconstructions(
                validation_loader)
            samples = samples[:3]
            reconstructions = reconstructions[:3]
            samples_audio = inference_vqvae.mag_and_IF_to_audio(
                samples, use_mel_frequency=True)
            reconstructions_audio = inference_vqvae.mag_and_IF_to_audio(
                reconstructions, use_mel_frequency=True)
            tensorboard_writer.add_audio('Original (end of epoch, validation data)',
                                         samples_audio.flatten(),
                                         epoch_index)
            tensorboard_writer.add_audio('Reconstructions (end of epoch, validation data)',
                                         reconstructions_audio.flatten(),
                                         epoch_index)
            mel_specs_original, mel_IFs_original = (
                np.swapaxes(samples.data.cpu().numpy(), 0, 1))
            mel_specs_reconstructions, mel_IFs_reconstructions = (
                np.swapaxes(reconstructions.data.cpu().numpy(), 0, 1))
            mel_specs = np.concatenate([mel_specs_original,
                                        mel_specs_reconstructions], axis=0)
            mel_IFs = np.concatenate([mel_IFs_original,
                                      mel_IFs_reconstructions], axis=0)

            spec_figure, _ = gansynthplots.plot_mel_representations_batch(
                log_melspecs=mel_specs, mel_IFs=mel_IFs,
                hop_length=HOP_LENGTH, fs_hz=FS_HZ)
            tensorboard_writer.add_figure('Originals + Reconstructions (mel-scale, logspec/IF, validation data)',
                                          spec_figure,
                                          epoch_index)
            elif dataset_name == 'imagenet':
                if epoch_index % 5 == 0:
                    for subset_name, subset_loader in [('training', loader),
                                                       ('validation', validation_loader)]:
                        with torch.no_grad():
                            samples = subset_loader.__iter__().__next__()[0].to(device)[:16]
                            reconstructions = model(samples)[0]
                            samples_and_reconstructions = torch.cat(
                                [samples, reconstructions], dim=0)
                            utils.save_image(
                                samples_and_reconstructions,
                                os.path.join(DIRPATH, f'samples/{run_ID}/',
                                            f'{str(epoch_index + 1).zfill(5)}_{subset_name}.png')
                            )

            tensorboard_writer.flush()
