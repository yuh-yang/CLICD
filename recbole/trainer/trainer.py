# @Time   : 2020/6/26
# @Author : Shanlei Mu
# @Email  : slmu@ruc.edu.cn

# UPDATE:
# @Time   : 2021/6/23, 2020/9/26, 2020/9/26, 2020/10/01, 2020/9/16
# @Author : Zihan Lin, Yupeng Hou, Yushuo Chen, Shanlei Mu, Xingyu Pan
# @Email  : zhlin@ruc.edu.cn, houyupeng@ruc.edu.cn, chenyushuo@ruc.edu.cn, slmu@ruc.edu.cn, panxy@ruc.edu.cn

# UPDATE:
# @Time   : 2020/10/8, 2020/10/15, 2020/11/20, 2021/2/20, 2021/3/3, 2021/3/5, 2021/7/18
# @Author : Hui Wang, Xinyan Fan, Chen Yang, Yibo Li, Lanling Xu, Haoran Cheng, Zhichao Feng
# @Email  : hui.wang@ruc.edu.cn, xinyan.fan@ruc.edu.cn, 254170321@qq.com, 2018202152@ruc.edu.cn, xulanling_sherry@163.com, chenghaoran29@foxmail.com, fzcbupt@gmail.com

r"""
recbole.trainer.trainer
################################
"""

from cmath import sqrt
import os
from logging import getLogger
from re import L
from sched import scheduler
from time import time
from collections import defaultdict

from torch import nn
import numpy as np
import torch
import torch.optim as optim
from torch.nn.utils.clip_grad import clip_grad_norm_
from tqdm import tqdm
import torch.nn.functional as F
import random
import scipy.stats as ss
from recbole.data.interaction import Interaction
from recbole.data.dataloader import FullSortEvalDataLoader, FastSampleEvalDataLoader
from recbole.evaluator import Evaluator, Collector
from recbole.utils import ensure_dir, get_local_time, early_stopping, calculate_valid_score, dict2str, \
    EvaluatorType, KGDataLoaderState, get_tensorboard, set_color, get_gpu_usage, WandbLogger, meta_minibatch


class AbstractTrainer(object):
    r"""Trainer Class is used to manage the training and evaluation processes of recommender system models.
    AbstractTrainer is an abstract class in which the fit() and evaluate() method should be implemented according
    to different training and evaluation strategies.
    """

    def __init__(self, config, model):
        self.config = config
        self.model = model

    def fit(self, train_data):
        r"""Train the model based on the train data.

        """
        raise NotImplementedError('Method [next] should be implemented.')

    def evaluate(self, eval_data):
        r"""Evaluate the model based on the eval data.

        """

        raise NotImplementedError('Method [next] should be implemented.')


