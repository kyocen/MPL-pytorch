import argparse
import logging
import math
import os
import random
import time

import numpy as np
import torch
from torch.cuda import amp
from torch import nn
from torch.nn import functional as F
from torch import optim
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from .cifar import DATASET_GETTERS
from .models import build_wideresnet, ModelEMA
from .utils import (AverageMeter, accuracy, create_loss_fn,
                    save_checkpoint, reduce_tensor, module_load_state_dict)

logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser()
parser.add_argument('--name', type=str, required=True, help='experiment name')
parser.add_argument('--data-path', type=str, required=True, help='data path')
parser.add_argument('--save-path', default='./checkpoint', type=str, help='save path')
parser.add_argument('--dataset', default='cifar10', type=str,
                    choices=['cifar10', 'cifar100', 'svhn'], help='dataset name')
parser.add_argument('--num-labeled', type=int, default=4000, help='number of labeled data')
parser.add_argument("--expand-labels", action="store_true", help="expand labels to fit eval steps")
parser.add_argument('--total-steps', default=300000, type=int, help='number of total steps to run')
parser.add_argument('--eval-step', default=1000, type=int, help='number of eval steps to run')
parser.add_argument('--start-step', default=0, type=int,
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--workers', default=4, type=int, help='number of workers')
parser.add_argument('--num-classes', default=10, type=int, help='number of classes')
parser.add_argument('--dense-dropout', default=0, type=float, help='dropout on last dense layer')
parser.add_argument('--resize', default=32, type=int, help='resize image')
parser.add_argument('--batch-size', default=64, type=int, help='train batch size')
parser.add_argument('--lr', default=0.01, type=float, help='learning late')
parser.add_argument('--momentum', default=0.9, type=float, help='SGD Momentum')
parser.add_argument('--nesterov', action='store_true', help='use nesterov')
parser.add_argument('--weight-decay', default=0, type=float)
parser.add_argument('--ema', default=0, type=float, help='EMA decay rate')
parser.add_argument('--warmup-steps', default=0, type=int, help='warmup steps')
parser.add_argument('--grad-clip', default=0., type=float, help='gradient norm clipping')
parser.add_argument('--resume', default='', type=str, help='path to checkpoint')
parser.add_argument('--evaluate', action='store_true', help='evaluate model on validation set')
parser.add_argument('--seed', default=None, type=int, help='seed for initializing training')
parser.add_argument('--label-smoothing', default=0, type=float, help='label smoothing alpha')
parser.add_argument('--mu', default=7, type=int, help='coefficient of unlabeled batch size')
parser.add_argument('--threshold', default=0.95, type=float, help='pseudo label threshold')
parser.add_argument('--temperature', default=1, type=float, help='pseudo label temperature')
parser.add_argument('--lambda-u', default=1, type=float, help='coefficient of unlabeled loss')
parser.add_argument("--randaug", nargs="+", type=int, help="use it like this. --randaug 2 10")
parser.add_argument("--amp", action="store_true", help="use 16-bit (mixed) precision")
parser.add_argument('--world-size', default=-1, type=int,
                    help='number of nodes for distributed training')
parser.add_argument("--local_rank", type=int, default=-1,
                    help="For distributed training: local_rank")


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)


def get_cosine_schedule_with_warmup(optimizer,
                                    num_warmup_steps,
                                    num_training_steps,
                                    num_cycles=0.5,
                                    last_epoch=-1):
    def lr_lambda(current_step):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))

        progress = float(current_step - num_warmup_steps) / \
            float(max(1, num_training_steps - num_warmup_steps))
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * float(num_cycles) * 2.0 * progress)))

    return LambdaLR(optimizer, lr_lambda, last_epoch)


def get_lr(optimizer):
    return optimizer.param_groups[0]['lr']


