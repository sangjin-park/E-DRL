#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: train-atari.py
# Author: Yuxin Wu <ppwwyyxxc@gmail.com>, Music Lee <yuezhanl@andrew.cmu.edu>

import numpy as np
import tensorflow as tf
import os, sys, re, time
import random
import uuid
import argparse
import multiprocessing, threading
from collections import deque
import six
from six.moves import queue

from tensorpack.tfutils import symbolic_functions as symbf

import argparse
from tensorpack.predict.common import PredictConfig, get_predict_func
from tensorpack import *
from tensorpack.models.model_desc import ModelDesc, InputVar
from tensorpack.train.config import TrainConfig
from tensorpack.tfutils.sessinit import SessionInit, JustCurrentSession, NewSession
from tensorpack.tfutils.common import *
from tensorpack.tfutils.tower import get_current_tower_context
from tensorpack.callbacks.group import Callbacks
from tensorpack.callbacks.stat import StatPrinter
from tensorpack.callbacks.common import ModelSaver, Callback
from tensorpack.callbacks.param import ScheduledHyperParamSetter, HumanHyperParamSetter
from tensorpack.tfutils.summary import add_moving_summary, add_param_summary
from tensorpack.RL.expreplay import ExpReplay
from tensorpack.tfutils.sessinit import SaverRestore
from tensorpack.train.queue import QueueInputTrainer
from tensorpack.RL.common import MapPlayerState
from tensorpack.RL.gymenv import GymEnv
from tensorpack.RL.common import LimitLengthPlayer, PreventStuckPlayer
from tensorpack.RL.history import HistoryFramePlayer
from tensorpack.tfutils.argscope import argscope
from tensorpack.models.conv2d import Conv2D
from tensorpack.models.pool import MaxPooling
from tensorpack.models.nonlin import LeakyReLU, PReLU
from tensorpack.models.fc import FullyConnected
import tensorpack.tfutils.summary as summary
from tensorpack.tfutils.gradproc import MapGradient, SummaryGradient
from tensorpack.callbacks.graph import RunOp
from tensorpack.callbacks.base import PeriodicCallback
from tensorpack.predict.concurrency import MultiThreadAsyncPredictor
from tensorpack.utils.concurrency import ensure_proc_terminate, start_proc_mask_signal
from tensorpack.utils.gpu import get_nr_gpu
from tensorpack.dataflow.common import BatchData
from tensorpack.dataflow.raw import DataFromQueue
from tensorpack.train.multigpu import AsyncMultiGPUTrainer
from tensorpack.utils.serialize import dumps
import gym
import numpy as np
import common
from common import (play_model, Evaluator, eval_model_multithread)
from tensorpack.RL.simulator import SimulatorProcess, SimulatorMaster, TransitionExperience

IMAGE_SIZE = (84, 84)
FRAME_HISTORY = 4
GAMMA = 0.99
CHANNEL = FRAME_HISTORY# * 3
IMAGE_SHAPE3 = IMAGE_SIZE + (CHANNEL,)

