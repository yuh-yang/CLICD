# -*- coding: utf-8 -*-
# @Time    : 2020/9/18 12:08
# @Author  : Hui Wang
# @Email   : hui.wang@ruc.edu.cn

r"""
BERT4Rec
################################################

Reference:
    Fei Sun et al. "BERT4Rec: Sequential Recommendation with Bidirectional Encoder Representations from Transformer."
    In CIKM 2019.

Reference code:
    The authors' tensorflow implementation https://github.com/FeiSun/BERT4Rec

"""
import logging
from collections import defaultdict
from copy import deepcopy
import random

import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from recbole.model.abstract_recommender import SequentialRecommender
from recbole.model.layers import TransformerEncoder, CLLayer

import dgl
from dgl.nn.pytorch import GraphConv

from scipy.sparse import csr_matrix
from sklearn.metrics.pairwise import cosine_similarity


def graph_dual_neighbor_readout(g: dgl.DGLGraph, aug_g: dgl.DGLGraph, node_ids, features):
    _, all_neighbors = g.out_edges(node_ids)
    all_nbr_num = g.out_degrees(node_ids)
    _, foreign_neighbors = aug_g.out_edges(node_ids)
    for_nbr_num = aug_g.out_degrees(node_ids)
    all_neighbors = [set(t.tolist())
                     for t in all_neighbors.split(all_nbr_num.tolist())]
    foreign_neighbors = [set(t.tolist())
                         for t in foreign_neighbors.split(for_nbr_num.tolist())]
    # sample foreign neighbors
    for i, nbrs in enumerate(foreign_neighbors):
        if len(nbrs) > 10:
            nbrs = random.sample(nbrs, 10)
            foreign_neighbors[i] = set(nbrs)
    civil_neighbors = [all_neighbors[i]-foreign_neighbors[i]
                       for i in range(len(all_neighbors))]
    # sample civil neighbors
    for i, nbrs in enumerate(civil_neighbors):
        if len(nbrs) > 10:
            nbrs = random.sample(nbrs, 10)
            civil_neighbors[i] = set(nbrs)
    for_lens = [len(t) for t in foreign_neighbors]
    cv_lens = torch.tensor([len(t)
                           for t in civil_neighbors], dtype=torch.int16)
    zero_indicies = (cv_lens == 0).nonzero().view(-1).tolist()
    cv_lens = cv_lens[cv_lens > 0].tolist()
    foreign_neighbors = torch.cat(
        [torch.tensor(list(s), dtype=torch.long) for s in foreign_neighbors])
    civil_neighbors = torch.cat(
        [torch.tensor(list(s), dtype=torch.long) for s in civil_neighbors])
    cv_feats = features[civil_neighbors].split(cv_lens)
    cv_feats = [t.mean(dim=0) for t in cv_feats]
    # insert zero vector for zero-length neighbors
    if len(zero_indicies) > 0:
        for i in zero_indicies:
            cv_feats.insert(i, torch.zeros_like(features[0]))
    for_feats = features[foreign_neighbors].split(for_lens)
    for_feats = [t.mean(dim=0) for t in for_feats]
    return torch.stack(cv_feats, dim=0), torch.stack(for_feats, dim=0)


def graph_neighbor_readout(g: dgl.DGLGraph, node_ids, features):
    # Readout the neighbors of the given node.
    _, neighbors = g.out_edges(node_ids)
    neighbor_nums = g.out_degrees(node_ids)
    neighbor_features = features[neighbors].split(neighbor_nums.tolist())
    neighbor_features = [t.mean(dim=0) for t in neighbor_features]
    return torch.stack(neighbor_features, dim=0)


def collect_user_sequence(iter_data):
    # Collect user sequences from the dataset.
    user_sequences = defaultdict(list)
    for interaction in iter_data:
        item_seq = interaction["item_id_list"].tolist()
        last_item = interaction["item_id"].tolist()
        seq_length = interaction["item_length"].tolist()
        uid = interaction["session_id"].tolist()
        for i, u in enumerate(uid):
            user_sequences[u].extend(
                item_seq[i][:seq_length[i]] + [last_item[i]])
    return user_sequences