def train_loop(args, labeled_loader, unlabeled_loader, test_loader,
               teacher_model, student_model, avg_student_model, criterion,
               t_optimizer, s_optimizer, t_scheduler, s_scheduler, t_scaler, s_scaler):
    if args.world_size > 1:
        labeled_epoch = 0
        unlabeled_epoch = 0
        labeled_loader.sampler.set_epoch(labeled_epoch)
        unlabeled_loader.sampler.set_epoch(unlabeled_epoch)

    labeled_iter = iter(labeled_loader)
    unlabeled_iter = iter(unlabeled_loader)

    moving_dot_product = torch.empty(1).to(args.device)
    limit = 3.0**(0.5)  # 3 = 6 / (f_in + f_out)
    nn.init.uniform_(moving_dot_product, -limit, limit)

    for step in range(args.start_step, args.total_steps):
        if step % args.eval_step == 0:
            pbar = tqdm(range(args.eval_step), disable=args.local_rank not in [-1, 0])
            batch_time = AverageMeter()
            data_time = AverageMeter()
            s_losses = AverageMeter()
            t_losses = AverageMeter()
            t_losses_l = AverageMeter()
            t_losses_u = AverageMeter()
            t_losses_mpl = AverageMeter()
            mean_mask = AverageMeter()

        teacher_model.train()
        student_model.train()
        end = time.time()

        try:
            images_l, targets = labeled_iter.next()
        except:
            if args.world_size > 1:
                labeled_epoch += 1
                labeled_loader.sampler.set_epoch(labeled_epoch)
            labeled_iter = iter(labeled_loader)
            images_l, targets = labeled_iter.next()

        try:
            images_uw, images_us, _ = unlabeled_iter.next()
        except:
            if args.world_size > 1:
                unlabeled_epoch += 1
                unlabeled_loader.sampler.set_epoch(unlabeled_epoch)
            unlabeled_iter = iter(unlabeled_loader)
            images_uw, images_us, _ = unlabeled_iter.next()

        data_time.update(time.time() - end)

        images_l = images_l.to(args.device)
        images_uw = images_uw.to(args.device)
        images_us = images_us.to(args.device)
        targets = targets.to(args.device)
        with amp.autocast(enabled=args.amp):
            batch_size = images_l.shape[0]
            t_images = torch.cat((images_l, images_uw, images_us))
            t_logits = teacher_model(t_images)
            t_logits_l = t_logits[:batch_size]
            t_logits_uw, t_logits_us = t_logits[batch_size:].chunk(2)
            del t_logits

            t_loss_l = criterion(t_logits_l, targets)

            soft_pseudo_label = torch.softmax(t_logits_uw.detach()/args.temperature, dim=-1)
            max_probs, hard_pseudo_label = torch.max(soft_pseudo_label, dim=-1)
            mask = max_probs.ge(args.threshold).float()
            t_loss_u = torch.mean(
                -(soft_pseudo_label * torch.log_softmax(t_logits_us, dim=-1)).sum(dim=-1) * mask
            )
            t_loss_uda = t_loss_l + args.lambda_u * t_loss_u

            s_images = torch.cat((images_l, images_us))
            s_logits = student_model(s_images)
            s_logits_l = s_logits[:batch_size]
            s_logits_us = s_logits[batch_size:]
            del s_logits

            s_loss_l_old = F.cross_entropy(s_logits_l.detach(), targets)
            s_loss = criterion(s_logits_us, hard_pseudo_label)

        s_scaler.scale(s_loss).backward()
        s_scaler.step(s_optimizer)
        s_scaler.update()
        s_scheduler.step()
        if args.ema > 0:
            avg_student_model.update_parameters(student_model)

        with amp.autocast(enabled=args.amp):
            with torch.no_grad():
                s_logits_l = student_model(images_l)
            s_loss_l_new = F.cross_entropy(s_logits_l.detach(), targets)
            dot_product = s_loss_l_new - s_loss_l_old
            moving_dot_product = moving_dot_product * 0.99 + dot_product * 0.01
            dot_product = dot_product - moving_dot_product
            _, hard_pseudo_label = torch.max(t_logits_us.detach(), dim=-1)
            t_loss_mpl = dot_product * F.cross_entropy(t_logits_us.detach(), hard_pseudo_label)
            t_loss = t_loss_uda + t_loss_mpl

        t_scaler.scale(t_loss).backward()
        t_scaler.step(t_optimizer)
        t_scaler.update()
        t_scheduler.step()

        teacher_model.zero_grad()
        student_model.zero_grad()

        if args.world_size > 1:
            s_loss = reduce_tensor(s_loss.detach(), args.world_size)
            t_loss = reduce_tensor(t_loss.detach(), args.world_size)
            t_loss_l = reduce_tensor(t_loss_l.detach(), args.world_size)
            t_loss_u = reduce_tensor(t_loss_u.detach(), args.world_size)
            t_loss_mpl = reduce_tensor(t_loss_mpl.detach(), args.world_size)
            mask = reduce_tensor(mask, args.world_size)

        s_losses.update(s_loss.item())
        t_losses.update(t_loss.item())
        t_losses_l.update(t_loss_l.item())
        t_losses_u.update(t_loss_u.item())
        t_losses_mpl.update(t_loss_mpl.item())
        mean_mask.update(mask.mean().item())

        batch_time.update(time.time() - end)
        end = time.time()
        pbar.set_description(
            f"Train Iter: {step+1:3}/{args.eval_step:3}. "
            f"LR: {get_lr(s_optimizer):.4f}. Data: {data_time.avg:.2f}s. "
            f"Batch: {batch_time.avg:.2f}s. S_Loss: {s_losses.avg:.4f}. "
            f"T_Loss: {t_losses.avg:.4f}. Mask: {mean_mask.avg}")
        if args.local_rank in [-1, 0]:
            args.writer.add_scalar("lr", get_lr(s_optimizer))

        if step+1 % args.eval_step == 0:
            pbar.close()
        if args.local_rank in [-1, 0]:
            args.writer.add_scalar("loss/s_loss", s_losses.avg)
            args.writer.add_scalar("loss/t_loss", t_losses.avg)
            args.writer.add_scalar("loss/t_labeled", t_losses_l.avg)
            args.writer.add_scalar("loss/t_unlabeled", t_losses_u.avg)
            args.writer.add_scalar("loss/t_mpl", t_losses_mpl.avg)
            args.writer.add_scalar("loss/mask", mean_mask.avg)

        if args.local_rank in [-1, 0]:
            test_model = avg_student_model if avg_student_model is not None else student_model
            losses, top1, top5 = evaluate(args, test_loader, test_model, criterion)
            is_best = top1 > args.best_top1
            if is_best:
                args.best_top1 = top1
                args.best_top5 = top5
            if args.k_fold is None and args.local_rank in [-1, 0]:
                save_checkpoint(args, {
                    'step': step + 1,
                    'arch': args.arch,
                    'teacher_state_dict': teacher_model.state_dict(),
                    'student_state_dict': student_model.state_dict(),
                    'avg_state_dict': avg_student_model.state_dict() if avg_student_model is not None else None,
                    'best_top1': args.best_top1,
                    'best_top5': args.best_top5,
                    'teacher_optimizer': t_optimizer.state_dict(),
                    'student_optimizer': s_optimizer.state_dict(),
                    'teacher_scheduler': t_scheduler.state_dict(),
                    'student_scheduler': s_scheduler.state_dict(),
                    'teacher_scaler': t_scaler.state_dict(),
                    'student_scaler': s_scaler.state_dict(),
                }, is_best)
    return


