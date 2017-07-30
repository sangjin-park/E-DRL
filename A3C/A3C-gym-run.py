#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: run-atari.py
# Author: Yuxin Wu <ppwwyyxxc@gmail.com>

import numpy as np
import tensorflow as tf
import os, sys, re, time
import random
import argparse
import six

from tensorpack import *
from tensorpack.RL import *
import gym

IMAGE_SIZE = (84, 84)
FRAME_HISTORY = 4
CHANNEL = FRAME_HISTORY# * 3
IMAGE_SHAPE3 = IMAGE_SIZE + (CHANNEL,)

NUM_ACTIONS = None
ENV_NAME = None

from common import play_one_episode

def get_player(dumpdir=None):
    pl = GymEnv(ENV_NAME, dumpdir=dumpdir, auto_restart=False)
    def resize(img):
        return cv2.resize(img, IMAGE_SIZE)
    def grey(img):
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img = resize(img)
        img = img[:, :, np.newaxis]
        return img
    pl = MapPlayerState(pl, grey)


    global NUM_ACTIONS
    NUM_ACTIONS = pl.get_action_space().num_actions()

    pl = HistoryFramePlayer(pl, FRAME_HISTORY)
    return pl

class Model(ModelDesc):
    def _get_input_vars(self):
        assert NUM_ACTIONS is not None
        return [InputVar(tf.float32, (None,) + IMAGE_SHAPE3, 'state'),
                InputVar(tf.int32, (None,), 'action'),
                InputVar(tf.float32, (None,), 'futurereward') ]

    def _get_NN_prediction(self, image):
        image = image / 255.0
        with argscope(Conv2D, nl=tf.nn.relu):
            # l = Conv2D('conv0', image, out_channel=32, kernel_shape=5)
            # l = MaxPooling('pool0', l, 2)
            # l = Conv2D('conv1', l, out_channel=32, kernel_shape=5)
            # l = MaxPooling('pool1', l, 2)
            # l = Conv2D('conv2', l, out_channel=64, kernel_shape=4)
            # l = MaxPooling('pool2', l, 2)
            # l = Conv2D('conv3', l, out_channel=64, kernel_shape=3)
            l = Conv2D('conv0', image, out_channel=32, kernel_shape=8, stride=4)
            l = Conv2D('conv1', l, out_channel=64, kernel_shape=4, stride=2)
            l = Conv2D('conv2', l, out_channel=64, kernel_shape=3)

        l = FullyConnected('fc0', l, 512, nl=tf.identity)
        l = PReLU('prelu', l)
        policy = FullyConnected('fc-pi', l, out_dim=NUM_ACTIONS, nl=tf.identity)
        return policy

    def _build_graph(self, inputs):
        state, action, futurereward = inputs
        policy = self._get_NN_prediction(state)
        self.logits = tf.nn.softmax(policy, name='logits')

def run_submission(cfg, output, nr):
    player = get_player(dumpdir=output)
    predfunc = get_predict_func(cfg)
    for k in range(nr):
        if k != 0:
            player.restart_episode()
        score = play_one_episode(player, predfunc, verbose=False)
        print("Total:", score)
    player.finish()

def do_submit(output, api_key):
    gym.upload(output, api_key=api_key)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.') # nargs='*' in multi mode
    parser.add_argument('--load', help='load model', required=True)
    parser.add_argument('--env', help='environment name', required=True)
    parser.add_argument('--episode', help='number of episodes to run',
            type=int, default=100)
    parser.add_argument('--output', help='output directory', default='gym-submit')
    parser.add_argument('--api', help='submission API', default=None)
    parser.add_argument('--submit', help='Just submit', default=False)
    args = parser.parse_args()

    ENV_NAME = args.env
    assert ENV_NAME
    logger.info("Environment Name: {}".format(ENV_NAME))
    p = get_player(); del p    # set NUM_ACTIONS

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    if args.submit == 't':
        logger.info("Submitting to gym")
        do_submit(args.output, args.api)
        exit()


    cfg = PredictConfig(
            model=Model(),
            session_init=SaverRestore(args.load),
            input_var_names=['state'],
            output_var_names=['logits'])
    run_submission(cfg, args.output, args.episode)
    do_submit(args.output, args.api)