class Trainer(AbstractTrainer):
    r"""The basic Trainer for basic training and evaluation strategies in recommender systems. This class defines common
    functions for training and evaluation processes of most recommender system models, including fit(), evaluate(),
    resume_checkpoint() and some other features helpful for model training and evaluation.

    Generally speaking, this class can serve most recommender system models, If the training process of the model is to
    simply optimize a single loss without involving any complex training strategies, such as adversarial learning,
    pre-training and so on.

    Initializing the Trainer needs two parameters: `config` and `model`. `config` records the parameters information
    for controlling training and evaluation, such as `learning_rate`, `epochs`, `eval_step` and so on.
    `model` is the instantiated object of a Model Class.

    """

    def __init__(self, config, model):
        super(Trainer, self).__init__(config, model)

        self.logger = getLogger()
        self.tensorboard = get_tensorboard(self.logger)
        self.wandblogger = WandbLogger(config)
        self.learner = config['learner']
        self.learning_rate = config['learning_rate']
        self.epochs = config['epochs']
        self.eval_step = min(config['eval_step'], self.epochs)
        self.stopping_step = config['stopping_step']
        self.clip_grad_norm = config['clip_grad_norm']
        self.valid_metric = config['valid_metric'].lower()
        self.valid_metric_bigger = config['valid_metric_bigger']
        self.test_batch_size = config['eval_batch_size']
        self.gpu_available = torch.cuda.is_available() and config['use_gpu']
        self.device = config['device']
        if self.device == "cpu":
            self.gpu_available = False
        self.checkpoint_dir = config['checkpoint_dir']
        ensure_dir(self.checkpoint_dir)
        saved_model_file = '{}-{}.pth'.format(self.config['model'], self.config['dataset'])
        self.saved_model_file = os.path.join(self.checkpoint_dir, saved_model_file)
        self.weight_decay = config['weight_decay']

        self.start_epoch = 0
        self.cur_step = 0
        self.best_valid_score = -np.inf if self.valid_metric_bigger else np.inf
        self.best_valid_result = None
        self.train_loss_dict = dict()
        self.optimizer = self._build_optimizer(self.model.parameters())
        self.eval_type = config['eval_type']
        self.eval_collector = Collector(config)
        self.evaluator = Evaluator(config)
        self.item_tensor = None
        self.tot_item_num = None

    def _build_optimizer(self, params):
        r"""Init the Optimizer

        Returns:
            torch.optim: the optimizer
        """
        if self.config['reg_weight'] and self.weight_decay and self.weight_decay * self.config['reg_weight'] > 0:
            self.logger.warning(
                'The parameters [weight_decay] and [reg_weight] are specified simultaneously, '
                'which may lead to double regularization.'
            )

        if self.learner.lower() == 'adam':
            optimizer = optim.Adam(params, lr=self.learning_rate, weight_decay=self.weight_decay)
        elif self.learner.lower() == 'sgd':
            optimizer = optim.SGD(params, lr=self.learning_rate, weight_decay=self.weight_decay)
        elif self.learner.lower() == 'adagrad':
            optimizer = optim.Adagrad(params, lr=self.learning_rate, weight_decay=self.weight_decay)
        elif self.learner.lower() == 'rmsprop':
            optimizer = optim.RMSprop(params, lr=self.learning_rate, weight_decay=self.weight_decay)
        elif self.learner.lower() == 'sparse_adam':
            optimizer = optim.SparseAdam(params, lr=self.learning_rate)
            if self.weight_decay > 0:
                self.logger.warning('Sparse Adam cannot argument received argument [{weight_decay}]')
        else:
            self.logger.warning('Received unrecognized optimizer, set default Adam optimizer')
            optimizer = optim.Adam(params, lr=self.learning_rate)
        return optimizer

    def _train_epoch(self, train_data, epoch_idx, loss_func=None, show_progress=False):
        r"""Train the model in an epoch

        Args:
            train_data (DataLoader): The train data.
            epoch_idx (int): The current epoch id.
            loss_func (function): The loss function of :attr:`model`. If it is ``None``, the loss function will be
                :attr:`self.model.calculate_loss`. Defaults to ``None``.
            show_progress (bool): Show the progress of training epoch. Defaults to ``False``.

        Returns:
            float/tuple: The sum of loss returned by all batches in this epoch. If the loss in each batch contains
            multiple parts and the model return these multiple parts loss instead of the sum of loss, it will return a
            tuple which includes the sum of loss in each part.
        """
        self.model.train()
        # 每个epoch都清零global encoding
        # nn.init.zeros_(self.model.global_encodings)
        loss_func = loss_func or self.model.calculate_loss
        total_loss = None
        iter_data = (
            tqdm(
                train_data,
                total=len(train_data),
                ncols=100,
                desc=set_color(f"Train {epoch_idx:>5}", 'pink'),
            ) if show_progress else train_data
        )
        for batch_idx, interaction in enumerate(iter_data):
            interaction = interaction.to(self.device)
            self.optimizer.zero_grad()
            losses = loss_func(interaction)
            if isinstance(losses, tuple):
                loss = sum(losses)
                loss_tuple = tuple(per_loss.item() for per_loss in losses)
                total_loss = loss_tuple if total_loss is None else tuple(map(sum, zip(total_loss, loss_tuple)))
            else:
                loss = losses
                total_loss = losses.item() if total_loss is None else total_loss + losses.item()
            self._check_nan(loss)
            loss.backward()
            if self.clip_grad_norm:
                clip_grad_norm_(self.model.parameters(), **self.clip_grad_norm)
            self.optimizer.step()
            if self.gpu_available and show_progress:
                iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
        return total_loss

    def _valid_epoch(self, valid_data, show_progress=False):
        r"""Valid the model with valid data

        Args:
            valid_data (DataLoader): the valid data.
            show_progress (bool): Show the progress of evaluate epoch. Defaults to ``False``.

        Returns:
            float: valid score
            dict: valid result
        """
        valid_result = self.evaluate(valid_data, load_best_model=False, show_progress=show_progress)
        valid_score = calculate_valid_score(valid_result, self.valid_metric)
        return valid_score, valid_result

    def _save_checkpoint(self, epoch):
        r"""Store the model parameters information and training information.

        Args:
            epoch (int): the current epoch id

        """
        state = {
            'config': self.config,
            'epoch': epoch,
            'cur_step': self.cur_step,
            'best_valid_score': self.best_valid_score,
            'state_dict': self.model.state_dict(),
            'other_parameter': self.model.other_parameter(),
            'optimizer': self.optimizer.state_dict(),
        }
        torch.save(state, self.saved_model_file)

    def resume_checkpoint(self, resume_file):
        r"""Load the model parameters information and training information.

        Args:
            resume_file (file): the checkpoint file

        """
        resume_file = str(resume_file)
        self.saved_model_file = resume_file
        checkpoint = torch.load(resume_file)
        self.start_epoch = checkpoint['epoch'] + 1
        self.cur_step = checkpoint['cur_step']
        self.best_valid_score = checkpoint['best_valid_score']

        # load architecture params from checkpoint
        if checkpoint['config']['model'].lower() != self.config['model'].lower():
            self.logger.warning(
                'Architecture configuration given in config file is different from that of checkpoint. '
                'This may yield an exception while state_dict is being loaded.'
            )
        self.model.load_state_dict(checkpoint['state_dict'])
        self.model.load_other_parameter(checkpoint.get('other_parameter'))

        # load optimizer state from checkpoint only when optimizer type is not changed
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        message_output = 'Checkpoint loaded. Resume training from epoch {}'.format(self.start_epoch)
        self.logger.info(message_output)

    def _check_nan(self, loss):
        if torch.isnan(loss):
            raise ValueError('Training loss is nan')

    def _generate_train_loss_output(self, epoch_idx, s_time, e_time, losses):
        des = self.config['loss_decimal_place'] or 4
        train_loss_output = (set_color('epoch %d training', 'green') + ' [' + set_color('time', 'blue') +
                             ': %.2fs, ') % (epoch_idx, e_time - s_time)
        if isinstance(losses, tuple):
            des = (set_color('train_loss%d', 'blue') + ': %.' + str(des) + 'f')
            train_loss_output += ', '.join(des % (idx + 1, loss) for idx, loss in enumerate(losses))
        else:
            des = '%.' + str(des) + 'f'
            train_loss_output += set_color('train loss', 'blue') + ': ' + des % losses
        return train_loss_output + ']'

    def _add_train_loss_to_tensorboard(self, epoch_idx, losses, tag='Loss/Train'):
        if isinstance(losses, tuple):
            for idx, loss in enumerate(losses):
                self.tensorboard.add_scalar(tag + str(idx), loss, epoch_idx)
        else:
            self.tensorboard.add_scalar(tag, losses, epoch_idx)

    def _add_hparam_to_tensorboard(self, best_valid_result):
        # base hparam
        hparam_dict = {
            'learner': self.config['learner'],
            'learning_rate': self.config['learning_rate'],
            'train_batch_size': self.config['train_batch_size']
        }
        # unrecorded parameter
        unrecorded_parameter = {
            parameter
            for parameters in self.config.parameters.values() for parameter in parameters
        }.union({'model', 'dataset', 'config_files', 'device'})
        # other model-specific hparam
        hparam_dict.update({
            para: val
            for para, val in self.config.final_config_dict.items() if para not in unrecorded_parameter
        })
        for k in hparam_dict:
            if hparam_dict[k] is not None and not isinstance(hparam_dict[k], (bool, str, float, int)):
                hparam_dict[k] = str(hparam_dict[k])

        self.tensorboard.add_hparams(hparam_dict, {'hparam/best_valid_result': best_valid_result})

    def fit(self, train_data, valid_data=None, verbose=True, saved=True, show_progress=False, callback_fn=None):
        r"""Train the model based on the train data and the valid data.

        Args:
            train_data (DataLoader): the train data
            valid_data (DataLoader, optional): the valid data, default: None.
                                               If it's None, the early_stopping is invalid.
            verbose (bool, optional): whether to write training and evaluation information to logger, default: True
            saved (bool, optional): whether to save the model parameters, default: True
            show_progress (bool): Show the progress of training epoch and evaluate epoch. Defaults to ``False``.
            callback_fn (callable): Optional callback function executed at end of epoch.
                                    Includes (epoch_idx, valid_score) input arguments.

        Returns:
             (float, dict): best valid score and best valid result. If valid_data is None, it returns (-1, None)
        """
        if saved and self.start_epoch >= self.epochs:
            self._save_checkpoint(-1)

        self.eval_collector.data_collect(train_data)
        valid_step = 0

        for epoch_idx in range(self.start_epoch, self.epochs):
            # train
            training_start_time = time()
            train_loss = self._train_epoch(train_data, epoch_idx, show_progress=show_progress)
            self.train_loss_dict[epoch_idx] = sum(train_loss) if isinstance(train_loss, tuple) else train_loss
            training_end_time = time()
            train_loss_output = \
                self._generate_train_loss_output(epoch_idx, training_start_time, training_end_time, train_loss)
            if verbose:
                self.logger.info(train_loss_output)
            # self._add_train_loss_to_tensorboard(epoch_idx, train_loss)
            self.wandblogger.log_metrics({'epoch': epoch_idx, 'train_loss': train_loss, 'train_step':epoch_idx}, head='train')

            # eval
            if self.eval_step <= 0 or not valid_data:
                if saved:
                    self._save_checkpoint(epoch_idx)
                    update_output = set_color('Saving current', 'blue') + ': %s' % self.saved_model_file
                    if verbose:
                        self.logger.info(update_output)
                continue
            if (epoch_idx + 1) % self.eval_step == 0:
                valid_start_time = time()
                valid_score, valid_result = self._valid_epoch(valid_data, show_progress=show_progress)
                self.best_valid_score, self.cur_step, stop_flag, update_flag = early_stopping(
                    valid_score,
                    self.best_valid_score,
                    self.cur_step,
                    max_step=self.stopping_step,
                    bigger=self.valid_metric_bigger
                )
                valid_end_time = time()
                valid_score_output = (set_color("epoch %d evaluating", 'green') + " [" + set_color("time", 'blue')
                                    + ": %.2fs, " + set_color("valid_score", 'blue') + ": %f]") % \
                                     (epoch_idx, valid_end_time - valid_start_time, valid_score)
                valid_result_output = set_color('valid result', 'blue') + ': \n' + dict2str(valid_result)
                if verbose:
                    self.logger.info(valid_score_output)
                    self.logger.info(valid_result_output)
                # self.tensorboard.add_scalar('Vaild_score', valid_score, epoch_idx)
                self.wandblogger.log_metrics({**valid_result, 'valid_step': valid_step}, head='valid')

                if update_flag:
                    find_better = set_color('Find better model', 'blue')
                    self.logger.info(find_better)
                    if saved:
                        self._save_checkpoint(epoch_idx)
                        update_output = set_color('Saving current best', 'blue') + ': %s' % self.saved_model_file
                        if verbose:
                            self.logger.info(update_output)
                    self.best_valid_result = valid_result

                if callback_fn:
                    callback_fn(epoch_idx, valid_score)

                if stop_flag:
                    stop_output = 'Finished training, best eval result in epoch %d' % \
                                  (epoch_idx - self.cur_step * self.eval_step)
                    if verbose:
                        self.logger.info(stop_output)
                    break

                valid_step+=1

        # self._add_hparam_to_tensorboard(self.best_valid_score)
        return self.best_valid_score, self.best_valid_result

    def _full_sort_batch_eval(self, batched_data):
        interaction, history_index, positive_u, positive_i = batched_data
        try:
            # Note: interaction without item ids
            scores = self.model.full_sort_predict(interaction.to(self.device))
        except NotImplementedError:
            inter_len = len(interaction)
            new_inter = interaction.to(self.device).repeat_interleave(self.tot_item_num)
            batch_size = len(new_inter)
            new_inter.update(self.item_tensor.repeat(inter_len))
            if batch_size <= self.test_batch_size:
                scores = self.model.predict(new_inter)
            else:
                scores = self._spilt_predict(new_inter, batch_size)

        scores = scores.view(-1, self.tot_item_num)
        scores[:, 0] = -np.inf
        if history_index is not None:
            scores[history_index] = -np.inf
        return interaction, scores, positive_u, positive_i

    def _fast_neg_batch_eval(self, batched_data):
        interaction, row_idx, positive_u, positive_i = batched_data
        origin_scores = self.model.fast_predict(interaction.to(self.device))

        origin_scores = origin_scores.view(-1, 101)
        col_idx = interaction["item_id_with_negs"]
        batch_user_num = positive_u[-1] + 1
        scores = torch.full((batch_user_num, self.tot_item_num), -np.inf, device=self.device)
        for u in positive_u:
            scores[u, col_idx[u]] = origin_scores[u]

        return interaction, scores, positive_u, positive_i

    def _neg_sample_batch_eval(self, batched_data):
        interaction, row_idx, positive_u, positive_i = batched_data
        # print(positive_i.tolist())
        batch_size = interaction.length
        if batch_size <= self.test_batch_size:
            origin_scores = self.model.predict(interaction.to(self.device))
        else:
            origin_scores = self._spilt_predict(interaction, batch_size)

        if self.config['eval_type'] == EvaluatorType.VALUE:
            return interaction, origin_scores, positive_u, positive_i
        elif self.config['eval_type'] == EvaluatorType.RANKING:
            col_idx = interaction[self.config['ITEM_ID_FIELD']]
            batch_user_num = positive_u[-1] + 1
            scores = torch.full((batch_user_num, self.tot_item_num), -np.inf, device=self.device)
            scores[row_idx, col_idx] = origin_scores
            return interaction, scores, positive_u, positive_i

    @torch.no_grad()
    def evaluate(self, eval_data, load_best_model=True, model_file=None, show_progress=False):
        r"""Evaluate the model based on the eval data.

        Args:
            eval_data (DataLoader): the eval data
            load_best_model (bool, optional): whether load the best model in the training process, default: True.
                                              It should be set True, if users want to test the model after training.
            model_file (str, optional): the saved model file, default: None. If users want to test the previously
                                        trained model file, they can set this parameter.
            show_progress (bool): Show the progress of evaluate epoch. Defaults to ``False``.

        Returns:
            dict: eval result, key is the eval metric and value in the corresponding metric value.
        """
        if not eval_data:
            return

        if load_best_model:
            if model_file:
                checkpoint_file = model_file
            else:
                checkpoint_file = self.saved_model_file
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['state_dict'])
            self.model.load_other_parameter(checkpoint.get('other_parameter'))
            message_output = 'Loading model structure and parameters from {}'.format(checkpoint_file)
            self.logger.info(message_output)

        self.model.eval()

        if isinstance(eval_data, FullSortEvalDataLoader):
            eval_func = self._full_sort_batch_eval
            if self.item_tensor is None:
                self.item_tensor = eval_data.dataset.get_item_feature().to(self.device)
        elif isinstance(eval_data, FastSampleEvalDataLoader):
            eval_func = self._fast_neg_batch_eval
        else:
            eval_func = self._neg_sample_batch_eval
        if self.config['eval_type'] == EvaluatorType.RANKING:
            self.tot_item_num = eval_data.dataset.item_num

        iter_data = (
            tqdm(
                eval_data,
                total=len(eval_data),
                ncols=100,
                desc=set_color(f"Evaluate   ", 'pink'),
            ) if show_progress else eval_data
        )
        for batch_idx, batched_data in enumerate(iter_data):
            interaction, scores, positive_u, positive_i = eval_func(batched_data)
            if self.gpu_available and show_progress:
                iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
            self.eval_collector.eval_batch_collect(scores, interaction, positive_u, positive_i)
        self.eval_collector.model_collect(self.model)
        struct = self.eval_collector.get_data_struct()
        result = self.evaluator.evaluate(struct)
        self.wandblogger.log_eval_metrics(result, head='eval')

        return result

    @torch.no_grad()
    def fast_eval_return_rank(self, eval_data, load_best_model=True, model_file=None, show_progress=False):
        if not eval_data:
            return

        if load_best_model:
            if model_file:
                checkpoint_file = model_file
            else:
                checkpoint_file = self.saved_model_file
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['state_dict'])
            self.model.load_other_parameter(checkpoint.get('other_parameter'))
            message_output = 'Loading model structure and parameters from {}'.format(checkpoint_file)
            self.logger.info(message_output)

        self.model.eval()

        if self.config['eval_type'] == EvaluatorType.RANKING:
            self.tot_item_num = eval_data.dataset.item_num

        iter_data = (
            tqdm(
                eval_data,
                total=len(eval_data),
                ncols=100,
                desc=set_color(f"Evaluate   ", 'pink'),
            ) if show_progress else eval_data
        )
        rank_result = {
            'seq': [],
            'pos_item': [],
            'pos_rank': [],
            'top10': []
        }
        for batch_idx, batched_data in enumerate(iter_data):
            interaction, row_idx, positive_u, positive_i = batched_data
            origin_scores = self.model.fast_predict(interaction.to(self.device))
            origin_scores = origin_scores.view(-1, 101)
            _, topk_idx = torch.topk(origin_scores, k=101, dim=-1)
            topk_idx = topk_idx.cpu()
            rank_result['seq'].extend(interaction['item_id_list'].tolist())
            rank_result['pos_item'].extend(positive_i.tolist())
            # find the index of 0 in topk_idx
            rank_result['pos_rank'].extend((topk_idx == 0).nonzero(as_tuple=True)[1].cpu().tolist())
            top10_items = torch.gather(interaction["item_id_with_negs"], 1, topk_idx[:, :10]).tolist()
            rank_result['top10'].extend(top10_items)
        return rank_result

    @torch.no_grad()
    def fast_eval_return_conformity(self, eval_data, load_best_model=True, model_file=None, show_progress=False):
        if not eval_data:
            return

        if load_best_model:
            if model_file:
                checkpoint_file = model_file
            else:
                checkpoint_file = self.saved_model_file
            checkpoint = torch.load(checkpoint_file)
            self.model.load_state_dict(checkpoint['state_dict'])
            self.model.load_other_parameter(checkpoint.get('other_parameter'))
            message_output = 'Loading model structure and parameters from {}'.format(checkpoint_file)
            self.logger.info(message_output)

        self.model.eval()

        if self.config['eval_type'] == EvaluatorType.RANKING:
            self.tot_item_num = eval_data.dataset.item_num

        iter_data = (
            tqdm(
                eval_data,
                total=len(eval_data),
                ncols=100,
                desc=set_color(f"Evaluate   ", 'pink'),
            ) if show_progress else eval_data
        )
        rank_result = {
            'seq': [],
            'pos_item': [],
            'pos_rank': [],
            'top10': [],
            'conformity': []
        }
        for batch_idx, batched_data in enumerate(iter_data):
            interaction, row_idx, positive_u, positive_i = batched_data
            origin_scores, conformity_scores = self.model.fast_predict_with_conformity_weights(interaction.to(self.device))
            origin_scores = origin_scores.view(-1, 101)
            _, topk_idx = torch.topk(origin_scores, k=101, dim=-1)
            topk_idx = topk_idx.cpu()
            rank_result['seq'].extend(interaction['item_id_list'].tolist())
            rank_result['pos_item'].extend(positive_i.tolist())
            # find the index of 0 in topk_idx
            rank_result['pos_rank'].extend((topk_idx == 0).nonzero(as_tuple=True)[1].cpu().tolist())
            top10_items = torch.gather(interaction["item_id_with_negs"], 1, topk_idx[:, :10]).tolist()
            rank_result['top10'].extend(top10_items)
            rank_result['conformity'].extend(conformity_scores.cpu().tolist())
        return rank_result

    def _spilt_predict(self, interaction, batch_size):
        spilt_interaction = dict()
        for key, tensor in interaction.interaction.items():
            spilt_interaction[key] = tensor.split(self.test_batch_size, dim=0)
        num_block = (batch_size + self.test_batch_size - 1) // self.test_batch_size
        result_list = []
        for i in range(num_block):
            current_interaction = dict()
            for key, spilt_tensor in spilt_interaction.items():
                current_interaction[key] = spilt_tensor[i]
            result = self.model.predict(Interaction(current_interaction).to(self.device))
            if len(result.shape) == 0:
                result = result.unsqueeze(0)
            result_list.append(result)
        return torch.cat(result_list, dim=0)

    def _spilt_predict(self, interaction, batch_size):
        spilt_interaction = dict()
        for key, tensor in interaction.interaction.items():
            spilt_interaction[key] = tensor.split(self.test_batch_size, dim=0)
        num_block = (batch_size + self.test_batch_size - 1) // self.test_batch_size
        result_list = []
        for i in range(num_block):
            current_interaction = dict()
            for key, spilt_tensor in spilt_interaction.items():
                current_interaction[key] = spilt_tensor[i]
            result = self.model.predict(Interaction(current_interaction).to(self.device))
            if len(result.shape) == 0:
                result = result.unsqueeze(0)
            result_list.append(result)
        return torch.cat(result_list, dim=0)


