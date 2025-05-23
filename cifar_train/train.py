import os
import sys
import time
import glob
import numpy as np
import torch
# from torchstat import stat
# from torchsummary import summary

# os.environ['CUDA_VISIBLE_DEVICES'] = '3'

import utils
import logging
import argparse
import torch.nn as nn
import genotypes
import torch.utils
import torchvision.datasets as dset
import torch.backends.cudnn as cudnn
from thop import profile, clever_format

from torch.autograd import Variable
from model import NetworkCIFAR as Network
# from torchsummary import summary

parser = argparse.ArgumentParser("cifar")
parser.add_argument('--data', type=str, default='../data',
                    help='location of the data corpus')
parser.add_argument('--batch_size', type=int, default=96, help='batch size')
parser.add_argument('--learning_rate', type=float, default=0.025, help='init learning rate')
parser.add_argument('--momentum', type=float, default=0.9, help='momentum')
parser.add_argument('--weight_decay', type=float, default=3e-4, help='weight decay')
parser.add_argument('--report_freq', type=float, default=50, help='report frequency')
# parser.add_argument('--gpu', type=int, default=0, help='gpu device id')
parser.add_argument('--epochs', type=int, default=200, help='num of training epochs')
parser.add_argument('--init_channels', type=int, default=36, help='num of init channels')
parser.add_argument('--model_path', type=str, default='saved_models', help='path to save the model')
parser.add_argument('--auxiliary_weight', type=float, default=0.4, help='weight for auxiliary loss')
parser.add_argument('--cutout', action='store_false', default=True, help='use cutout')
parser.add_argument('--cutout_length', type=int, default=16, help='cutout length')
parser.add_argument('--drop_path_prob', type=float, default=0.2, help='drop path probability')
parser.add_argument('--save', type=str, default='EXP', help='experiment name')
parser.add_argument('--seed', type=int, default=0, help='random seed')
parser.add_argument('--arch', type=str, default='s103_1', help='which architecture to use')
parser.add_argument('--grad_clip', type=float, default=5, help='gradient clipping')
parser.add_argument('--gpu', type=int, default=1, help='gpu')

# parser.add_argument("--local_rank", type=int, default=-1)
args, unparsed = parser.parse_known_args()

args.save = 'train-{}-{}'.format(args.save, time.strftime("%Y%m%d-%H%M%S"))

utils.create_exp_dir(args.save, scripts_to_save=glob.glob('*.py'))

log_format = '%(asctime)s %(message)s'
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format=log_format, datefmt='%m/%d %I:%M:%S %p')
fh = logging.FileHandler(os.path.join(args.save, 'log.txt'))
fh.setFormatter(logging.Formatter(log_format))
logging.getLogger().addHandler(fh)

CIFAR_CLASSES = 10

