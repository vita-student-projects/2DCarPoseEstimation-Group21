# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import argparse
import datetime
import random
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader

import util.misc as utils
from datasets import build_dataset, get_coco_api_from_dataset
from engine import evaluate, train_one_epoch
from models import build_model

import os
from torch.utils.tensorboard import SummaryWriter
import logging
from datetime import datetime
import math

import sys
sys.out = None

def get_args_parser():
    parser = argparse.ArgumentParser('Set transformer detector', add_help=False)
    parser.add_argument('training_name',type=str)
    parser.add_argument('--pretrained_weight_path',type=str)
    parser.add_argument('-j', '--json_file',type=str,help='Path to the json output file')
    parser.add_argument('-v','--visualize_folder',type=str, help='The folder in which we should store the output images')

    parser.add_argument('--lr', default=1e-4, type=float)
    parser.add_argument('--lr_backbone', default=1e-5, type=float)
    parser.add_argument('--batch_size', default=2, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--epochs', default=300, type=int)
    parser.add_argument('--lr_drop', default=200, type=int)
    parser.add_argument('--clip_max_norm', default=0.1, type=float,
                        help='gradient clipping max norm')

    # Model parameters
    parser.add_argument('--frozen_weights', type=str, default=None,
                        help='Path to the pretrained model. If set, only the mask head will be trained')
    # * Backbone
    parser.add_argument('--backbone', default='resnet50', type=str,
                        help='Name of the convolutional backbone to use')
    parser.add_argument('--dilation', action='store_true',
                        help='If true, we replace stride with dilation in the last convolutional block (DC5)')
    parser.add_argument('--position_embedding', default='sine', type=str, choices=('sine', 'learned'),
                        help='Type of positional embedding to use on top of the image features')

    # * Transformer
    parser.add_argument('--enc_layers', default=6, type=int,
                        help='Number of encoding layers in the transformer')
    parser.add_argument('--dec_layers', default=6, type=int,
                        help='Number of decoding layers in the transformer')
    parser.add_argument('--dim_feedforward', default=2048, type=int,
                        help='Intermediate size of the feedforward layers in the transformer blocks')
    parser.add_argument('--hidden_dim', default=256, type=int,
                        help='Size of the embeddings (dimension of the transformer)')
    parser.add_argument('--dropout', default=0.1, type=float,
                        help='Dropout applied in the transformer')
    parser.add_argument('--nheads', default=8, type=int,
                        help='Number of attention heads inside the transformer\'s attentions')
    parser.add_argument('--num_queries', default=50, type=int,
                        help='Number of query slots')
    parser.add_argument('--pre_norm', action='store_true')

    # * Segmentation
    parser.add_argument('--masks', action='store_true',
                        help='Train segmentation head if the flag is provided')

    # Loss
    parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                        help='Disables auxiliary decoding losses (loss at each layer)')
    # * Matcher
    parser.add_argument('--set_cost_class', default=1, type=float,
                        help='Class coefficient in the matching cost')
    parser.add_argument('--set_cost_keypoints', default=1, type=float,
                        help='L1 keypoints coefficient in the matching cost')
    # * Loss coefficients
    parser.add_argument('--mask_loss_coef', default=1, type=float)
    parser.add_argument('--dice_loss_coef', default=1, type=float)
    parser.add_argument('--bbox_loss_coef', default=5, type=float)
    parser.add_argument('--giou_loss_coef', default=2, type=float)
    parser.add_argument('--eos_coef', default=0.1, type=float,
                        help='Relative classification weight of the no-object class')

    # dataset parameters
    parser.add_argument('--dataset_file', default='coco')
    parser.add_argument('--coco_path', type=str, default='carpe_data')
    parser.add_argument('--remove_difficult', action='store_true')

    parser.add_argument('-o', '--output_dir', default='snapshots',
                        help='path where to save, empty for no saving')
    parser.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=42, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--num_workers', default=2, type=int)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')

    # for keypoints
    parser.add_argument('-k', '--num_keypoints', default=24, type=int,
                        help='number of keypoints')

    # for data augmentation
    parser.add_argument('--apply_augmentation', action='store_true',
                        help='If we apply the data augmentation')
    parser.add_argument('--apply_occlusion_augmentation', action='store_true',
                        help='If we should apply the occlusion augmentation')
    
    parser.add_argument('--eval_n_epochs', default=20, type=int)

    parser.add_argument('--pretrained_detr',  help='resume from pretrained detr', action='store_true')
    parser.add_argument('-t', '--threshold',default=0.5,type=float)
    parser.add_argument('--threshold_keypoints',default=0.5,type=float)
    parser.add_argument('--threshold_iou',default=0.5,type=float)
    parser.add_argument('--pretrained_poet',help="path to the pretrained poet model",type=str)

    return parser