class KGTrainer(Trainer):
    r"""KGTrainer is designed for Knowledge-aware recommendation methods. Some of these models need to train the
    recommendation related task and knowledge related task alternately.

    """

    def __init__(self, config, model):
        super(KGTrainer, self).__init__(config, model)

        self.train_rec_step = config['train_rec_step']
        self.train_kg_step = config['train_kg_step']

    def _train_epoch(self, train_data, epoch_idx, loss_func=None, show_progress=False):
        if self.train_rec_step is None or self.train_kg_step is None:
            interaction_state = KGDataLoaderState.RSKG
        elif epoch_idx % (self.train_rec_step + self.train_kg_step) < self.train_rec_step:
            interaction_state = KGDataLoaderState.RS
        else:
            interaction_state = KGDataLoaderState.KG
        train_data.set_mode(interaction_state)
        if interaction_state in [KGDataLoaderState.RSKG, KGDataLoaderState.RS]:
            return super()._train_epoch(train_data, epoch_idx, show_progress=show_progress)
        elif interaction_state in [KGDataLoaderState.KG]:
            return super()._train_epoch(
                train_data, epoch_idx, loss_func=self.model.calculate_kg_loss, show_progress=show_progress
            )
        return None


class KGATTrainer(Trainer):
    r"""KGATTrainer is designed for KGAT, which is a knowledge-aware recommendation method.

    """

    def __init__(self, config, model):
        super(KGATTrainer, self).__init__(config, model)

    def _train_epoch(self, train_data, epoch_idx, loss_func=None, show_progress=False):
        # train rs
        train_data.set_mode(KGDataLoaderState.RS)
        rs_total_loss = super()._train_epoch(train_data, epoch_idx, show_progress=show_progress)

        # train kg
        train_data.set_mode(KGDataLoaderState.KG)
        kg_total_loss = super()._train_epoch(
            train_data, epoch_idx, loss_func=self.model.calculate_kg_loss, show_progress=show_progress
        )

        # update A
        self.model.eval()
        with torch.no_grad():
            self.model.update_attentive_A()

        return rs_total_loss, kg_total_loss


