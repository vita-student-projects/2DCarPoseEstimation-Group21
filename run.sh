#!/bin/bash
#SBATCH --chdir /home/plumey/ProjectV3
#SBATCH --nodes 1
#SBATCH --ntasks 1
#SBATCH --cpus-per-task 4
#SBATCH --mem 16G
#SBATCH --partition gpu
#SBATCH --gres gpu:1
#SBATCH --qos dlav
#SBATCH --account civil-459-2023
#SBATCH --time 24:00:00

source ../venvs/venv-g21/bin/activate
python3 train.py poet --batch_size 16 --pretrained_poet snapshots/poet_ckpt.pth --apply_augmentation --apply_occlusion_augmentation