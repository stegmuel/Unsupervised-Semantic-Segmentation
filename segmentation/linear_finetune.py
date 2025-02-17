#
# Authors: Wouter Van Gansbeke & Simon Vandenhende
# Licensed under the CC BY-NC 4.0 license (https://creativecommons.org/licenses/by-nc/4.0/)

import argparse
import cv2
import os
import sys
import torch
import json
from pathlib import Path

from utils.config import update_config
from utils.common_config import get_train_dataset, get_train_transformations,\
                                get_val_dataset, get_val_transformations,\
                                get_train_dataloader, get_val_dataloader,\
                                get_optimizer, get_model, adjust_learning_rate
from utils.train_utils import train_segmentation_vanilla
from utils.evaluate_utils import eval_segmentation_supervised_online, save_results_to_disk,\
                                 eval_segmentation_supervised_offline
from termcolor import colored
from utils.logger import Logger
from utils.dino_utils import bool_flag
import time
import tarfile


def get_args_parser():
    parser = argparse.ArgumentParser('LC', add_help=False)
    parser.add_argument("--root_dir",
                        default='/home/thomas/Documents/phd/samno_paper/Unsupervised-Semantic-Segmentation/output/LC',
                        type=str)
    parser.add_argument("--train_db_name", default="VOCSegmentation", type=str)
    parser.add_argument("--data_path", default='/media/thomas/Samsung_T5/VOC12/VOCdevkit/VOC2012/', type=str)
    parser.add_argument("--split", default='trainaug', type=str)
    parser.add_argument("--batch_size", default=16, type=int)
    parser.add_argument("--val_db_name", default="VOCSegmentation")
    parser.add_argument("--num_workers", default=4, type=int)
    parser.add_argument("--backbone", default='vit', type=str)
    parser.add_argument("--dilated", default=False, type=bool_flag)
    parser.add_argument("--resnet_dilate", default=1, type=int)
    parser.add_argument("--head", default='linear', type=str)
    parser.add_argument("--arch", default='vit_small', type=str)
    parser.add_argument("--patch_size", default=16, type=int)
    parser.add_argument("--checkpoint_key", default='teacher', type=str)
    parser.add_argument("--pretraining",
                        default='/home/thomas/Documents/phd/samno_paper/samno/output/dino_deitsmall16_pretrain_full_checkpoint.pth', type=str)
    parser.add_argument("--epochs", default=60, type=int)
    parser.add_argument("--optimizer", default='adam', type=str)
    parser.add_argument("--lr", default=0.1, type=float)
    parser.add_argument("--freeze_batchnorm", default='all', type=str)
    parser.add_argument('--crf_postprocess', default=False, help='Apply CRF post-processing during evaluation')
    # parser.add_argument('--upsample_size', default=320, help='Apply CRF post-processing during evaluation')
    parser.add_argument('--embeddings_upsample', default=448, help='')
    parser.add_argument('--masks_upsample', default=448, help='')
    parser.add_argument("--n_last_blocks", default=2, type=int)
    parser.add_argument("--untar_path", default="", type=str)
    return parser


def untar_to_dst(untar_path, src):
    assert (untar_path != "")
    if untar_path[0] == '$':
        untar_path = os.environ[untar_path[1:]]
    start_copy_time = time.time()

    job_dir = os.path.join(untar_path, "data", os.environ["SLURM_JOB_ID"])
    Path(job_dir).mkdir(exist_ok=True, parents=True)

    with tarfile.open(src, 'r') as f:
        f.extractall(job_dir)
    print('Time taken for untar:', time.time() - start_copy_time)
    return job_dir


dataset_dict = {
    'VOCSegmentation': 'VOC12/VOCdevkit/VOC2012',
    'coco_thing': 'coco',
    'coco_stuff': 'coco'
}