class PretrainTrainer(Trainer):
    r"""PretrainTrainer is designed for pre-training.
    It can be inherited by the trainer which needs pre-training and fine-tuning.
    """

    def __init__(self, config, model):
        super(PretrainTrainer, self).__init__(config, model)
        self.pretrain_epochs = self.config['pretrain_epochs']
        self.save_step = self.config['save_step']

    def save_pretrained_model(self, epoch, saved_model_file):
        r"""Store the model parameters information and training information.

        Args:
            epoch (int): the current epoch id
            saved_model_file (str): file name for saved pretrained model

        """
        state = {
            'config': self.config,
            'epoch': epoch,
            'state_dict': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'other_parameter': self.model.other_parameter(),
        }
        torch.save(state, saved_model_file)

    def pretrain(self, train_data, verbose=True, show_progress=False):
        for epoch_idx in range(self.start_epoch, self.pretrain_epochs):
            # train
            training_start_time = time()
            train_loss = self._train_epoch(train_data, epoch_idx, show_progress=show_progress)
            self.train_loss_dict[epoch_idx] = sum(train_loss) if isinstance(train_loss, tuple) else train_loss
            training_end_time = time()
            train_loss_output = \
                self._generate_train_loss_output(epoch_idx, training_start_time, training_end_time, train_loss)
            if verbose:
                self.logger.info(train_loss_output)
            # self._add_train_loss_to_tensorboard(epoch_idx, train_loss)

            if (epoch_idx + 1) % self.save_step == 0:
                saved_model_file = os.path.join(
                    self.checkpoint_dir,
                    '{}-{}-{}.pth'.format(self.config['model'], self.config['dataset'], str(epoch_idx + 1))
                )
                self.save_pretrained_model(epoch_idx, saved_model_file)
                update_output = set_color('Saving current', 'blue') + ': %s' % saved_model_file
                if verbose:
                    self.logger.info(update_output)

        return self.best_valid_score, self.best_valid_result


