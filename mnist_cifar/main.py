from __future__ import print_function
import os
import shutil
import time
import argparse
import logging

import torch
import torch.nn.functional as F
import torch.optim as optim
import torch.backends.cudnn as cudnn

import sparselearning
from sparselearning.core import Masking, CosineDecay
from sparselearning.models import AlexNet, VGG16, LeNet_300_100, LeNet_5_Caffe, WideResNet
from sparselearning.utils import get_mnist_dataloaders, get_cifar10_dataloaders

from apex.fp16_utils import FP16_Optimizer

cudnn.benchmark = True
cudnn.deterministic = True

if not os.path.exists('./models'): os.mkdir('./models')
logging.basicConfig(filename='./mnist_cifar.log',
                            filemode='a',
                            format='%(asctime)s %(message)s',
                            datefmt='%H:%M:%S',
                            level=logging.INFO)

logging.info("Sparse learning on CIFAR/MNIST")

logger = logging.getLogger('Sparse_MNIST_CIFAR')

models = {}
models['lenet5'] = (LeNet_5_Caffe,[])
models['lenet300-100'] = (LeNet_300_100,[])
models['alexnet-s'] = (AlexNet, ['s', 10])
models['alexnet-b'] = (AlexNet, ['b', 10])
models['vgg-c'] = (VGG16, ['C', 10])
models['vgg-d'] = (VGG16, ['D', 10])
models['vgg-like'] = (VGG16, ['like', 10])
models['wrn-28-2'] = (WideResNet, [28, 2, 10, 0.3])
models['wrn-22-8'] = (WideResNet, [22, 8, 10, 0.3])
models['wrn-16-8'] = (WideResNet, [16, 8, 10, 0.3])
models['wrn-16-10'] = (WideResNet, [16, 10, 10, 0.3])

def print_and_log(msg):
    print(msg)
    logger.info(msg)

def train(args, model, device, train_loader, optimizer, epoch, lr_scheduler, mask=None):
    model.train()
    for batch_idx, (data, target) in enumerate(train_loader):
        if lr_scheduler is not None: lr_scheduler.step()
        data, target = data.to(device), target.to(device)
        if args.fp16:
            data = data.half()
        optimizer.zero_grad()
        output = model(data)

        loss = F.nll_loss(output, target)

        if args.fp16:
            optimizer.backward(loss)
        else:
            loss.backward()

        if mask is not None: mask.step()
        else: optimizer.step()

        if batch_idx % args.log_interval == 0:
            print_and_log('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(data), len(train_loader)*args.batch_size,
                100. * batch_idx / len(train_loader), loss.item()))

def evaluate(args, model, device, test_loader):
    model.eval()
    test_loss = 0
    correct = 0
    n = 0
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            model.t = target
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction='sum').item() # sum up batch loss
            pred = output.argmax(dim=1, keepdim=True) # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()
            n += target.shape[0]

    test_loss /= float(n)

    print_and_log('\nEvaluation: Average loss: {:.4f}, Accuracy: {}/{} ({:.3f}%)\n'.format(
        test_loss, correct, n, 100. * correct / float(n)))
    return correct / float(n)

def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'model_best.pth.tar')

