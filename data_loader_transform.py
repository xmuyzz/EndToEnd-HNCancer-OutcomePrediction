import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import gc
import nibabel as nib
import torch
import torchtuples as tt
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torchvision import datasets, transforms
from torchvision.utils import save_image
import torchvision.models as models
from pycox.datasets import metabric
from pycox.evaluation import EvalSurv
from pycox.models import PCHazard, CoxPH, LogisticHazard, DeepHitSingle
from pycox.utils import kaplan_meier
from pycox.models.data import pair_rank_mat, _pair_rank_mat
import monai
from monai.utils import first
from monai.transforms import (AddChannel, AsChannelFirst, EnsureChannelFirst, RepeatChannel,
    ToTensor, RemoveRepeatedChannel, EnsureType, Compose, CropForeground, LoadImage,
    Orientation, RandSpatialCrop, Spacing, Resize, ScaleIntensity, RandRotate, RandZoom,
    RandGaussianNoise, RandGaussianSharpen, RandGaussianSmooth, RandFlip, Rotate90, RandRotate90, 
    EnsureType, RandAffine)
from get_data.get_dataset import get_dataset



def data_prep(pro_data_dir, batch_size, _cox_model, num_durations, _outcome_model,
              tumor_type, input_data_type, i_kfold):
    
    """
    Prerpocess image and lable for DataLoader
    Args:
        batch_size {int} -- batch size for data loading;
        _cox_model {str} -- cox model name;
        number_durations {int} -- number to discretize survival time;
    Returns:
        Dataloaders for train, tune and val datasets;
    """    
    
    ## load train and val dataset
    fns_train, fns_val, fn_test = get_dataset(
        tumor_type=tumor_type, 
        input_data_type=input_data_type)
    fn_train = fns_train[i_kfold]
    fn_val = fns_val[i_kfold]
    df_train_ = pd.read_csv(os.path.join(pro_data_dir, fn_train))
    df_train = df_train_.sample(frac=0.9, random_state=200)
    df_tune = df_train_.drop(df_train.index)
    df_val = pd.read_csv(os.path.join(pro_data_dir, fn_val))
    df_test = pd.read_csv(os.path.join(pro_data_dir, fn_test))
    print('df_train shape:', df_train.shape)
    print('df_tune shape:', df_tune.shape)
    print('df_val shape:', df_val.shape)
    print('df_test shape:', df_test.shape)

    if _cox_model == 'PCHazard':
        labtrans = PCHazard.label_transform(num_durations)
    elif _cox_model == 'LogisticHazard':
        labtrans = LogisticHazard.label_transform(num_durations)
    elif _cox_model == 'DeepHit':
        labtrans = DeepHitSingle.label_transform(num_durations)
    else:
        print('choose other cox models!')
    
    """
    Outcome prediction model: 
        1) overall survival: death_time, death_event; 
        2) local/regional control: lr_time, lr_event;
        3) distant control: ds_time, ds_event;
    """
    if _outcome_model == 'overall_survival':
        get_target = lambda df: (df['death_time'].values, df['death_event'].values)
    elif _outcome_model == 'local_control':
        get_target = lambda df: (df['lr_time'].values, df['lr_event'].values)
    elif _outcome_model == 'distant_control':
        get_target = lambda df: (df['ds_time'].values, df['ds_event'].values)
    # label transform 
    dfs = []
    for df in [df_train, df_tune, df_val, df_test]:
        if _cox_model in ['PCHazard', 'LogisticHazard', 'DeepHit']:
            #labtrans = PCHazard.label_transform(num_durations)
            y = labtrans.fit_transform(*get_target(df))
            #y = labtrans.transform(*get_target(df))
            out_features = labtrans.out_features
            duration_index = labtrans.cuts
            #print('y[0]:', y[0])
            #print('y[1]:', y[1])
        elif _cox_model == 'CoxPH':
            y = get_target(df)
            out_features = 1
        # create new df
        df['time'], df['event'] = [y[0], y[1]]
        dfs.append(df)
    df_train = dfs[0]
    df_tune = dfs[1]
    df_val = dfs[2]
    df_test = dfs[3]
    #print('out_features:', out_features)
    #print('duration_index:', duration_index)
    #print(labtrans.cuts[y_train[0]])
    # save duration index for train and evaluate steps
    if not os.path.exists(os.path.join(pro_data_dir, 'duration_index.npy')):
        np.save(os.path.join(pro_data_dir, 'duration_index.npy'), duration_index)
     
    return df_train, df_tune, df_val, df_test