class S3RecTrainer(PretrainTrainer):
    r"""S3RecTrainer is designed for S3Rec, which is a self-supervised learning based sequential recommenders.
        It includes two training stages: pre-training ang fine-tuning.

        """

    def __init__(self, config, model):
        super(S3RecTrainer, self).__init__(config, model)

    def fit(self, train_data, valid_data=None, verbose=True, saved=True, show_progress=False, callback_fn=None):
        if self.model.train_stage == 'pretrain':
            return self.pretrain(train_data, verbose, show_progress)
        elif self.model.train_stage == 'finetune':
            return super().fit(train_data, valid_data, verbose, saved, show_progress, callback_fn)
        else:
            raise ValueError("Please make sure that the 'train_stage' is 'pretrain' or 'finetune'!")

class S3RecISPTrainer(PretrainTrainer):
    r"""S3RecTrainer is designed for S3Rec, which is a self-supervised learning based sequential recommenders.
        It includes two training stages: pre-training ang fine-tuning.

        """

    def __init__(self, config, model):
        super(S3RecISPTrainer, self).__init__(config, model)

    def fit(self, train_data, valid_data=None, verbose=True, saved=True, show_progress=False, callback_fn=None):
        if self.model.train_stage == 'pretrain':
            return self.pretrain(train_data, verbose, show_progress)
        elif self.model.train_stage == 'finetune':
            return super().fit(train_data, valid_data, verbose, saved, show_progress, callback_fn)
        else:
            raise ValueError("Please make sure that the 'train_stage' is 'pretrain' or 'finetune'!")

class MKRTrainer(Trainer):
    r"""MKRTrainer is designed for MKR, which is a knowledge-aware recommendation method.

    """

    def __init__(self, config, model):
        super(MKRTrainer, self).__init__(config, model)
        self.kge_interval = config['kge_interval']

    def _train_epoch(self, train_data, epoch_idx, loss_func=None, show_progress=False):
        rs_total_loss, kg_total_loss = 0., 0.

        # train rs
        self.logger.info('Train RS')
        train_data.set_mode(KGDataLoaderState.RS)
        rs_total_loss = super()._train_epoch(
            train_data, epoch_idx, loss_func=self.model.calculate_rs_loss, show_progress=show_progress
        )

        # train kg
        if epoch_idx % self.kge_interval == 0:
            self.logger.info('Train KG')
            train_data.set_mode(KGDataLoaderState.KG)
            kg_total_loss = super()._train_epoch(
                train_data, epoch_idx, loss_func=self.model.calculate_kg_loss, show_progress=show_progress
            )

        return rs_total_loss, kg_total_loss


class TraditionalTrainer(Trainer):
    r"""TraditionalTrainer is designed for Traditional model(Pop,ItemKNN), which set the epoch to 1 whatever the config.

    """

    def __init__(self, config, model):
        super(TraditionalTrainer, self).__init__(config, model)
        self.epochs = 1  # Set the epoch to 1 when running memory based model


class DecisionTreeTrainer(AbstractTrainer):
    """DecisionTreeTrainer is designed for DecisionTree model.

    """

    def __init__(self, config, model):
        super(DecisionTreeTrainer, self).__init__(config, model)

        self.logger = getLogger()
        self.tensorboard = get_tensorboard(self.logger)
        self.label_field = config['LABEL_FIELD']
        self.convert_token_to_onehot = self.config['convert_token_to_onehot']

        # evaluator
        self.eval_type = config['eval_type']
        self.epochs = config['epochs']
        self.eval_step = min(config['eval_step'], self.epochs)
        self.valid_metric = config['valid_metric'].lower()
        self.eval_collector = Collector(config)
        self.evaluator = Evaluator(config)

        # model saved
        self.checkpoint_dir = config['checkpoint_dir']
        ensure_dir(self.checkpoint_dir)
        temp_file = '{}-{}-temp.pth'.format(self.config['model'], get_local_time())
        self.temp_file = os.path.join(self.checkpoint_dir, temp_file)

        temp_best_file = '{}-{}-temp-best.pth'.format(self.config['model'], get_local_time())
        self.temp_best_file = os.path.join(self.checkpoint_dir, temp_best_file)

        saved_model_file = '{}-{}.pth'.format(self.config['model'], get_local_time())
        self.saved_model_file = os.path.join(self.checkpoint_dir, saved_model_file)

        self.stopping_step = config['stopping_step']
        self.valid_metric_bigger = config['valid_metric_bigger']
        self.cur_step = 0
        self.best_valid_score = -np.inf if self.valid_metric_bigger else np.inf
        self.best_valid_result = None

    def _interaction_to_sparse(self, dataloader):
        r"""Convert data format from interaction to sparse or numpy

        Args:
            dataloader (DecisionTreeDataLoader): DecisionTreeDataLoader dataloader.
        Returns:
            cur_data (sparse or numpy): data.
            interaction_np[self.label_field] (numpy): label.
        """
        interaction = dataloader.dataset[:]
        interaction_np = interaction.numpy()
        cur_data = np.array([])
        columns = []
        for key, value in interaction_np.items():
            value = np.resize(value, (value.shape[0], 1))
            if key != self.label_field:
                columns.append(key)
                if cur_data.shape[0] == 0:
                    cur_data = value
                else:
                    cur_data = np.hstack((cur_data, value))

        if self.convert_token_to_onehot:
            from scipy import sparse
            from scipy.sparse import dok_matrix
            convert_col_list = dataloader.dataset.convert_col_list
            hash_count = dataloader.dataset.hash_count

            new_col = cur_data.shape[1] - len(convert_col_list)
            for key, values in hash_count.items():
                new_col = new_col + values
            onehot_data = dok_matrix((cur_data.shape[0], new_col))

            cur_j = 0
            new_j = 0

            for key in columns:
                if key in convert_col_list:
                    for i in range(cur_data.shape[0]):
                        onehot_data[i, int(new_j + cur_data[i, cur_j])] = 1
                    new_j = new_j + hash_count[key] - 1
                else:
                    for i in range(cur_data.shape[0]):
                        onehot_data[i, new_j] = cur_data[i, cur_j]
                cur_j = cur_j + 1
                new_j = new_j + 1

            cur_data = sparse.csc_matrix(onehot_data)

        return cur_data, interaction_np[self.label_field]

    def _interaction_to_lib_datatype(self, dataloader):
        pass

    def _valid_epoch(self, valid_data):
        r"""

        Args:
            valid_data (DecisionTreeDataLoader): DecisionTreeDataLoader, which is the same with GeneralDataLoader.
        """
        valid_result = self.evaluate(valid_data, load_best_model=False)
        valid_score = calculate_valid_score(valid_result, self.valid_metric)
        return valid_score, valid_result

    def _save_checkpoint(self, epoch):
        r"""Store the model parameters information and training information.

        Args:
            epoch (int): the current epoch id

        """
        state = {
            'config': self.config,
            'epoch': epoch,
            'cur_step': self.cur_step,
            'best_valid_score': self.best_valid_score,
            'state_dict': self.temp_best_file,
            'other_parameter': None
        }
        torch.save(state, self.saved_model_file)

    def fit(self, train_data, valid_data=None, verbose=True, saved=True, show_progress=False):
        for epoch_idx in range(self.epochs):
            self._train_at_once(train_data, valid_data)

            if (epoch_idx + 1) % self.eval_step == 0:
                # evaluate
                valid_start_time = time()
                valid_score, valid_result = self._valid_epoch(valid_data)

                self.best_valid_score, self.cur_step, stop_flag, update_flag = early_stopping(
                    valid_score,
                    self.best_valid_score,
                    self.cur_step,
                    max_step=self.stopping_step,
                    bigger=self.valid_metric_bigger
                )

                valid_end_time = time()
                valid_score_output = (set_color("epoch %d evaluating", 'green') + " [" + set_color("time", 'blue')
                                    + ": %.2fs, " + set_color("valid_score", 'blue') + ": %f]") % \
                                     (epoch_idx, valid_end_time - valid_start_time, valid_score)
                valid_result_output = set_color('valid result', 'blue') + ': \n' + dict2str(valid_result)
                if verbose:
                    self.logger.info(valid_score_output)
                    self.logger.info(valid_result_output)
                self.tensorboard.add_scalar('Vaild_score', valid_score, epoch_idx)

                if update_flag:
                    if saved:
                        self.model.save_model(self.temp_best_file)
                        self._save_checkpoint(epoch_idx)
                        update_output = set_color('Saving current best', 'blue') + ': %s' % self.saved_model_file
                        if verbose:
                            self.logger.info(update_output)
                    self.best_valid_result = valid_result

                if stop_flag:
                    stop_output = 'Finished training, best eval result in epoch %d' % \
                                  (epoch_idx - self.cur_step * self.eval_step)
                    if self.temp_file:
                        os.remove(self.temp_file)
                    if verbose:
                        self.logger.info(stop_output)
                    break

        return self.best_valid_score, self.best_valid_result

    def evaluate(self, eval_data, load_best_model=True, model_file=None, show_progress=False):
        raise NotImplementedError

    def _train_at_once(self, train_data, valid_data):
        raise NotImplementedError