def evaluate(args, test_loader, model, criterion):
    def evaluation_step(args, model, criterion, images, targets):
        images = images.to(args.device)
        targets = targets.to(args.device)
        with amp.autocast(enabled=args.amp):
            output = model(images)
            loss = criterion(output, targets)
        return loss, output

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    acc_mat = torch.zeros(args.num_classes, args.num_classes)
    model.eval()
    test_loader = tqdm(test_loader, disable=args.local_rank not in [-1, 0])
    with torch.no_grad():
        end = time.time()
        for step, (path, images, targets) in enumerate(test_loader):
            batch_size = targets.shape[0]
            data_time.update(time.time() - end)
            loss, output = evaluation_step(args, model, criterion, images, targets)
            acc1, acc5 = accuracy(path, output, targets, acc_mat, (1, 5), args.do_debug)
            losses.update(loss.item(), batch_size)
            top1.update(acc1[0], batch_size)
            top5.update(acc5[0], batch_size)
            batch_time.update(time.time() - end)
            end = time.time()
            test_loader.set_description(
                f"Test Iter: {step+1:3}/{len(test_loader):3}. Data: {data_time.avg:.2f}s. "
                f"Batch: {batch_time.avg:.2f}s. Loss: {losses.avg:.4f}. "
                f"top1: {top1.avg:.2f}. top5: {top5.avg:.2f}. ")

        test_loader.close()
        if args.local_rank in [-1, 0]:
            args.writer.add_scalar("loss/val", losses.avg)

        return losses.avg, top1.avg, top5.avg


