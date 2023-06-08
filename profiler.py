"""
Copyright Snap Inc. 2021. This sample code is made available by Snap Inc. for informational purposes only.
No license, whether implied or otherwise, is granted in or to such code (including any rights to copy, modify,
publish, distribute and/or commercialize such code), unless you have entered into a separate agreement for such rights.
Such code is provided as-is, without warranty of any kind, express or implied, including any warranties of merchantability,
title, fitness for a particular purpose, non-infringement, or that such code is free of defects, errors or viruses.
In no event will Snap Inc. be liable for any damages or losses of any kind arising from the sample code or your use thereof.
"""

import os
import random
import sys
import time
import warnings

import numpy as np
import torch
from torch.backends import cudnn

from data import create_dataloader
import common as mc
from utils.logger import Logger
from utils.common import load_pretrained_student, load_pretrained_spade_student, shrink

from torchprofile import profile_macs


def set_seed(seed):
    cudnn.benchmark = False
    cudnn.deterministic = True
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


class Profiler:
    def __init__(self, task):
        if task == 'train':
            from options.train_options import TrainOptions as Options
            from models import create_model as create_model
        elif task == 'distill':
            from options.distill_options import DistillOptions as Options
            from distillers import create_distiller as create_model
        else:
            raise NotImplementedError('Unknown task [%s]!!!' % task)
        opt = Options().parse()
        opt.tensorboard_dir = opt.log_dir if opt.tensorboard_dir is None else opt.tensorboard_dir
        print(' '.join(sys.argv))
        if opt.phase != 'train':
            warnings.warn('You are not using training set for %s!!!' % task)
        with open(os.path.join(opt.log_dir, 'opt.txt'), 'a') as f:
            f.write(' '.join(sys.argv) + '\n')
        set_seed(opt.seed)

        dataloader = create_dataloader(opt)
        dataset_size = len(dataloader.dataset)
        print('The number of training images = %d' % dataset_size)
        opt.iters_per_epoch = len(dataloader)
        if opt.dataset_mode in ['aligned', 'unaligned']:
            opt.data_channel, opt.data_height, opt.data_width = next(
                iter(dataloader))['A' if opt.direction ==
                                  'AtoB' else 'B'].shape[1:]
        elif opt.dataset_mode in ['cityscapes']:
            input_ = next(iter(dataloader))
            opt.data_height, opt.data_width = input_['label'].shape[2:]
            opt.data_channel = opt.input_nc
            if opt.contain_dontcare_label:
                opt.data_channel += 1
            if not opt.no_instance:
                opt.data_channel += input_['instance'].shape[1]
        else:
            raise NotImplementedError
        print(
            f'data shape is: channel={opt.data_channel}, height={opt.data_height}, width={opt.data_width}.'
        )

        model = create_model(opt)
        model.setup(opt)
        logger = Logger(opt)

        if getattr(opt, 'pretrained_student_G_path', '') and task == 'distill':
            if 'spade' in opt.teacher_netG:
                assert 'spade' in opt.student_netG
                assert 'spade' in opt.pretrained_netG
                load_pretrained_spade_student(model, opt)
            else:
                load_pretrained_student(model, opt)

        self.opt = opt
        self.dataloader = dataloader
        self.model = model
        self.logger = logger
        self.task = task

        modules_on_one_gpu = getattr(model, 'modules_on_one_gpu', model)
        if self.task == 'distill':
            logger.print_info(
                f'netG teacher FLOPs: {mc.unwrap_model(modules_on_one_gpu.netG_teacher).n_macs}.'
            )
            logger.print_info(
                f'netG student FLOPs: {mc.unwrap_model(modules_on_one_gpu.netG_student).n_macs}.'
            )

            data_input = torch.ones(
                [1, opt.data_channel, opt.data_height,
                 opt.data_width]).to(model.device)
            macs_t = profile_macs(
                mc.unwrap_model(modules_on_one_gpu.netG_teacher).to(
                    model.device), data_input)
            macs_s = profile_macs(
                mc.unwrap_model(modules_on_one_gpu.netG_student).to(
                    model.device), data_input)
            params_t = 0
            params_s = 0
            for p in modules_on_one_gpu.netG_teacher.parameters():
                params_t += p.numel()
            for p in modules_on_one_gpu.netG_student.parameters():
                params_s += p.numel()
            logger.print_info(
                f'netG teacher FLOPs: {macs_t}; Params: {params_t}.')
            logger.print_info(
                f'netG student FLOPs: {macs_s}; Params: {params_s}.')

    def evaluate(self, epoch, iter, message, save_image=False):
        start_time = time.time()
        metrics = self.model.evaluate_model(iter, save_image=save_image)
        self.logger.print_current_metrics(epoch, iter, metrics,
                                          time.time() - start_time)
        self.logger.print_info(message)

    def start(self):
        opt = self.opt
        dataloader = self.dataloader
        model = self.model
        logger = self.logger

        if self.task == 'distill':
            N_test = 10
            pruning_time_list = []
            for i in range(5):
                shrink(model, opt)
            for i in range(N_test):
                pruning_time = shrink(model, opt)
                pruning_time_list.append(pruning_time)
            print(
                f'Average pruning time for {N_test} experiments: {np.mean(pruning_time_list):.2f}s.'
            )

        save_image = True
        evaluate = True

        if getattr(opt, 'pretrained_student_G_path',
                   '') and self.task == 'distill':
            if 'spade' in opt.teacher_netG:
                assert 'spade' in opt.student_netG
                assert 'spade' in opt.pretrained_netG
                load_pretrained_spade_student(model, opt)
            else:
                load_pretrained_student(model, opt)

        if evaluate:
            self.evaluate(0, 0, 'Model evaluated.', save_image=save_image)
