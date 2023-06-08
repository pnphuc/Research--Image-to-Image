"""
Copyright Snap Inc. 2021. This sample code is made available by Snap Inc. for informational purposes only.
No license, whether implied or otherwise, is granted in or to such code (including any rights to copy, modify,
publish, distribute and/or commercialize such code), unless you have entered into a separate agreement for such rights.
Such code is provided as-is, without warranty of any kind, express or implied, including any warranties of merchantability,
title, fitness for a particular purpose, non-infringement, or that such code is free of defects, errors or viruses.
In no event will Snap Inc. be liable for any damages or losses of any kind arising from the sample code or your use thereof.
"""

import argparse
import os

import numpy as np
import torch

from data import create_eval_dataloader
from metric.fid_score import InceptionV3
from metric.mIoU_score import DRNSeg
from models.modules.spade_modules.spade_distiller_modules import SPADEDistillerModules
from models.modules.sync_batchnorm import DataParallelWithCallback
from models.spade_model import SPADEModel
from utils import util
from utils.model_profiling import model_profiling


class BaseSPADEDistiller(SPADEModel):
    @staticmethod
    def modify_commandline_options(parser, is_train):
        assert isinstance(parser, argparse.ArgumentParser)
        parser.add_argument(
            '--num_upsampling_layers',
            choices=('normal', 'more', 'most'),
            default='more',
            help=
            "If 'more', adds upsampling layer between the two middle resnet blocks. "
            "If 'most', also add one more upsampling + resnet layer at the end of the generator"
        )
        parser.add_argument('--teacher_netG',
                            type=str,
                            default='inception_spade',
                            help='specify teacher generator architecture',
                            choices=['inception_spade'])
        parser.add_argument('--student_netG',
                            type=str,
                            default='inception_spade',
                            help='specify student generator architecture',
                            choices=['inception_spade'])
        parser.add_argument(
            '--teacher_ngf',
            type=int,
            default=64,
            help='the base number of filters of the teacher generator')
        parser.add_argument(
            '--student_ngf',
            type=int,
            default=48,
            help='the base number of filters of the student generator')
        parser.add_argument(
            '--teacher_norm_G',
            type=str,
            default='spadesyncbatch3x3',
            help=
            'instance normalization or batch normalization of the teacher model'
        )
        parser.add_argument(
            '--student_norm_G',
            type=str,
            default='spadesyncbatch3x3',
            help=
            'instance normalization or batch normalization of the student model'
        )
        parser.add_argument('--restore_teacher_G_path',
                            type=str,
                            required=True,
                            help='the path to restore the teacher generator')
        parser.add_argument('--restore_student_G_path',
                            type=str,
                            default=None,
                            help='the path to restore the student generator')
        parser.add_argument(
            '--restore_A_path',
            type=str,
            default=None,
            help='the path to restore the adaptors for distillation')
        parser.add_argument('--restore_D_path',
                            type=str,
                            default=None,
                            help='the path to restore the discriminator')
        parser.add_argument('--restore_O_path',
                            type=str,
                            default=None,
                            help='the path to restore the optimizer')
        parser.add_argument('--lambda_gan',
                            type=float,
                            default=1,
                            help='weight for gan loss')
        parser.add_argument('--lambda_feat',
                            type=float,
                            default=10,
                            help='weight for gan feature loss')
        parser.add_argument('--lambda_vgg',
                            type=float,
                            default=10,
                            help='weight for vgg loss')
        parser.add_argument('--lambda_distill',
                            type=float,
                            default=10,
                            help='weight for vgg loss')
        parser.add_argument('--distill_G_loss_type',
                            type=str,
                            default='mse',
                            choices=['mse', 'ka'],
                            help='the type of the G distillation loss')
        parser.add_argument('--beta2',
                            type=float,
                            default=0.999,
                            help='momentum term of adam')
        parser.add_argument('--no_TTUR',
                            action='store_true',
                            help='Use TTUR training scheme')
        parser.add_argument('--no_fid',
                            action='store_true',
                            help='No FID evaluation during training')
        parser.add_argument('--no_mIoU',
                            action='store_true',
                            help='No mIoU evaluation during training '
                            '(sometimes because there are CUDA memory)')
        parser.set_defaults(netD='multi_scale',
                            ndf=64,
                            dataset_mode='cityscapes',
                            batch_size=16,
                            print_freq=50,
                            save_latest_freq=10000000000,
                            save_epoch_freq=10,
                            nepochs=100,
                            nepochs_decay=100,
                            init_type='xavier')
        return parser

    def __init__(self, opt):
        super(SPADEModel, self).__init__(opt)
        self.model_names = ['G_student', 'G_teacher', 'D']
        self.visual_names = ['labels', 'Tfake_B', 'Sfake_B', 'real_B']
        self.model_names.append('D')
        self.loss_names = [
            'G_gan', 'G_feat', 'G_vgg', 'G_distill', 'D_real', 'D_fake'
        ]
        if hasattr(opt, 'distiller'):
            self.modules = SPADEDistillerModules(opt).to(self.device)
            if len(opt.gpu_ids) > 0:
                self.modules = DataParallelWithCallback(self.modules,
                                                        device_ids=opt.gpu_ids)
                self.modules_on_one_gpu = self.modules.module
            else:
                self.modules_on_one_gpu = self.modules
        for i in range(len(self.modules_on_one_gpu.mapping_layers)):
            self.loss_names.append('G_distill%d' % i)
        self.optimizer_G, self.optimizer_D = self.modules_on_one_gpu.create_optimizers(
        )
        self.optimizers = [self.optimizer_G, self.optimizer_D]
        if not opt.no_fid:
            block_idx = InceptionV3.BLOCK_INDEX_BY_DIM[2048]
            self.inception_model = InceptionV3([block_idx])
            self.inception_model.to(self.device)
            self.inception_model.eval()
        if 'cityscapes' in opt.dataroot and not opt.no_mIoU:
            self.drn_model = DRNSeg('drn_d_105', 19, pretrained=False)
            util.load_network(self.drn_model, opt.drn_path, verbose=False)
            self.drn_model.to(self.device)
            self.drn_model.eval()
        self.eval_dataloader = create_eval_dataloader(self.opt)
        self.best_fid = 1e9
        self.best_mIoU = -1e9
        self.fids, self.mIoUs = [], []
        self.is_best = False
        self.npz = np.load(opt.real_stat_path)

        model_profiling(self.modules_on_one_gpu.netG_teacher,
                        self.opt.data_height,
                        self.opt.data_width,
                        channel=self.opt.data_channel,
                        num_forwards=0,
                        verbose=False)
        model_profiling(self.modules_on_one_gpu.netG_student,
                        self.opt.data_height,
                        self.opt.data_width,
                        channel=self.opt.data_channel,
                        num_forwards=0,
                        verbose=False)
        print(
            f'Teacher FLOPs: {self.modules_on_one_gpu.netG_teacher.n_macs}, Student FLOPs: {self.modules_on_one_gpu.netG_student.n_macs}.'
        )

    def forward(self, on_one_gpu=False):
        if on_one_gpu:
            self.Tfake_B, self.Sfake_B = self.modules_on_one_gpu(
                self.input_semantics)
        else:
            self.Tfake_B, self.Sfake_B = self.modules(self.input_semantics)

    def load_networks(self,
                      verbose=True,
                      teacher_only=False,
                      restore_pretrain=True):
        self.modules_on_one_gpu.load_networks(
            verbose,
            teacher_only=teacher_only,
            restore_pretrain=restore_pretrain)
        if self.opt.restore_O_path is not None:
            for i, optimizer in enumerate(self.optimizers):
                path = '%s-%d.pth' % (self.opt.restore_O_path, i)
                util.load_optimizer(optimizer, path, verbose)
                for param_group in optimizer.param_groups:
                    param_group['lr'] = self.opt.lr

    def save_networks(self, epoch):
        self.modules_on_one_gpu.save_networks(epoch, self.save_dir)
        for i, optimizer in enumerate(self.optimizers):
            save_filename = '%s_optim-%d.pth' % (epoch, i)
            save_path = os.path.join(self.save_dir, save_filename)
            torch.save(optimizer.state_dict(), save_path)

    def evaluate_model(self, step):
        raise NotImplementedError

    def optimize_parameters(self, steps):
        self.set_requires_grad(self.modules_on_one_gpu.netD, False)
        self.optimizer_G.zero_grad()
        self.backward_G()
        self.optimizer_G.step()
        self.set_requires_grad(self.modules_on_one_gpu.netD, True)
        self.optimizer_D.zero_grad()
        self.backward_D()
        self.optimizer_D.step()