def main():
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch MNIST Example')
    parser.add_argument('--batch-size', type=int, default=100, metavar='N',
                        help='input batch size for training (default: 100)')
    parser.add_argument('--test-batch-size', type=int, default=100, metavar='N',
                        help='input batch size for testing (default: 100)')
    parser.add_argument('--epochs', type=int, default=100, metavar='N',
                        help='number of epochs to train (default: 100)')
    parser.add_argument('--lr', type=float, default=0.1, metavar='LR',
                        help='learning rate (default: 0.1)')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                        help='SGD momentum (default: 0.9)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--seed', type=int, default=17, metavar='S', help='random seed (default: 17)')
    parser.add_argument('--log-interval', type=int, default=100, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--save-model', type=str, default='./models/model.pt', help='For Saving the current Model')
    parser.add_argument('--data', type=str, default='mnist')
    parser.add_argument('--augment', action='store_true')
    parser.add_argument('--decay_frequency', type=int, default=25000)
    parser.add_argument('--l1', type=float, default=0.0)
    parser.add_argument('--fp16', action='store_true', help='Run in fp16 mode.')
    parser.add_argument('--valid_split', type=float, default=0.1)
    parser.add_argument('--resume', type=str)
    parser.add_argument('--start-epoch', type=int, default=1)
    parser.add_argument('--model', type=str, default='')
    parser.add_argument('--l2', type=float, default=5.0e-4)
    parser.add_argument('--iterations', type=int, default=1, help='How many times the model should be run after each other. Default=1')
    parser.add_argument('--save-features', action='store_true', help='Resumes a saved model and saves its feature data to disk for plotting.')
    parser.add_argument('--bench', action='store_true', help='Enables the benchmarking of layers and estimates sparse speedups')
    sparselearning.core.add_sparse_args(parser)

    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    print_and_log('\n\n')
    print_and_log('='*80)
    print_and_log('='*80)
    print_and_log(args)
    torch.manual_seed(args.seed)
    for i in range(args.iterations):
        print_and_log("\nIteration start: {0}/{1}\n".format(i+1, args.iterations))

        if args.data == 'mnist':
            train_loader, valid_loader, test_loader = get_mnist_dataloaders(args, validation_split=args.valid_split)
        else:
            train_loader, valid_loader, test_loader = get_cifar10_dataloaders(args, args.valid_split)

        if args.model not in models:
            print('You need to select an existing model via the --model argument. Available models include: ')
            for key in models:
                print('\t{0}'.format(key))
            raise Exception('You need to select a model')
        else:
            cls, cls_args = models[args.model]
            cls_args.append(args.save_features)
            cls_args.append(args.bench)
            model = cls(*cls_args).to(device)
            print_and_log(model)
            print_and_log('='*60)
            print_and_log(args.model)
            print_and_log('='*60)

            print_and_log('='*60)
            print_and_log('Death mode: {0}'.format(args.death))
            print_and_log('Growth mode: {0}'.format(args.growth))
            print_and_log('Redistribution mode: {0}'.format(args.redistribution))
            print_and_log('='*60)

        optimizer = optim.SGD(model.parameters(),lr=args.lr,momentum=args.momentum,weight_decay=args.l2, nesterov=True)
        lr_scheduler = optim.lr_scheduler.StepLR(optimizer, args.decay_frequency, gamma=0.1)

        if args.resume:
            if os.path.isfile(args.resume):
                print_and_log("=> loading checkpoint '{}'".format(args.resume))
                checkpoint = torch.load(args.resume)
                args.start_epoch = checkpoint['epoch']
                model.load_state_dict(checkpoint['state_dict'])
                optimizer.load_state_dict(checkpoint['optimizer'])
                print_and_log("=> loaded checkpoint '{}' (epoch {})"
                      .format(args.resume, checkpoint['epoch']))
                print_and_log('Testing...')
                evaluate(args, model, device, test_loader)
                plot_class_feature_histograms(args, model, device, train_loader, optimizer)
            else:
                print_and_log("=> no checkpoint found at '{}'".format(args.resume))


        if args.fp16:
            optimizer = FP16_Optimizer(optimizer,
                                       static_loss_scale = None,
                                       dynamic_loss_scale = True,
                                       dynamic_loss_args = {'init_scale': 2 ** 16})
            model = model.half()

        mask = None
        if args.sparse:
            decay = CosineDecay(args.death_rate, len(train_loader)*(args.epochs))
            mask = Masking(optimizer, death_mode=args.death, death_rate_decay=decay, growth_mode=args.growth, redistribution_mode=args.redistribution)
            mask.add_module(model, density=args.density)

        for epoch in range(1, args.epochs + 1):

            t0 = time.time()
            train(args, model, device, train_loader, optimizer, epoch, lr_scheduler, mask)

            if args.valid_split > 0.0:
                val_acc = evaluate(args, model, device, valid_loader)

            save_checkpoint({'epoch': epoch + 1,
                             'state_dict': model.state_dict(),
                             'optimizer' : optimizer.state_dict()},
                            is_best=False, filename=args.save_model)

            if args.sparse and epoch < args.epochs:
                mask.at_end_of_epoch()

            print_and_log('Current learning rate: {0}. Time taken for epoch: {1}.\n'.format(optimizer.param_groups[0]['lr'], time.time() - t0))

        evaluate(args, model, device, test_loader)
        print_and_log("\nIteration end: {0}/{1}\n".format(i+1, args.iterations))

if __name__ == '__main__':
   main()