def data_loader_transform(pro_data_dir, batch_size, _cox_model, num_durations, 
                          _outcome_model, tumor_type, input_data_type, i_kfold):
    
    """
    DataLoader with image augmentation using Pycox/TorchTuple and Monai packages.

    Args:
        _cox_model {str} -- cox model name;
        _outcome_model {str} -- outcome model name ('os|lrc|dc');

    Retunrs:
        data loader with real time augmentation;
    """

    # image transforma with MONAI
    train_transforms = Compose([
        #EnsureChannelFirst(),
        #ScaleIntensity(minv=0.0, maxv=1.0),
        #Resized(spatial_size=(96, 96, 96)),
        RandGaussianNoise(prob=0.1, mean=0.0, std=0.1),
        RandGaussianSharpen(),
        RandGaussianSmooth(),
        RandAffine(prob=0.5, translate_range=10),
        RandFlip(prob=0.5, spatial_axis=None),
        RandRotate(prob=0.5, range_x=5, range_y=5, range_z=5),
        EnsureType(data_type='tensor')
        ])
    tune_transforms = Compose([
        #AddChannel,
        #EnsureChannelFirst(),
        #ScaleIntensity(minv=0.0, maxv=1.0),
        #RandGaussianNoise(prob=0.1, mean=0.0, std=0.1),
        #RandAffine(prob=0.5, translate_range=10),
        EnsureType(data_type='tensor')
        ])
    val_transforms = Compose([
        #AddChannel,
        #EnsureChannelFirst(),
        #ScaleIntensity(minv=0.0, maxv=1.0),
        EnsureType(data_type='tensor')
        ])
    test_transforms = Compose([
        #AddChannel,
        #EnsureChannelFirst(),
        ScaleIntensity(minv=0.0, maxv=1.0),
        EnsureType(data_type='tensor')
        ])

    # get dataset for train, tune and val
    df_train, df_tune, df_val, df_test = data_prep(
        pro_data_dir=pro_data_dir,
        batch_size=batch_size,
        _cox_model=_cox_model,
        num_durations=num_durations,
        _outcome_model=_outcome_model,
        tumor_type=tumor_type,
        input_data_type=input_data_type, 
        i_kfold=i_kfold)

    # train, tune, val dataset
    if _cox_model in ['CoxPH']:
        dataset_train = DatasetCoxPH(df=df_train, transform=train_transforms)
        dataset_tune = DatasetCoxPH(df=df_tune, transform=tune_transforms)
    elif _cox_model in ['PCHazard', 'LogisticHazard']:
        dataset_train = Dataset0(df=df_train, transform=train_transforms)
        dataset_tune = Dataset0(df=df_tune, transform=tune_transforms)
    elif _cox_model in ['DeepHit']:
        dataset_train = DatasetDeepHit(df=df_train, transform=train_transforms)
        dataset_tune = DatasetDeepHit(df=df_tune, transform=tune_transforms)
    else:
        print('choose another cox model!')
    
    dataset_tune_cb = DatasetPred(df_tune, transform=val_transforms)
    dataset_val = DatasetPred(df_val, transform=val_transforms)
    dataset_test = DatasetPred(df_test, transform=val_transforms)
    # check data
    #check_loader = DataLoader(dataset_train, batch_size=1)
    #check_data = first(check_loader)
    #print('\ncheck image and lable shape:', check_data)

    # batch data loader using Pycox and TorchTuple
    dl_train = DataLoader(
        dataset=dataset_train, 
        batch_size=batch_size, 
        shuffle=True, 
        collate_fn=collate_fn)
    dl_tune = DataLoader(
        dataset=dataset_tune, 
        batch_size=batch_size, 
        shuffle=False, 
        collate_fn=collate_fn)
    # tuning set dataloader for c-index callback
    dl_tune_cb = DataLoader(
        dataset=dataset_tune_cb,
        batch_size=batch_size,
        shuffle=False)
    dl_val = DataLoader(
        dataset=dataset_val,
        batch_size=batch_size,
        shuffle=False)
    dl_test = DataLoader(
        dataset=dataset_test,
        batch_size=batch_size,
        shuffle=False)
    #print('successfully created data loaders!')

    return dl_train, dl_tune, dl_val, dl_test, dl_tune_cb, df_tune



def collate_fn(batch):
    """
    Stacks the entries of a nested tuple
    """
    return tt.tuplefy(batch).stack()


class DatasetPCHazard():
    """
    Dataset class for PCHazard model
    Includes image and lables for training and tuning dataloader
    """
    def __init__(self, data, idx_duration, event, t_frac):
        self.data = data
        self.idx_duration, self.event, self.t_frac = tt.tuplefy(
            idx_duration, event, t_frac).to_tensor()
    def __len__(self):
        return self.data.shape[0]
    def __getitem__(self, index):
        img = self.data[index]
        return img, (self.idx_duration[index], self.event[index], self.t_frac[index])