def main():
    # if not torch.cuda.is_available():
    #     logging.info('no gpu device available')
    #     sys.exit(1)

    np.random.seed(args.seed)
    torch.cuda.set_device(args.gpu)
    # torch.distributed.init_process_group(backend='nccl')
    cudnn.benchmark = True
    torch.manual_seed(args.seed)
    cudnn.enabled = True
    torch.cuda.manual_seed(args.seed)
    # logging.info('gpu device = %d' % args.gpu)
    logging.info("args = %s", args)

    genotype = eval("genotypes.%s" % args.arch)
    model = Network(args.init_channels, CIFAR_CLASSES, genotype)
    model.drop_path_prob = 0
    # summary(model, input_size=(3, 32, 32))
    # stat(model, (3, 32, 32))
    # model = torch.nn.parallel.DistributedDataParallel(model, find_unused_parameters=True)

    logging.info("param size = %fMB", utils.count_parameters_in_MB(model))

    # utils.load(model, os.path.join('/root/autodl-tmp/train-EXP-20230810-123356', 'weights.pt'))
    # model_weight = np.load(os.path.join('/root/autodl-tmp/search-EXP-20230804-215226', 'w_final.npy'), allow_pickle=True)
    # model.init_final_weights(model_weight)

    # mask = torch.load(
    #     os.path.join("/ubda/home/21041193r/NAS/search-EXP-20230105-140638/mask_w_final.npy"))
    # # mask = mask.cpu()
    # # for i in range(len(mask)):
    # #     for j in range(len(mask[i])):
    # #         for k in range(len(mask[i][j])):
    # #             mask[i][j][k] = mask[i][j][k].cpu()
    # torch.save(mask,os.path.join(args.save, 'mask.npy'))

    # /hdd/xiaoyun
    # /ubda/home/21041193r/NAS
    # model._masks_k = np.load(
    #     os.path.join("/ubda/home/16904228r/liuxiaoyun/search-EXP-20230430-205135", 'mask_k2.npy'), allow_pickle=True)
    model = model.cuda()
    # model._masks_w = torch.load(
    #     os.path.join("/ubda/home/21041193r/NAS/search-EXP-20250305-060416/mask_w_final_6.npy"))
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    # model._masks_w = torch.load(
    #     os.path.join("/hdd/xiaoyun/search-EXP-20250307-191548/mask_w_final.npy"), map_location=device)
    model._masks_w = torch.load(
            os.path.join("/root/autodl-tmp/search-EXP-20250307-202021/mask_w_final.npy"), map_location=device)

    zero = 0
    for i in range(len(model._masks_w)):
        for j in range(len(model._masks_w[i])):
            for k in range(len(model._masks_w[i][j])):
                zero+= model._masks_w[i][j][k].nelement()-model._masks_w[i][j][k].sum()
    logging.info("Pruning weight:"+str(zero))

    input_size = torch.randn(1, 3, 32, 32).cuda()
    flops, params = profile(model, inputs=(input_size,))
    flops, params = clever_format([flops, params], "%.3f")
    logging.info("param size = %s, flops = %s", params, flops)


    # logging.info(model.cells[0]._ops[0].op[1].weight.shape)
    # logging.info(model.cells[0]._ops[0].op[2].weight.shape)
    # logging.info(model.cells[0]._ops[0].op[5].weight.shape)
    # logging.info(model.cells[0]._ops[0].op[6].weight.shape)

    criterion = nn.CrossEntropyLoss()
    criterion = criterion.cuda()
    optimizer = torch.optim.SGD(
        model.parameters(),
        args.learning_rate,
        momentum=args.momentum,
        weight_decay=args.weight_decay
    )

    train_transform, valid_transform = utils._data_transforms_cifar10(args)
    train_data = dset.CIFAR10(root=args.data, train=True, download=True, transform=train_transform)
    # train_sampler = torch.utils.data.distributed.DistributedSampler(train_data)
    valid_data = dset.CIFAR10(root=args.data, train=False, download=True, transform=valid_transform)

    # train_queue = torch.utils.data.DataLoader(
    #     train_data, batch_size=args.batch_size, pin_memory=True, num_workers=4, sampler=train_sampler)
    train_queue = torch.utils.data.DataLoader(
        train_data, batch_size=args.batch_size, shuffle=True, pin_memory=True, num_workers=4)

    valid_queue = torch.utils.data.DataLoader(
        valid_data, batch_size=args.batch_size, shuffle=False, pin_memory=True, num_workers=4)

    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, float(args.epochs))

    best_acc = 0
    for epoch in range(args.epochs):
    # for epoch in range(300):
    # best_acc = 94.619998
    # for epoch in range(205):
    #     scheduler.step()
    # for epoch in range(205,args.epochs):
        scheduler.step()
        logging.info('epoch %d lr %e', epoch, scheduler.get_lr()[0])
        model.drop_path_prob = args.drop_path_prob * epoch / args.epochs

        train_acc1,train_acc2,train_acc3, train_obj = train(train_queue, model, criterion, optimizer)
        logging.info('train_acc %f %f %f', train_acc1,train_acc2,train_acc3)

        valid_acc1,valid_acc2,valid_acc3,valid_obj = infer(valid_queue, model, criterion)
        logging.info('valid_acc %f %f %f', valid_acc1,valid_acc2,valid_acc3)
        if best_acc < valid_acc3:
            best_acc = valid_acc3
            utils.save(model, os.path.join(args.save, 'weights.pt'))
        logging.info('best_acc %f', best_acc)


