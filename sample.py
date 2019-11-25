from typing import Union, Optional, Iterable
import argparse
import pathlib
import os
from datetime import datetime
import uuid
import soundfile
from tqdm import tqdm
import numpy as np

import torch
import torchaudio
from torch import nn
from torchvision.utils import save_image

from vqvae import VQVAE, InferenceVQVAE
from pixelsnail import PixelSNAIL
from transformer import UnconditionalTransformer, ConditionalTransformer
from GANsynth_pytorch.pytorch_nsynth_lib.nsynth import (
    wavfile_to_melspec_and_IF)

if torch.cuda.is_available():
    torch.set_default_tensor_type(torch.cuda.FloatTensor)


@torch.no_grad()
def sample_model(model: PixelSNAIL, device: Union[torch.device, str],
                 batch_size: int, codemap_size: Iterable[int],
                 temperature: float, condition: Optional[torch.Tensor] = None,
                 constraint: Optional[torch.Tensor] = None):
    """Generate a sample from the provided PixelSNAIL

    Arguments:
        model (PixelSNAIL)
        device (torch.device or str):
            The device on which to perform the sampling
        batch_size (int)
        codemap_size (Iterable[int]):
            The size of the codemap to generate
        temperature (float):
            Sampling temperature (lower means the model is more conservative)
        condition (torch.Tensor, optional, default None):
            Another codemap to use as hierarchical conditionning for sampling.
            If not provided, sampling is unconditionned.
        constraint_2D (torch.Tensor, optional, default None):
            If provided, fixes the top-left part of the generated 2D codemap
            to be the given Tensor.
            `constraint_2D.size` should be less or equal to codemap_size.
    """
    codemap = (torch.zeros(batch_size, *codemap_size, dtype=torch.int64)
               .to(device)
               )
    parallel_model = nn.DataParallel(model)

    constraint_height = -1
    constraint_width = -1
    if constraint is not None:
        if list(constraint.shape) > codemap_size:
            raise ValueError("Incorrect size of constraint, constraint "
                             "should be smaller than the target codemap size")
        else:
            _, constraint_height, constraint_width = constraint.shape

            padding_left = padding_top = 0
            padding_bottom = codemap_size[0] - constraint_height
            padding_right = codemap_size[1] - constraint_width
            padder = torch.nn.ConstantPad2d(
                (padding_left, padding_right, padding_top, padding_bottom),
                value=0).to(device)
            channel_dim = 1
            codemap = (padder(constraint.unsqueeze(channel_dim))
                       .detach()
                       .squeeze(channel_dim)
                       )

    cache = {}

    if model.predict_frequencies_first:
        for j in tqdm(range(codemap_size[1]), position=0):
            start_column = (0 if j > constraint_width
                            else constraint_height + 1)
            for i in tqdm(range(start_column, codemap_size[0]), position=1):
                out, cache = parallel_model(codemap, condition=condition,
                                            cache=cache)
                prob = torch.softmax(out[:, :, i, j] / temperature, 1)
                sample = torch.multinomial(prob, 1).squeeze(-1)
                codemap[:, i, j] = sample
    else:
        raise NotImplementedError

    return codemap


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch_size', type=int, default=8)
    parser.add_argument('--model_type_top', type=str,
                        choices=['PixelSNAIL', 'Transformer'],
                        default='PixelSNAIL')
    parser.add_argument('--model_type_bottom', type=str,
                        choices=['PixelSNAIL', 'Transformer'],
                        default='PixelSNAIL')
    parser.add_argument('--vqvae_parameters_path', type=str, required=True)
    parser.add_argument('--vqvae_weights_path', type=str, required=True)
    parser.add_argument('--prediction_top_parameters_path', type=str,
                        required=True)
    parser.add_argument('--prediction_top_weights_path', type=str,
                        required=True)
    parser.add_argument('--prediction_bottom_parameters_path', type=str,
                        required=True)
    parser.add_argument('--prediction_bottom_weights_path', type=str,
                        required=True)
    parser.add_argument('--temperature', type=float, default=1.0)
    parser.add_argument('--hop_length', type=int, default=512)
    parser.add_argument('--n_fft', type=int, default=2048)
    parser.add_argument('--use_mel_frequency', type=int, default=True)
    parser.add_argument('--sample_rate_hz', type=int, default=16000)
    parser.add_argument('--condition_top_audio_path', type=str)
    parser.add_argument('--constraint_top_audio_path', type=str)
    parser.add_argument('--constraint_top_num_timesteps', type=int)
    parser.add_argument('--output_directory', type=str, default='./')

    args = parser.parse_args()

    run_ID = (datetime.now().strftime('%Y%m%d-%H%M%S-')
              + str(uuid.uuid4())[:6])
    print("Sample ID: ", run_ID)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    if args.model_type_top == 'PixelSNAIL':
        ModelTop = PixelSNAIL
    elif args.model_type_top == 'Transformer':
        ModelTop = UnconditionalTransformer
    else:
        raise ValueError(
            f"Unexpected value {args.model_type_top} for option model_type_top")

    if args.model_type_bottom == 'PixelSNAIL':
        ModelBottom = PixelSNAIL
    elif args.model_type_bottom == 'Transformer':
        ModelBottom = ConditionalTransformer
    else:
        raise ValueError(
            f"Unexpected value {args.model_type_bottom} for option model_type_bottom")

    def absolute_path(path: str) -> str:
        return str(pathlib.Path(path).expanduser().absolute())

    model_vqvae = VQVAE.from_parameters_and_weights(
        absolute_path(args.vqvae_parameters_path),
        absolute_path(args.vqvae_weights_path),
        device=device
        ).to(device).eval()
    model_top = ModelTop.from_parameters_and_weights(
        absolute_path(args.prediction_top_parameters_path),
        absolute_path(args.prediction_top_weights_path),
        device=device
        ).to(device).eval()
    model_bottom = ModelBottom.from_parameters_and_weights(
        absolute_path(args.prediction_bottom_parameters_path),
        absolute_path(args.prediction_bottom_weights_path),
        device=device
        ).to(device).eval()

    with torch.no_grad():
        if args.condition_top_audio_path is not None:
            condition_mel_spec_and_IF = wavfile_to_melspec_and_IF(
                args.condition_top_audio_path)

            (_, _, _, condition_code_top, condition_code_bottom,
             *_) = model_vqvae.encode(condition_mel_spec_and_IF.to(device))

            # repeat condition for the whole batch
            top_code = condition_code_top.repeat(args.batch_size, 1, 1)
        elif args.constraint_top_audio_path is not None:
            constraint_mel_spec_and_IF = wavfile_to_melspec_and_IF(
                args.constraint_top_audio_path)

            (_, _, _, constraint_code_top, *_) = model_vqvae.encode(
                constraint_mel_spec_and_IF.to(device))
            constraint_code_top_restrained = (
                constraint_code_top[:, :args.constraint_top_num_timesteps-1])
            top_code_sample = sample_model(
                model_top, device, batch_size=1, codemap_size=model_top.shape,
                temperature=args.temperature,
                constraint=constraint_code_top_restrained)

            # repeat condition for the whole batch
            top_code = top_code_sample.repeat(args.batch_size, 1, 1)
        else:
            top_code_sample = sample_model(
                model_top, device, args.batch_size, model_top.shape,
                args.temperature)
            top_code = top_code_sample
        bottom_sample = sample_model(
            model_bottom, device, args.batch_size, model_bottom.shape,
            args.temperature, condition=top_code
        )

        decoded_sample = model_vqvae.decode_code(top_code, bottom_sample)

    inference_vqvae = InferenceVQVAE(model_vqvae, device,
                                     hop_length=args.hop_length,
                                     n_fft=args.n_fft)

    condition_top_audio = None
    if args.condition_top_audio_path is not None:
        import torchvision.transforms as transforms
        sample_audio, fs_hz = torchaudio.load_wav(args.condition_top_audio_path,
                                                  channels_first=True)
        toFloat = transforms.Lambda(lambda x: (x / np.iinfo(np.int16).max))
        sample_audio = toFloat(sample_audio)
        condition_top_audio = sample_audio.flatten().cpu().numpy()

    def make_audio(mag_and_IF_batch: torch.Tensor,
                   condition_audio: Optional[np.ndarray]) -> np.ndarray:
        audio_batch = inference_vqvae.mag_and_IF_to_audio(
            mag_and_IF_batch, use_mel_frequency=args.use_mel_frequency)
        normalized_audio_batch = (
            audio_batch
            / audio_batch.abs().max(dim=1, keepdim=True)[0])
        audio_mono_concatenated = normalized_audio_batch.flatten().cpu().numpy()
        if condition_audio is not None:
            audio_mono_concatenated = np.concatenate([condition_audio,
                                                     np.zeros(condition_audio.shape),
                                                     audio_mono_concatenated])
        return audio_mono_concatenated

    os.makedirs(args.output_directory, exist_ok=True)

    audio_sample_path = os.path.join(args.output_directory, f'{run_ID}.wav')
    soundfile.write(audio_sample_path,
                    make_audio(decoded_sample, condition_top_audio),
                    samplerate=args.sample_rate_hz)

    # write spectrogram and IF
    channel_dim = 1
    for channel_index, channel_name in enumerate(
            ['spectrogram', 'instantaneous_frequency']):
        channel = decoded_sample.select(channel_dim, channel_index
                                        ).unsqueeze(channel_dim)
        save_image(
            channel,
            os.path.join(args.output_directory, f'{run_ID}-{channel_name}.png'),
            nrow=args.batch_size,
            # normalize=True,
            # range=(-1, 1),
            # scale_each=True,
        )