LOCAL_TIME_MAX = 5
STEP_PER_EPOCH = 6000
EVAL_EPISODE = 10
BATCH_SIZE = 128
SIMULATOR_PROC = 50
PREDICTOR_THREAD_PER_GPU = 2
PREDICTOR_THREAD = None
EVALUATE_PROC = min(multiprocessing.cpu_count() // 2, 20)

NUM_ACTIONS = None
ENV_NAME = None
PC_METHOD = None # Pseudo count method
NETWORK_ARCH = None # Network Architecture
FEATURE = None
#FEATURE_EPOCH = None # If None, always use the up-to-date CNN
PC_MULT,PC_THRE,PC_TIME = None, None, None
POLICY_DIST = False # draw from policy distribution when testing, instead of epsilon greedy
# After testing, False results in better evaluation scores.
PC_ACTION = False
PC_DOWNSAMPLE_VALUE = None
PC_CLEAN = False
UCB1 = False

def get_player(viz=False, train=False, dumpdir=None):
    #TODO: (Next Plan)idea1 use CNN as features of our density model
    #TODO: idea1.5 clear counter in some intermeidate points
    #TODO: (on EXP now)idea2 time increasing with psuedo reward.  IF the pseudo reward is less than a threshold (e.g.,0.01) for most of the states, increase the pseudo reward.
    #TODO: (on EXP now)Not decrease Explore Factor after several epochs. The exp results show not enough exploration afterwards. But the scores are remained greatly.
    #TODO: (Read more papers)idea2.5: Intuition from people. Exploration and Exploitation modes. Remember the good rewards and turn into Exploitation modes, explore other possibilities.
    #TODO: (Done)Evaluate with policy probability
    if PC_METHOD and train:
        pl = GymEnv(ENV_NAME, dumpdir=dumpdir, pc_method=PC_METHOD, pc_mult=PC_MULT, pc_thre=PC_THRE, pc_time=PC_TIME, feature=FEATURE, pc_action=PC_ACTION, pc_downsample_value=PC_DOWNSAMPLE_VALUE, pc_clean=PC_CLEAN, UCB1=UCB1)
    else:
        pl = GymEnv(ENV_NAME, dumpdir=dumpdir)
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
    #if not train:
    #    pl = PreventStuckPlayer(pl, 30, 1)
    pl = LimitLengthPlayer(pl, 40000)
    return pl
common.get_player = get_player

class MySimulatorWorker(SimulatorProcess):
    def _build_player(self):
        return get_player(train=True)

class Model(ModelDesc):
    def _get_input_vars(self):
        assert NUM_ACTIONS is not None
        return [InputVar(tf.float32, (None,) + IMAGE_SHAPE3, 'state'),
                InputVar(tf.int64, (None,), 'action'),
                InputVar(tf.float32, (None,), 'futurereward') ]

    def _get_NN_prediction(self, image):
        image = image / 255.0
        with argscope(Conv2D, nl=tf.nn.relu):
            if NETWORK_ARCH == '1':
                l = Conv2D('conv0', image, out_channel=32, kernel_shape=5)
                l = MaxPooling('pool0', l, 2)
                l = Conv2D('conv1', l, out_channel=32, kernel_shape=5)
                l = MaxPooling('pool1', l, 2)
                l = Conv2D('conv2', l, out_channel=64, kernel_shape=4)
                l = MaxPooling('pool2', l, 2)
                l = Conv2D('conv3', l, out_channel=64, kernel_shape=3)
            # conv3 output: [None, 10, 10, 64]
            elif NETWORK_ARCH == 'nature':
                l = Conv2D('conv0', image, out_channel=32, kernel_shape=8, stride=4)
                l = Conv2D('conv1', l, out_channel=64, kernel_shape=4, stride=2)
                l = Conv2D('conv2', l, out_channel=64, kernel_shape=3)
            # conv2 output: [None, 11, 11, 64]
        conv2 = tf.identity(l, name='convolutional-2')
        l = FullyConnected('fc0', l, 512, nl=tf.identity)
        l = PReLU('prelu', l)
        fc = tf.identity(l, name='fully-connected')
        policy = FullyConnected('fc-pi', l, out_dim=NUM_ACTIONS, nl=tf.identity)
        value = FullyConnected('fc-v', l, 1, nl=tf.identity)
        return policy, value

    def _build_graph(self, inputs):
        state, action, futurereward = inputs
        policy, self.value = self._get_NN_prediction(state)
        self.value = tf.squeeze(self.value, [1], name='pred_value') # (B,)
        self.logits = tf.nn.softmax(policy, name='logits')

        expf = tf.get_variable('explore_factor', shape=[],
                initializer=tf.constant_initializer(1), trainable=False)
        logitsT = tf.nn.softmax(policy * expf, name='logitsT') #The larger expf, the less exploration
        is_training = get_current_tower_context().is_training
        if not is_training:
            return
        log_probs = tf.log(self.logits + 1e-6)

        log_pi_a_given_s = tf.reduce_sum(
                log_probs * tf.one_hot(action, NUM_ACTIONS), 1)
        advantage = tf.sub(tf.stop_gradient(self.value), futurereward, name='advantage')
        policy_loss = tf.reduce_sum(log_pi_a_given_s * advantage, name='policy_loss')
        xentropy_loss = tf.reduce_sum(
                self.logits * log_probs, name='xentropy_loss')
        value_loss = tf.nn.l2_loss(self.value - futurereward, name='value_loss')

        pred_reward = tf.reduce_mean(self.value, name='predict_reward')
        advantage = symbf.rms(advantage, name='rms_advantage')
        summary.add_moving_summary(policy_loss, xentropy_loss, value_loss, pred_reward, advantage)
        entropy_beta = tf.get_variable('entropy_beta', shape=[],
                initializer=tf.constant_initializer(0.01), trainable=False)
        self.cost = tf.add_n([policy_loss, xentropy_loss * entropy_beta, value_loss])
        self.cost = tf.truediv(self.cost,
                tf.cast(tf.shape(futurereward)[0], tf.float32),
                name='cost')

    def get_gradient_processor(self):
        return [MapGradient(lambda grad: tf.clip_by_average_norm(grad, 0.1)),
                SummaryGradient()]

class MySimulatorMaster(SimulatorMaster, Callback):
    def __init__(self, pipe_c2s, pipe_s2c, model):
        super(MySimulatorMaster, self).__init__(pipe_c2s, pipe_s2c)
        self.M = model
        self.queue = queue.Queue(maxsize=BATCH_SIZE*8*2)

    def _setup_graph(self):
        self.sess = self.trainer.sess
        self.async_predictor = MultiThreadAsyncPredictor(
                self.trainer.get_predict_funcs(['state'], ['logitsT', 'pred_value'],
                PREDICTOR_THREAD), batch_size=15)
        # else:
        #     self.async_predictor = MultiThreadAsyncPredictor(
        #         self.trainer.get_predict_funcs(['state'], ['logitsT', 'pred_value', FEATURE],
        #                                        PREDICTOR_THREAD), batch_size=15)
        if FEATURE:
            logger.info("Initialize density network")
            cfg = PredictConfig(
                    session_init=NewSession(),
                    model=Model(),
                    input_var_names=['state'],
                    output_var_names=[FEATURE])
            self.offline_predictor = get_predict_func(cfg)
        self.async_predictor.run()


    def _trigger_epoch(self):
        if FEATURE:
            if self.epoch_num % 1 == 0:
                logger.info("update density network at epoch %d."%(self.epoch_num))
                cfg = PredictConfig(
                    session_init=JustCurrentSession(),
                    model = Model(),
                    input_var_names=['state'],
                    output_var_names=[FEATURE])
                self.offline_predictor = get_predict_func(cfg)

    def _on_state(self, state, ident):
        def cb(outputs):
            #if not FEATURE:
            distrib, value = outputs.result()
            #else:
            #    distrib, value, feature = outputs.result()
            assert np.all(np.isfinite(distrib)), distrib
            action = np.random.choice(len(distrib), p=distrib)
            client = self.clients[ident]
            client.memory.append(TransitionExperience(state, action, None, value=value))
            if not FEATURE:
                self.send_queue.put([ident, dumps(action)])
            else:
                feature = self.offline_predictor([[state]])[0][0]
                self.send_queue.put([ident, dumps([action, feature])])
        self.async_predictor.put_task([state], cb)

    def _on_episode_over(self, ident):
        self._parse_memory(0, ident, True)

    def _on_datapoint(self, ident):
        client = self.clients[ident]
        if len(client.memory) == LOCAL_TIME_MAX + 1:
            R = client.memory[-1].value
            self._parse_memory(R, ident, False)

    def _parse_memory(self, init_r, ident, isOver):
        client = self.clients[ident]
        mem = client.memory
        if not isOver:
            last = mem[-1]
            mem = mem[:-1]

        mem.reverse()
        R = float(init_r)
        for idx, k in enumerate(mem):
            R = np.clip(k.reward, -1, 1) + GAMMA * R
            self.queue.put([k.state, k.action, R])

        if not isOver:
            client.memory = [last]
        else:
            client.memory = []

def get_config():
    logger.set_logger_dir(LOG_DIR)
    M = Model()

    name_base = str(uuid.uuid1())[:6]
    PIPE_DIR = os.environ.get('TENSORPACK_PIPEDIR', '.').rstrip('/')
    namec2s = 'ipc://{}/sim-c2s-{}'.format(PIPE_DIR, name_base)
    names2c = 'ipc://{}/sim-s2c-{}'.format(PIPE_DIR, name_base)
    procs = [MySimulatorWorker(k, namec2s, names2c) for k in range(SIMULATOR_PROC)]
    ensure_proc_terminate(procs)
    start_proc_mask_signal(procs)

    master = MySimulatorMaster(namec2s, names2c, M)
    dataflow = BatchData(DataFromQueue(master.queue), BATCH_SIZE)

    lr = tf.Variable(0.001, trainable=False, name='learning_rate')
    tf.scalar_summary('learning_rate', lr)

    return TrainConfig(
        dataset=dataflow,
        optimizer=tf.train.AdamOptimizer(lr, epsilon=1e-3),
        callbacks=Callbacks([
            StatPrinter(), PeriodicCallback(ModelSaver(), 5),
            #ScheduledHyperParamSetter('learning_rate', [(80, 0.0003), (120, 0.0001)]),
            ScheduledHyperParamSetter('entropy_beta', [(80, 0.005)]),
            #ScheduledHyperParamSetter('explore_factor',
                #[(80, 2), (100, 3), (120, 4), (140, 5)]),
            HumanHyperParamSetter('learning_rate'),
            HumanHyperParamSetter('entropy_beta'),
            HumanHyperParamSetter('explore_factor'),
            master,
            PeriodicCallback(Evaluator(EVAL_EPISODE, ['state'], ['logits'], policy_dist=POLICY_DIST), 5),
        ]),
        extra_threads_procs=[master],
        session_config=get_default_sess_config(0.5),
        model=M,
        step_per_epoch=STEP_PER_EPOCH,
        max_epoch=1000,
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.') # nargs='*' in multi mode
    parser.add_argument('--load', help='load model')
    parser.add_argument('--env', help='env', required=True)
    parser.add_argument('--task', help='task to perform',
            choices=['play', 'eval', 'train'], default='train')
    parser.add_argument('--logdir', help='output directory', required=True)
    parser.add_argument('--pc', help='pseudo count method', choices=[None, 'joint', 'CTS'], default=None)
    parser.add_argument('--network', help='network architecture', choices=['nature','1'], default='nature')
    parser.add_argument('--feature', help='Feature to use in the density model', choices=[None, 'fully-connected', 'convolutional-2'], default=None)

    #parser.add_argument('--fixed_epoch', help='How many epochs we fix the CNN for pc and then update', default=None)
    parser.add_argument('--pcfactor', help='Pseudo count factor. PC_MULT,PC_THRE,PC_TIME', default=None) #2.5,0.01,1000
    parser.add_argument('--pc_action', help='Pseudo count function of (action and old state)', action='store_true')
    parser.add_argument('--pc_downsample_value', help='Pseudo count downsample max value', default='32')
    parser.add_argument('--pc_clean', help='Clean Pseudo count every 10 epoch', action='store_true')
    parser.add_argument('--UCB1', help='Use UCB1 Algorithn', action='store_true')
    args = parser.parse_args()

    LOG_DIR = args.logdir
    ENV_NAME = args.env
    assert ENV_NAME
    p = get_player(); del p    # set NUM_ACTIONS
    logger.info("Playing the game: " + ENV_NAME)
    logger.info("The log directory: " + LOG_DIR)
    PC_METHOD = args.pc
    FEATURE = args.feature
    PC_ACTION = args.pc_action
    PC_DOWNSAMPLE_VALUE = int(args.pc_downsample_value)
    PC_CLEAN = args.pc_clean
    UCB1 = args.UCB1
    logger.info("Using feature " + str(FEATURE) + " for density model")
    if POLICY_DIST:
        logger.info("Draw from policy distribution when evaluation")
    if PC_METHOD:
        logger.info("Using Pseudo Count method: " + PC_METHOD)
        logger.info("Pseudo count downsample value: " + str(PC_DOWNSAMPLE_VALUE))
        if args.UCB1:
            logger.info("Using UCB1 algorithm")
        if PC_ACTION:
            logger.info("Pseudo count function of (old state, action)")
        else:
            logger.info("Pseudo count function of transmitted state")
        if PC_CLEAN:
            logger.info("clean counter every 10 epochs")
        if not FEATURE:
            logger.info("Using image raw pixels as the input to pseudo count method.")
        else:
            logger.info("Using " + FEATURE + " layer feature as input to pseudo count method.")
        if not args.pcfactor:
            logger.info("Do not use pc factor.")
        else:
            PC_MULT, PC_THRE, PC_TIME = args.pcfactor.split(',')
            logger.info("Use pc factor to encourage explore")
            logger.info("Multiplier=%s, Threshold=%s, Repeat Times=%s"%(PC_MULT, PC_THRE, PC_TIME))
            PC_MULT, PC_THRE, PC_TIME = float(PC_MULT), float(PC_THRE), int(PC_TIME)

    else:
        logger.info("Don't use Pseudo Count method")
    NETWORK_ARCH = args.network
    logger.info("Using network architecutre: " + NETWORK_ARCH)

    raw_input("Please make sure the parameters are right")

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    if args.task != 'train':
        assert args.load is not None

    if args.task != 'train':
        cfg = PredictConfig(
                model=Model(),
                session_init=SaverRestore(args.load),
                input_var_names=['state'],
                output_var_names=['logits'])
        if args.task == 'play':
            play_model(cfg)
        elif args.task == 'eval':
            eval_model_multithread(cfg, EVAL_EPISODE)
    else:
        nr_gpu = get_nr_gpu()
        if nr_gpu > 1:
            predict_tower = range(nr_gpu)[-nr_gpu/2:]
        else:
            predict_tower = [0]
        PREDICTOR_THREAD = len(predict_tower) * PREDICTOR_THREAD_PER_GPU
        config = get_config()
        if args.load:
            config.session_init = SaverRestore(args.load)
        config.tower = range(nr_gpu)[:-nr_gpu/2] or [0]
        logger.info("[BA3C] Train on gpu {} and infer on gpu {}".format(
            ','.join(map(str, config.tower)), ','.join(map(str, predict_tower))))
        AsyncMultiGPUTrainer(config, predict_tower=predict_tower).train()
