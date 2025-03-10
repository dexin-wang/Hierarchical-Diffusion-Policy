from typing import List, Dict, Optional
import numpy as np
import gym
from gym.spaces import Box
from robomimic.envs.env_robosuite import EnvRobosuite
from hiera_diffusion_policy.common.visual import getFingersPos


def updateState(raw_obs, obs_keys):
    """将 robot0_gripper_qpos 替换为 finger pos"""
    fl_pos, fr_pos = getFingersPos(
            raw_obs['robot0_eef_pos'], 
            raw_obs['robot0_eef_quat'], 
            raw_obs['robot0_gripper_qpos'][0]+0.0145/2,
            raw_obs['robot0_gripper_qpos'][1]-0.0145/2
            )
    # obs = np.concatenate(
    #     [raw_obs[key][:7] for key in obs_keys[:-1]] + [fl_pos, fr_pos], 
    #     axis=0)
    
    obs = np.concatenate(
        [raw_obs[key] for key in obs_keys[:-1]] + [fl_pos, fr_pos], 
        axis=0)
    
    # obs = np.concatenate(
    #     [raw_obs[key] for key in obs_keys
    #     ], axis=0)

    # obs = np.concatenate(
    #     [raw_obs[key][:7] for key in obs_keys], 
    #     axis=0)

    return obs


class RobomimicPcdWrapper(gym.Env):
    def __init__(self, 
        env: EnvRobosuite,
        obs_keys: List[str]=[
            'object', 
            'robot0_eef_pos', 
            'robot0_eef_quat', 
            'robot0_gripper_qpos'],
        init_state: Optional[np.ndarray]=None,
        render_hw=(256,256),
        render_camera_name='agentview'
        ):

        self.env = env
        self.obs_keys = obs_keys
        self.init_state = init_state
        self.render_hw = render_hw
        self.render_camera_name = render_camera_name
        self.seed_state_map = dict()
        self._seed = None
        
        # setup spaces
        low = np.full(env.action_dimension, fill_value=-1)
        high = np.full(env.action_dimension, fill_value=1)
        self.action_space = Box(
            low=low,
            high=high,
            shape=low.shape,
            dtype=low.dtype
        )
        obs_example = self.get_observation()
        low = np.full_like(obs_example, fill_value=-1)
        high = np.full_like(obs_example, fill_value=1)
        # 在Nonprehensile任务中，改成维度为包括 object pose/gripper pose/finger position
        self.observation_space = Box(
            low=low,
            high=high,
            shape=low.shape,
            dtype=low.dtype
        )
  
    def pcd_goal(self):
        return self.env.pcd_goal()


    def get_observation(self):
        """
        获取flatten的观测数据
        """
        raw_obs = self.env.get_observation()
        obs = updateState(raw_obs, self.obs_keys)
        return obs

    def seed(self, seed=None):
        np.random.seed(seed=seed)
        self._seed = seed
    
    def reset(self):
        if self.init_state is not None:
            # always reset to the same state
            # to be compatible with gym
            self.env.reset_to({'states': self.init_state})
        elif self._seed is not None:
            # reset to a specific seed
            seed = self._seed
            if seed in self.seed_state_map:
                # env.reset is expensive, use cache
                self.env.reset_to({'states': self.seed_state_map[seed]})
            else:
                # robosuite's initializes all use numpy global random state
                np.random.seed(seed=seed)
                self.env.reset()
                state = self.env.get_state()['states']
                self.seed_state_map[seed] = state
            self._seed = None
        else:
            # random reset
            self.env.reset()

        # return obs
        obs = self.get_observation()
        return obs
    
    def step(self, action):
        raw_obs, reward, done, info = self.env.step(action)

        obs = updateState(raw_obs, self.obs_keys)
        return obs, reward, done, info
    
    def render(self, mode='rgb_array'):
        h, w = self.render_hw
        return self.env.render(mode=mode, 
            height=h, width=w, 
            camera_name=self.render_camera_name)
