from typing import Optional
from datetime import datetime
import uuid
import argparse
import os
import pathlib
import json

import numpy as np
import torch
from torch import nn, optim
from torch.utils.data import DataLoader
from tqdm import tqdm
from radam import RAdam

try:
    from apex import amp

except ImportError:
    amp = None

from torch.utils.tensorboard.writer import SummaryWriter

from dataset import LMDBDataset
from pixelsnail import PixelSNAIL, LabelSmoothingLoss
from scheduler import CycleScheduler

DIRPATH = os.path.dirname(os.path.abspath(__file__))

if torch.cuda.is_available():
    torch.set_default_tensor_type(torch.cuda.FloatTensor)


def num_samples_in_loader(loader: torch.utils.data.DataLoader):
    if loader.drop_last:
        return len(loader.dataset)
    else:
        batch_size = loader.batch_size
        return len(loader) * batch_size


def run_model(args, epoch, loader, model, optimizer, scheduler, device,
              criterion: nn.Module,
              tensorboard_writer: Optional[SummaryWriter] = None,
              is_training: bool = True):
    run_type = 'training' if is_training else 'validation'
    status_bar = tqdm(total=0, position=0, bar_format='{desc}')
    tqdm_loader = tqdm(loader, position=1)

    loss_sum = 0
    total_accuracy = 0
    num_samples_seen_epoch = 0
    # number of samples seen across runs, useful for TensorBoard tracking
    num_samples_seen_total = epoch * num_samples_in_loader(loader)

    if is_training:
        model = model.train()
    else:
        model = model.eval()

    for i, (top, bottom, label) in enumerate(tqdm_loader):
        if is_training:
            model.zero_grad()

        top = top.to(device)

        if args.hier == 'top':
            target = top
            out, _ = model(top)

        elif args.hier == 'bottom':
            bottom = bottom.to(device)
            target = bottom
            out, _ = model(bottom, condition=top)

        loss = criterion(out, target)

        if is_training:
            loss.backward()

            if scheduler is not None:
                scheduler.step()
            optimizer.step()

        _, pred = out.max(1)
        correct = (pred == target).float()
        accuracy = correct.sum() / target.numel()

        lr = optimizer.param_groups[0]['lr']

        batch_size = top.shape[0]
        loss_sum += loss.item() * batch_size
        total_accuracy += accuracy * batch_size
        num_samples_seen_epoch += batch_size
        num_samples_seen_total += batch_size

        status_bar.set_description_str(
            (
                f'{run_type}, epoch: {epoch + 1}; avg loss: {loss_sum / num_samples_seen_epoch:.5f}; '
                f'acc: {accuracy:.5f}; lr: {lr:.5f}'
            )
        )

        if is_training and tensorboard_writer is not None:
            # report metrics per batch
            loss_name = str(criterion)
            tensorboard_writer.add_scalar(f'pixelsnail_{run_type}_{args.hier}/{loss_name}',
                                          loss,
                                          num_samples_seen_total)
            tensorboard_writer.add_scalar(f'pixelsnail_{run_type}_{args.hier}/accuracy',
                                          accuracy,
                                          num_samples_seen_total)

    if not is_training and tensorboard_writer is not None:
        # only report metrics over full validation/test set
        loss_name = str(criterion)
        tensorboard_writer.add_scalar(f'pixelsnail_{run_type}_{args.hier}/mean_{loss_name}',
                                      loss_sum / num_samples_seen_epoch,
                                      epoch)
        tensorboard_writer.add_scalar(f'pixelsnail_{run_type}_{args.hier}/mean_accuracy',
                                      total_accuracy / num_samples_seen_epoch,
                                      epoch)

    return loss_sum, total_accuracy, num_samples_seen_epoch