class xgboostTrainer(DecisionTreeTrainer):
    """xgboostTrainer is designed for XGBOOST.

    """

    def __init__(self, config, model):
        super(xgboostTrainer, self).__init__(config, model)

        self.xgb = __import__('xgboost')
        self.boost_model = config['xgb_model']
        self.silent = config['xgb_silent']
        self.nthread = config['xgb_nthread']

        # train params
        self.params = config['xgb_params']
        self.num_boost_round = config['xgb_num_boost_round']
        self.evals = ()
        self.early_stopping_rounds = config['xgb_early_stopping_rounds']
        self.evals_result = {}
        self.verbose_eval = config['xgb_verbose_eval']
        self.callbacks = None
        self.deval = None
        self.eval_pred = self.eval_true = None

    def _interaction_to_lib_datatype(self, dataloader):
        r"""Convert data format from interaction to DMatrix

        Args:
            dataloader (DecisionTreeDataLoader): xgboost dataloader.
        Returns:
            DMatrix: Data in the form of 'DMatrix'.
        """
        data, label = self._interaction_to_sparse(dataloader)
        return self.xgb.DMatrix(data=data, label=label, silent=self.silent, nthread=self.nthread)

    def _train_at_once(self, train_data, valid_data):
        r"""

        Args:
            train_data (DecisionTreeDataLoader): DecisionTreeDataLoader, which is the same with GeneralDataLoader.
            valid_data (DecisionTreeDataLoader): DecisionTreeDataLoader, which is the same with GeneralDataLoader.
        """
        self.dtrain = self._interaction_to_lib_datatype(train_data)
        self.dvalid = self._interaction_to_lib_datatype(valid_data)
        self.evals = [(self.dtrain, 'train'), (self.dvalid, 'valid')]
        self.model = self.xgb.train(
            self.params,
            self.dtrain,
            self.num_boost_round,
            self.evals,
            early_stopping_rounds=self.early_stopping_rounds,
            evals_result=self.evals_result,
            verbose_eval=self.verbose_eval,
            xgb_model=self.boost_model,
            callbacks=self.callbacks
        )

        self.model.save_model(self.temp_file)
        self.boost_model = self.temp_file

    def evaluate(self, eval_data, load_best_model=True, model_file=None, show_progress=False):
        if load_best_model:
            if model_file:
                checkpoint_file = model_file
            else:
                checkpoint_file = self.temp_best_file
            self.model.load_model(checkpoint_file)

        self.deval = self._interaction_to_lib_datatype(eval_data)
        self.eval_true = torch.Tensor(self.deval.get_label())
        self.eval_pred = torch.Tensor(self.model.predict(self.deval))

        self.eval_collector.eval_collect(self.eval_pred, self.eval_true)
        result = self.evaluator.evaluate(self.eval_collector.get_data_struct())
        return result


class lightgbmTrainer(DecisionTreeTrainer):
    """lightgbmTrainer is designed for lightgbm.

    """

    def __init__(self, config, model):
        super(lightgbmTrainer, self).__init__(config, model)

        self.lgb = __import__('lightgbm')
        self.boost_model = config['lgb_model']
        self.silent = config['lgb_silent']

        # train params
        self.params = config['lgb_params']
        self.num_boost_round = config['lgb_num_boost_round']
        self.evals = ()
        self.early_stopping_rounds = config['lgb_early_stopping_rounds']
        self.evals_result = {}
        self.verbose_eval = config['lgb_verbose_eval']
        self.learning_rates = config['lgb_learning_rates']
        self.callbacks = None
        self.deval_data = self.deval_label = None
        self.eval_pred = self.eval_true = None

    def _interaction_to_lib_datatype(self, dataloader):
        r"""Convert data format from interaction to Dataset

        Args:
            dataloader (DecisionTreeDataLoader): xgboost dataloader.
        Returns:
            dataset(lgb.Dataset): Data in the form of 'lgb.Dataset'.
        """
        data, label = self._interaction_to_sparse(dataloader)
        return self.lgb.Dataset(data=data, label=label, silent=self.silent)

    def _train_at_once(self, train_data, valid_data):
        r"""

        Args:
            train_data (DecisionTreeDataLoader): DecisionTreeDataLoader, which is the same with GeneralDataLoader.
            valid_data (DecisionTreeDataLoader): DecisionTreeDataLoader, which is the same with GeneralDataLoader.
        """
        self.dtrain = self._interaction_to_lib_datatype(train_data)
        self.dvalid = self._interaction_to_lib_datatype(valid_data)
        self.evals = [self.dtrain, self.dvalid]
        self.model = self.lgb.train(
            self.params,
            self.dtrain,
            self.num_boost_round,
            self.evals,
            early_stopping_rounds=self.early_stopping_rounds,
            evals_result=self.evals_result,
            verbose_eval=self.verbose_eval,
            learning_rates=self.learning_rates,
            init_model=self.boost_model,
            callbacks=self.callbacks
        )

        self.model.save_model(self.temp_file)
        self.boost_model = self.temp_file

    def evaluate(self, eval_data, load_best_model=True, model_file=None, show_progress=False):
        if load_best_model:
            if model_file:
                checkpoint_file = model_file
            else:
                checkpoint_file = self.temp_best_file
            self.model = self.lgb.Booster(model_file=checkpoint_file)

        self.deval_data, self.deval_label = self._interaction_to_sparse(eval_data)
        self.eval_true = torch.Tensor(self.deval_label)
        self.eval_pred = torch.Tensor(self.model.predict(self.deval_data))

        self.eval_collector.eval_collect(self.eval_pred, self.eval_true)
        result = self.evaluator.evaluate(self.eval_collector.get_data_struct())
        return result


class RaCTTrainer(PretrainTrainer):
    r"""RaCTTrainer is designed for RaCT, which is an actor-critic reinforcement learning based general recommenders.
        It includes three training stages: actor pre-training, critic pre-training and actor-critic training.

        """

    def __init__(self, config, model):
        super(RaCTTrainer, self).__init__(config, model)

    def fit(self, train_data, valid_data=None, verbose=True, saved=True, show_progress=False, callback_fn=None):
        if self.model.train_stage == 'actor_pretrain':
            return self.pretrain(train_data, verbose, show_progress)
        elif self.model.train_stage == "critic_pretrain":
            return self.pretrain(train_data, verbose, show_progress)
        elif self.model.train_stage == 'finetune':
            return super().fit(train_data, valid_data, verbose, saved, show_progress, callback_fn)
        else:
            raise ValueError(
                "Please make sure that the 'train_stage' is "
                "'actor_pretrain', 'critic_pretrain' or 'finetune'!"
            )