def main(args):
    logging.basicConfig(level=10)
    log = logging.getLogger('g21')

    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    model, criterion, postprocessors = build_model(args)

    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    log.info(f'Number of trainable parameters {n_parameters}' )
    
    if args.pretrained_poet is not None:
        is_backbone = lambda name : all([not(n in name) for n in ["class_embed_out","pose_embed","query_embed"]])
    else:
        is_backbone = lambda name : "backbone" in name 

    param_dicts = [
        {'params': [p for n, p in model.named_parameters() if not is_backbone(n) and p.requires_grad]},
        {
            'params': [p for n, p in model.named_parameters() if is_backbone(n) and p.requires_grad],
            'lr': args.lr_backbone,
        },
    ]
    optimizer = torch.optim.AdamW(param_dicts, lr=args.lr,
                                  weight_decay=args.weight_decay)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop)


    dataset_train = build_dataset(image_set='train', args=args)
    dataset_val = build_dataset(image_set='val', args=args)
    dataset_test = build_dataset(image_set='test', args=args)
        
    sampler_train = torch.utils.data.RandomSampler(dataset_train)
    sampler_val = torch.utils.data.SequentialSampler(dataset_val)


    batch_sampler_train = torch.utils.data.BatchSampler(
        sampler_train, args.batch_size, drop_last=True)

    data_loader_train = DataLoader(dataset_train, batch_sampler=batch_sampler_train,
                                   collate_fn=utils.collate_fn, num_workers=args.num_workers)
    data_loader_val = DataLoader(dataset_val, args.batch_size, sampler=sampler_val,
                                 drop_last=False, collate_fn=utils.collate_fn, num_workers=args.num_workers)
    output_dir = os.path.join(args.output_dir, args.training_name)
    training_name = args.training_name
    if os.path.exists(output_dir):
        timestamp = datetime.now().strftime('_%Y_%m_%d_%H_%M_%S')
        output_dir += timestamp
        training_name = args.training_name + timestamp

    writer = SummaryWriter(log_dir=os.path.join('runs',training_name))
    log.info('The output directory is :'+output_dir)
    os.makedirs(output_dir)
    output_dir = Path(output_dir)

    if args.pretrained_detr:
        model_ckpt = torch.hub.load('facebookresearch/detr:main', 'detr_resnet50', pretrained=True)
        model_state = model.state_dict()
        pretrained_state = { k:v for k,v in model_ckpt.state_dict().items() if k in model_state and v.size() == model_state[k].size() }
        for k,v in model.state_dict().items():
            if k not in pretrained_state:
                log.info('The following key '+k+' has not been found in the pretrained dictionary.')
                pretrained_state[k] = v

        model_state.update(pretrained_state)
        model.load_state_dict(model_state)

    elif (args.pretrained_weight_path is not None):
        if not os.path.isfile(args.pretrained_weight_path):
            raise ValueError("The given pretrained path doesn't exist.")

        pretrained_state = torch.load(args.pretrained_weight_path)
        log.info(f'Loading pretrained weights from {args.pretrained_weight_path}, epoch {pretrained_state["epoch"]} ')
        pretrained_state = pretrained_state['model']
        model_state = model.state_dict()
        for k,v in model.state_dict().items():
            if k not in pretrained_state:
                log.info('The following key '+k+' has not been found in the pretrained dictionary.')
                pretrained_state[k] = v

        model_state.update(pretrained_state)
        model.load_state_dict(model_state)
    elif (args.pretrained_poet is not None):
        if not os.path.isfile(args.pretrained_poet):
            raise ValueError("The given pretrained path doesn't exist.")
        log.info('Loading pretrained POET model for training weights from '+args.pretrained_poet)
        pretrained_state = torch.load(args.pretrained_poet)['model']
        query_embed_weight = pretrained_state["query_embed.weight"].clone()
        del(pretrained_state["query_embed.weight"])

        model_state = model.state_dict()
        pretrained_state = {k:v for k,v in pretrained_state.items() if k in model_state}
        
        for k,v in model.state_dict().items():
            if k not in pretrained_state:
                if "batches_tracked" not in k:
                    log.info('The following key '+k+' has not been found in the pretrained dictionary.')
                pretrained_state[k] = v
                
        model_state.update(pretrained_state)
        model.load_state_dict(model_state)

        for i in range(math.ceil(args.num_queries/25)):
            min_ = i*25
            max_ = min((i+1)*25, args.num_queries)
            model.query_embed.weight.data[min_:max_] = query_embed_weight.clone()[:max_-min_]

    model.to(device)

    if args.eval:
        coco_evaluator = evaluate(
            model, criterion, postprocessors,
            data_loader_val,
            get_coco_api_from_dataset(dataset_val), 
            device,num_keypoints=args.num_keypoints,
            visualize_folder=args.visualize_folder
        )
        return


    log.info('Start training...')
    best_AP = 0
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        log.info(f'Starting training step for epoch {epoch}...')
        train_one_epoch(
            model, criterion, 
            data_loader_train,
            optimizer, device, epoch,
            args.clip_max_norm, logger=writer,
            postprocessors=postprocessors,
            num_keypoints=args.num_keypoints,
            visualize_folder=args.visualize_folder,
            json_file=args.json_file,
            )

        lr_scheduler.step()
        
        log.info(f'Saving model at epoch {epoch}...')
        checkpoint_paths = [output_dir / f'checkpoint{epoch:04}.pth']
        for checkpoint_path in checkpoint_paths:
            torch.save({
                'model': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'lr_scheduler': lr_scheduler.state_dict(),
                'epoch': epoch,
                'args': args,
            }, checkpoint_path)

        if epoch >= 2:
            to_remove = [output_dir / f'checkpoint{(epoch-2):04}.pth']
            for rm in to_remove:
                if os.path.isfile(rm):
                    os.remove(rm)

        if ((epoch + 1) % args.eval_n_epochs) == 0:

            log.info(f'Starting evaluation step for epoch {epoch}...')

            coco_evaluator = evaluate(
                model, criterion, postprocessors,
                data_loader_val,
                get_coco_api_from_dataset(dataset_val), 
                device, num_keypoints=args.num_keypoints,
                visualize_folder=args.visualize_folder,
                json_file=args.json_file,
            )

            if coco_evaluator is not None:
                AP = coco_evaluator.coco_eval['keypoints'].stats.tolist()[0]
                if(AP >= best_AP):
                    best_AP = AP
                    log.info('Saving best model...')
                    torch.save({
                        'model': model.state_dict(),
                        'optimizer': optimizer.state_dict(),
                        'lr_scheduler': lr_scheduler.state_dict(),
                        'epoch': epoch,
                        'args': args,
                    }, output_dir / f'best_model.pth')

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    log.info('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('DETR training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