class PixelTransform:
    def __init__(self):
        pass

    def __call__(self, input):
        ar = np.array(input)

        return torch.from_numpy(ar).long()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--num_epochs', type=int, default=420)
    parser.add_argument('--batch_size', type=int, default=32)
    parser.add_argument('--hier', type=str, default='top')
    parser.add_argument('--lr', type=float, default=3e-4)
    parser.add_argument('--channel', type=int, default=256)
    parser.add_argument('--label_smoothing', default=0.0, type=float)
    parser.add_argument('--n_res_block', type=int, default=4)
    parser.add_argument('--n_res_channel', type=int, default=256)
    parser.add_argument('--n_out_res_block', type=int, default=0)
    parser.add_argument('--n_cond_res_block', type=int, default=3)
    parser.add_argument('--dropout', type=float, default=0.1)
    parser.add_argument('--predict_frequencies_first', action='store_true')
    parser.add_argument('--amp', type=str, default='O0')
    parser.add_argument('--sched', type=str)
    parser.add_argument('--pixelsnail_initial_weights_path', type=str,
                        help=("Initialize trained PixelSNAIL with the weights "
                              "contained in the provided PyTorch checkpoint"))
    parser.add_argument('--disable_writes_to_disk', action='store_true')
    parser.add_argument('--disable_tensorboard', action='store_true')
    parser.add_argument('--database_path', type=str, required=True)
    parser.add_argument('--validation_database_path', type=str, default=None)
    parser.add_argument('--vqvae_run_id', type=str, required=True)
    parser.add_argument('--num_workers', type=int, default=4,
                        help='Number of worker processes for the Dataloaders')

    args = parser.parse_args()

    print(args)

    run_ID = (f'pixelsnail-{args.hier}_layer-'
              + datetime.now().strftime('%Y%m%d-%H%M%S-')
              + str(uuid.uuid4())[:6])

    print("Run ID: ", run_ID)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    dataset = LMDBDataset(args.database_path)
    loader = DataLoader(
        dataset, batch_size=args.batch_size, shuffle=True,
        num_workers=args.num_workers, drop_last=True
    )

    validation_loader = None
    if args.validation_database_path is not None:
        validation_dataset = LMDBDataset(args.validation_database_path)
        validation_loader = DataLoader(
            validation_dataset, batch_size=args.batch_size, shuffle=False,
            num_workers=args.num_workers, drop_last=False
        )

    model_checkpoint_weights = None
    if args.pixelsnail_initial_weights_path is not None:
        model_checkpoint_weights = torch.load(
            args.pixelsnail_initial_weights_path)
        # if 'args' in model_checkpoint_weights:
        #     args = model_checkpoint_weights['args']

    shape_top, shape_bottom = (list(dataset[0][i].shape) for i in range(2))
    if args.hier == 'top':
        snail = PixelSNAIL(
            shape_top,
            512,
            args.channel,
            5,
            4,
            args.n_res_block,
            args.n_res_channel,
            dropout=args.dropout,
            n_out_res_block=args.n_out_res_block,
            predict_frequencies_first=args.predict_frequencies_first
        )
    elif args.hier == 'bottom':
        snail = PixelSNAIL(
            shape_bottom,
            512,
            args.channel,
            5,
            4,
            args.n_res_block,
            args.n_res_channel,
            attention=False,
            dropout=args.dropout,
            n_cond_res_block=args.n_cond_res_block,
            cond_res_channel=args.n_res_channel,
            predict_frequencies_first=args.predict_frequencies_first
        )

    if model_checkpoint_weights is not None:
        if 'model' in model_checkpoint_weights:
            snail.load_state_dict(model_checkpoint_weights['model'])
        else:
            snail.load_state_dict(model_checkpoint_weights)

    snail = snail.to(device)
    optimizer = RAdam(snail.parameters(), lr=args.lr)

    if amp is not None:
        snail, optimizer = amp.initialize(snail, optimizer, opt_level=args.amp)

    model = nn.DataParallel(snail).to(device)

    MAIN_DIR = pathlib.Path(DIRPATH)
    CHECKPOINTS_DIR_PATH = pathlib.Path(
        f'checkpoints/pixelsnail/vqvae-{args.vqvae_run_id}/{run_ID}/')
    if not args.disable_writes_to_disk:
        os.makedirs(CHECKPOINTS_DIR_PATH, exist_ok=True)
        with open(CHECKPOINTS_DIR_PATH / 'command_line_parameters.json', 'w') as f:
            json.dump(args.__dict__, f)
        snail.store_instantiation_parameters(
            CHECKPOINTS_DIR_PATH / 'pixelSNAIL_instantiation_parameters.json')

    tensorboard_writer = None
    if not (args.disable_writes_to_disk or args.disable_tensorboard):
        tensorboard_dir_path = MAIN_DIR / f'runs/{run_ID}/'
        os.makedirs(tensorboard_dir_path, exist_ok=True)
        tensorboard_writer = SummaryWriter(tensorboard_dir_path)

    scheduler = None
    if args.sched == 'cycle':
        scheduler = CycleScheduler(
            optimizer, args.lr, n_iter=len(loader) * args.epoch, momentum=None
        )

    num_classes = snail.n_class
    criterion = LabelSmoothingLoss(num_classes=num_classes,
                                   smoothing=args.label_smoothing,
                                   dim=1)

    checkpoint_name = f'pixelsnail-layer_{args.hier}'
    checkpoint_path = CHECKPOINTS_DIR_PATH / f'{checkpoint_name}.pt'
    best_model_checkpoint_path = (
        CHECKPOINTS_DIR_PATH
        / f'{checkpoint_name}-best_performing.pt')

    if validation_loader is not None:
        best_validation_loss = float("inf")
    for epoch in range(args.num_epochs):
        run_model(args, epoch, loader, model, optimizer, scheduler, device,
                  criterion, tensorboard_writer=tensorboard_writer,
                  is_training=True)

        checkpoint_dict = {
            'command_line_arguments': args.__dict__,
            'model': model.module.state_dict(),
            'pixelsnail_instatiation_parameters': (
                snail._instantiation_parameters),
            'epoch': epoch}
        if not args.disable_writes_to_disk:
            torch.save(checkpoint_dict, checkpoint_path)

        if validation_loader is not None:
            with torch.no_grad():
                total_validation_loss, total_accuracy, num_validation_samples = run_model(
                    args, epoch, validation_loader, model, optimizer,
                    scheduler, device, criterion,
                    tensorboard_writer=tensorboard_writer, is_training=False)
            if total_validation_loss < best_validation_loss:
                best_validation_loss = total_validation_loss

                validation_dict = {
                    'criterion': str(criterion),
                    'dataset': args.validation_database_path,
                    'loss': total_validation_loss
                }
                checkpoint_dict['validation'] = validation_dict
                if not args.disable_writes_to_disk:
                    torch.save(checkpoint_dict, best_model_checkpoint_path)