class RecVAETrainer(Trainer):
    r"""RecVAETrainer is designed for RecVAE, which is a general recommender.

    """

    def __init__(self, config, model):
        super(RecVAETrainer, self).__init__(config, model)
        self.n_enc_epochs = config['n_enc_epochs']
        self.n_dec_epochs = config['n_dec_epochs']

        self.optimizer_encoder = self._build_optimizer(self.model.encoder.parameters())
        self.optimizer_decoder = self._build_optimizer(self.model.decoder.parameters())

    def _train_epoch(self, train_data, epoch_idx, loss_func=None, show_progress=False):
        self.optimizer = self.optimizer_encoder
        encoder_loss_func = lambda data: self.model.calculate_loss(data, encoder_flag=True)
        for epoch in range(self.n_enc_epochs):
            super()._train_epoch(train_data, epoch_idx, loss_func=encoder_loss_func, show_progress=show_progress)

        self.model.update_prior()
        loss = 0.0
        self.optimizer = self.optimizer_decoder
        decoder_loss_func = lambda data: self.model.calculate_loss(data, encoder_flag=False)
        for epoch in range(self.n_dec_epochs):
            loss += super()._train_epoch(
                train_data, epoch_idx, loss_func=decoder_loss_func, show_progress=show_progress
            )
        return loss