class Dataset0(Dataset):
    
    """Dataset class for Logistic Hazard model
    """
    def __init__(self, df, transform, target_transform=None):
        self.df = df
        self.img_dir = df['img_dir'].to_list()
        #self.time, self.event = tt.tuplefy(
        #    df['sur_duration'].to_numpy(), 
        #    df['survival'].to_numpy()).to_tensor()
        self.time = torch.from_numpy(df['time'].to_numpy())
        self.event = torch.from_numpy(df['event'].to_numpy())
        self.transform = transform
        self.target_transform = target_transform

    def __len__(self):
        #print('event size:', self.event.size(dim=0))
        return self.event.shape[0]

    def __getitem__(self, index):
        if type(index) is not int:
            raise ValueError(f'Need `index` to be `int`. Got {type(index)}.')
        img = nib.load(self.img_dir[index])
        arr = img.get_data()
        # choose image channel
        #img = np.broadcast_to(arr, (3, arr.shape[0], arr.shape[1], arr.shape[2]))
        img = arr.reshape(1, arr.shape[0], arr.shape[1], arr.shape[2])
        #if self.channel == 1:
        #    img = arr.reshape(arr.shape[0], arr.shape[1], arr.shape[2], 1)
        #elif self.channel == 3:
        #    img = np.broadcast_to(arr, (3, arr.shape[0], arr.shape[1], arr.shape[2]))
        # convert numpy array to torch tensor
        #img = torch.from_numpy(img).float()
        # data and label transform
        #print(arr.shape)
        if self.transform:
            img = self.transform(img)
        if self.target_transform:
            label = self.target_transform(label)
        return img, (self.time[index], self.event[index])


class DatasetPred(Dataset):
    
    """Dataset class for CoxPH model
    """
    def __init__(self, df, channel=3, transform=None):
        self.img_dir = df['img_dir'].to_list()
        self.transform = transform

    def __len__(self):
        print('data size:', len(self.img_dir))
        return len(self.img_dir)

    def __getitem__(self, index):
        if type(index) is not int:
            raise ValueError(f'Need `index` to be `int`. Got {type(index)}.')
        img = nib.load(self.img_dir[index])
        arr = img.get_data()
        img = arr.reshape(1, arr.shape[0], arr.shape[1], arr.shape[2])
        # choose image channel
        #if self.channel == 1:
        #    img = arr.reshape(arr.shape[0], arr.shape[1], arr.shape[2], 1)
        #elif self.channel == 3:
        #    img = np.broadcast_to(arr, (3, arr.shape[0], arr.shape[1], arr.shape[2]))
        #img = np.broadcast_to(arr, (3, arr.shape[0], arr.shape[1], arr.shape[2]))
        #img = arr.reshape(arr.shape[0], arr.shape[1], arr.shape[2], 1)
        # convert numpy array to torch tensor
        #img = torch.from_numpy(img).float()
        # data and label transform
        if self.transform:
            img = self.transform(img)
        return img


class DatasetDeepHit(Dataset):
    
    """Dataset class for DeepHit model
    """
    def __init__(self, df, transform, target_transform=None):
        self.df = df
        self.transform = transform
        self.target_transform = target_transform

    def __len__(self):
        event = self.df['event'].to_numpy()
        return event.shape[0]

    def __getitem__(self, index):
        if type(index) is not int:
            raise ValueError(f'Need `index` to be `int`. Got {type(index)}.')
        # image
        img_dir = self.df['img_dir'].to_list()
        img = nib.load(img_dir[index])
        arr = img.get_data()
        img = arr.reshape(1, arr.shape[0], arr.shape[1], arr.shape[2])
        # target
        time = self.df['time'].to_numpy()
        event = self.df['event'].to_numpy()
        rank_mat = pair_rank_mat(time[index], event[index])
        time = torch.from_numpy(time)
        event = torch.from_numpy(event)
        rank_mat = torch.from_numpy(rank_mat)
        if self.transform:
            img = self.transform(img)
        if self.target_transform:
            label = self.target_transform(label)
        
        return img, (time[index], event[index], rank_mat)


class DatasetCoxPH(Dataset):
    
    """Dataset class for CoxPH method
    """
    def __init__(self, df, transform=None, target_transform=None):
        self.df = df
        self.transform = transform
        self.target_transform = target_transform

    def __len__(self):
        event = self.df['event'].to_numpy()
        return event.shape[0]

    def __getitem__(self, index):
        assert type(index) is int
        # image
        img_dir = self.df['img_dir'].to_list()
        img = nib.load(img_dir[index])
        arr = img.get_data()
        img = arr.reshape(1, arr.shape[0], arr.shape[1], arr.shape[2])
        # target
        time = self.df['time'].to_numpy()
        event = self.df['event'].to_numpy()
        time = torch.from_numpy(time)
        event = torch.from_numpy(event)
        if self.transform:
            img = self.transform(img)
        if self.target_transform:
            label = self.target_transform(label)

        return img, (time[index], event[index])