def train(train_queue, model, criterion, optimizer):
    objs = utils.AvgrageMeter()
    top1_1 = utils.AvgrageMeter()
    top1_2 = utils.AvgrageMeter()
    top1_3 = utils.AvgrageMeter()
    top5 = utils.AvgrageMeter()
    model.train()

    for step, (input, target) in enumerate(train_queue):
        input = Variable(input).cuda()
        target = Variable(target).cuda()

        optimizer.zero_grad()
        # logits, logits_aux1, logits_aux2 = model(input)
        logits, logits_aux2 = model(input)
        loss = criterion(logits, target)
        # loss_aux1 = criterion(logits_aux1, target)
        loss_aux2 = criterion(logits_aux2, target)
        # loss += loss_aux1 + loss_aux2
        loss += 0.4 * loss_aux2
        loss.backward()
        nn.utils.clip_grad_norm(model.parameters(), args.grad_clip)
        optimizer.step()

        # prec1_1, correct = utils.accuracy(logits_aux1, target, topk=(1, ))
        prec1_2 = utils.accuracy(logits_aux2, target, topk=(1, ))
        prec1_3 = utils.accuracy(logits, target, topk=(1, ))
        # prec1 = (prec1_1[0]+prec1_2[0]+prec1_3[0])/3
        # prec5 = (prec5_1+prec5_2+prec5_3)/3
        n = input.size(0)
        objs.update(loss.data.item(), n)
        # top1_1.update(prec1_1[0].data.item(), n)
        top1_2.update(prec1_2[0].data.item(), n)
        top1_3.update(prec1_3[0].data.item(), n)
        # top5.update(prec5.data.item(), n)

        if step % args.report_freq == 0:
            # logging.info('train %03d %e %f %f %f', step, objs.avg, top1_1.avg, top1_2.avg, top1_3.avg)
            logging.info('train %03d %e %f %f', step, objs.avg, top1_2.avg, top1_3.avg)

    return top1_1.avg, top1_2.avg, top1_3.avg, objs.avg


def infer(valid_queue, model, criterion):
    objs = utils.AvgrageMeter()
    top1_1 = utils.AvgrageMeter()
    top1_2 = utils.AvgrageMeter()
    top1_3 = utils.AvgrageMeter()
    # top1 = utils.AvgrageMeter()
    top5 = utils.AvgrageMeter()
    model.eval()

    for step, (input, target) in enumerate(valid_queue):
        input = Variable(input, volatile=True).cuda()
        target = Variable(target, volatile=True).cuda()

        with torch.no_grad():
            # logits, logits_aux1, logits_aux2 = model(input)
            logits, logits_aux2 = model(input)
            loss = criterion(logits, target)
            # loss_aux1 = criterion(logits_aux1, target)
            loss_aux2 = criterion(logits_aux2, target)
            # loss += loss_aux1 + loss_aux2
            loss += 0.4 * loss_aux2

        # prec1_1, correct = utils.accuracy(logits_aux1, target, topk=(1, ))
        prec1_2 = utils.accuracy(logits_aux2, target, topk=(1, ))
        prec1_3 = utils.accuracy(logits, target, topk=(1, ))
        # prec1 = (prec1_1[0] + prec1_2[0] + prec1_3[0]) / 3
        # prec5 = (prec5_1 + prec5_2 + prec5_3) / 3
        n = input.size(0)
        objs.update(loss.data.item(), n)
        # top1_1.update(prec1_1[0].data.item(), n)
        top1_2.update(prec1_2[0].data.item(), n)
        top1_3.update(prec1_3[0].data.item(), n)
        # top1.update(prec1.data.item(), n)
        # top5.update(prec5.data.item(), n)

        if step % args.report_freq == 0:
            # logging.info('valid %03d %e %f %f %f', step, objs.avg, top1_1.avg, top1_2.avg, top1_3.avg)
            logging.info('valid %03d %e %f %f', step, objs.avg, top1_2.avg, top1_3.avg)

    return top1_1.avg, top1_2.avg, top1_3.avg, objs.avg


if __name__ == '__main__':
    main()