class MetaTrainer(Trainer):
    def __init__(self, config, model):
        super().__init__(config, model)
        # 5种采样长度, 5种任务
        if config["meta_task_lengths"][0] == "uniform":
            self.task_lengths = self._generate_task_lengths(*config["meta_task_lengths"][1:])
        else:
            self.task_lengths = config["meta_task_lengths"]
        self.padding_len = self.task_lengths[-1]
        self.update_lr = config["meta_update_lr"]
        self.update_step = config["meta_update_step"]
        # self.sample_size = config["meta_sample_size"]
        self.aimed_parameters = []
        self.param_names = []
        self.meta_optimizer = optim.Adam(params=self.model.parameters(), lr=self.learning_rate, weight_decay=self.config["meta_decay"])
        self.scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer=self.optimizer, T_max=self.config["meta_pretrain_step"],eta_min=1e-4)
        # MAML只优化embedding
        # for p, w in self.model.named_parameters():
        #     if "embedding" in p:
        #         self.param_names.append(p)
        # x        self.aimed_parameters.append(getattr(self.model, p.split(".")[0]).weight)
        #         # self.origin_parameters.append(w.clone().detach())
    
    def _generate_task_lengths(self, a, b, std, mean):
        # truncated normal distribution
        a, b = (a - mean) / std, (b - mean) / std
        samples = ss.truncnorm.rvs(a, b, loc=mean, scale=std, size=5)
        samples = sorted(list(set(list(map(lambda x:int(x), samples.tolist())))))
        self.padding_len = samples[-1]
        return samples

    def _get_per_step_loss_importance_vector(self, epoch):
        """
        Generates a tensor of dimensionality (num_inner_loop_steps) indicating the importance of each step's target
        loss towards the optimization loss.
        :return: A tensor to be used to compute the weighted average of the loss, useful for
        the MSL (Multi Step Loss) mechanism.
        """
        loss_weights = np.ones(shape=(self.update_step)) * (
                1.0 / self.update_step)
        decay_rate = 1.0 / self.update_step / self.config["meta_pretrain_step"]
        min_value_for_non_final_losses = 0.03 / self.update_step
        for i in range(len(loss_weights) - 1):
            curr_value = np.maximum(loss_weights[i] - (epoch * decay_rate), min_value_for_non_final_losses)
            loss_weights[i] = curr_value

        curr_value = np.minimum(
            loss_weights[-1] + (epoch * (self.update_step - 1) * decay_rate),
            1.0 - ((self.update_step - 1) * min_value_for_non_final_losses))
        loss_weights[-1] = curr_value
        loss_weights = torch.Tensor(loss_weights).to(device=self.device)
        return loss_weights

    def _cal_meta_loss(self, meta_train):
        self._update_weights()
        batch_support, batch_query = meta_train
        losses_q = [0 for _ in range(self.update_step)]  # losses_q[i] is the loss on step i

        for task in batch_support:
            # self._set_weights(self.origin_parameters)
            # 1. run the i-th task and compute loss for k=0
            loss = self.model.calculate_loss_meta(batch_support[task])
            grad = torch.autograd.grad(loss, self.aimed_parameters)
            local_aimed_parameters = list(map(lambda p: p[1] - self.update_lr * p[0], zip(grad, self.aimed_parameters)))
            # self._set_weights(self.aimed_parameters)
            for k in range(0, self.update_step):
                # 1. run the i-th task and compute loss for k=1~K-1
                loss = self.model.calculate_loss_meta(batch_support[task],
                 {self.param_names[i]: local_aimed_parameters[i] 
                    for i in range(len(self.param_names))})
                # 2. compute grad on theta_pi
                grad = torch.autograd.grad(loss, local_aimed_parameters)
                # 3. theta_pi = theta_pi - train_lr * grad
                local_aimed_parameters = list(map(lambda p: p[1] - self.update_lr * p[0], zip(grad, local_aimed_parameters)))
                # self._set_weights(self.aimed_parameters)
                loss_q = self.model.calculate_loss_meta(batch_query[task])
                # loss_q will be overwritten and just keep the loss_q on last update step.
                losses_q[k] += loss_q

        # end of all tasks
        # sum over all losses on query set across all tasks
        loss_q = torch.mul(self.current_loss_weights, torch.stack(losses_q, dim=0)).sum() / len(batch_support)
        self._check_nan(loss_q)
        return loss_q


    def _set_weights(self, weights):
        for p, w in zip(self.param_names, weights):
            layer = getattr(self.model, p.split(".")[0])
            setattr(layer.weight, "data", w)
        self.aimed_parameters = []
        for p, w in self.model.named_parameters():
            if "embedding" in p:
                self.aimed_parameters.append(getattr(self.model, p.split(".")[0]).weight)
        pass

    def _update_weights(self):
        self.aimed_parameters = []
        self.param_names = []
        for p, w in self.model.named_parameters():
            if "embedding" in p:
                self.param_names.append(p)
                self.aimed_parameters.append(getattr(self.model, p.split(".")[0]).weight)
        pass

    def _flatten_meta_train_data(self, shots=400):
        # shuffle
        random.shuffle(self.batch_meta_train_data)

        support_data = defaultdict(list)
        query_data = defaultdict(list)
        for support, query in self.batch_meta_train_data:
            for t in support:
                if len(support_data[t]) < shots:
                    support_data[t].extend(support[t])
            for t in query:
                if len(query_data[t]) < shots:
                    query_data[t].extend(query[t])

        for d in (support_data, query_data):
            for t in d:
                d[t] = torch.stack(d[t]).long()

        self.logger.info("shot counts: " + "\t".join([f"|{l}|: {support_data[l].shape[0]}" for l in self.task_lengths]))
        
        return support_data, query_data

    def _padding(self, seq):
        if seq.shape[0] == self.padding_len:
            return seq
        padded = torch.zeros((self.padding_len))
        padded[:seq.shape[0]] = seq
        return padded
    
    def _generate_meta_train_data(self, batch_interaction):
        lens = [i for i in self.task_lengths]
        support_data = defaultdict(list)
        query_data = defaultdict(list)
        # 在一个batch中随机选取子序列长度采样
        for seq_idx in range(batch_interaction["item_length"].shape[0]):
            lens_i = [l for l in lens]
            sub_length = random.choice(lens_i)
            sprt = []
            qry = []
            # 选择合适的采样长度
            seq = batch_interaction["item_id_list"][seq_idx]
            seq_len = batch_interaction["item_length"][seq_idx]
            if seq_len<2*lens[0]:
                continue
            while seq_len<2*sub_length:
                lens_i.remove(sub_length)
                sub_length = random.choice(lens_i)
            # 采样
            sub_seqs = torch.split(seq, sub_length)[:seq_len//sub_length]
            for i in range(len(sub_seqs)-1):
                sprt.append(self._padding(sub_seqs[i]))
                qry.append(self._padding(sub_seqs[i+1]))
            query_data[sub_length].extend(qry)
            support_data[sub_length].extend(sprt)

        support_data, query_data = dict(support_data), dict(query_data)
        for d in (support_data, query_data):
            for t in d:
                d[t] = torch.stack(d[t]).long()

        return support_data, query_data

    def _pretrain_meta(self, pretrain_data, epoch_idx, loss_func=None, show_progress=False):
        """
        :param support: dict   {task_num: setsize, L}
        :param query: dict   {task_num: querysize, L}
        :return:
        """
        self.current_loss_weights = self._get_per_step_loss_importance_vector(epoch_idx)
        total_meta_loss = 0.
        for batch_data in tqdm(meta_minibatch(pretrain_data, batch_size=self.config["meta_batch_size"]), desc=set_color(f"Pretrain {epoch_idx:>5}", 'pink')):
            loss_q = self._cal_meta_loss(batch_data)
            self.meta_optimizer.zero_grad()
            loss_q.backward()
            self.meta_optimizer.step()
            total_meta_loss += loss_q.item()
        self.scheduler.step()
        return total_meta_loss

    def _train_epoch_and_meta(self, train_data, epoch_idx, loss_func=None, show_progress=False):
        """
        :param support: dict   {task_num: setsize, L}
        :param query: dict   {task_num: querysize, L}
        :return:
        """
        self.model.train()
        total_loss = 0.
        iter_data = (
            tqdm(
                train_data,
                total=len(train_data),
                ncols=100,
                desc=set_color(f"Train {epoch_idx:>5}", 'pink'),
            ) if show_progress else train_data
        )
        for batch_idx, interaction in enumerate(iter_data):
            interaction = interaction.to(self.device)
            self.optimizer.zero_grad()
            main_loss = self.model.calculate_loss(interaction)
            # meta learning
            meta_data_batch = self._generate_meta_train_data(interaction)
            meta_loss = self._cal_meta_loss(meta_data_batch)
            loss = meta_loss + main_loss
            total_loss += loss.item()
            self._check_nan(loss)
            loss.backward()
            self.optimizer.step()
            if self.gpu_available and show_progress:
                iter_data.set_postfix_str(set_color('GPU RAM: ' + get_gpu_usage(self.device), 'yellow'))
        return total_loss

    def fit(self, train_data, valid_data=None, verbose=True, saved=True, show_progress=False, callback_fn=None):

        self.eval_collector.data_collect(train_data)
        valid_step = 0


        for epoch_idx in range(self.config["meta_pretrain_step"]):
            # sample tasks and prepare for meta training data
            if self.config["meta_task_lengths"][0] == "uniform":
                # generate normally distributed task lengths from pre-defined a, b, std and mean
                self.task_lengths = self._generate_task_lengths(3, 50, 7, 6)
            self.batch_meta_train_data = []
            for interaction in train_data:
                self.batch_meta_train_data.append(self._generate_meta_train_data(interaction))
            pretrain_data = self._flatten_meta_train_data(shots=self.config["meta_shots"])
            # train
            training_start_time = time()
            train_loss_meta = self._pretrain_meta(pretrain_data, epoch_idx, show_progress=show_progress)
            train_loss = train_loss_meta
            training_end_time = time()
            train_loss_output = \
                self._generate_train_loss_output(epoch_idx, training_start_time, training_end_time, train_loss)
            if verbose:
                self.logger.info(train_loss_output)
            # self._add_train_loss_to_tensorboard(epoch_idx, train_loss)
            self.wandblogger.log_metrics({'epoch': epoch_idx, 'train_loss': train_loss, 'train_step':epoch_idx}, head='train')
        # del variables for pretraining
        del self.aimed_parameters
        
        # self.model._forget_weights()

        for epoch_idx in range(self.start_epoch, self.epochs):
            training_start_time = time()
            train_loss = self._train_epoch(train_data, epoch_idx, show_progress=show_progress)
            self.train_loss_dict[epoch_idx] = sum(train_loss) if isinstance(train_loss, tuple) else train_loss
            training_end_time = time()
            train_loss_output = \
                self._generate_train_loss_output(epoch_idx, training_start_time, training_end_time, train_loss)
            if verbose:
                self.logger.info(train_loss_output)
            # self._add_train_loss_to_tensorboard(epoch_idx, train_loss)
            self.wandblogger.log_metrics({'epoch': epoch_idx, 'train_loss': train_loss, 'train_step':epoch_idx}, head='train')

            # eval
            if self.eval_step <= 0 or not valid_data:
                if saved:
                    self._save_checkpoint(epoch_idx)
                    update_output = set_color('Saving current', 'blue') + ': %s' % self.saved_model_file
                    if verbose:
                        self.logger.info(update_output)
                continue
            if (epoch_idx + 1) % self.eval_step == 0:
                valid_start_time = time()
                valid_score, valid_result = self._valid_epoch(valid_data, show_progress=show_progress)
                self.best_valid_score, self.cur_step, stop_flag, update_flag = early_stopping(
                    valid_score,
                    self.best_valid_score,
                    self.cur_step,
                    max_step=self.stopping_step,
                    bigger=self.valid_metric_bigger
                )
                valid_end_time = time()
                valid_score_output = (set_color("epoch %d evaluating", 'green') + " [" + set_color("time", 'blue')
                                    + ": %.2fs, " + set_color("valid_score", 'blue') + ": %f]") % \
                                     (epoch_idx, valid_end_time - valid_start_time, valid_score)
                valid_result_output = set_color('valid result', 'blue') + ': \n' + dict2str(valid_result)
                if verbose:
                    self.logger.info(valid_score_output)
                    self.logger.info(valid_result_output)
                # self.tensorboard.add_scalar('Vaild_score', valid_score, epoch_idx)
                self.wandblogger.log_metrics({**valid_result, 'valid_step': valid_step}, head='valid')

                if update_flag:
                    if saved:
                        self._save_checkpoint(epoch_idx)
                        update_output = set_color('Saving current best', 'blue') + ': %s' % self.saved_model_file
                        if verbose:
                            self.logger.info(update_output)
                    self.best_valid_result = valid_result

                if callback_fn:
                    callback_fn(epoch_idx, valid_score)

                if stop_flag:
                    stop_output = 'Finished training, best eval result in epoch %d' % \
                                  (epoch_idx - self.cur_step * self.eval_step)
                    if verbose:
                        self.logger.info(stop_output)
                    break

                valid_step+=1

        # self._add_hparam_to_tensorboard(self.best_valid_score)
        return self.best_valid_score, self.best_valid_result