def build_usim(user_sequences, n_items):
    row = []
    col = []
    for usr, itms in user_sequences.items():
        col.extend(list(itms))
        row.extend([usr]*len(itms))
    row = np.array(row)
    col = np.array(col)
    feature_mtx = csr_matrix(
        ([1]*len(row), (row, col)), shape=(max(user_sequences.keys())+1, n_items+1))
    similarity = cosine_similarity(feature_mtx)
    # n_friends + 1
    return similarity.argsort()[:, -(4+1):]


def graph_mask(g: dgl.DGLGraph, mask_indices):
    edge_ids = g.edge_ids(mask_indices[0], mask_indices[1])
    masked_g = deepcopy(g)
    masked_g.edata["w"][edge_ids] = 0.
    return masked_g


def graph_augment(g: dgl.DGLGraph, user_ids, user_edges):
    # Augment the graph with the item sequence, deleting co-occurrence edges in the batched sequences
    # generating indicies like: [1,2] [2,3] ... as the co-occurrence rel.
    # indexing edge data using node indicies and delete them
    # for edge weights, delete them from the raw data using indexed edges
    user_ids = user_ids.cpu().numpy()
    node_indicies_a = np.concatenate(
        user_edges.loc[user_ids, "item_edges_a"].to_numpy())
    node_indicies_b = np.concatenate(
        user_edges.loc[user_ids, "item_edges_b"].to_numpy())
    node_indicies_a = torch.from_numpy(
        node_indicies_a).to(g.device)
    node_indicies_b = torch.from_numpy(
        node_indicies_b).to(g.device)
    edge_ids = g.edge_ids(node_indicies_a, node_indicies_b)

    aug_g: dgl.DGLGraph = deepcopy(g)
    # The features for the removed edges will be removed accordingly.
    aug_g.remove_edges(edge_ids)
    return aug_g


def graph_dropout(g: dgl.DGLGraph, keep_prob):
    # Firstly mask selected edge values, returns the true values along with the masked graph.
    origin_edge_w = g.edata['w']

    drop_size = int((1-keep_prob) * g.num_edges())
    random_index = torch.randint(
        0, g.num_edges(), (drop_size,), device=g.device)
    mask = torch.zeros(g.num_edges(), dtype=torch.uint8,
                       device=g.device).bool()
    mask[random_index] = True
    g.edata['w'].masked_fill_(mask, 0)

    return origin_edge_w, g


class GCN(nn.Module):
    def __init__(self, in_dim, out_dim, dropout_prob=0.7):
        super(GCN, self).__init__()
        self.dropout_prob = dropout_prob
        self.layer = GraphConv(in_dim, out_dim, weight=False,
                               bias=False, allow_zero_in_degree=True)

    def forward(self, graph, feature):
        origin_w, graph = graph_dropout(graph, 1-self.dropout_prob)
        embs = [feature]
        for i in range(2):
            feature = self.layer(graph, feature, edge_weight=graph.edata['w'])
            # TODO:这里需不需要dropout??
            F.dropout(feature, p=0.2, training=self.training)
            embs.append(feature)
        embs = torch.stack(embs, dim=1)
        # print(embs.size())
        final_emb = torch.mean(embs, dim=1)
        # recover edge weight
        graph.edata['w'] = origin_w
        return final_emb