def main():
    args = parser.parse_args()
    args.best_top1 = 0.
    args.best_top5 = 0.

    if args.local_rank != -1:
        args.gpu = args.local_rank
        torch.distributed.init_process_group(backend='nccl')
        args.world_size = torch.distributed.get_world_size()
    else:
        args.gpu = 0
        args.world_size = 1

    args.device = torch.device('cuda', args.gpu)

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)

    logger.warning(
        f"Process rank: {args.local_rank}, "
        f"device: {args.device}, "
        f"distributed training: {bool(args.local_rank != -1)}, "
        f"16-bits training: {args.amp}",)

    logger.info(dict(args._get_kwargs()))

    if args.local_rank in [-1, 0]:
        args.writer = SummaryWriter(f"results/{args.name}")

    if args.seed is not None:
        set_seed(args)

    labeled_dataset, unlabeled_dataset, test_dataset = DATASET_GETTERS[args.dataset](args)

    train_sampler = RandomSampler if args.local_rank == -1 else DistributedSampler
    labeled_loader = DataLoader(
        labeled_dataset,
        sampler=train_sampler(labeled_dataset),
        batch_size=args.batch_size,
        num_workers=args.workers,
        drop_last=True)

    unlabeled_loader = DataLoader(
        unlabeled_dataset,
        sampler=train_sampler(unlabeled_dataset),
        batch_size=args.batch_size*args.mu,
        num_workers=args.num_workers,
        drop_last=True)

    test_loader = DataLoader(
        test_dataset,
        sampler=SequentialSampler(test_dataset),
        batch_size=args.batch_size,
        num_workers=args.workers)

    if args.local_rank in [-1, 0]:
        print(f"Training Steps: {args.total_steps}")

    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()

    teacher_model = build_wideresnet(args, depth=28, widen_factor=2)
    student_model = build_wideresnet(args, depth=28, widen_factor=2)

    if args.local_rank == 0:
        torch.distributed.barrier()

    teacher_model.to(args.device)
    student_model.to(args.device)
    avg_student_model = None
    if args.ema > 0:
        avg_student_model = ModelEMA(student_model, args.ema)

    criterion = create_loss_fn(args)
    t_optimizer = optim.SGD(teacher_model.parameters(),
                            lr=args.lr,
                            momentum=args.momentum,
                            weight_decay=args.weight_decay,
                            nesterov=args.nesterov)
    s_optimizer = optim.SGD(student_model.parameters(),
                            lr=args.lr,
                            momentum=args.momentum,
                            weight_decay=args.weight_decay,
                            nesterov=args.nesterov)

    t_scheduler = get_cosine_schedule_with_warmup(t_optimizer,
                                                  args.warmup_steps,
                                                  args.total_steps)
    s_scheduler = get_cosine_schedule_with_warmup(s_optimizer,
                                                  args.warmup_steps,
                                                  args.total_steps)

    t_scaler = amp.GradScaler(enabled=args.amp)
    s_scaler = amp.GradScaler(enabled=args.amp)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print(f"=> loading checkpoint '{args.resume}'")
            loc = f'cuda:{args.gpu}'
            checkpoint = torch.load(args.resume, map_location=loc)
            args.start_step = checkpoint['step']
            args.best_top1 = checkpoint['best_top1']
            args.best_top5 = checkpoint['best_top5']
            t_optimizer.load_state_dict(checkpoint['teacher_optimizer'])
            s_optimizer.load_state_dict(checkpoint['student_optimizer'])
            t_scheduler.load_state_dict(checkpoint['teacher_scheduler'])
            s_scheduler.load_state_dict(checkpoint['student_scheduler'])
            t_scaler.load_state_dict(checkpoint['teacher_scaler'])
            s_scaler.load_state_dict(checkpoint['student_scaler'])
            try:
                teacher_model.load_state_dict(checkpoint['teacher_state_dict'])
            except:
                module_load_state_dict(teacher_model, checkpoint['teacher_state_dict'])
            try:
                student_model.load_state_dict(checkpoint['student_state_dict'])
            except:
                module_load_state_dict(student_model, checkpoint['student_state_dict'])
            if avg_student_model is not None:
                try:
                    avg_student_model.load_state_dict(checkpoint['avg_state_dict'])
                except:
                    module_load_state_dict(avg_student_model, checkpoint['avg_state_dict'])
            print(f"=> loaded checkpoint '{args.resume}' (step {checkpoint['step']})")
        else:
            print(f"=> no checkpoint found at '{args.resume}'")

    if args.local_rank != -1:
        teacher_model = nn.parallel.DistributedDataParallel(
            teacher_model, device_ids=[args.local_rank],
            output_device=args.local_rank, find_unused_parameters=True)
        student_model = nn.parallel.DistributedDataParallel(
            student_model, device_ids=[args.local_rank],
            output_device=args.local_rank, find_unused_parameters=True)

    if args.evaluate:
        if avg_student_model is not None:
            student_model = avg_student_model
        evaluate(args, test_loader, student_model, criterion)
        return

    teacher_model.zero_grad()
    student_model.zero_grad()
    train_loop(args, labeled_loader, unlabeled_loader, test_loader,
               teacher_model, student_model, avg_student_model, criterion,
               t_optimizer, s_optimizer, t_scheduler, s_scheduler, t_scaler, s_scaler)
    return


if __name__ == '__main__':
    main()
