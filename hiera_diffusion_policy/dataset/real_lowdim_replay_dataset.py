from typing import Dict, List
import torch
import numpy as np
import h5py
from tqdm import tqdm
import json
import copy
import hiera_diffusion_policy.common.transformation as tf
from hiera_diffusion_policy.common.robot import get_subgoals_stage_real
from hiera_diffusion_policy.common.visual import visual_subgoals_real_v6
from hiera_diffusion_policy.common.pytorch_util import dict_apply
from hiera_diffusion_policy.dataset.base_dataset import BasePcdDataset
from hiera_diffusion_policy.model.common.normalizer import Normalizer
from hiera_diffusion_policy.model.common.rotation_transformer import RotationTransformer
from hiera_diffusion_policy.common.replay_buffer import ReplayBuffer
from hiera_diffusion_policy.common.sampler import (
    SequenceSampler, get_val_mask, downsample_mask)
from hiera_diffusion_policy.common.normalize_util import (
    robomimic_abs_action_only_normalizer_from_stat,
    robomimic_abs_action_only_dual_arm_normalizer_from_stat,
    get_identity_normalizer_from_stat,
    array_to_stats
)


class RealReplayDataset(BasePcdDataset):
    def __init__(self,
            dataset_path: str,
            observation_history_num=2,
            use_subgoal=True,
            horizon=1,  # 16
            pad_before=0,   # 1
            pad_after=0,    # 7
            max_train_episodes=None,
            abs_action=False,   # True
            rotation_rep='rotation_6d',
            seed=42,
            Tr=1,
            val_ratio=0.02
        ):
        rotation_transformer = RotationTransformer(
            from_rep='axis_angle', to_rep=rotation_rep)
        
        replay_buffer = ReplayBuffer.create_empty_numpy()
        with h5py.File(dataset_path) as file:
            demos = file['data']

            if abs_action and 'absactions' in demos['demo_0']:
                action_key = 'absactions'
            else:
                action_key = 'actions'
            
            print('action_key =', action_key)


            # 遍历轨迹，获取 state / action
            for i in tqdm(range(len(demos)), desc="Loading hdf5 to ReplayBuffer"):
                demo = demos[f'demo_{i}']
                # get state / action
                data = _data_to_obs(
                    raw_obs=demo['obs'],
                    raw_actions=demo[action_key][:].astype(np.float32),
                    abs_action=abs_action,
                    rotation_transformer=rotation_transformer,
                    Tr=Tr)

                if use_subgoal:
                    # get subgoals and reward
                    subgoals = get_subgoals_stage_real(
                        demo['obs']['state'],
                        fin_rad=0.008,
                        contact_state=demo['obs']['contact'],
                        reward_mode='only_success',
                        Tr=Tr)
                    data.update(subgoals)

                    #* 可视化每个状态的子目标
                    # visual_subgoals_real_v6(
                    #     state=data['state'],
                    #     subgoal=subgoals['subgoal'],
                    #     reward=subgoals['reward'],
                    #     pcd=data['pcd'],
                    #     action=data['action'])
                    
                replay_buffer.add_episode(data)
                
        val_mask = get_val_mask(    # shape=(n_episodes,)  用于测试的episode为1，用于训练的为0
            n_episodes=replay_buffer.n_episodes,
            val_ratio=val_ratio,
            seed=seed)
        train_mask = ~val_mask

        if max_train_episodes is not None:
            print(f'Use {max_train_episodes} demos to train!')
        else:
            print('Use all demos to train!')
        train_mask = downsample_mask(   # train_mask的shape不变，1的数量等于max_train_episodes
            mask=train_mask, 
            max_n=max_train_episodes, 
            seed=seed)

        sampler = SequenceSampler(
            replay_buffer=replay_buffer, 
            abs_action=abs_action,
            sequence_length=horizon,
            pad_before=pad_before, 
            pad_after=pad_after,
            episode_mask=train_mask)
        
        self.replay_buffer = replay_buffer
        self.use_subgoal = use_subgoal
        self.observation_history_num = observation_history_num
        self.sampler = sampler
        self.abs_action = abs_action
        self.train_mask = train_mask
        self.val_mask = val_mask
        self.horizon = horizon
        self.pad_before = pad_before
        self.pad_after = pad_after
        self.Tr = Tr

    
    def get_validation_dataset(self):
        val_set = copy.copy(self)
        val_set.sampler = SequenceSampler(
            replay_buffer=self.replay_buffer, 
            abs_action=self.abs_action,
            sequence_length=self.horizon,
            pad_before=self.pad_before, 
            pad_after=self.pad_after,
            episode_mask=self.val_mask
            )
        val_set.train_mask = self.val_mask
        return val_set


    def get_normalizer(self) -> Normalizer:
        normalizer = Normalizer()
        # action
        action_stat = array_to_stats(self.replay_buffer['action'])
        # if self.abs_action:
        action_params = robomimic_abs_action_only_normalizer_from_stat(action_stat)
        # else:
        #     # already normalized
        #     action_params = get_identity_normalizer_from_stat(action_stat)
        normalizer.params_dict['action'] = action_params
        
        # state
        # offset为0，scale的所有元素相同
        state_stat = array_to_stats(self.replay_buffer['state']) # 归一化参数去除物体位姿
        normalizer.params_dict['state'] = normalizer_from_stat(state_stat)

        return normalizer
    
    def __len__(self):
        return len(self.sampler)

    def _sample_to_data(self, sample, i):
        data = {
            'id': np.array([i,]),

            'state': sample['data']['state'][:self.observation_history_num],  # (n, 27)
            'action': sample['data']['action'],

            'next_state': sample['data']['next_state'][:self.observation_history_num],  # (n, 27)
            'next_action': sample['data']['next_action'],
        }
        if self.use_subgoal:
            subgoal_data = {
                'subgoal': sample['data']['subgoal'][self.observation_history_num-1],   # (8,)
                'next_subgoal': sample['data']['next_subgoal'][self.observation_history_num-1],   # (8,)
                'reward': sample['data']['reward'][self.observation_history_num-1:self.observation_history_num],
            }
            data.update(subgoal_data)

        return data


    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        sample = self.sampler.sample_sequence(idx)
        data = self._sample_to_data(sample, idx)
        torch_data = dict_apply(data, torch.from_numpy)
        return torch_data

def normalizer_from_stat(stat):
    max_abs = np.maximum(stat['max'].max(), np.abs(stat['min']).max())
    scale = np.full_like(stat['max'], fill_value=1/max_abs)
    offset = np.zeros_like(stat['max'])
    return Normalizer.create_manual(
        scale=scale,
        offset=offset,
        input_stats_dict=stat
    )
    

def _data_to_obs(raw_obs, raw_actions, abs_action, rotation_transformer, Tr):
    """
    args:
        raw_obs: h5py dict {state}
        raw_actions: np.ndarray shape=(N, A) N为当前轨迹长度，A为action维度

    return: Dict
        `state`: (N, S) S为需要的观测合并的维度： 机械臂末端位姿/两个手指的位置
        `action`: (N, A) 
        `next_state`: (N, A) 
    """
    state = raw_obs['state']

    if abs_action:
        pos = raw_actions[...,:3]
        rot = raw_actions[...,3:6]  
        gripper = raw_actions[...,6:]
        rot = rotation_transformer.forward(rot)
        raw_actions = np.concatenate([
            pos, rot, gripper
        ], axis=-1).astype(np.float32)

    data = {
        'state': state[:-Tr],
        'action': raw_actions[:-Tr],

        'next_state': state[Tr:],
        'next_action': raw_actions[Tr:],
    }
    return data