class CLICD(SequentialRecommender):

    def __init__(self, config, dataset, external_data):
        super(CLICD, self).__init__(config, dataset)

        self.config = config
        # load parameters info
        self.device = config["device"]
        self.n_layers = config['n_layers']
        self.n_heads = config['n_heads']
        self.hidden_size = config['hidden_size']  # same as embedding_size
        # the dimensionality in feed-forward layer
        self.inner_size = config['inner_size']
        self.hidden_dropout_prob = config['hidden_dropout_prob']
        self.attn_dropout_prob = config['attn_dropout_prob']
        self.hidden_act = config['hidden_act']
        self.layer_norm_eps = config['layer_norm_eps']

        self.mask_ratio = config['mask_ratio']

        self.loss_type = config['loss_type']
        self.initializer_range = config['initializer_range']

        # load dataset info
        # define layers and loss
        self.item_embedding = nn.Embedding(
            self.n_items + 1, self.hidden_size, padding_idx=0)  # mask token add 1
        self.position_embedding = nn.Embedding(
            self.max_seq_length + 1, self.hidden_size)  # add mask_token at the last
        self.trm_encoder = TransformerEncoder(
            n_layers=self.n_layers,
            n_heads=self.n_heads,
            hidden_size=self.hidden_size,
            inner_size=self.inner_size,
            hidden_dropout_prob=self.hidden_dropout_prob,
            attn_dropout_prob=self.attn_dropout_prob,
            hidden_act=self.hidden_act,
            layer_norm_eps=self.layer_norm_eps
        )
        self.LayerNorm = nn.LayerNorm(
            self.hidden_size, eps=self.layer_norm_eps)
        self.dropout = nn.Dropout(self.hidden_dropout_prob)

        # Contrastive Learning
        self.contrastive_learning_layer = CLLayer(self.hidden_size, tau=config['cl_temp'])

        # Fusion Attn
        self.attn_weights = nn.Parameter(
            torch.Tensor(self.hidden_size, self.hidden_size))
        self.attn = nn.Parameter(torch.Tensor(1, self.hidden_size))
        nn.init.normal_(self.attn, std=0.02)
        nn.init.normal_(self.attn_weights, std=0.02)

        # task length lable
        self.task_length_label = nn.Embedding(6, self.hidden_size)
        nn.init.normal_(self.task_length_label.weight, std=0.02)

        # Global Graph Learning
        # self.cooccurrence_graph = self._build_graph_dgl()
        self.item_adjgraph = external_data["adj_graph"].to(self.device)
        self.user_edges = external_data["user_edges"]
        self.item_simgraph = external_data["sim_graph"].to(self.device)
        self.graph_dropout = config["graph_dropout_prob"]

        self.gcn = GCN(self.hidden_size, self.hidden_size, self.graph_dropout)
        # self.graph_lr_projecting = nn.Parameter(torch.tensor(self.n_items, 10))
        # nn.init.normal_(self.graph_lr_projecting.weight, std=0.02)

        self.layernorm = nn.LayerNorm(
            self.hidden_size, eps=self.layer_norm_eps)

        self.loss_fct = nn.CrossEntropyLoss()

        # we only need compute the loss at the masked position
        try:
            assert self.loss_type in ['CE']
        except AssertionError:
            raise AssertionError("Make sure 'loss_type' be CE!")

        # parameters initialization
        self.apply(self._init_weights)

        # dropout
        # F.dropout(self.item_embedding.weight.data, p=0.5, inplace=True)

    def _subgraph_agreement(self, aug_g, raw_output_all, raw_output_seq, valid_items_flatten):
        # here it firstly removes items of the sequence in the cooccurrence graph, and then performs the gnn aggregation, and finally calculates the item-wise agreement score.
        aug_output_seq = self.gcn_forward(g=aug_g)[valid_items_flatten]
        civil_nbr_ro, foreign_nbr_ro = graph_dual_neighbor_readout(
            self.item_adjgraph, aug_g, valid_items_flatten, raw_output_all)

        # Settings 3: half-interactive and mean
        # view1_sim = F.cosine_similarity(raw_output_seq, self.weight_mlp_2x(torch.cat([aug_output[valid_items_flatten], foreign_nbr_ro], dim=1)), eps=1e-12)
        # view2 = F.cosine_similarity(civil_nbr_ro, foreign_nbr_ro, eps=1e-12)
        # agreement = F.cosine_similarity(view1, view2, eps=1e-12)

        # # Settings 1: interactive
        # view1_sim = F.cosine_similarity(raw_output_seq, self.weight_mlp_2x(torch.cat([aug_output[valid_items_flatten], foreign_nbr_ro], dim=1)), eps=1e-12)
        # view2_sim = F.cosine_similarity(
        #     self.weight_mlp_2x(torch.cat([civil_nbr_ro, raw_output_seq], dim=1)), foreign_nbr_ro, eps=1e-12)
        # agreement = (view1_sim+view2_sim)/2

        # Settings 2: Triple Mean-pooling
        view1_sim = F.cosine_similarity(
            raw_output_seq, aug_output_seq, eps=1e-12)
        view2_sim = F.cosine_similarity(
            raw_output_seq, foreign_nbr_ro, eps=1e-12)
        view3_sim = F.cosine_similarity(
            civil_nbr_ro, foreign_nbr_ro, eps=1e-12)
        agreement = (view1_sim+view2_sim+view3_sim)/3
        agreement = torch.sigmoid(agreement)
        # agreement = torch.exp(agreement)
        agreement = (agreement - agreement.min()) / \
            (agreement.max() - agreement.min())
        agreement = (self.config["weight_mean"] / agreement.mean()) * agreement
        return agreement

    def _init_weights(self, module):
        """ Initialize the weights """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.initializer_range)
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    def get_attention_mask(self, item_seq, task_label=False):
        """Generate bidirectional attention mask for multi-head attention."""
        if task_label:
            label_pos = torch.ones((item_seq.size(0), 1), device=self.device)
            item_seq = torch.cat((label_pos, item_seq), dim=1)
        attention_mask = (item_seq > 0).long()
        extended_attention_mask = attention_mask.unsqueeze(
            1).unsqueeze(2)  # torch.int64
        # bidirectional mask
        extended_attention_mask = extended_attention_mask.to(
            dtype=next(self.parameters()).dtype)  # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        return extended_attention_mask

    def _padding_sequence(self, sequence, max_length):
        # 0在后面的mask, 和原版BERT4Rec不同.
        pad_len = max_length - len(sequence)
        sequence = sequence + [0] * pad_len
        return sequence

    def gcn_forward(self, g=None):
        item_emb = self.item_embedding.weight
        item_emb = self.dropout(item_emb)
        light_out = self.gcn(g, item_emb)
        return self.layernorm(light_out+item_emb)

    def forward(self, item_seq, item_seq_len, return_all=False):
        position_ids = torch.arange(item_seq.size(
            1), dtype=torch.long, device=item_seq.device)
        position_ids = position_ids.unsqueeze(0).expand_as(item_seq)
        position_embedding = self.position_embedding(position_ids)

        item_emb = self.item_embedding(item_seq)
        input_emb = item_emb + position_embedding
        input_emb = self.LayerNorm(input_emb)
        input_emb = self.dropout(input_emb)

        extended_attention_mask = self.get_attention_mask(item_seq)

        trm_output = self.trm_encoder(
            input_emb, extended_attention_mask, output_all_encoded_layers=True)
        output = trm_output[-1]
        if return_all:
            return output
        output = self.gather_indexes(output, item_seq_len - 1)
        return output  # [B H]

    def multi_hot_embed(self, masked_index, max_length):
        """
        For memory, we only need calculate loss for masked position.
        Generate a multi-hot vector to indicate the masked position for masked sequence, and then is used for
        gathering the masked position hidden representation.

        Examples:
            sequence: [1 2 3 4 5]

            masked_sequence: [1 mask 3 mask 5]

            masked_index: [1, 3]

            max_length: 5

            multi_hot_embed: [[0 1 0 0 0], [0 0 0 1 0]]
        """
        masked_index = masked_index.view(-1)
        multi_hot = torch.zeros(masked_index.size(
            0), max_length, device=masked_index.device)
        multi_hot[torch.arange(masked_index.size(0)), masked_index] = 1
        return multi_hot

    def calculate_loss(self, interaction):
        if self.config["graphcl_enable"]:
            return self.calculate_loss_graphcl(interaction)

        item_seq = interaction[self.ITEM_SEQ]
        last_item = interaction[self.ITEM_ID]
        masked_item_seq, pos_items = self.reconstruct_train_data(
                item_seq, last_item=last_item)
        seq_output = self.forward(masked_item_seq)

        masked_index = (masked_item_seq == self.mask_token)
        # [mask_num, H]
        seq_output = seq_output[masked_index]
        # [item_num, H]
        # [item_num H]
        test_item_emb = self.item_embedding.weight[:self.n_items]
        # [mask_num, item_num]
        logits = torch.mm(seq_output, test_item_emb.transpose(0, 1))

        loss = self.loss_fct(logits, pos_items)

        if torch.isnan(loss):
            print(masked_item_seq.tolist())
            print(masked_index.tolist())
            input()
        return loss

    def calculate_loss_graphcl(self, interaction):
        user_ids = interaction[self.USER_ID]
        item_seq = interaction[self.ITEM_SEQ]
        pos_items = interaction[self.ITEM_ID]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]

        last_items_indices = torch.tensor([i*self.max_seq_length+j for i, j in enumerate(
            item_seq_len - 1)], dtype=torch.long, device=item_seq.device).view(-1)
        # only the last one
        last_items_flatten = item_seq.view(-1)[last_items_indices]
        valid_items_flatten = last_items_flatten
        valid_items_indices = last_items_indices
        # all valid items, sample some to accelerate.
        # valid_items_indices = last_items_indices - 1
        # valid_items_indices = valid_items_indices[valid_items_indices >= 0]
        # valid_items_indices = torch.cat((valid_items_indices, last_items_indices), dim=0)
        # valid_items_indices = torch.unique_consecutive(valid_items_indices)
        # valid_items_flatten = item_seq.view(-1)[valid_items_indices]
        # graph view
        masked_g = self.item_adjgraph
        aug_g = graph_augment(self.item_adjgraph, user_ids, self.user_edges)
        iadj_graph_output_raw = self.gcn_forward(masked_g)
        isim_graph_output_raw = self.gcn_forward(self.item_simgraph)
        iadj_graph_output_seq = iadj_graph_output_raw[valid_items_flatten]
        isim_graph_output_seq = isim_graph_output_raw[valid_items_flatten]

        seq_output = self.forward(item_seq, item_seq_len, return_all=False)
        aug_seq_output = self.forward(item_seq, item_seq_len, return_all=True).view(
            -1, self.config["hidden_size"])[valid_items_indices]
        # First-stage CL, providing CL weights
        # CL weights from augmentation
        mainstream_weights = self._subgraph_agreement(
            aug_g, iadj_graph_output_raw, iadj_graph_output_seq, valid_items_flatten)
        # filtering those len=1, set weight=0.5
        # mainstream_weights = torch.where(item_seq_len > 1, mainstream_weights, 0.5*torch.ones_like(mainstream_weights))
        mainstream_weights[item_seq_len == 1] = 0.5

        expected_weights_distribution = torch.normal(
            self.config["weight_mean"], 0.1, size=mainstream_weights.size()).sort()[0].to(self.device)
        kl_loss = self.config["kl_weight"] * F.kl_div(F.log_softmax(
            mainstream_weights, dim=0).sort()[0], expected_weights_distribution, reduction="batchmean")
        personlization_weights = mainstream_weights.max() - mainstream_weights

        # contrastive learning
        if self.config["cl_ablation"] == "adj":
            cl_loss_adj = self.contrastive_learning_layer.grace_loss(
                aug_seq_output, iadj_graph_output_seq)
            cl_loss = (self.config["graphcl_coefficient"]
                       * (mainstream_weights * cl_loss_adj)).mean()
        elif self.config["cl_ablation"] == "a2s":
            cl_loss_a2s = self.contrastive_learning_layer.grace_loss(
                iadj_graph_output_seq, isim_graph_output_seq)
            cl_loss = (self.config["graphcl_coefficient"]
                       * (personlization_weights * cl_loss_a2s)).mean()
        elif self.config["cl_ablation"] == "nocl":
            cl_loss = torch.zeros(1).to(self.device)
        elif self.config["cl_ablation"] == "static":
            cl_loss_adj = self.contrastive_learning_layer.grace_loss(
                aug_seq_output, iadj_graph_output_seq)
            cl_loss_a2s = self.contrastive_learning_layer.grace_loss(
                iadj_graph_output_seq, isim_graph_output_seq)
            cl_loss = (0.5 * self.config["graphcl_coefficient"] * (
                       cl_loss_adj + cl_loss_a2s)).mean()
        elif self.config["cl_ablation"] == "full":
            cl_loss_adj = self.contrastive_learning_layer.grace_loss(
                aug_seq_output, iadj_graph_output_seq)
            cl_loss_a2s = self.contrastive_learning_layer.grace_loss(
                iadj_graph_output_seq, isim_graph_output_seq)
            cl_loss = (self.config["graphcl_coefficient"] * (mainstream_weights *
                       cl_loss_adj + personlization_weights * cl_loss_a2s)).mean()
        elif self.config["cl_ablation"] == "pushandpull":
            cl_loss_adj = self.contrastive_learning_layer.grace_loss(
                aug_seq_output, iadj_graph_output_seq)
            polar_weights = (mainstream_weights - 0.5).abs()
            cl_loss_push = 2*self.contrastive_learning_layer.push_loss(
                iadj_graph_output_seq, isim_graph_output_seq
            )
            cl_loss = (self.config["graphcl_coefficient"] * (mainstream_weights *
                       cl_loss_adj + polar_weights * cl_loss_push)).mean()
        elif self.config["cl_ablation"] == "vanilla_loss":
            cl_loss_adj = self.contrastive_learning_layer.vanilla_loss(
                aug_seq_output, iadj_graph_output_seq)
            cl_loss_a2s = self.contrastive_learning_layer.vanilla_loss(
                iadj_graph_output_seq, isim_graph_output_seq)
            cl_loss = (self.config["graphcl_coefficient"] * (mainstream_weights *
                       cl_loss_adj + personlization_weights * cl_loss_a2s)).mean()
        elif self.config["cl_ablation"] == "one_neg":
            cl_loss_adj = self.contrastive_learning_layer.vanilla_loss_with_one_negative(
                aug_seq_output, iadj_graph_output_seq)
            cl_loss_a2s = self.contrastive_learning_layer.vanilla_loss_with_one_negative(
                iadj_graph_output_seq, isim_graph_output_seq)
            cl_loss = (5 * self.config["graphcl_coefficient"] * (mainstream_weights *
                       cl_loss_adj + personlization_weights * cl_loss_a2s)).mean()

        # Fusion After CL
        if self.config["graph_view_fusion"]:
            # 3, N_mask, dim
            mixed_x = torch.stack(
                (seq_output, iadj_graph_output_raw[last_items_flatten], isim_graph_output_raw[last_items_flatten]), dim=0)
            weights = (torch.matmul(
                mixed_x, self.attn_weights.unsqueeze(0))*self.attn).sum(-1)
            # 3, N_mask, 1
            score = F.softmax(weights, dim=0).unsqueeze(-1)
            seq_output = (mixed_x*score).sum(0)
        # [item_num, H]
        test_item_emb = self.item_embedding.weight
        logits = torch.matmul(seq_output, test_item_emb.transpose(0, 1))
        loss = self.loss_fct(logits, pos_items)

        if torch.isnan(loss):
            logging.error("loss is nan")
        return loss, cl_loss, kl_loss

    def fast_predict(self, interaction):
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        test_item = interaction["item_id_with_negs"]
        seq_output = self.forward(item_seq, item_seq_len)

        if self.config["graph_view_fusion"]:
            last_items_flatten = torch.gather(
                item_seq, 1, (item_seq_len - 1).unsqueeze(1)).squeeze()
            # graph view
            masked_g = self.item_adjgraph
            iadj_graph_output_raw = self.gcn_forward(masked_g)
            iadj_graph_output_seq = iadj_graph_output_raw[last_items_flatten]
            isim_graph_output_seq = self.gcn_forward(self.item_simgraph)[
                last_items_flatten]
            # 3, N_mask, dim
            mixed_x = torch.stack(
                (seq_output, iadj_graph_output_seq, isim_graph_output_seq), dim=0)
            weights = (torch.matmul(
                mixed_x, self.attn_weights.unsqueeze(0))*self.attn).sum(-1)
            # 3, N_mask, 1
            score = F.softmax(weights, dim=0).unsqueeze(-1)
            seq_output = (mixed_x*score).sum(0)

        test_item_emb = self.item_embedding(test_item)  # [B, num, H]
        scores = torch.matmul(seq_output.unsqueeze(
            1), test_item_emb.transpose(1, 2)).squeeze()
        return scores

    def fast_predict_with_conformity_weights(self, interaction):
        user_ids = interaction[self.USER_ID]
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        test_item = interaction["item_id_with_negs"]
        seq_output = self.forward(item_seq, item_seq_len)

        last_items_flatten = torch.gather(
            item_seq, 1, (item_seq_len - 1).unsqueeze(1)).squeeze()
        # graph view
        masked_g = self.item_adjgraph
        iadj_graph_output_raw = self.gcn_forward(masked_g)
        iadj_graph_output_seq = iadj_graph_output_raw[last_items_flatten]
        isim_graph_output_seq = self.gcn_forward(self.item_simgraph)[
            last_items_flatten]

        last_items_indices = torch.tensor([i*self.max_seq_length+j for i, j in enumerate(
            item_seq_len - 1)], dtype=torch.long, device=item_seq.device).view(-1)
        # only the last one
        last_items_flatten = item_seq.view(-1)[last_items_indices]
        valid_items_flatten = last_items_flatten
        valid_items_indices = last_items_indices

        masked_g = self.item_adjgraph
        aug_g = graph_augment(self.item_adjgraph, user_ids, self.user_edges)
        iadj_graph_output_raw = self.gcn_forward(masked_g)
        isim_graph_output_raw = self.gcn_forward(self.item_simgraph)
        iadj_graph_output_seq = iadj_graph_output_raw[valid_items_flatten]
        isim_graph_output_seq = isim_graph_output_raw[valid_items_flatten]

        # CL weights from augmentation
        mainstream_weights = self._subgraph_agreement(
            aug_g, iadj_graph_output_raw, iadj_graph_output_seq, valid_items_flatten)
        # filtering those len=1, set weight=0.5
        # mainstream_weights = torch.where(item_seq_len > 1, mainstream_weights, 0.5*torch.ones_like(mainstream_weights))
        mainstream_weights[item_seq_len == 1] = 0.5

        # 3, N_mask, dim
        mixed_x = torch.stack(
            (seq_output, iadj_graph_output_seq, isim_graph_output_seq), dim=0)
        weights = (torch.matmul(
            mixed_x, self.attn_weights.unsqueeze(0))*self.attn).sum(-1)
        # 3, N_mask, 1
        score = F.softmax(weights, dim=0).unsqueeze(-1)
        seq_output = (mixed_x*score).sum(0)


        test_item_emb = self.item_embedding(test_item)  # [B, num, H]
        scores = torch.matmul(seq_output.unsqueeze(
            1), test_item_emb.transpose(1, 2)).squeeze()
        return scores, mainstream_weights
    
    def get_fused_emb(self):
        item_emb = self.item_embedding.weight
        adj_graph_emb = self.gcn_forward(self.item_adjgraph)
        sim_graph_emb = self.gcn_forward(self.item_simgraph)
        # 3, N_mask, dim
        mixed_x = torch.stack(
            (item_emb, adj_graph_emb, sim_graph_emb), dim=0)
        weights = (torch.matmul(
            mixed_x, self.attn_weights.unsqueeze(0))*self.attn).sum(-1)
        # 3, N_mask, 1
        score = F.softmax(weights, dim=0).unsqueeze(-1)
        fused_emb = (mixed_x*score).sum(0)
        return fused_emb