def main(args):
    p = vars(args)
    cv2.setNumThreads(1)

    # Untar if needed
    if p['data_path'].endswith('tar'):
        untar_path = untar_to_dst(p['untar_path'], p['data_path'])
        p['data_path'] = os.path.join(untar_path, dataset_dict[p['train_db_name']])

    # Retrieve config file
    p = update_config(p)
    sys.stdout = Logger(p['log_file'])
    print('Python script is {}'.format(os.path.abspath(__file__)))
    print(colored(p, 'red'))

    # Get model
    print(colored('Retrieve model', 'blue'))
    model = get_model(p)
    model = model.cuda()

    # Freeze all layers except final 1 by 1 convolutional layer
    for param in model.backbone.parameters():
        param.requires_grad = False

    # Get criterion
    print(colored('Get loss', 'blue'))
    criterion = torch.nn.CrossEntropyLoss(ignore_index=255)
    criterion.cuda()
    print(criterion)

    # CUDNN
    print(colored('Set CuDNN benchmark', 'blue'))
    torch.backends.cudnn.benchmark = True

    # Optimizer
    print(colored('Retrieve optimizer', 'blue'))
    parameters = list(filter(lambda p: p.requires_grad, model.parameters()))
    assert len(parameters) == 2  # decoder.4.weight, decoder.4.bias
    optimizer = get_optimizer(p, parameters)
    print(optimizer)

    # Dataset
    print(colored('Retrieve dataset', 'blue'))
    train_transforms = get_train_transformations()
    val_transforms = get_val_transformations()
    train_dataset = get_train_dataset(p, train_transforms)
    val_dataset = get_val_dataset(p, val_transforms)
    true_val_dataset = get_val_dataset(p, None) # True validation dataset without reshape - For validation.
    train_dataloader = get_train_dataloader(p, train_dataset)
    val_dataloader = get_val_dataloader(p, val_dataset)
    print(colored('Train samples %d - Val samples %d' %(len(train_dataset), len(val_dataset)), 'yellow'))

    # Resume from checkpoint
    if os.path.exists(p['checkpoint']):
        print(colored('Restart from checkpoint {}'.format(p['checkpoint']), 'blue'))
        checkpoint = torch.load(p['checkpoint'], map_location='cpu')
        optimizer.load_state_dict(checkpoint['optimizer'])
        model.load_state_dict(checkpoint['model'])
        model.cuda()
        start_epoch = checkpoint['epoch']
        best_epoch = checkpoint['best_epoch']
        best_iou = checkpoint['best_iou']

    else:
        print(colored('No checkpoint file at {}'.format(p['checkpoint']), 'blue'))
        start_epoch = 0
        best_epoch = 0
        best_iou = 0
        model = model.cuda()

    # Main loop
    print(colored('Starting main loop', 'blue'))

    for epoch in range(start_epoch, p['epochs']):
        print(colored('Epoch %d/%d' %(epoch+1, p['epochs']), 'yellow'))
        print(colored('-'*10, 'yellow'))

        # Adjust lr
        # lr = adjust_learning_rate(p, optimizer, epoch)
        # print('Adjusted learning rate to {:.5f}'.format(lr))

        # Train 
        print('Train ...')
        eval_train = train_segmentation_vanilla(p, train_dataloader, model, criterion, optimizer, epoch,
                                                freeze_batchnorm=p['freeze_batchnorm'])

        # Evaluate online -> This will use batched eval where every image is resized to the same resolution.
        print('Evaluate ...')
        eval_val = eval_segmentation_supervised_online(p, val_dataloader, model)
        if eval_val['mIoU'] > best_iou:
            print('Found new best model: %.2f -> %.2f (mIoU)' %(100*best_iou, 100*eval_val['mIoU']))
            best_iou = eval_val['mIoU']
            best_epoch = epoch
            torch.save(model.state_dict(), p['best_model'])
        
        else:
            print('No new best model: %.2f -> %.2f (mIoU)' %(100*best_iou, 100*eval_val['mIoU']))
            print('Last best model was found in epoch %d' %(best_epoch))

        # Checkpoint
        print('Checkpoint ...')
        torch.save({'optimizer': optimizer.state_dict(), 'model': model.state_dict(), 
                    'epoch': epoch + 1, 'best_epoch': best_epoch, 'best_iou': best_iou}, 
                    p['checkpoint'])

    # Evaluate best model at the end -> This will evaluate the predictions on the original resolution.
    print(colored('Evaluating best model at the end', 'blue'))
    model.load_state_dict(torch.load(p['best_model']))
    save_results_to_disk(p, val_dataloader, model, crf_postprocess=args.crf_postprocess)
    eval_stats = eval_segmentation_supervised_online(p, val_dataloader, model)

    # Write the full results
    with open(os.path.join(p['output_dir'], 'full_results.txt'), 'w') as file:
        file.write(json.dumps(eval_stats))

    # Write only the mIoU
    with open(os.path.join(p['output_dir'], 'mIoU_results.txt'), 'w') as file:
        file.write(f"mIoU: {100 * eval_stats['mIoU']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser('LC', parents=[get_args_parser()])
    args = parser.parse_args()
    main(args)
